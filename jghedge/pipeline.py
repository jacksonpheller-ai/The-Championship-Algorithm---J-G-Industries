"""Nightly run: data -> projections -> simulation -> pricing -> parlays -> Kelly -> JSON."""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from . import BRAND, __version__
from .config import CURRENT_SEASON, DATA_DIR, SITE_DATA, load_params
from .data import espn, history
from .data.http import Fetcher
from .loader import bbref_table, build_engine
from .model import players as pl
from .model.odds import (american_to_decimal, decimal_to_american, inv_logit, logit,
                         novig_multiplicative, novig_power)
from .model.props import COMBOS, PropModel
from .backtest import p_home_win
from .model.simulate import GameSim, win_prob
from .model.situational import schedule_features
from .portfolio import discrete, kelly
from .portfolio.parlay import build_parlays, same_game_fair, select_parlays
from .teams import TEAMS

STAT_LABEL = {"pts": "Points", "reb": "Rebounds", "ast": "Assists", "fg3m": "3-Pointers Made",
              "stl": "Steals", "blk": "Blocks", "tov": "Turnovers", "pra": "Pts+Reb+Ast",
              "pr": "Pts+Reb", "pa": "Pts+Ast", "ra": "Reb+Ast"}


def us_today() -> date:
    return (datetime.now(timezone.utc) - timedelta(hours=6)).date()


def _w_time(hours_to_tip: float, early: float, late: float) -> float:
    x = min(max((hours_to_tip - 1.0) / 5.0, 0.0), 1.0)
    return late + (early - late) * x


def _r(x, n=3):
    if x is None:
        return None
    try:
        if isinstance(x, float) and math.isnan(x):
            return None
        return round(float(x), n)
    except (TypeError, ValueError):
        return None


