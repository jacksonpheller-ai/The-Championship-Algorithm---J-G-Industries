"""Full-pipeline replay ("paper trading") over past nights under the growth policy.

For every night of a past season, the exact nightly pipeline is run with:
  * the slate and the morning (opening) lines stored for that night,
  * ratings, Elo and player values built only from games before that night,
  * no injury report (the stored history has none), only the "missed last 3 games" signal.
The SHRUNK joint simulation (jghedge.portfolio.policy: s = 0.5 on totals and parlays) is then
sized with the same policy the desk uses live: night verdict on the $100 / $0.50 reference
ticket (PASS nights are skipped), adaptive Kelly tier at the RUNNING bankroll, per-bet growth
gate, whole-unit stakes with the 5% / 10% caps. Bets are settled from final scores and the
bankroll compounded. Two random seeds (0 and 1000) are run and averaged.

Not replayed: player props (ESPN's stored historical prop prices are mostly in-game; props are
informational, s = 0) and PrizePicks pick'em slips (there is no historical PrizePicks line
archive). Both are stated in the output.
"""
from __future__ import annotations

import json
from collections import defaultdict

import numpy as np
import pandas as pd

from . import pipeline
from .config import DATA_DIR, REF_BANKROLL, REF_UNIT, SITE_DATA
from .data import history
from .data.espn import PLAYER_COLS
from .data.http import Fetcher
from .ledger import _settle_leg
from .loader import build_engine
from .portfolio import policy

SEEDS = (0, 1000)
NOT_REPLAYED = ["Player props are not replayed (stored historical prop prices are mostly in-game; props are "
                "informational at s = 0).",
                "PrizePicks pick'em slips cannot be replayed: no historical PrizePicks line archive exists."]


def _settle(ref, bets, parlays, games, players):
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


def _slate(season: int, lines: str) -> tuple[dict, dict, dict]:
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
            "season_type": g.get("season_type"),
            "odds": {"provider": (g.get("odds") or {}).get("provider"), "close": o,
                     "open": (g.get("odds") or {}).get("open") or {}}})
    return games, players, by_day


