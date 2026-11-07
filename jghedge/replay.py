"""Full-pipeline replay ("paper trading") over past nights.

For every night of a past season, the exact nightly pipeline is run with:
  * the slate and the morning (opening) lines stored for that night,
  * ratings, Elo and player values built only from games before that night,
  * no injury report (the stored history has none), only the "missed last 3 games" signal.
The recommended allocation is then settled from final scores and the bankroll compounded.
Player props are excluded because ESPN's stored historical prop prices are mostly in-game.
"""
from __future__ import annotations

import json
from collections import defaultdict

import numpy as np
import pandas as pd

from . import pipeline
from .config import DATA_DIR, SITE_DATA
from .data import history
from .data.espn import PLAYER_COLS
from .data.http import Fetcher
from .ledger import _settle_leg
from .loader import build_engine
from .portfolio import discrete, kelly


def _settle(item, bets, parlays, games, players):
    ref = item["ref"]
    if ref in bets:
        r = _settle_leg(bets[ref], games, players)
        if r is None:
            return None, None
        b = bets[ref]
        return r, ((b["dec"] - 1) if r == "win" else (-1.0 if r == "loss" else 0.0))
    p = parlays.get(ref)
    if not p:
        return None, None
    res = [_settle_leg(bets[l], games, players) for l in p["legs"]]
    if any(r is None for r in res):
        return None, None
    if "loss" in res:
        return "loss", -1.0
    dec = 1.0
    for l, r in zip(p["legs"], res):
        dec *= bets[l]["dec"] if r == "win" else 1.0
    return ("win" if dec > 1 else "push"), dec - 1.0


