"""Player-prop backtest against real closing prop lines (ESPN/DraftKings).

For each game day the prop model is rebuilt from games strictly before that day. Every
closing prop line is priced, then scored against the player's actual box score.
Players who did not play are excluded (those bets are void at the book).

Fits a logistic stacking model per market type:

    logit p_final = a * logit p_model + c * logit p_market + b

which simultaneously calibrates our model and decides how much to trust it vs the book.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from .config import DATA_DIR
from .data import espn
from .data.http import Fetcher
from .model import players as pl
from .model.engine import Engine
from .model.odds import american_to_decimal, logit, market_margin, market_total, novig_multiplicative
from .model.props import PropModel
from .model.simulate import GameSim

PROPS_FILE = DATA_DIR / "prop_backtest.json"


def collect(f: Fetcher, eng: Engine, season: int, min_day: int = 7, n: int = 4000,
            provider: str = "100", verbose: bool = True) -> pd.DataFrame:
    gdf = eng.gdf[eng.gdf["season"] == season]
    start = gdf["date"].min() + pd.Timedelta(days=min_day)
    gdf = gdf[gdf["date"] >= start]
    pg_s = eng.pg[eng.pg["season"] == season]
    p = eng.params
    rng = np.random.default_rng(11)

    providers = [provider] if isinstance(provider, str) else list(provider)

    def fetch(gid):
        for prov in providers:
            try:
                got = espn.props(f, gid, prov, ttl=None)
            except Exception:  # noqa: BLE001
                got = []
            if got:
                return got
        return []

    with ThreadPoolExecutor(8) as ex:
        lines = dict(zip(gdf["id"], ex.map(fetch, gdf["id"])))
    rows = []
    for D, day in gdf.groupby("date"):
        st = eng.state(D, season)
        pm = PropModel(eng.pg, eng.tg, season, D, p)
        for g in day.itertuples(index=False):
            pl_lines = lines.get(g.id) or []
            if not pl_lines:
                continue
            mm = market_margin(g.spread_home, g.spread_home_price, g.spread_away_price, p["margin_sd"],
                               g.ml_home, g.ml_away)
            mt = market_total(g.total, g.over_price, g.under_price, p["total_sd"])
            if mm is None or mt is None:
                continue
            sim = GameSim(mm, mt, p, n, rng)
            box = pg_s[pg_s["game_id"] == g.id].set_index("id")
            sims, projs = {}, {}
            for team, opp, pts, msign in ((g.home, g.away, sim.hs, 1), (g.away, g.home, sim.as_, -1)):
                rot = pl.rotation(pg_s, team, D)
                played = set(box.index[box["team"] == team])
                pp = {str(pid): (1.0 if pid in played else 0.0) for pid in rot["id"]} if not rot.empty else {}
                tp = st["teams"][team]
                pace_factor = (st["mu_pace"] + tp["P"] + st["teams"][opp]["P"]) / max(st["mu_pace"] + tp["P"], 80)
                players = pm.project_team(rot, team, opp, pp, pace_factor, st["teams"][opp]["D"], st["mu"])
                players = [x for x in players if x["play_prob"] > 0]
                for x in players:
                    x["play_prob"] = 1.0
                team_exp = (mt + msign * mm) / 2
                s = pm.simulate(players, pts, team_exp, msign * sim.margin, rng)
                sims.update(s)
                projs.update({x["id"]: x for x in players})
            tip = pd.Timestamp(g.tip)
            for pr in pl_lines:
                if pr["kind"] != "ou":
                    continue   # historical milestone ladders are unreliable (see docs)
                up = pr.get("updated")
                if not up or pd.Timestamp(up) > tip - pd.Timedelta(minutes=5):
                    continue   # price was updated in-game: not a pre-game line
                pid = str(pr["athlete_id"])
                if pid not in sims or pid not in box.index:
                    continue
                stat = pr["stat"]
                if stat not in sims[pid]:
                    continue
                actual = float(box.loc[pid, stat]) if stat in box.columns else None
                if actual is None or np.isnan(actual):
                    continue
                x = sims[pid][stat]
                p_model = float(np.mean(x > pr["line"]))
                rows.append({"game_id": g.id, "date": D, "player_id": pid, "player": projs[pid]["name"],
                             "stat": stat, "kind": pr["kind"], "line": pr["line"], "over": pr["over"],
                             "under": pr["under"], "proj": projs[pid]["exp_line"].get(stat),
                             "exp_min": projs[pid]["exp_min"], "actual": actual,
                             "actual_min": float(box.loc[pid, "min"]), "p_model": p_model,
                             "hit_over": float(actual > pr["line"]),
                             "hours_before_tip": (tip - pd.Timestamp(up)).total_seconds() / 3600})
        if verbose:
            print(f"  {D.date()}: {len(rows)} prop rows")
    return pd.DataFrame(rows)


def anchor_milestones(df: pd.DataFrame) -> pd.DataFrame:
    """Fair milestone probabilities via a power de-vig anchored to the same player's two-way line:
    gamma solves implied(L+0.5)**gamma = p_novig(over L); applied to every rung of the ladder."""
    df = df.copy()
    df["imp_over"] = 1 / df["over"].map(american_to_decimal)
    ou = df[df["kind"] == "ou"].copy()
    ou["p_mkt"] = [novig_multiplicative(o, u)[0] for o, u in zip(ou["over"], ou["under"])]
    ms = df[df["kind"] == "milestone"].copy()
    key = ["game_id", "player_id", "stat"]
    anchors = ou[key + ["line", "p_mkt"]].rename(columns={"line": "ou_line"})
    ms = ms.merge(anchors, on=key, how="left")
    gam = []
    for r in ms.itertuples(index=False):
        gam.append(np.nan)
    ms["gamma"] = np.nan
    # gamma per (game, player, stat) from the rung matching the O/U line
    rung = ms[np.isclose(ms["line"], ms["ou_line"])]
    g = {(r.game_id, r.player_id, r.stat): np.log(r.p_mkt) / np.log(r.imp_over)
         for r in rung.itertuples(index=False) if 0 < r.imp_over < 1 and 0 < r.p_mkt < 1}
    med = float(np.nanmedian(list(g.values()))) if g else 1.08
    ms["gamma"] = [g.get((a, b, c), med) for a, b, c in zip(ms["game_id"], ms["player_id"], ms["stat"])]
    ms["gamma"] = ms["gamma"].clip(1.0, 1.6)
    ms["p_mkt"] = ms["imp_over"] ** ms["gamma"]
    ms = ms.drop(columns=["ou_line", "p_mkt_x"], errors="ignore")
    return pd.concat([ou, ms[ou.columns.intersection(ms.columns)]], ignore_index=True), med


STACK_PRIOR = np.array([0.15, 0.85, 0.0])


def _fit_logit(X: np.ndarray, y: np.ndarray, l2: float = 40.0, iters: int = 50) -> np.ndarray:
    """Logistic regression with an L2 prior centred on a mostly-market blend."""
    w = STACK_PRIOR.copy()
    for _ in range(iters):
        z = X @ w
        pr = 1 / (1 + np.exp(-z))
        g = X.T @ (pr - y) + l2 * (w - STACK_PRIOR)
        H = (X * (pr * (1 - pr))[:, None]).T @ X + l2 * np.eye(len(w))
        w -= np.linalg.solve(H, g)
    return w


def _ll(p, y):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def analyse(df: pd.DataFrame) -> dict:
    df, gamma_med = anchor_milestones(df)
    out = {"rows": int(len(df)), "milestone_gamma_median": round(gamma_med, 4), "by_kind": {},
           "prop_bias": {s: round(float(g["actual"].sum() / max(g["proj"].sum(), 1e-6)), 4)
                         for s, g in df.groupby("stat") if len(g) >= 40}}
    for kind, g in df.groupby("kind"):
        g = g[(g["p_mkt"] > 0.02) & (g["p_mkt"] < 0.98)]
        y = g["hit_over"].to_numpy()
        lm = logit(g["p_model"].clip(0.005, 0.995).to_numpy())
        lk = logit(g["p_mkt"].to_numpy())
        X = np.column_stack([lm, lk, np.ones(len(g))])
        # time-ordered split: fit on first 60%, score on last 40%
        order = np.argsort(g["date"].to_numpy(), kind="stable")
        cut = int(len(order) * 0.6)
        tr, te = order[:cut], order[cut:]
        w_tr = _fit_logit(X[tr], y[tr])
        p_stack_te = 1 / (1 + np.exp(-(X[te] @ w_tr)))
        w_all = _fit_logit(X, y)
        p_mk = g["p_mkt"].to_numpy()
        p_md = g["p_model"].clip(0.005, 0.995).to_numpy()
        res = {
            "n": int(len(g)), "hit_rate_over": round(float(y.mean()), 4),
            "mean_market_p_over": round(float(p_mk.mean()), 4), "mean_model_p_over": round(float(p_md.mean()), 4),
            "proj_bias_vs_actual": round(float((g["actual"] - g["proj"]).mean()), 3),
            "proj_mae": round(float((g["actual"] - g["proj"]).abs().mean()), 3),
            "line_mae": round(float((g["actual"] - g["line"]).abs().mean()), 3),
            "logloss_market": round(_ll(p_mk, y), 4), "logloss_model": round(_ll(p_md, y), 4),
            "logloss_stack_oos": round(_ll(p_stack_te, y[te]), 4),
            "logloss_market_oos": round(_ll(p_mk[te], y[te]), 4),
            "stack_coef_all": [round(float(v), 4) for v in w_all],
            "stack_coef_train": [round(float(v), 4) for v in w_tr],
        }
        # betting simulation on OOS part using stacked probabilities
        sims = {}
        for thr in (0.02, 0.05, 0.10):
            pnl = nb = wins = 0
            for i, ps in zip(te, p_stack_te):
                r = g.iloc[i]
                for side, prob, price in (("over", ps, r["over"]), ("under", 1 - ps, r["under"])):
                    if price is None or (isinstance(price, float) and np.isnan(price)):
                        continue
                    dec = american_to_decimal(price)
                    if prob * dec - 1 >= thr:
                        won = (r["hit_over"] == 1) if side == "over" else (r["hit_over"] == 0)
                        nb += 1
                        wins += won
                        pnl += (dec - 1) if won else -1
            sims[f"ev>={thr:.0%}"] = {"bets": nb, "wins": int(wins), "roi": round(pnl / max(nb, 1), 4)}
        res["oos_betting"] = sims
        # calibration of stacked probabilities (all, in-sample coef) for display
        ps_all = 1 / (1 + np.exp(-(X @ w_all)))
        cal = []
        for lo in np.arange(0, 1, 0.1):
            m = (ps_all >= lo) & (ps_all < lo + 0.1)
            if m.sum() >= 25:
                cal.append({"bin": f"{lo:.1f}-{lo + 0.1:.1f}", "pred": round(float(ps_all[m].mean()), 3),
                            "actual": round(float(y[m].mean()), 3), "n": int(m.sum())})
        res["calibration"] = cal
        by_stat = {}
        for s, gs in g.groupby("stat"):
            by_stat[s] = {"n": int(len(gs)), "proj_bias": round(float((gs["actual"] - gs["proj"]).mean()), 3),
                          "hit_over": round(float(gs["hit_over"].mean()), 3),
                          "mkt_p_over": round(float(gs["p_mkt"].mean()), 3),
                          "model_p_over": round(float(gs["p_model"].mean()), 3)}
        res["by_stat"] = by_stat
        out["by_kind"][kind] = res
    return out


def save(report: dict) -> None:
    PROPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROPS_FILE.write_text(json.dumps(report, indent=1))