def replay(season: int, lines: str = "open", bankroll: float = 1000.0, unit: float = 1.0,
           n_sims: int = 6000, verbose: bool = True, engine=None, overrides: dict | None = None,
           with_parlays: bool = True, small_start: float = 10.0, small_units: tuple = (0.5, 0.1),
           seed_offset: int = 0) -> dict:
    """One seed path. The main track starts at `bankroll` with `unit` stakes (1,000 units at
    $1,000 / $1: the 'standard' tier); the small tracks start at `small_start` with each of
    `small_units` under the adaptive tiers (tiny k 0.5 below 25 units, small 0.35, standard 0.25)."""
    f = Fetcher()
    eng = engine or build_engine(f, [season - 2, season - 1, season])
    games, players, by_day = _slate(season, lines)
    bank, peak, max_dd = bankroll, bankroll, 0.0
    nights, rows = [], []
    verdict_counts = {"PASS": 0, "LIGHT": 0, "BET": 0}
    small = {f"{u:g}": {"bank": small_start, "peak": small_start, "max_dd": 0.0, "bets": 0, "wins": 0,
                        "bust": None, "unit": u, "tiers": defaultdict(int)} for u in small_units}
    for i, d in enumerate(sorted(by_day)):
        ov = dict(overrides or {})
        if not with_parlays:
            ov["max_parlays"] = 0
        res = pipeline.run(day=d, n_sims=n_sims, update=False, with_props=False, verbose=False,
                           engine=eng, slate=by_day[d], injuries={}, replay=True, season=season,
                           seed=i + seed_offset, overrides=ov, hours_override=10.0 if lines == "open" else 0.5,
                           label="replay")
        bets = {b["id"]: b for b in res["bets"]}
        parlays = {p["id"]: p for p in res["parlays"]}
        R_s, ids = res.get("_R_shrunk"), res.get("portfolio", {}).get("ids", [])
        v, score = "PASS", 0.0
        if ids:
            ref_units = policy.reference_units(R_s)
            score = policy.night_score(R_s, ref_units, REF_UNIT, REF_BANKROLL)
            v = policy.verdict(score, int((ref_units > 0).sum()))
        verdict_counts[v] += 1
        night = {"date": str(d), "games": len(by_day[d]), "verdict": v, "score": round(score, 6),
                 "bets": 0, "staked": 0.0, "ret": 0.0, "bank": round(bank, 2)}
        if v != "PASS":
            tk = policy.size_ticket(R_s, bank, unit, verdict=v)
            pnl = staked = 0.0
            for ref, u_n, g1 in zip(ids, tk["units"], tk["growth_1u"]):
                if u_n <= 0:
                    continue
                r, u = _settle(ref, bets, parlays, games, players)
                if r is None:
                    continue
                stake = u_n * unit
                pnl += stake * u
                staked += stake
                obj = bets.get(ref) or parlays.get(ref)
                rows.append({"date": str(d), "ref": ref, "kind": obj.get("kind", "parlay"), "units": int(u_n),
                             "stake": round(stake, 2), "frac": round(stake / bank, 5), "result": r, "pnl_per_1": u,
                             "ev": obj.get("ev"), "shrunk_ev": obj.get("shrunk_ev"),
                             "p": obj.get("p_final", obj.get("p_hit")), "dec": obj.get("dec"),
                             "growth_1u": None if g1 is None or not np.isfinite(g1) else round(float(g1), 6),
                             "verdict": v, "score": round(score, 6), "tier": tk["tier"]["label"]})
            night.update({"bets": int((tk["units"] > 0).sum()), "staked": round(staked / bank, 4),
                          "ret": round(pnl / bank, 5), "tier": tk["tier"]["label"]})
            bank += pnl
            peak = max(peak, bank)
            max_dd = max(max_dd, 1 - bank / peak)
            night["bank"] = round(bank, 2)
            # small-bankroll tracks: adaptive tier at the running bankroll, whole units
            for k, trk in small.items():
                su = trk["unit"]
                if trk["bust"] is None and trk["bank"] < su:
                    trk["bust"] = str(d)
                if trk["bust"] is not None:
                    continue
                stk = policy.size_ticket(R_s, trk["bank"], su, verdict=v)
                trk["tiers"][stk["tier"]["label"]] += 1
                p_small = 0.0
                for ref, u_n in zip(ids, stk["units"]):
                    if u_n <= 0:
                        continue
                    r, u = _settle(ref, bets, parlays, games, players)
                    if r is None:
                        continue
                    trk["bets"] += 1
                    trk["wins"] += r == "win"
                    p_small += u_n * su * u
                trk["bank"] = round(trk["bank"] + p_small, 2)
                trk["peak"] = max(trk["peak"], trk["bank"])
                trk["max_dd"] = max(trk["max_dd"], 1 - trk["bank"] / trk["peak"])
        for k, trk in small.items():
            night[f"small_{k}"] = trk["bank"]
        nights.append(night)
        if verbose and i % 20 == 0:
            print(f"  {d}: bankroll {bank:,.0f} ({len(nights)} nights, {verdict_counts})")
    df = pd.DataFrame(rows)
    out = {"season": season, "lines": lines, "policy": "shrunk s=0.5, adaptive tiers, growth gate, PASS nights skipped",
           "seed_offset": seed_offset, "start_bankroll": bankroll, "unit": unit,
           "end_bankroll": round(bank, 2), "nights": len(nights),
           "nights_bet": int(sum(1 for n in nights if n["bets"])),
           "verdicts": verdict_counts,
           "winning_nights": int(sum(1 for n in nights if n["ret"] > 0)),
           "losing_nights": int(sum(1 for n in nights if n["ret"] < 0)),
           "max_drawdown": round(max_dd, 4), "nightly": nights, "rows": rows,
           "not_replayed": NOT_REPLAYED,
           "small": {f"10@{k}": {"start": small_start, "unit": v_["unit"], "profile": "adaptive", "end": v_["bank"],
                                 "bets": v_["bets"], "wins": v_["wins"], "max_drawdown": round(v_["max_dd"], 4),
                                 "bust_date": v_["bust"], "tier_nights": dict(v_["tiers"])} for k, v_ in small.items()}}
    if len(df):
        settled = df[df["result"].isin(["win", "loss"])]
        out.update({
            "bets": int(len(df)), "wins": int((df["result"] == "win").sum()), "losses": int((df["result"] == "loss").sum()),
            "flat_roi": round(float(df["pnl_per_1"].sum() / max(len(settled), 1)), 4),
            "stake_weighted_roi": round(float((df["stake"] * df["pnl_per_1"]).sum() / df["stake"].sum()), 4),
            "avg_predicted_ev": round(float(df["ev"].mean()), 4),
            "avg_shrunk_ev": round(float(df["shrunk_ev"].dropna().mean()), 4) if df["shrunk_ev"].notna().any() else None,
            "by_kind": {k: {"bets": int(len(g)), "win_pct": round(float((g["result"] == "win").sum() / max(g["result"].isin(["win", "loss"]).sum(), 1)), 4),
                            "flat_roi": round(float(g["pnl_per_1"].sum() / max(g["result"].isin(["win", "loss"]).sum(), 1)), 4)}
                        for k, g in df.groupby("kind")},
        })
        cal = []
        singles = df[df["kind"] != "parlay"].dropna(subset=["p"])
        for lo in np.arange(0.3, 0.8, 0.1):
            m = (singles["p"] >= lo) & (singles["p"] < lo + 0.1) & singles["result"].isin(["win", "loss"])
            if m.sum() >= 15:
                cal.append({"bin": f"{lo:.1f}-{lo + 0.1:.1f}", "pred": round(float(singles.loc[m, "p"].mean()), 3),
                            "actual": round(float((singles.loc[m, "result"] == "win").mean()), 3), "n": int(m.sum())})
        out["bet_calibration"] = cal
    else:
        out.update({"bets": 0, "wins": 0, "losses": 0, "flat_roi": 0.0, "stake_weighted_roi": 0.0})
    return out


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return round(float(np.mean(vals)), 4) if vals else None