def replay(season: int, lines: str = "open", profile: str = "balanced", bankroll: float = 1000.0,
           n_sims: int = 6000, verbose: bool = True, engine=None, overrides: dict | None = None,
           with_parlays: bool = True, small_start: float = 10.0, small_unit: float = 0.5,
           small_profiles: tuple = ("balanced", "aggressive", "max_growth"), small_units: tuple = (0.5, 0.1)) -> dict:
    f = Fetcher()
    eng = engine or build_engine(f, [season - 2, season - 1, season])
    recs = history.load_season(season)
    games = {g["id"]: g for g in recs}
    players = {}
    for g in recs:
        for p in g.get("players", []):
            r = dict(zip(PLAYER_COLS, p))
            players[(g["id"], str(r["id"]))] = r
    by_day = defaultdict(list)
    for g in recs:
        if g.get("season_type") not in (2, 5):
            continue   # regular season + play-in
        o = (g.get("odds") or {}).get(lines) or {}
        if o.get("spread_home") is None or o.get("spread_home_price") is None:
            continue
        by_day[history.game_day(g["date"])].append({
            "id": g["id"], "date": g["date"], "home": g["home"], "away": g["away"],
            "neutral": bool(g.get("neutral")), "venue_city": g.get("venue_city"), "state": "pre",
            "odds": {"provider": (g.get("odds") or {}).get("provider"), "close": o,
                     "open": (g.get("odds") or {}).get("open") or {}}})
    bank = bankroll
    peak = bankroll
    max_dd = 0.0
    nights, rows = [], []
    # small-bankroll tracks: whole-unit stakes, no fractional positions
    small = {(f"{k}@{u:g}" if u != small_units[0] else k): {"bank": small_start, "peak": small_start, "max_dd": 0.0,
             "bets": 0, "wins": 0, "bust": None, "profile": k, "unit": u}
             for u in small_units for k in small_profiles}
    for i, d in enumerate(sorted(by_day)):
        ov = dict(overrides or {})
        seed_offset = int(ov.pop("_seed_offset", 0))
        if not with_parlays:
            ov["max_parlays"] = 0
        res = pipeline.run(day=d, n_sims=n_sims, update=False, with_props=False, verbose=False,
                           engine=eng, slate=by_day[d], injuries={}, replay=True, season=season, seed=i + seed_offset,
                           overrides=ov, hours_override=10.0 if lines == "open" else 0.5)
        bets = {b["id"]: b for b in res["bets"]}
        parlays = {p["id"]: p for p in res["parlays"]}
        items = res["allocations"][profile]["items"]
        ret = staked = 0.0
        for it in items:
            r, u = _settle(it, bets, parlays, games, players)
            if r is None:
                continue
            ret += it["frac"] * u
            staked += it["frac"]
            ref = bets.get(it["ref"]) or parlays.get(it["ref"])
            rows.append({"date": str(d), "ref": it["ref"], "kind": ref.get("kind", "parlay"),
                         "frac": it["frac"], "result": r, "units": u, "ev": ref.get("ev"),
                         "p": ref.get("p_final", ref.get("p_hit")), "dec": ref.get("dec")})
        bank *= 1 + ret
        peak = max(peak, bank)
        max_dd = max(max_dd, 1 - bank / peak)
        night = {"date": str(d), "games": len(by_day[d]), "bets": len(items), "staked": round(staked, 4),
                 "ret": round(ret, 5), "bank": round(bank, 2)}
        R, ids = res.get("_R"), res.get("portfolio", {}).get("ids", [])
        for k, trk in small.items():
            prof = kelly.PROFILES[trk["profile"]]
            su = trk["unit"]
            if trk["bust"] is not None or R is None or not ids or trk["bank"] < su:
                if trk["bust"] is None and trk["bank"] < su:
                    trk["bust"] = str(d)
                night[f"small_{k}"] = round(trk["bank"], 2)
                continue
            units = discrete.discrete_allocate(R, trk["bank"], su, prof["kelly"], prof["max_bet"], prof["max_total"])
            pnl = 0.0
            for ref, u_n in zip(ids, units):
                if u_n <= 0:
                    continue
                r, u = _settle({"ref": ref}, bets, parlays, games, players)
                if r is None:
                    continue
                trk["bets"] += 1
                trk["wins"] += r == "win"
                pnl += u_n * su * u
            trk["bank"] = round(trk["bank"] + pnl, 2)
            trk["peak"] = max(trk["peak"], trk["bank"])
            trk["max_dd"] = max(trk["max_dd"], 1 - trk["bank"] / trk["peak"])
            night[f"small_{k}"] = trk["bank"]
        nights.append(night)
        if verbose and i % 20 == 0:
            print(f"  {d}: bankroll {bank:,.0f} ({len(nights)} nights)")
    df = pd.DataFrame(rows)
    out = {"season": season, "lines": lines, "profile": profile, "start_bankroll": bankroll,
           "end_bankroll": round(bank, 2), "nights": len(nights),
           "nights_bet": int(sum(1 for n in nights if n["bets"])),
           "winning_nights": int(sum(1 for n in nights if n["ret"] > 0)),
           "losing_nights": int(sum(1 for n in nights if n["ret"] < 0)),
           "max_drawdown": round(max_dd, 4), "nightly": nights,
           "small": {k: {"start": small_start, "unit": v["unit"], "profile": v["profile"], "end": v["bank"], "bets": v["bets"],
                         "wins": v["wins"], "max_drawdown": round(v["max_dd"], 4), "bust_date": v["bust"]} for k, v in small.items()}}
    if len(df):
        settled = df[df["result"].isin(["win", "loss"])]
        out.update({
            "bets": int(len(df)), "wins": int((df["result"] == "win").sum()), "losses": int((df["result"] == "loss").sum()),
            "flat_roi": round(float(df["units"].sum() / max(len(settled), 1)), 4),
            "stake_weighted_roi": round(float((df["frac"] * df["units"]).sum() / df["frac"].sum()), 4),
            "avg_predicted_ev": round(float(df["ev"].mean()), 4),
            "by_kind": {k: {"bets": int(len(g)), "win_pct": round(float((g["result"] == "win").sum() / max(g["result"].isin(["win", "loss"]).sum(), 1)), 4),
                            "flat_roi": round(float(g["units"].sum() / max(g["result"].isin(["win", "loss"]).sum(), 1)), 4)}
                        for k, g in df.groupby("kind")},
        })
        # calibration of the desk's own probabilities on the bets it actually placed
        cal = []
        singles = df[df["kind"] != "parlay"].dropna(subset=["p"])
        for lo in np.arange(0.3, 0.8, 0.1):
            m = (singles["p"] >= lo) & (singles["p"] < lo + 0.1) & singles["result"].isin(["win", "loss"])
            if m.sum() >= 15:
                cal.append({"bin": f"{lo:.1f}-{lo + 0.1:.1f}", "pred": round(float(singles.loc[m, "p"].mean()), 3),
                            "actual": round(float((singles.loc[m, "result"] == "win").mean()), 3), "n": int(m.sum())})
        out["bet_calibration"] = cal
    return out


def run_and_save(seasons: list[int], lines: str = "open") -> dict:
    f = Fetcher()
    allres = {}
    for s in seasons:
        eng = build_engine(f, [s - 2, s - 1, s])
        print(f"Replaying {s} with {lines} lines ...")
        allres[str(s)] = replay(s, lines=lines, engine=eng, n_sims=4000, verbose=False)
    (DATA_DIR / f"replay_{lines}.json").write_text(json.dumps(allres, indent=1))
    SITE_DATA.mkdir(parents=True, exist_ok=True)
    (SITE_DATA / f"replay_{lines}.json").write_text(json.dumps(allres, separators=(",", ":")))
    return allres