def run(day: date | None = None, n_sims: int | None = None, update: bool = True,
        with_props: bool = True, verbose: bool = True, seed: int = 7, engine=None,
        slate: list[dict] | None = None, injuries: dict | None = None, replay: bool = False,
        season: int | None = None, overrides: dict | None = None, hours_override: float | None = None) -> dict:
    """Price one night. With `replay=True` (used by jghedge.replay) the caller supplies the
    engine, the slate with the morning lines and an empty injury report, and every input is
    restricted to information available before that night (pre-game Elo, prior-season BPM)."""
    params = load_params()
    params.update(overrides or {})
    n = n_sims or int(params["n_sims"])
    day = day or us_today()
    season = season or CURRENT_SEASON
    f = Fetcher()
    log = print if verbose else (lambda *a, **k: None)

    if update:
        log("Updating game history ...")
        history.update_season(f, season, verbose=verbose)
    eng = engine or build_engine(f, [season - 2, season - 1, season], params)
    asof = pd.Timestamp(day)
    st = eng.state(asof, season)
    pg_s = eng.pg[eng.pg["season"] == season]
    live_vals = eng.injury_values(season, live=not replay)

    log(f"Slate for {day} ...")
    if slate is None:
        slate = [g for g in espn.scoreboard(f, day) if g["state"] == "pre"
                 and g["home"] in TEAMS and g["away"] in TEAMS]
    inj = injuries if injuries is not None else espn.injuries(f)
    team_tbl = None if replay else bbref_table(f, "team", season)

    # situational features need the schedule including tonight
    sched = eng.gdf[["id", "date", "tip", "home", "away", "neutral", "venue_city"]].copy()
    sched = sched[~sched["id"].isin({g["id"] for g in slate})]
    tonight = pd.DataFrame([{"id": g["id"], "date": pd.Timestamp(history.game_day(g["date"])), "tip": g["date"],
                             "home": g["home"], "away": g["away"], "neutral": g["neutral"],
                             "venue_city": g["venue_city"]} for g in slate])
    sit = schedule_features(pd.concat([sched, tonight], ignore_index=True)) if len(tonight) else None

    pm = PropModel(eng.pg, eng.tg, season, asof, params) if with_props else None
    rng = np.random.default_rng(seed)
    now = datetime.now(timezone.utc)

    games_out, bets, payoffs, sims_by_game = [], [], {}, {}
    for g in slate:
        h, a = g["home"], g["away"]
        tip = datetime.fromisoformat(g["date"].replace("Z", "+00:00"))
        hours = hours_override if hours_override is not None else (
            10.0 if replay else (tip - now).total_seconds() / 3600)
        team_pp, team_inj, rots, adj = {}, {}, {}, {}
        for side, team in (("h", h), ("a", a)):
            rows = inj.get(team, [])
            pp = {r["id"]: r["play_prob"] for r in rows if r.get("id")}
            team_pp[team] = pp
            rot = pl.rotation(pg_s, team, asof)
            rots[team] = rot
            dO, dD, detail = pl.availability_adjustment(rot, live_vals, pp, params)
            adj[f"dO_{side}"], adj[f"dD_{side}"] = dO, dD
            listed = {r["id"]: r for r in rows}
            team_inj[team] = [{**d, "status": listed.get(d["id"], {}).get("status", "Not listed (missed last 3)"),
                               "note": listed.get(d["id"], {}).get("detail", "")}
                              for d in detail if d["play_prob"] < 1.0]
            for r in rows:   # listed players with no recent minutes (still show them)
                if r.get("id") and not any(x["id"] == r["id"] for x in team_inj[team]):
                    team_inj[team].append({"id": r["id"], "name": r["name"], "status": r["status"],
                                           "play_prob": r["play_prob"], "net_impact": 0.0,
                                           "note": r.get("detail", ""), "typ_min": 0.0, "bpm": None})
        sf = sit.loc[g["id"]].to_dict() if sit is not None else None
        proj = eng.project(st, h, a, g["neutral"], sf, adj,
                           elo_pair=eng.elo_pre.get(g["id"]) if replay else None)
        p_live = dict(params)
        p_live["w_model_vs_market_margin"] = _w_time(hours, params.get("w_margin_early", 0.25),
                                                     params["w_model_vs_market_margin"])
        p_live["w_model_vs_market_total"] = _w_time(hours, params.get("w_total_early", 0.30),
                                                    params["w_model_vs_market_total"])
        eng.params = p_live
        bl = eng.blend_with_market(proj, g["odds"])
        eng.params = params
        sim = GameSim(bl["m_final"], bl["t_final"], params, n, rng)
        sims_by_game[g["id"]] = sim
        o = (g["odds"] or {}).get("close") or {}
        op = (g["odds"] or {}).get("open") or {}
        gbets = []

        def add(bid, kind, label, side, line, price, pay, p_mkt, extra=None):
            if price is None:
                return
            dec = american_to_decimal(price)
            pf = win_prob(pay)
            b = {"id": bid, "game_id": g["id"], "kind": kind, "label": label, "side": side, "line": line,
                 "price": price, "dec": dec, "p_final": pf, "p_market": p_mkt, "ev": float(pay.mean()),
                 "edge": (pf - p_mkt) if p_mkt is not None else None,
                 "matchup": f"{a} @ {h}", "tip": g["date"], **(extra or {})}
            bets.append(b)
            payoffs[bid] = pay
            gbets.append(bid)

        gid = g["id"]
        if o.get("ml_home") is not None and o.get("ml_away") is not None:
            ph, pa = novig_power(o["ml_home"], o["ml_away"])
            # moneyline: de-vigged price plus the model's tilt (spread->win conversion is
            # less accurate than the moneyline market itself; see backtest)
            tilt = 0.0
            if bl["m_market"] is not None:
                tilt = p_home_win(np.array([bl["m_final"]]), params)[0] - p_home_win(np.array([bl["m_market"]]), params)[0]
            p_home_final = float(np.clip(ph + tilt, 0.01, 0.99))
            add(f"{gid}-ml-h", "moneyline", f"{TEAMS[h].name} ML", "home", None, o["ml_home"],
                sim.ml_anchored("home", american_to_decimal(o["ml_home"]), p_home_final, rng), ph)
            add(f"{gid}-ml-a", "moneyline", f"{TEAMS[a].name} ML", "away", None, o["ml_away"],
                sim.ml_anchored("away", american_to_decimal(o["ml_away"]), p_home_final, rng), pa)
        if o.get("spread_home") is not None and o.get("spread_home_price") is not None:
            sh = o["spread_home"]
            ph, pa = novig_multiplicative(o["spread_home_price"], o["spread_away_price"] or -110)
            add(f"{gid}-sp-h", "spread", f"{TEAMS[h].name} {sh:+g}", "home", sh, o["spread_home_price"],
                sim.spread("home", sh, american_to_decimal(o["spread_home_price"])), ph)
            add(f"{gid}-sp-a", "spread", f"{TEAMS[a].name} {-sh:+g}", "away", -sh, o["spread_away_price"],
                sim.spread("away", -sh, american_to_decimal(o["spread_away_price"] or -110)), pa)
        if o.get("total") is not None and o.get("over_price") is not None:
            t = o["total"]
            po, pu = novig_multiplicative(o["over_price"], o["under_price"] or -110)
            add(f"{gid}-to-o", "total", f"{a}/{h} Over {t:g}", "over", t, o["over_price"],
                sim.total_bet("over", t, american_to_decimal(o["over_price"])), po)
            add(f"{gid}-to-u", "total", f"{a}/{h} Under {t:g}", "under", t, o["under_price"],
                sim.total_bet("under", t, american_to_decimal(o["under_price"] or -110)), pu)

        # ------------------------------------------------------------ props
        n_props = 0
        if pm is not None:
            team_players = {}
            for team, opp, pts_draw, pts_exp, msign in ((h, a, sim.hs, bl["pts_h"], 1), (a, h, sim.as_, bl["pts_a"], -1)):
                tp = st["teams"][team]
                pace_factor = proj["pace"] / max(st["mu_pace"] + tp["P"], 80)
                players = pm.project_team(rots[team], team, opp, team_pp[team], pace_factor,
                                          st["teams"][opp]["D"], st["mu"])
                # scale expected team points to the blended game projection
                scale = (bl["t_final"] + msign * bl["m_final"]) / 2
                psims = pm.simulate(players, pts_draw, scale, msign * sim.margin, rng)
                for p in players:
                    team_players[p["id"]] = (p, psims[p["id"]], team)
            try:
                plines = espn.props(f, gid)
            except Exception:  # noqa: BLE001
                plines = []
            a_s, c_s, b_s = params.get("prop_stack") or [0.15, 0.85, -0.1]
            for pr in plines:
                if pr["kind"] != "ou" or pr["under"] is None:
                    continue   # milestone ladders failed data-integrity checks in backtest
                tpl = team_players.get(str(pr["athlete_id"]))
                if not tpl:
                    continue
                p, ps, team = tpl
                stat = pr["stat"]
                if stat not in ps:
                    continue
                x = ps[stat].astype(float)
                plays = ps["_plays"]
                line = pr["line"]
                p_model = float(np.mean(x[plays] > line)) if plays.any() else 0.0
                p_over_mkt, _ = novig_multiplicative(pr["over"], pr["under"])
                sides = (("over", pr["over"], p_over_mkt), ("under", pr["under"], 1 - p_over_mkt))
                p_mkt_over = sides[0][2]
                p_fin_over = float(inv_logit(a_s * logit(p_model) + c_s * logit(p_mkt_over) + b_s))
                # rank-preserving threshold so simulated outcomes match the blended probability
                jit = x + rng.random(len(x)) - 0.5
                act = jit[plays] if plays.any() else jit
                thr = float(np.quantile(act, 1 - p_fin_over)) if 0 < p_fin_over < 1 else line
                over_hit = jit > thr
                for side, price, p_mkt in sides:
                    if price is None:
                        continue
                    dec = american_to_decimal(price)
                    hit = over_hit if side == "over" else ~over_hit
                    pay = np.where(~plays, 0.0, np.where(hit, dec - 1, -1.0))
                    kind_lbl = f"{'Over' if side == 'over' else 'Under'} {line:g}"
                    bid = f"{gid}-pp-{p['id']}-{stat}-{pr['kind']}-{line:g}-{side}"
                    if bid in payoffs:
                        continue
                    add(bid, "prop", f"{p['name']} {kind_lbl} {STAT_LABEL[stat]}", side, line, price, pay,
                        p_mkt, {"player": p["name"], "player_id": p["id"], "team": team, "stat": stat,
                                "prop_kind": pr["kind"], "proj": p["exp_line"].get(stat),
                                "p_model_raw": p_model, "exp_min": round(p["exp_min"], 1),
                                "play_prob": p["play_prob"]})
                    n_props += 1

        def tstats(team):
            if team_tbl is None or team_tbl.empty or team not in team_tbl.index:
                return {}
            r = team_tbl.loc[team]
            keys = ["off_rtg", "def_rtg", "net_rtg", "pace", "srs", "efg_pct", "tov_pct", "orb_pct", "ft_rate",
                    "opp_efg_pct", "opp_tov_pct", "drb_pct", "opp_ft_rate", "ts_pct", "fg3a_per_fga_pct",
                    "wins", "losses", "mov", "sos"]
            return {k: _r(r.get(k)) for k in keys if k in r}

        def rotation_rows(team):
            rot = rots.get(team)
            if rot is None or rot.empty:
                return []
            out = []
            for r in rot.head(9).itertuples(index=False):
                v = live_vals.get(str(r.id), {})
                pp = team_pp[team].get(str(r.id), 1.0 if r.last3 > 0 else 0.35)
                out.append({"name": r.name, "min": _r(r.typ_min, 1), "play_prob": _r(pp, 2),
                            "bpm": _r(v.get("bpm_cur", v.get("bpm")), 1), "obpm": _r(v.get("obpm_cur"), 1),
                            "dbpm": _r(v.get("dbpm_cur"), 1), "value": _r(v.get("bpm"), 1),
                            "ortg": _r(v.get("ortg"), 0), "drtg": _r(v.get("drtg"), 0), "per": _r(v.get("per"), 1),
                            "ts": _r(v.get("ts_pct"), 3), "usg": _r(v.get("usg_pct"), 1), "vorp": _r(v.get("vorp"), 1)})
            return out

        def side_info(team, s):
            tr = st["teams"][team]
            return {"abbr": team, "name": TEAMS[team].name, "adj_o": _r(st["mu"] + tr["O"], 1),
                    "adj_d": _r(st["mu"] + tr["D"], 1), "adj_net": _r(tr["net"], 1),
                    "adj_pace": _r(st["mu_pace"] + tr["P"], 1), "games": tr["games"],
                    "elo": _r(bl["elo_h"] if s == "h" else bl["elo_a"], 0),
                    "rest_days": (sf or {}).get(f"{s}_rest"), "b2b": bool((sf or {}).get(f"{s}_b2b")),
                    "three_in_four": bool((sf or {}).get(f"{s}_3in4")),
                    "travel_miles": _r((sf or {}).get(f"{s}_travel"), 0), "tz_shift": (sf or {}).get(f"{s}_tz"),
                    "altitude": bool((sf or {}).get(f"{s}_alt")),
                    "avail_dO": _r(adj[f"dO_{s}"], 2), "avail_dD": _r(adj[f"dD_{s}"], 2),
                    "injuries": team_inj[team], "season_stats": tstats(team), "rotation": rotation_rows(team),
                    "proj_pts": _r(bl["pts_h"] if s == "h" else bl["pts_a"], 1)}

        hw = win_prob(payoffs[f"{gid}-ml-h"]) if f"{gid}-ml-h" in payoffs else float(np.mean(sim.margin > 0))
        games_out.append({
            "id": gid, "tip": g["date"], "hours_to_tip": _r(hours, 1), "neutral": g["neutral"],
            "venue_city": g["venue_city"], "notes": g.get("notes"),
            "home": side_info(h, "h"), "away": side_info(a, "a"),
            "market": {"provider": (g["odds"] or {}).get("provider"), **{k: _r(v, 1) for k, v in o.items()},
                       "open_spread_home": _r(op.get("spread_home"), 1), "open_total": _r(op.get("total"), 1)},
            "model": {"margin_ratings": _r(bl["m_ratings"], 2), "margin_elo": _r(bl["m_elo"], 2),
                      "margin_model": _r(bl["m_model"], 2), "margin_market": _r(bl["m_market"], 2),
                      "margin_final": _r(bl["m_final"], 2), "total_model": _r(bl["t_model"], 1),
                      "total_market": _r(bl["t_market"], 1), "total_final": _r(bl["t_final"], 1),
                      "pace": _r(bl["pace"], 1), "sit_margin": _r(bl["sit_margin"], 2),
                      "avail_margin": _r(bl["avail_margin"], 2), "p_home_win": _r(hw, 4),
                      "w_model_margin": _r(p_live["w_model_vs_market_margin"], 3),
                      "w_model_total": _r(p_live["w_model_vs_market_total"], 3),
                      "home_score_p10_p90": [int(np.percentile(sim.hs, 10)), int(np.percentile(sim.hs, 90))],
                      "away_score_p10_p90": [int(np.percentile(sim.as_, 10)), int(np.percentile(sim.as_, 90))]},
            "n_props": n_props,
        })
        log(f"  {a} @ {h}: final margin {bl['m_final']:+.1f} (model {bl['m_model']:+.1f}, market "
            f"{(bl['m_market'] if bl['m_market'] is not None else float('nan')):+.1f}), total {bl['t_final']:.1f}, "
            f"{n_props} props priced")

    # ------------------------------------------------------- portfolio
    min_ev, max_edge = params["min_ev"], params["max_edge_prob"]

    no_margin = params.get("w_margin_early", 0) <= 0 and params.get("w_model_vs_market_margin", 0) <= 0

    def eligible(b):
        if b["ev"] < min_ev or b["p_final"] < 0.25:
            return False
        if no_margin and b["kind"] in ("spread", "moneyline"):
            return False   # spreads/moneylines are shown for information only (see backtest)
        if b["edge"] is not None and abs(b["edge"]) > max_edge:
            return False   # gap this large usually means stale line or late news
        if b["kind"] == "prop":
            if b.get("play_prob", 1) < 0.85:
                return False
            if not (params.get("prop_min_ev", 0.02) <= b["ev"] <= params.get("prop_max_ev", 0.06)):
                return False
            # signal agreement: the raw projection must also favour this side
            raw = b.get("p_model_raw")
            if raw is not None:
                raw_side = raw if b["side"] == "over" else 1 - raw
                if raw_side < (b["p_market"] or 0.5):
                    return False
        return True

    cands = sorted([b for b in bets if eligible(b)], key=lambda b: b["ev"], reverse=True)
    # cap props per player and per game to keep the book diversified
    seen_pp, kept = {}, []
    for b in cands:
        k = (b["game_id"], b.get("player_id"))
        if b["kind"] == "prop":
            if seen_pp.get(k, 0) >= 2:
                continue
            seen_pp[k] = seen_pp.get(k, 0) + 1
        kept.append(b)
    singles = kept[:30]
    lock = max(kept, key=lambda b: (b["p_final"], b["ev"])) if kept else None
    if lock is not None and lock not in singles:
        singles.append(lock)
    parlays_all = build_parlays(singles, payoffs, max_legs=int(params.get("parlay_max_legs", 4)),
                                pool=int(params.get("parlay_pool", 14)),
                                min_leg_prob=float(params.get("parlay_min_leg_prob", 0.45)))
    parlays = select_parlays(parlays_all, int(params.get("max_parlays", 6)))
    for i, p in enumerate(parlays):
        p["id"] = f"parlay-{i + 1}"
        p["american"] = decimal_to_american(p["dec"])
        payoffs[p["id"]] = p["payoff"]

    ids = [b["id"] for b in singles] + [p["id"] for p in parlays]
    R = np.column_stack([payoffs[i] for i in ids]) if ids else np.zeros((n, 0))
    full = kelly.optimal_kelly(R) if ids else np.zeros(0)
    allocations = {}
    for name, prof in kelly.PROFILES.items():
        frac = np.minimum(full * prof["kelly"], prof["max_bet"])
        if frac.sum() > prof["max_total"]:
            frac *= prof["max_total"] / frac.sum()
        items = [{"ref": i, "frac": _r(x, 5)} for i, x in zip(ids, frac) if x >= 0.0005]
        allocations[name] = {**{k: v for k, v in prof.items()}, "items": items,
                             "risk": {k: _r(v, 4) for k, v in kelly.risk_summary(R, frac).items()}}
    mu, cov = discrete.moments(R)
    portfolio = {"ids": ids, "mu": [round(float(x), 5) for x in mu],
                 "cov": [[round(float(x), 6) for x in row] for row in cov]}
    for b, fk in zip(singles, full[:len(singles)]):
        b["kelly_full"] = float(fk)
    for p, fk in zip(parlays, full[len(singles):]):
        p["kelly_full"] = float(fk)

    # ----------------------------------------------------------- picks
    picks = {}
    if singles:
        picks["lock"] = lock["id"]
        picks["value"] = max(singles, key=lambda b: b.get("kelly_full", 0) * (1 + b["ev"]))["id"]
        game_singles = [b for b in singles if b["kind"] != "prop"]
        if game_singles:
            picks["best_game_bet"] = max(game_singles, key=lambda b: b["ev"])["id"]
        prop_singles = [b for b in singles if b["kind"] == "prop"]
        if prop_singles:
            picks["best_prop"] = max(prop_singles, key=lambda b: b["ev"])["id"]
    if parlays:
        live = [p for p in parlays if p["p_hit"] >= 0.20] or parlays
        picks["parlay"] = max(live, key=lambda p: p["ev"])["id"]
        picks["safe_parlay"] = max(parlays, key=lambda p: p["p_hit"])["id"]

    # same-game parlay fair odds for the top legs of each game (informational)
    sgp = []
    for g in games_out:
        gl = [b for b in kept if b["game_id"] == g["id"]][:3]
        if len(gl) >= 2:
            fair = same_game_fair(gl, payoffs)
            sgp.append({"game_id": g["id"], "legs": [b["id"] for b in gl], "p_hit": _r(fair["p_hit"], 4),
                        "fair_american": _r(decimal_to_american(fair["fair_dec"]), 0) if fair["fair_dec"] else None})

    eligible_ids = {b["id"] for b in kept}
    shown = [b for b in bets if b["kind"] != "prop"] + sorted(
        [b for b in bets if b["kind"] == "prop"], key=lambda b: b["ev"], reverse=True)[:400]
    shown_ids = {b["id"] for b in shown}
    for b in singles:
        if b["id"] not in shown_ids:
            shown.append(b)
    out_bets = []
    for b in shown:
        out_bets.append({k: (_r(v, 4) if isinstance(v, float) else v) for k, v in b.items()} |
                        {"american": _r(b["price"], 0), "eligible": b["id"] in eligible_ids})

    teams_tbl = sorted(({"team": t, "name": TEAMS[t].name, "adj_o": _r(st["mu"] + v["O"], 1),
                         "adj_d": _r(st["mu"] + v["D"], 1), "adj_net": _r(v["net"], 1),
                         "adj_pace": _r(st["mu_pace"] + v["P"], 1), "games": v["games"],
                         "elo": _r(eng.elo_now.get(t), 0)} for t, v in st["teams"].items()),
                       key=lambda r: -r["adj_net"])
    result = {
        "brand": BRAND, "version": __version__,
        "generated": now.isoformat(timespec="seconds"), "date": str(day), "season": season,
        "n_sims": n, "games": games_out, "bets": out_bets,
        "parlays": [{k: (_r(v, 4) if isinstance(v, float) else v) for k, v in p.items() if k != "payoff"}
                    for p in parlays],
        "same_game_fair": sgp, "allocations": allocations, "picks": picks, "teams": teams_tbl,
        "portfolio": portfolio, "_R": R,
        "params": {k: params[k] for k in ("margin_sd", "total_sd", "w_ratings", "w_model_vs_market_margin",
                                          "w_model_vs_market_total", "w_model_vs_market_props", "hca_points",
                                          "min_ev", "max_edge_prob")},
        "counts": {"games": len(games_out), "bets_priced": len(bets), "eligible": len(kept),
                   "props_priced": sum(1 for b in bets if b["kind"] == "prop")},
    }
    return result


def write_outputs(result: dict) -> None:
    SITE_DATA.mkdir(parents=True, exist_ok=True)
    (SITE_DATA / "slates").mkdir(exist_ok=True)
    result = {k: v for k, v in result.items() if not k.startswith("_")}
    txt = json.dumps(result, separators=(",", ":"), default=_json_default)
    (SITE_DATA / "latest.json").write_text(txt)
    # dated snapshot without the full props board (keeps the repo small)
    keep = {i["ref"] for a in result["allocations"].values() for i in a["items"]}
    keep |= {l for p in result["parlays"] for l in p["legs"]} | set(result["picks"].values())
    slim = {**result, "bets": [b for b in result["bets"] if b["id"] in keep or b["kind"] != "prop"]}
    (SITE_DATA / "slates" / f"{result['date']}.json").write_text(
        json.dumps(slim, separators=(",", ":"), default=_json_default))


def _json_default(o):
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (pd.Timestamp, datetime, date)):
        return str(o)
    raise TypeError(type(o))