def combine_seeds(runs: dict[str, dict]) -> dict:
    """Seed-averaged summary. The top-level `nightly` is the first seed's path; every other
    seed keeps its own `nightly` under `seeds` (jghedge.growth pools both layouts)."""
    keys = sorted(runs, key=int)
    first = runs[keys[0]]
    out = {k: v for k, v in first.items() if k not in ("rows", "seed_offset")}
    for fld in ("end_bankroll", "max_drawdown", "stake_weighted_roi", "flat_roi", "bets", "wins", "losses",
                "nights_bet", "winning_nights", "losing_nights", "avg_predicted_ev", "avg_shrunk_ev"):
        out[fld] = _mean([r.get(fld) for r in runs.values()])
    out["verdicts"] = {v: _mean([r["verdicts"].get(v) for r in runs.values()]) for v in ("PASS", "LIGHT", "BET")}
    out["small"] = {k: {**first["small"][k], "end": _mean([r["small"][k]["end"] for r in runs.values()]),
                        "max_drawdown": _mean([r["small"][k]["max_drawdown"] for r in runs.values()]),
                        "bets": _mean([r["small"][k]["bets"] for r in runs.values()]),
                        "wins": _mean([r["small"][k]["wins"] for r in runs.values()]),
                        "bust_date": [r["small"][k]["bust_date"] for r in runs.values()]}
                    for k in first["small"]}
    out["seeds_run"] = [int(k) for k in keys]
    out["seeds"] = {}
    for k in keys:
        r = runs[k]
        slim = {fld: r.get(fld) for fld in ("seed_offset", "end_bankroll", "max_drawdown", "stake_weighted_roi", "flat_roi",
                                             "bets", "wins", "losses", "nights_bet", "verdicts", "by_kind", "small")}
        slim["rows"] = r.get("rows", [])
        if k != keys[0]:
            slim["nightly"] = r["nightly"]
        out["seeds"][k] = slim
    out["per_seed_end"] = {k: runs[k]["end_bankroll"] for k in keys}
    return out


def run_and_save(seasons: list[int], lines: str = "open", seeds: tuple = SEEDS, n_sims: int = 4000) -> dict:
    f = Fetcher()
    allres = {}
    for s in seasons:
        eng = build_engine(f, [s - 2, s - 1, s])
        runs = {}
        for off in seeds:
            print(f"Replaying {s} with {lines} lines, seed {off} ...")
            runs[str(off)] = replay(s, lines=lines, engine=eng, n_sims=n_sims, verbose=False, seed_offset=int(off))
        allres[str(s)] = combine_seeds(runs)
    (DATA_DIR / f"replay_{lines}.json").write_text(json.dumps(allres, indent=1))
    SITE_DATA.mkdir(parents=True, exist_ok=True)
    slim = {s: {k: v for k, v in r.items() if k != "seeds"} | {"seeds": {sd: {k: v for k, v in sr.items() if k != "rows"}
                                                                          for sd, sr in r["seeds"].items()}}
            for s, r in allres.items()}
    (SITE_DATA / f"replay_{lines}.json").write_text(json.dumps(slim, separators=(",", ":")))
    return allres
