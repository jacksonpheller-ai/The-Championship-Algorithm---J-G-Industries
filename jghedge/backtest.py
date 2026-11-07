"""Walk-forward backtest and parameter fitting.

For every game day, ratings are re-estimated using only games before that day; each game
is projected and compared with the closing (and opening) market. Parameters are fit on
training seasons and evaluated on held-out seasons (leave-one-season-out), so reported
results are out-of-sample.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .config import PARAMS_FILE, load_params
from .model import players as pl
from .model.engine import Engine
from .model.odds import american_to_decimal, market_margin, market_total, norm_cdf, novig_power
from .model.situational import SIT_FEATURES, feature_vector, schedule_features

SIT_PARAM = {"b2b_diff": "b2b_points", "rest_diff": "rest_advantage_points",
             "travel_diff": "travel_points_per_1000mi", "tz_diff": "tz_points_per_hour",
             "alt_edge": "altitude_points", "three_in_four_diff": "three_in_four_points"}


def walk_forward(engine: Engine, seasons: list[int], verbose: bool = True) -> pd.DataFrame:
    gdf = engine.gdf
    sit = schedule_features(gdf[["id", "date", "tip", "home", "away", "neutral", "venue_city"]])
    p0 = dict(engine.params)
    rows = []
    for season in seasons:
        g_s = gdf[gdf["season"] == season]
        pg_s = engine.pg[engine.pg["season"] == season]
        vals = engine.injury_values(season, live=False)
        rot_cache: dict = {}
        for D, day in g_s.groupby("date"):
            st = engine.state(D, season)
            for g in day.itertuples(index=False):
                av = {}
                for side, team in (("h", g.home), ("a", g.away)):
                    key = (team, D)
                    if key not in rot_cache:
                        rot_cache[key] = pl.rotation(pg_s, team, D)
                    rot = rot_cache[key]
                    if rot.empty:
                        av[f"dO_{side}"] = av[f"dD_{side}"] = 0.0
                        continue
                    played = set(pg_s.loc[(pg_s["game_id"] == g.id) & (pg_s["team"] == team), "id"])
                    pp = {str(pid): (1.0 if pid in played else 0.0) for pid in rot["id"]}
                    dO, dD, _ = pl.availability_adjustment(rot, vals, pp, p0)
                    av[f"dO_{side}"], av[f"dD_{side}"] = dO, dD
                sf = sit.loc[g.id].to_dict()
                proj = engine.project(st, g.home, g.away, bool(g.neutral), None, av, engine.elo_pre[g.id])
                fv = feature_vector(sf)
                rows.append({
                    "id": g.id, "date": D, "season": season, "home": g.home, "away": g.away,
                    "neutral": bool(g.neutral), "margin": g.margin, "total_pts": g.total_pts,
                    "m_ratings_base": proj["m_ratings"], "m_elo_base": proj["m_elo"],
                    "t_model": proj["t_model"], "avail_margin": proj["avail_margin"],
                    "pace": proj["pace"], "games_h": st["teams"][g.home]["games"],
                    "games_a": st["teams"][g.away]["games"],
                    "b2b_any": sf["h_b2b"] + sf["a_b2b"],
                    **fv,
                    "spread_home": g.spread_home, "spread_home_price": g.spread_home_price,
                    "spread_away_price": g.spread_away_price, "total": g.total,
                    "over_price": g.over_price, "under_price": g.under_price,
                    "ml_home": g.ml_home, "ml_away": g.ml_away,
                    "open_spread_home": g.open_spread_home, "open_total": g.open_total,
                })
            if verbose and D.day == 1:
                print(f"  {season} through {D.date()}  ({len(rows)} games)")
    return pd.DataFrame(rows)


# --------------------------------------------------------------- fitting
def _sit(preds: pd.DataFrame, p: dict) -> np.ndarray:
    return sum(p[SIT_PARAM[f]] * preds[f].to_numpy(dtype=float) for f in SIT_FEATURES)


def with_params(preds: pd.DataFrame, p: dict) -> pd.DataFrame:
    d = preds.copy()
    s = _sit(d, p)
    d["m_ratings"] = d["m_ratings_base"] + s
    d["m_elo"] = d["m_elo_base"] + s
    d["m_model"] = p["w_ratings"] * d["m_ratings"] + (1 - p["w_ratings"]) * d["m_elo"]
    d["t_model2"] = d["t_model"] + p.get("total_b2b_points", -0.8) * d["b2b_any"]
    d["m_market"] = [market_margin(*a, p["margin_sd"]) if not pd.isna(a[0]) else np.nan for a in
                     zip(d["spread_home"], d["spread_home_price"], d["spread_away_price"])]
    d["t_market"] = [market_total(*a, p["total_sd"]) if not pd.isna(a[0]) else np.nan for a in
                     zip(d["total"], d["over_price"], d["under_price"])]
    wm, wt = p["w_model_vs_market_margin"], p["w_model_vs_market_total"]
    d["m_final"] = np.where(d["m_market"].notna(), wm * d["m_model"] + (1 - wm) * d["m_market"], d["m_model"])
    d["t_final"] = np.where(d["t_market"].notna(), wt * d["t_model2"] + (1 - wt) * d["t_market"], d["t_model2"])
    return d


def fit(preds: pd.DataFrame, base: dict) -> dict:
    p = dict(base)
    tr = preds.dropna(subset=["margin"])
    # 1) situational coefficients + home-court: ridge toward defaults (lambda in games)
    X = tr[SIT_FEATURES].to_numpy(dtype=float)
    hca_col = (~tr["neutral"]).to_numpy(dtype=float)
    X = np.column_stack([X, hca_col])
    # remove the HCA already inside m_ratings_base so it can be re-estimated
    y = tr["margin"].to_numpy() - (tr["m_ratings_base"].to_numpy() - hca_col * base["hca_points"] * tr["pace"].to_numpy() / 100)
    prior = np.array([base[SIT_PARAM[f]] for f in SIT_FEATURES] + [base["hca_points"]])
    lam = np.array([300.0] * len(SIT_FEATURES) + [50.0])
    A = X.T @ X + np.diag(lam)
    beta = np.linalg.solve(A, X.T @ y + lam * prior)
    lo = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.5])
    hi = np.array([3.5, 1.2, 1.5, 0.8, 3.0, 2.0, 4.0])
    beta = np.clip(beta, lo, hi)
    for f, b in zip(SIT_FEATURES, beta[:-1]):
        p[SIT_PARAM[f]] = round(float(b), 3)
    new_hca = float(beta[-1])
    shift = (new_hca - base["hca_points"]) * hca_col * tr["pace"].to_numpy() / 100
    tr = tr.assign(m_ratings_base=tr["m_ratings_base"] + shift, m_elo_base=tr["m_elo_base"])
    p["hca_points"] = round(new_hca, 3)
    # 2) ratings vs Elo weight
    d = with_params(tr, p)
    best = min(np.linspace(0, 1, 21), key=lambda w: np.mean((d["margin"] - (w * d["m_ratings"] + (1 - w) * d["m_elo"])) ** 2))
    p["w_ratings"] = round(float(best), 3)
    d = with_params(tr, p)
    # 3) model vs market weights (close)
    mk = d.dropna(subset=["m_market"])
    p["w_model_vs_market_margin"] = round(float(min(np.linspace(0, 1, 41), key=lambda w: np.mean(
        (mk["margin"] - (w * mk["m_model"] + (1 - w) * mk["m_market"])) ** 2))), 3)
    tk = d.dropna(subset=["t_market"])
    p["w_model_vs_market_total"] = round(float(min(np.linspace(0, 1, 41), key=lambda w: np.mean(
        (tk["total_pts"] - (w * tk["t_model2"] + (1 - w) * tk["t_market"])) ** 2))), 3)
    d = with_params(tr, p)
    # 4) dispersion
    p["margin_sd"] = round(float(np.sqrt(np.mean((d["margin"] - d["m_final"]) ** 2))), 3)
    p["total_sd"] = round(float(np.sqrt(np.mean((d["total_pts"] - d["t_final"]) ** 2))), 3)
    res_m = d["margin"] - d["m_final"]
    res_t = d["total_pts"] - d["t_final"]
    p["margin_total_corr"] = round(float(np.corrcoef(res_m, res_t)[0, 1]), 3)
    from .model.simulate import QGRID
    qm = np.quantile(res_m - res_m.mean(), QGRID)
    qt = np.quantile(res_t - res_t.mean(), QGRID)
    p["margin_resid_q"] = [round(float(x), 3) for x in qm]
    p["total_resid_q"] = [round(float(x), 3) for x in qt]
    return p


def p_home_win(m: np.ndarray, p: dict) -> np.ndarray:
    """P(margin > 0) using the empirical residual distribution when available."""
    from .model.simulate import QGRID
    q = p.get("margin_resid_q")
    if not q:
        return norm_cdf(np.asarray(m) / p["margin_sd"])
    q = np.asarray(q)
    return 1 - np.interp(-np.asarray(m), q, QGRID)


# ------------------------------------------------------------ evaluation
def _ll(p, y):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _settle(margin_or_total: float, line: float, side: int) -> float:
    """side=+1: home cover / over. Returns 1 win, 0 loss, 0.5 push."""
    v = side * (margin_or_total + (line if side == 1 else line))
    return 1.0 if v > 0 else (0.5 if v == 0 else 0.0)


def evaluate(preds: pd.DataFrame, p: dict) -> dict:
    d = with_params(preds, p)
    d = d.dropna(subset=["margin"])
    out: dict = {"games": int(len(d))}
    sd = p["margin_sd"]
    mk = d.dropna(subset=["m_market"])
    out["margin_rmse"] = {k: round(float(np.sqrt(np.mean((mk["margin"] - mk[c]) ** 2))), 3) for k, c in
                          (("model_only", "m_model"), ("ratings_only", "m_ratings"), ("elo_only", "m_elo"),
                           ("market_close", "m_market"), ("final_blend", "m_final"))}
    tk = d.dropna(subset=["t_market"])
    out["total_rmse"] = {k: round(float(np.sqrt(np.mean((tk["total_pts"] - tk[c]) ** 2))), 3) for k, c in
                         (("model_only", "t_model2"), ("market_close", "t_market"), ("final_blend", "t_final"))}
    # win probability quality
    y = (d["margin"] > 0).to_numpy(dtype=float)
    pm = p_home_win(d["m_model"].to_numpy(), p)
    pf = p_home_win(d["m_final"].to_numpy(), p)
    ml = d.dropna(subset=["ml_home", "ml_away"])
    pmk = np.array([novig_power(a, b)[0] for a, b in zip(ml["ml_home"], ml["ml_away"])])
    yml = (ml["margin"] > 0).to_numpy(dtype=float)
    pf_ml = p_home_win(ml["m_final"].to_numpy(), p)
    out["win_prob"] = {
        "brier_model": round(float(np.mean((pm - y) ** 2)), 4),
        "brier_final": round(float(np.mean((pf - y) ** 2)), 4),
        "brier_market_ml": round(float(np.mean((pmk - yml) ** 2)), 4),
        "brier_final_same_games": round(float(np.mean((pf_ml - yml) ** 2)), 4),
        "logloss_model": round(_ll(pm, y), 4), "logloss_final": round(_ll(pf, y), 4),
        "logloss_market_ml": round(_ll(pmk, yml), 4),
        "accuracy_final": round(float(np.mean((pf > 0.5) == (y == 1))), 4),
    }
    # calibration (final blend)
    bins = np.linspace(0, 1, 11)
    cal = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = (pf >= lo) & (pf < hi)
        if m.sum() >= 10:
            cal.append({"bin": f"{lo:.1f}-{hi:.1f}", "pred": round(float(pf[m].mean()), 3),
                        "actual": round(float(y[m].mean()), 3), "n": int(m.sum())})
    out["calibration"] = cal
    # betting simulations at closing prices
    out["ats_close"] = _ats(mk, p, "m_final", "m_market", "spread_home", price=True)
    out["totals_close"] = _tot(tk, "t_final", "t_market", "total")
    # opening-line strategy (bet the morning number), plus closing line value
    out["ats_open"] = _ats_open(d, p)
    out["ml_close"] = _ml(ml, p)
    return out


def _ats(d, p, col, mcol, line_col, price=True):
    res = {}
    for thr in (1.0, 2.0, 3.0):
        diff = d[col] - d[mcol]
        sel = d[diff.abs() >= thr]
        w = l = pu = 0
        pnl = 0.0
        for r in sel.itertuples(index=False):
            side = 1 if getattr(r, col) > getattr(r, mcol) else -1
            v = r.margin + r.spread_home if side == 1 else -(r.margin + r.spread_home)
            pr = (r.spread_home_price if side == 1 else r.spread_away_price) if price else -110
            pr = -110 if pd.isna(pr) else pr
            if v > 0:
                w += 1
                pnl += american_to_decimal(pr) - 1
            elif v < 0:
                l += 1
                pnl -= 1
            else:
                pu += 1
        n = w + l + pu
        res[f"edge>={thr:g}pt"] = {"bets": n, "w": w, "l": l, "p": pu,
                                   "win_pct": round(w / max(w + l, 1), 4), "roi": round(pnl / max(n, 1), 4)}
    return res


def _tot(d, col, mcol, line_col):
    res = {}
    for thr in (1.5, 3.0, 5.0):
        diff = d[col] - d[mcol]
        sel = d[diff.abs() >= thr]
        w = l = pu = 0
        pnl = 0.0
        for r in sel.itertuples(index=False):
            side = 1 if getattr(r, col) > getattr(r, mcol) else -1
            v = side * (r.total_pts - r.total)
            pr = r.over_price if side == 1 else r.under_price
            pr = -110 if pd.isna(pr) else pr
            if v > 0:
                w += 1
                pnl += american_to_decimal(pr) - 1
            elif v < 0:
                l += 1
                pnl -= 1
            else:
                pu += 1
        n = w + l + pu
        res[f"edge>={thr:g}pt"] = {"bets": n, "w": w, "l": l, "p": pu,
                                   "win_pct": round(w / max(w + l, 1), 4), "roi": round(pnl / max(n, 1), 4)}
    return res


def _ats_open(d, p):
    o = d.dropna(subset=["open_spread_home", "spread_home"])
    wm = p["w_model_vs_market_margin"]
    res = {}
    for thr in (1.0, 2.0, 3.0):
        w = l = pu = 0
        clv = []
        for r in o.itertuples(index=False):
            m_open = -r.open_spread_home
            fin = wm * r.m_model + (1 - wm) * m_open
            if abs(fin - m_open) < thr:
                continue
            side = 1 if fin > m_open else -1
            v = side * (r.margin + r.open_spread_home)
            w, l, pu = w + (v > 0), l + (v < 0), pu + (v == 0)
            clv.append(side * (r.open_spread_home - r.spread_home))
        n = w + l + pu
        res[f"edge>={thr:g}pt"] = {"bets": int(n), "w": int(w), "l": int(l), "p": int(pu),
                                   "win_pct": round(w / max(w + l, 1), 4),
                                   "roi_at_-110": round((w * (100 / 110) - l) / max(n, 1), 4),
                                   "avg_clv_pts": round(float(np.mean(clv)) if clv else 0.0, 3),
                                   "beat_close_pct": round(float(np.mean(np.array(clv) > 0)) if clv else 0.0, 4)}
    return res


def _ml(d, p):
    sd = p["margin_sd"]
    res = {}
    for thr in (0.02, 0.05):
        n = 0
        pnl = 0.0
        wins = 0
        for r in d.itertuples(index=False):
            # anchor to the de-vigged moneyline; add only the model's tilt vs the spread centre
            tilt = float(p_home_win(np.array([r.m_final]), p)[0] - p_home_win(np.array([r.m_market]), p)[0]) \
                if not pd.isna(r.m_market) else 0.0
            ph = float(np.clip(novig_power(r.ml_home, r.ml_away)[0] + tilt, 0.01, 0.99))
            for side, prob, price in ((1, ph, r.ml_home), (-1, 1 - ph, r.ml_away)):
                dec = american_to_decimal(price)
                ev = prob * dec - 1
                if ev >= thr:
                    n += 1
                    won = (r.margin > 0) if side == 1 else (r.margin < 0)
                    wins += won
                    pnl += (dec - 1) if won else -1
        res[f"ev>={thr:.0%}"] = {"bets": n, "wins": int(wins), "roi": round(pnl / max(n, 1), 4)}
    return res


# ------------------------------------------------------------- driver
def run(engine: Engine | None, seasons: list[int], verbose: bool = True,
        preds: pd.DataFrame | None = None) -> tuple[dict, pd.DataFrame]:
    if preds is None:
        preds = walk_forward(engine, seasons, verbose)
    base = load_params()
    report: dict = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "seasons": seasons, "folds": []}
    # leave-one-season-out on completed seasons
    full = [s for s in seasons if (preds["season"] == s).sum() > 600]
    for s in full:
        train = preds[preds["season"] != s]
        test = preds[preds["season"] == s]
        pf = fit(train, base)
        report["folds"].append({"test_season": int(s), "params": pf, "metrics": evaluate(test, pf)})
    final = fit(preds, base)
    report["final_params"] = final
    report["in_sample_all"] = evaluate(preds, final)
    cur = [s for s in seasons if s not in full]
    if cur:
        pf = fit(preds[preds["season"].isin(full)], base)
        report["current_season_oos"] = {"season": cur, "metrics": evaluate(preds[preds["season"].isin(cur)], pf)}
    PARAMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PARAMS_FILE.write_text(json.dumps({"fitted": report["generated"], "params": final}, indent=2))
    return report, preds


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if math.isnan(o) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    return o


def open_line_study(preds: pd.DataFrame, p: dict) -> dict:
    """Does the model (WITHOUT lineup look-ahead) anticipate open->close line moves?
    Bets are taken at the opening number; closing line value (CLV) is the move in our favour."""
    d = with_params(preds, p).dropna(subset=["m_market", "open_spread_home", "margin"])
    d = d.assign(m_open=-d["open_spread_home"], m_noav=d["m_model"] - d["avail_margin"])
    out = {"spreads": {}, "totals": {}}
    for s, g in d.groupby("season"):
        sig = g["m_noav"] - g["m_open"]
        mv = g["m_market"] - g["m_open"]
        rows = {}
        for thr in (2, 3, 4, 5):
            m = sig.abs() >= thr
            side = np.sign(sig[m])
            sel = g[m]
            clv = side * (sel["m_market"] - sel["m_open"])
            cov = side * (sel["margin"] + sel["open_spread_home"])
            wins, losses = int((cov > 0).sum()), int((cov < 0).sum())
            rows[f"{thr}pt"] = {"bets": int(m.sum()), "avg_clv_pts": round(float(clv.mean()), 3),
                                "beat_close_pct": round(float((clv > 0).mean()), 4),
                                "ats_win_pct": round(wins / max(wins + losses, 1), 4),
                                "roi_at_-110": round((wins * 100 / 110 - losses) / max(int(m.sum()), 1), 4)}
        out["spreads"][str(int(s))] = {"games": int(len(g)), "corr_signal_vs_move": round(float(np.corrcoef(sig, mv)[0, 1]), 3),
                                       "thresholds": rows}
    t = with_params(preds, p).dropna(subset=["t_market", "open_total", "total_pts"])
    for s, g in t.groupby("season"):
        sig = g["t_model2"] - g["open_total"]
        mv = g["t_market"] - g["open_total"]
        rows = {}
        for thr in (3, 5, 7):
            m = sig.abs() >= thr
            side = np.sign(sig[m])
            sel = g[m]
            clv = side * (sel["t_market"] - sel["open_total"])
            res = side * (sel["total_pts"] - sel["open_total"])
            wins, losses = int((res > 0).sum()), int((res < 0).sum())
            rows[f"{thr}pt"] = {"bets": int(m.sum()), "avg_clv_pts": round(float(clv.mean()), 3),
                                "beat_close_pct": round(float((clv > 0).mean()), 4),
                                "win_pct": round(wins / max(wins + losses, 1), 4),
                                "roi_at_-110": round((wins * 100 / 110 - losses) / max(int(m.sum()), 1), 4)}
        out["totals"][str(int(s))] = {"games": int(len(g)), "corr_signal_vs_move": round(float(np.corrcoef(sig, mv)[0, 1]), 3),
                                      "thresholds": rows}
    return out
