"""Nightly run: data -> projections -> simulation -> pricing -> parlays -> policy -> JSON.

Growth rebuild (research memo, 2026-11-07): the desk is sized on SHRUNK payoffs (s = 0.5 on
totals and parlays, s = 0 on props), with a per-bet growth gate, an adaptive Kelly tier by
bankroll in units and a nightly verdict (PASS / LIGHT / BET) evaluated on a reference ticket
of $100 at $0.50 units. Game bets are for Fliff, player pick'em slips for PrizePicks. Every
run records the lines it saw (data/lines/<date>.json) for closing-line value.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import BRAND, __version__
from . import growth as growth_mod
from . import pickem
from .config import (CURRENT_SEASON, DATA_DIR, FLIFF_MIN_BET, PICKEM_LEAN, PICKEM_MIN_ENTRY, PICKEM_PAYOUTS, PROPS_REGIME,
                     REF_BANKROLL, REF_UNIT, RUN_LABEL_HOURS_ET, SITE_DATA, load_params)
from .data import espn, history, oddsapi, prizepicks
from .data import lines as lines_mod
from .data.http import Fetcher
from .loader import bbref_table, build_engine
from .model import context as ctx
from .model import players as pl
from .model.odds import (american_to_decimal, decimal_to_american, inv_logit, logit,
                         novig_multiplicative, novig_power)
from .model.props import COMBOS, PropModel
from .backtest import p_home_win
from .model.simulate import GameSim, _gauss_quantiles, centred_table, win_prob
from .model.situational import schedule_features
from .portfolio import discrete, kelly, policy
from .portfolio.parlay import build_parlays, same_game_fair, select_parlays
from .teams import TEAMS

STAT_LABEL = {"pts": "Points", "reb": "Rebounds", "ast": "Assists", "fg3m": "3-Pointers Made",
              "stl": "Steals", "blk": "Blocks", "tov": "Turnovers", "pra": "Pts+Reb+Ast",
              "pr": "Pts+Reb", "pa": "Pts+Ast", "ra": "Reb+Ast"}
ET = ZoneInfo("America/New_York")
PROPS_LOG_DIR = DATA_DIR / "props_log"
GROWTH_CACHE = DATA_DIR / "growth_grid.json"
MAX_PROPS_SHOWN = 300
MAX_PICKEM_LEGS = 40
MAX_PICKEM_DRAWS = 20000
GTD_STARTER_MIN = 24.0          # a 'starter' for the lineup-uncertainty flag: >= 24 typical minutes


def us_today() -> date:
    return (datetime.now(timezone.utc) - timedelta(hours=6)).date()


def run_label(now: datetime | None = None) -> str:
    """Snapshot label: JG_RUN_LABEL if set, else by Eastern hour (opener before 06:00 ET,
    morning, pretip for the early-tip run, afternoon)."""
    env = os.environ.get("JG_RUN_LABEL", "").strip().lower()
    if env:
        return env
    now = now or datetime.now(timezone.utc)
    h = now.astimezone(ET).hour + now.astimezone(ET).minute / 60.0
    if h < RUN_LABEL_HOURS_ET["opener"] or h >= RUN_LABEL_HOURS_ET["evening"]:
        return "opener"          # the 02:31 UTC job (21:31 ET) prices the next day's openers
    if h < RUN_LABEL_HOURS_ET["morning"]:
        return "morning"
    if h < RUN_LABEL_HOURS_ET["pretip"]:
        return "pretip"
    return "afternoon"


def slate_day(label: str, now: datetime | None = None) -> date:
    """The slate an unlabelled run prices: today (US), or tomorrow for an evening 'opener' run
    (the 02:31 UTC job prices the next day's openers)."""
    now = now or datetime.now(timezone.utc)
    et = now.astimezone(ET)
    d = et.date()                     # Eastern calendar date: an opener delayed past midnight ET
    if label == "opener" and et.hour >= 18:   # still prices the right (new) day
        d = d + timedelta(days=1)
    return d


def run_plan(now: datetime | None = None) -> list[tuple[date, str]]:
    """What an unlabelled run prices: tonight (the Eastern date) and tomorrow, so the page can offer both
    (tomorrow is priced even before its lines are posted: the page then shows each game's own number).
    Tonight's label follows the clock (opener before 06:00 ET, morning, pretip, afternoon; an evening look
    at games that have not tipped is 'afternoon'); a look at tomorrow is always an 'opener'."""
    now = now or datetime.now(timezone.utc)
    et = now.astimezone(ET)
    h = et.hour + et.minute / 60.0
    if h < RUN_LABEL_HOURS_ET["opener"]:
        lab = "opener"
    elif h < RUN_LABEL_HOURS_ET["morning"]:
        lab = "morning"
    elif h < RUN_LABEL_HOURS_ET["pretip"]:
        lab = "pretip"
    else:
        lab = "afternoon"
    return [(et.date(), lab), (et.date() + timedelta(days=1), "opener")]


def fetch_slate(f, day: date) -> list[dict]:
    """The games of `day` that can still be bet: not tipped, both teams known."""
    return [g for g in espn.scoreboard(f, day) if g["state"] == "pre" and g["home"] in TEAMS and g["away"] in TEAMS]


def pick_slates(today: dict | None, tomorrow: dict | None) -> tuple[dict | None, dict | None]:
    """(main, next) for the page from the two priced nights. Tonight is the main slate while it still has a
    game to bet and tomorrow rides along as 'next'; once tonight is over tomorrow becomes the main slate."""
    if today and today.get("games"):
        return today, (tomorrow if tomorrow and tomorrow.get("games") else None)
    if tomorrow and tomorrow.get("games"):
        return tomorrow, None
    return today or tomorrow, None


def _schedule_rows(games: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{"id": g["id"], "date": pd.Timestamp(history.game_day(g["date"])), "tip": g["date"],
                          "home": g["home"], "away": g["away"], "neutral": g["neutral"],
                          "venue_city": g["venue_city"]} for g in games],
                        columns=["id", "date", "tip", "home", "away", "neutral", "venue_city"])


def pending_games(f, day: date, known_ids: set, boards: dict | None = None) -> list[dict]:
    """Games on the two days before `day` that the history does not hold yet: scheduled, under way,
    or final within the last few minutes (postponed games are left out). `boards` maps a date to a
    scoreboard list and replaces the fetch in tests."""
    out = []
    for back in (2, 1):
        d = day - timedelta(days=back)
        try:
            board = boards[d] if boards is not None else espn.scoreboard(f, d)
        except Exception:  # noqa: BLE001
            board = []
        for g in board:
            if g["id"] in known_ids or g["home"] not in TEAMS or g["away"] not in TEAMS:
                continue
            if g.get("state") in ("pre", "in") or g.get("completed"):
                out.append(g)
    return out


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


def _r4(obj):
    """Round every float in a JSON-like structure to 4 dp (the contract's precision)."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else round(obj, 4)
    if isinstance(obj, (np.floating,)):
        return _r4(float(obj))
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, dict):
        return {k: _r4(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_r4(v) for v in obj]
    return obj


# ------------------------------------------------------------------ context helpers
def context_kill_switch(gdf: pd.DataFrame, season: int, asof, params: dict | None = None) -> dict:
    """Rolling over rate of this season's completed late-season games (mean games played >=
    late_gp) against the closing total; the adjustment is switched off below the floor."""
    p = {**ctx.CONTEXT_PARAMS, **(params or {})}
    if gdf is None or len(gdf) == 0:
        return {"kill": False, "n": 0, "over_rate": None}
    g = gdf[(gdf["season"] == season) & (gdf["date"] < pd.Timestamp(asof)) & (gdf["season_type"] == 2)]
    g = g.dropna(subset=["total", "total_pts"]).sort_values(["date", "id"])
    if g.empty:
        return {"kill": False, "n": 0, "over_rate": None}
    counts: dict[str, int] = {}
    results: list[bool] = []
    for r in g.itertuples(index=False):
        gh, ga = counts.get(r.home, 0), counts.get(r.away, 0)
        if (gh + ga) / 2 >= p["late_gp"] and r.total_pts != r.total:
            results.append(bool(r.total_pts > r.total))
        counts[r.home], counts[r.away] = gh + 1, ga + 1
    tail = results[-int(p["kill_switch_window"]):]
    return {"kill": ctx.kill_switch(results, p), "n": len(results),
            "over_rate": round(sum(tail) / len(tail), 4) if tail else None}


def game_context(eng, home: str, away: str, day, season: int, season_type, kill: bool) -> dict:
    return {"home_gp": ctx.games_played(eng.tg, home, day, season), "away_gp": ctx.games_played(eng.tg, away, day, season),
            "season_type": season_type, "home_winpct": ctx.team_winpct(eng.tg, home, day, season),
            "away_winpct": ctx.team_winpct(eng.tg, away, day, season), "kill": kill}


def pickem_lean(regime: dict | None, params: dict | None = None) -> dict:
    """Points moved from P(more) to P(less) when a pick is priced (config.PICKEM_LEAN): the prior blended
    with the rolling live under lean of the logged DraftKings board by line count, clipped to [0, max].
    The live sample only counts once props_regime has enough graded lines to report a lean."""
    p = {**PICKEM_LEAN, **(params or {})}
    live, n = (regime or {}).get("lean"), int((regime or {}).get("n") or 0)
    est = float(p["prior"])
    if live is not None and n > 0:
        est = (p["prior"] * p["prior_n"] + float(live) * n) / (p["prior_n"] + n)
    return {"value": round(float(min(max(est, 0.0), p["max"])), 4), "prior": float(p["prior"]), "prior_n": int(p["prior_n"]),
            "live": live, "live_n": n if live is not None else 0}


# ------------------------------------------------------------------ props regime
def moved_past(day, bets: list[dict], kept_ids: set, entries: list[dict]) -> list[dict]:
    """Totals the desk took earlier today (ledger rows, with the worst line it accepted then) against
    the line now. still_ok is False when the bet no longer qualifies at the current line AND that line
    is past the number the desk accepted: the page then says 'moved, skip'. (Comparing the current line
    with a max line recomputed from that same line, as before, could never be False.)"""
    cur = {b["id"]: b for b in bets if b.get("kind") == "total"}
    out = []
    for e in entries:
        if e.get("date") != str(day) or e.get("type") != "single" or e.get("kind") != "total" or not e.get("legs"):
            continue
        leg, b = e["legs"][0], cur.get(e.get("ref"))
        if b is None or leg.get("line") is None or b.get("line") is None:
            continue                          # the game has tipped, or the line is off the board
        ok = b["id"] in kept_ids or policy.still_ok(leg.get("side"), b["line"], e.get("max_line"))
        out.append({"bet_id": b["id"], "priced_at": e.get("recorded_at"), "line_then": leg["line"], "line_now": b["line"],
                    "max_line_then": e.get("max_line"), "moved_by": round(float(b["line"] - leg["line"]), 2), "still_ok": bool(ok)})
    return out


def log_props(day, rows: list[dict], log_dir=None) -> None:
    """Keep the pre-tip DraftKings 'ou' board (main lines) so the under lean can be measured.
    Several runs price a night and each sees only the games that have not tipped, so the file is
    merged by game: a game's rows are replaced by a later pre-tip look, never dropped."""
    if not rows:
        return
    log_dir = log_dir or PROPS_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{day}.json"
    old = []
    if path.exists():
        try:
            old = json.loads(path.read_text())
        except ValueError:
            old = []
    fresh = {str(r.get("game_id")) for r in rows}
    merged = [r for r in old if str(r.get("game_id")) not in fresh] + list(rows)
    path.write_text(json.dumps(merged, separators=(",", ":")))


def props_regime(day, params: dict | None = None, log_dir=None, results_dir=None) -> dict:
    """Rolling under lean of the logged DK board against final box scores (memo B6):
    lean = under hit rate - implied under rate over the last `window_nights` logged nights.
    'lean_under' when the lean clears the threshold, 'stand_down' when overs are beating their
    price, 'neutral' otherwise or when fewer than `min_lines` lines are available."""
    p = {**PROPS_REGIME, **(params or {})}
    log_dir = log_dir or PROPS_LOG_DIR
    results_dir = results_dir or (SITE_DATA / "results")
    out = {"regime": "neutral", "n": 0, "under_rate": None, "implied_under": None, "lean": None, "nights": 0}
    if not log_dir.exists():
        return out
    days = sorted(p_.stem for p_ in log_dir.glob("*.json") if p_.stem < str(day))[-int(p["window_nights"]):]
    hits, implied = [], []
    for d in days:
        rp = results_dir / f"{d}.json"
        if not rp.exists():
            continue
        try:
            res = json.loads(rp.read_text())
            rows = json.loads((log_dir / f"{d}.json").read_text())
        except ValueError:
            continue
        players = res.get("players") or {}
        for r in rows:
            prow = (players.get(str(r.get("game_id"))) or {}).get(str(r.get("player_id")))
            if prow is None or r.get("over") is None or r.get("under") is None:
                continue
            val = prow.get(r.get("stat"))
            if val is None or val == r["line"]:
                continue
            hits.append(float(val < r["line"]))
            implied.append(1.0 - novig_multiplicative(r["over"], r["under"])[0])
        out["nights"] += 1
    out["n"] = len(hits)
    if len(hits) < int(p["min_lines"]):
        return out
    ur, iu = float(np.mean(hits)), float(np.mean(implied))
    lean = ur - iu
    out.update({"under_rate": round(ur, 4), "implied_under": round(iu, 4), "lean": round(lean, 4)})
    if lean >= p["lean_under_at"]:
        out["regime"] = "lean_under"
    elif lean <= p["stand_down_at"]:
        out["regime"] = "stand_down"
    return out


# ------------------------------------------------------------------ growth cache
def growth_block_cached(replay_file=None, cache_file=None) -> dict:
    """growth.growth_block on data/replay_open.json, cached in data/growth_grid.json and
    recomputed only when the replay file's contents change (sha1 of the whole file)."""
    replay_file = replay_file or growth_mod.REPLAY_FILE
    cache_file = cache_file or GROWTH_CACHE
    if not replay_file.exists():
        return growth_mod.growth_block(replay_file)
    key = hashlib.sha1(replay_file.read_bytes()).hexdigest()
    if cache_file.exists():
        try:
            c = json.loads(cache_file.read_text())
            if c.get("key") == key and c.get("block"):
                return c["block"]
        except ValueError:
            pass
    block = growth_mod.growth_block(replay_file)
    block["grid"] = [{k: v for k, v in row.items() if k not in ("p_loss", "skip_share")} for row in block["grid"]]
    try:
        cache_file.write_text(json.dumps({"key": key, "block": block}, separators=(",", ":")))
    except OSError:
        pass
    return block


# ------------------------------------------------------------------ fliff
def fliff_instruction(b: dict) -> str:
    """'Over 228.5 at -110 or better; skip above 229.5' from the bet's max_line."""
    side, line, mx = b.get("side"), b.get("line"), b.get("max_line")
    price = b.get("american") if b.get("american") is not None else b.get("price")
    ptxt = f"{int(price):+d}" if price is not None else "-110"
    if b.get("kind") == "total" and side in ("over", "under") and line is not None:
        txt = f"{side.capitalize()} {line:g} at {ptxt} or better"
        if mx is not None:
            txt += f"; skip {'above' if side == 'over' else 'below'} {mx:g}"
        return txt
    if b.get("kind") == "parlay":
        return f"{b.get('label')} at {ptxt} or better; skip if any leg moved past its worst line"
    return f"{b.get('label')} at {ptxt} or better"


def fliff_block(ticket_bets: list[dict], ticket_parlays: list[dict], best_prices: bool) -> dict:
    notes = [f"Fliff minimum wager assumed ${FLIFF_MIN_BET:.2f} (ASSUMPTION, unverified; configurable in jghedge.config).",
             "Game totals and cross-game parlays only; spreads and moneylines are informational (lost in two of three replayed seasons).",
             "Lines are DraftKings via ESPN unless an Odds API key supplies Fliff's own number; bet the number shown or better."]
    if best_prices:
        notes.append("Odds API prices attached: fliff_line/fliff_price on each total are Fliff's own numbers at run time.")
    ins = [{"bet_id": b["id"], "text": fliff_instruction(b)} for b in ticket_bets]
    ins += [{"bet_id": p["id"], "text": fliff_instruction({"kind": "parlay", "label": " + ".join(p.get("leg_labels", p["legs"])),
                                                           "american": p.get("american")})} for p in ticket_parlays]
    return {"platform": "Fliff", "min_bet": FLIFF_MIN_BET, "notes": notes, "instructions": ins}


# ------------------------------------------------------------------ the run
def run(day: date | None = None, n_sims: int | None = None, update: bool = True,
        with_props: bool = True, verbose: bool = True, seed: int = 7, engine=None,
        slate: list[dict] | None = None, injuries: dict | None = None, replay: bool = False,
        season: int | None = None, overrides: dict | None = None, hours_override: float | None = None,
        label: str | None = None) -> dict:
    """Price one night. With `replay=True` (used by jghedge.replay) the caller supplies the
    engine, the slate with the morning lines and an empty injury report, and every input is
    restricted to information available before that night (pre-game Elo, prior-season BPM).
    Replay runs never write snapshots, logs or caches."""
    params = load_params()
    params.update(overrides or {})
    n = n_sims or int(params["n_sims"])
    label = label or run_label()
    day = day or slate_day(label)
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

    log(f"Slate for {day} ({label}) ...")
    if slate is None:
        slate = fetch_slate(f, day)
    inj = injuries if injuries is not None else espn.injuries(f, str(day))
    team_tbl = None if replay else bbref_table(f, "team", season)
    kill = context_kill_switch(eng.gdf, season, asof, params.get("context_params"))

    # situational features need the schedule including tonight, and any game before this slate that is
    # not in the history yet: an evening run prices tomorrow while tonight's games are still being played,
    # and without them a team on a back-to-back looks rested
    sched = eng.gdf[["id", "date", "tip", "home", "away", "neutral", "venue_city"]].copy()
    sched = sched[~sched["id"].isin({g["id"] for g in slate})]
    pending = [] if replay else pending_games(f, day, set(sched["id"]) | {g["id"] for g in slate})
    tonight = _schedule_rows(slate)
    sit = (schedule_features(pd.concat([sched, _schedule_rows(pending), tonight], ignore_index=True))
           if len(tonight) else None)

    pm = PropModel(eng.pg, eng.tg, season, asof, params) if with_props else None
    rng = np.random.default_rng(seed)
    now = datetime.now(timezone.utc)

    # optional outside feeds (never in replay)
    pp_rows, pp_err, pp_fetched = [], None, None
    odds_events, odds_map, odds_budget = {}, {}, None
    if not replay and with_props:
        pp_rows, pp_err = prizepicks.fetch_projections(f)
        pp_fetched = now.isoformat(timespec="seconds") if pp_rows else None
        log(f"PrizePicks board: {len(pp_rows)} rows" + (f" ({pp_err})" if pp_err else ""))
    if not replay and oddsapi.available():
        try:
            odds_budget = oddsapi.Budget()
            odds_events = oddsapi.fetch_slate_odds(f, budget=odds_budget)
            odds_map = oddsapi.map_to_espn(odds_events, slate)
        except Exception as e:  # noqa: BLE001  (optional feed; never fail the night)
            log(f"Odds API unavailable: {e}")
            odds_events, odds_map = {}, {}
    espn_to_event = {v: k for k, v in odds_map.items()}

    games_out, bets, payoffs, sims_by_game = [], [], {}, {}
    pk_inputs: dict[str, tuple] = {}
    gtd_games: list[str] = []
    props_log_rows: list[dict] = []
    for g in slate:
        h, a = g["home"], g["away"]
        tip = datetime.fromisoformat(g["date"].replace("Z", "+00:00"))
        hours = hours_override if hours_override is not None else (
            10.0 if replay else (tip - now).total_seconds() / 3600)
        team_pp, team_inj, rots, adj = {}, {}, {}, {}
        gtd = False
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
            gtd = gtd or any(0.05 < d["play_prob"] < 0.95 and (d.get("typ_min") or 0) >= GTD_STARTER_MIN
                             for d in detail)
            for r in rows:   # listed players with no recent minutes (still show them)
                if r.get("id") and not any(x["id"] == r["id"] for x in team_inj[team]):
                    team_inj[team].append({"id": r["id"], "name": r["name"], "status": r["status"],
                                           "play_prob": r["play_prob"], "net_impact": 0.0,
                                           "note": r.get("detail", ""), "typ_min": 0.0, "bpm": None})
        if gtd:
            gtd_games.append(g["id"])
        sf = sit.loc[g["id"]].to_dict() if sit is not None else None
        proj = eng.project(st, h, a, g["neutral"], sf, adj,
                           elo_pair=eng.elo_pre.get(g["id"]) if replay else None)
        p_live = dict(params)
        p_live["w_model_vs_market_margin"] = _w_time(hours, params.get("w_margin_early", 0.25),
                                                     params["w_model_vs_market_margin"])
        p_live["w_model_vs_market_total"] = _w_time(hours, params.get("w_total_early", 0.30),
                                                    params["w_model_vs_market_total"])
        eng.params = p_live
        gctx = game_context(eng, h, a, day, season, g.get("season_type"), kill["kill"])
        bl = eng.blend_with_market(proj, g["odds"], gctx)
        eng.params = params
        sim = GameSim(bl["m_final"], bl["t_final"], params, n, rng)
        sims_by_game[g["id"]] = sim
        o = (g["odds"] or {}).get("close") or {}
        op = (g["odds"] or {}).get("open") or {}
        gbets = []

        def add(bid, kind, label_, side, line, price, pay, p_mkt, extra=None):
            if price is None:
                return
            dec = american_to_decimal(price)
            pf = win_prob(pay)
            b = {"id": bid, "game_id": g["id"], "kind": kind, "label": label_, "side": side, "line": line,
                 "price": price, "dec": dec, "p_final": pf, "p_market": p_mkt, "ev": float(pay.mean()),
                 "edge": (pf - p_mkt) if p_mkt is not None else None,
                 "matchup": f"{a} @ {h}", "tip": g["date"], **(extra or {})}
            bets.append(b)
            payoffs[bid] = pay
            gbets.append(bid)

        gid = g["id"]
        if o.get("ml_home") is not None and o.get("ml_away") is not None:
            ph, pa = novig_power(o["ml_home"], o["ml_away"])
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
            books = (odds_events.get(espn_to_event.get(gid), {}) or {}).get("books") or {}
            extra_o = extra_u = None
            if books:
                fl = oddsapi.fliff_lines(books) or {}
                bo = oddsapi.best_price(books, "total", "over")
                bu = oddsapi.best_price(books, "total", "under")
                extra_o = {"best_book": bo[0], "best_price": bo[1], "best_book_line": bo[2],
                           "fliff_line": fl.get("total"), "fliff_price": fl.get("over_price")}
                extra_u = {"best_book": bu[0], "best_price": bu[1], "best_book_line": bu[2],
                           "fliff_line": fl.get("total"), "fliff_price": fl.get("under_price")}
            add(f"{gid}-to-o", "total", f"{a}/{h} Over {t:g}", "over", t, o["over_price"],
                sim.total_bet("over", t, american_to_decimal(o["over_price"])), po, extra_o)
            add(f"{gid}-to-u", "total", f"{a}/{h} Under {t:g}", "under", t, o["under_price"],
                sim.total_bet("under", t, american_to_decimal(o["under_price"] or -110)), pu, extra_u)

        # ------------------------------------------------------------ props
        n_props = 0
        if pm is not None:
            team_players = {}
            for team, opp, pts_draw, pts_exp, msign in ((h, a, sim.hs, bl["pts_h"], 1), (a, h, sim.as_, bl["pts_a"], -1)):
                tp = st["teams"][team]
                pace_factor = proj["pace"] / max(st["mu_pace"] + tp["P"], 80)
                players = pm.project_team(rots[team], team, opp, team_pp[team], pace_factor,
                                          st["teams"][opp]["D"], st["mu"])
                scale = (bl["t_final"] + msign * bl["m_final"]) / 2
                psims = pm.simulate(players, pts_draw, scale, msign * sim.margin, rng)
                for p in players:
                    team_players[p["id"]] = (p, psims[p["id"]], team)
            try:
                plines = espn.props(f, gid)
            except Exception:  # noqa: BLE001
                plines = []
            pk_inputs[gid] = (team_players, plines, {"id": gid, "matchup": f"{a} @ {h}", "tip": g["date"]})
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
                props_log_rows.append({"game_id": gid, "player_id": p["id"], "stat": stat, "line": pr["line"],
                                       "over": pr["over"], "under": pr["under"]})
                x = ps[stat].astype(float)
                plays = ps["_plays"]
                line = pr["line"]
                p_model = float(np.mean(x[plays] > line)) if plays.any() else 0.0
                p_over_mkt, _ = novig_multiplicative(pr["over"], pr["under"])
                sides = (("over", pr["over"], p_over_mkt), ("under", pr["under"], 1 - p_over_mkt))
                p_mkt_over = sides[0][2]
                p_fin_over = float(inv_logit(a_s * logit(p_model) + c_s * logit(p_mkt_over) + b_s))
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
                    "proj_pts": _r(bl["pts_h"] if s == "h" else bl["pts_a"], 1),
                    "games_played": gctx["home_gp" if s == "h" else "away_gp"],
                    "winpct": _r(gctx["home_winpct" if s == "h" else "away_winpct"], 3)}

        hw = win_prob(payoffs[f"{gid}-ml-h"]) if f"{gid}-ml-h" in payoffs else float(np.mean(sim.margin > 0))
        games_out.append({
            "id": gid, "tip": g["date"], "hours_to_tip": _r(hours, 1), "neutral": g["neutral"],
            "venue_city": g["venue_city"], "notes": g.get("notes"), "lineup_uncertain": gid in gtd_games,
            "home": side_info(h, "h"), "away": side_info(a, "a"),
            "market": {"provider": (g["odds"] or {}).get("provider"), **{k: _r(v, 1) for k, v in o.items()},
                       "open_spread_home": _r(op.get("spread_home"), 1), "open_total": _r(op.get("total"), 1)},
            "model": {"margin_ratings": _r(bl["m_ratings"], 2), "margin_elo": _r(bl["m_elo"], 2),
                      "margin_model": _r(bl["m_model"], 2), "margin_market": _r(bl["m_market"], 2),
                      "margin_final": _r(bl["m_final"], 2), "total_model": _r(bl["t_model"], 1),
                      "t_model": _r(bl["t_model"], 3), "t_final": _r(bl["t_final"], 3),      # full precision for live re-pricing
                      "total_market": _r(bl["t_market"], 1), "total_final": _r(bl["t_final"], 1),
                      "total_context": _r(bl.get("t_context"), 2), "fair_total": _r(policy.fair_line(sim.total), 1),
                      "pace": _r(bl["pace"], 1), "sit_margin": _r(bl["sit_margin"], 2),
                      "avail_margin": _r(bl["avail_margin"], 2), "p_home_win": _r(hw, 4),
                      "w_model_margin": _r(p_live["w_model_vs_market_margin"], 3),
                      "w_model_total": _r(p_live["w_model_vs_market_total"], 3),
                      "home_score_p10_p90": [int(np.percentile(sim.hs, 10)), int(np.percentile(sim.hs, 90))],
                      "away_score_p10_p90": [int(np.percentile(sim.as_, 10)), int(np.percentile(sim.as_, 90))]},
            "n_props": n_props,
        })
        log(f"  {a} @ {h}: final margin {bl['m_final']:+.1f} (model {bl['m_model']:+.1f}, market "
            f"{(bl['m_market'] if bl['m_market'] is not None else float('nan')):+.1f}), total {bl['t_final']:.1f}"
            f"{(' (ctx %+.1f)' % bl['t_context']) if bl.get('t_context') else ''}, {n_props} props priced")

    # ------------------------------------------------------- candidates (totals only)
    min_ev, max_edge = params["min_ev"], params["max_edge_prob"]

    def eligible(b):
        if b["kind"] != "total":
            return False   # spreads/moneylines informational (backtest); props informational (s = 0, memo)
        if b["ev"] < min_ev or b["p_final"] < 0.25:
            return False
        if b["edge"] is not None and abs(b["edge"]) > max_edge:
            return False   # gap this large usually means stale line or late news
        return True

    kept = sorted([b for b in bets if eligible(b)], key=lambda b: b["ev"], reverse=True)
    singles = kept[:30]
    parlays_all = build_parlays(singles, payoffs, max_legs=int(params.get("parlay_max_legs", 4)),
                                pool=int(params.get("parlay_pool", 14)),
                                min_leg_prob=float(params.get("parlay_min_leg_prob", 0.45)))
    parlays = select_parlays(parlays_all, int(params.get("max_parlays", 6)))
    label_of = {b["id"]: b["label"] for b in bets}
    for i, p in enumerate(parlays):
        p["id"] = f"parlay-{i + 1}"
        p["american"] = decimal_to_american(p["dec"])
        p["leg_labels"] = [label_of.get(l, l) for l in p["legs"]]
        payoffs[p["id"]] = p["payoff"]

    # ------------------------------------------------------- shrunk joint distribution + policy
    ids = [b["id"] for b in singles] + [p["id"] for p in parlays]
    kinds = ["total"] * len(singles) + ["parlay"] * len(parlays)
    decs = [b["dec"] for b in singles] + [p["dec"] for p in parlays]
    R = np.column_stack([payoffs[i] for i in ids]) if ids else np.zeros((n, 0))
    R_s = policy.shrink_matrix(R, kinds, decs, seed=seed, p_win=None) \
        if ids else R
    u_common = np.random.default_rng(seed).random(n)
    col_of = {i: j for j, i in enumerate(ids)}

    for b in bets:
        if b["kind"] == "total":
            sim = sims_by_game[b["game_id"]]
            col = R_s[:, col_of[b["id"]]] if b["id"] in col_of else policy.shrink_payoff(
                payoffs[b["id"]], kind="total", dec=b["dec"], u=u_common)
            b.update(policy.bet_metrics(col, b["dec"], unit=REF_UNIT))
            b["fair_line"] = policy.fair_line(sim.total)
            b["max_line"] = policy.max_line(sim.total, b["side"], b["line"], b["dec"])
            b["info_only"] = False
        else:
            b["info_only"] = True
            if b["kind"] == "prop":
                b["shrunk_ev"] = 0.0   # s = 0: no measured out-of-sample edge (memo s.1 point 3)
    for p, j in zip(parlays, range(len(singles), len(ids))):
        p.update(policy.bet_metrics(R_s[:, j], p["dec"], unit=REF_UNIT))

    meta = []
    for b in singles:
        meta.append({"id": b["id"], "kind": "total", "label": b["label"], "game_id": b["game_id"], "dec": b["dec"],
                     "side": b["side"], "line": b["line"], "max_line": b.get("max_line"), "fair_line": b.get("fair_line")})
    game_of = {b["id"]: b.get("game_id") for b in bets}
    for p in parlays:
        meta.append({"id": p["id"], "kind": "parlay", "label": " + ".join(p["leg_labels"]),
                     "game_id": None, "game_ids": [game_of.get(l) for l in p.get("legs", [])], "dec": p["dec"]})
    regime = props_regime(day) if not replay else {"regime": "neutral", "n": 0}
    hours_first = min((gm["hours_to_tip"] for gm in games_out if gm.get("hours_to_tip") is not None), default=None)
    flags = {"props_regime": regime["regime"], "props_regime_n": int(regime.get("n") or 0), "props_regime_min": int(PROPS_REGIME["min_lines"]),
             "lineup_uncertainty": gtd_games, "hours_to_first_tip": hours_first,
             "game_labels": {gm["id"]: f'{gm["away"].get("abbr", "")}@{gm["home"].get("abbr", "")}' for gm in games_out}}
    # ineligible totals are offered to would_change as 'misses' (what line would clear the floor)
    night = policy.night_block(R_s, meta, REF_BANKROLL, REF_UNIT, flags)
    misses = [b for b in bets if b["kind"] == "total" and b["id"] not in col_of and (b.get("shrunk_ev") or 0) > 0]
    if misses and night["verdict"] in ("PASS", "LIGHT"):
        extra = policy.would_change(night["verdict"], [{**b, "frac_ref": 0.0} for b in misses], night["score"], {})
        night["would_change"] = night["would_change"] + [t for t in extra if "would clear it" in t][:2]
    if night["verdict"] in ("PASS", "LIGHT"):
        # name the closest totals and the line each would need (best available bet on a quiet night)
        near = []
        for b in bets:
            if b["kind"] != "total" or b.get("max_line") is None or b.get("line") is None or b["id"] in col_of:
                continue
            gap = (b["line"] - b["max_line"]) if b["side"] == "over" else (b["max_line"] - b["line"])
            if gap > 0:
                near.append((gap, b))
        near.sort(key=lambda x: x[0])
        closest = []
        for gap, b in near[:2]:
            need = f"{b['max_line']:g} or lower" if b["side"] == "over" else f"{b['max_line']:g} or higher"
            closest.append(f"{b['label']} (now {b['line']:g}) becomes a bet at {need}: "
                           f"{gap:g} points away. Check Fliff's number before passing.")
        night["would_change"] = closest + night["would_change"]   # the most actionable line first
        if near:
            night.setdefault("best_available", [])
            night["best_available"] = list(dict.fromkeys(list(night["best_available"]) + [near[0][1]["id"]]))[:3]

    ref_units = policy.reference_units(R_s) if ids else np.zeros(0, dtype=int)
    opt_units = policy.allocate(R_s, REF_BANKROLL, REF_UNIT, policy.OPT_IN_K) if ids else np.zeros(0, dtype=int)

    def alloc_block(units, k):
        frac = units * REF_UNIT / REF_BANKROLL
        items = [{"ref": i, "units": int(u), "stake": _r(u * REF_UNIT, 2), "frac": _r(fr, 5)}
                 for i, u, fr in zip(ids, units, frac) if u > 0]
        return {"kelly": k, "max_bet": policy.PER_BET_CAP, "max_total": policy.NIGHT_CAP,
                "bankroll": REF_BANKROLL, "unit": REF_UNIT, "shrunk": True, "items": items,
                "risk": {k_: _r(v, 4) for k_, v in kelly.risk_summary(R_s, frac).items()}}

    ref_alloc = alloc_block(ref_units, policy.REF_K)
    opt_alloc = alloc_block(opt_units, policy.OPT_IN_K)
    # 'balanced'/'conservative' alias the reference ticket and 'aggressive'/'max_growth' the opt-in
    # ceiling so older dashboard builds keep rendering; the dashboard sizes the user's own ticket.
    allocations = {"reference": ref_alloc, "opt_in": opt_alloc, "balanced": ref_alloc, "conservative": ref_alloc,
                   "aggressive": opt_alloc, "max_growth": opt_alloc}
    mu, cov = discrete.moments(R_s) if ids else (np.zeros(0), np.zeros((0, 0)))
    portfolio = {"ids": ids, "kinds": kinds, "decs": [_r(d, 4) for d in decs], "shrunk": True,
                 "mu": [round(float(x), 5) for x in mu],
                 "cov": [[round(float(x), 6) for x in row] for row in cov],
                 "units_ref": [int(u) for u in ref_units], "growth_1u_per_100": [
                     _r(x, 6) for x in policy.growth_gate_matrix(R_s, REF_BANKROLL, REF_UNIT)] if ids else []}
    full = kelly.optimal_kelly(R_s) if ids else np.zeros(0)
    for b, fk in zip(singles, full[:len(singles)]):
        b["kelly_full"] = float(fk)
    for p, fk in zip(parlays, full[len(singles):]):
        p["kelly_full"] = float(fk)

    # ----------------------------------------------------------- picks (ranked by growth, not EV)
    picks = {}
    ticket_bets = [b for b, u in zip(singles, ref_units[:len(singles)]) if u > 0]
    ticket_parlays = [p for p, u in zip(parlays, ref_units[len(singles):]) if u > 0]
    if singles:
        picks["lock"] = max(singles, key=lambda b: (b["p_shrunk"], b["shrunk_ev"]))["id"]
        picks["value"] = max(singles, key=lambda b: (b.get("growth_1u_per_100") or -1))["id"]
        picks["best_game_bet"] = picks["value"]
    if parlays:
        picks["parlay"] = max(parlays, key=lambda p: (p.get("growth_1u_per_100") or -1))["id"]
        picks["safe_parlay"] = max(parlays, key=lambda p: p["p_hit"])["id"]
    best_prop = max([b for b in bets if b["kind"] == "prop"], key=lambda b: b["ev"], default=None)
    if best_prop:
        picks["best_prop"] = best_prop["id"]   # informational

    sgp = []
    for gm in games_out:
        gl = [b for b in kept if b["game_id"] == gm["id"]][:3]
        if len(gl) >= 2:
            fair = same_game_fair(gl, payoffs)
            sgp.append({"game_id": gm["id"], "legs": [b["id"] for b in gl], "p_hit": _r(fair["p_hit"], 4),
                        "fair_american": _r(decimal_to_american(fair["fair_dec"]), 0) if fair["fair_dec"] else None})

    # ----------------------------------------------------------- pick'em (PrizePicks)
    pickem_block = pickem.empty_block(notes=["Props disabled for this run."])
    lean = pickem_lean(regime)
    pk_source, pk_notes = "dk_proxy", []
    if pp_err:
        pk_notes.append(f"PrizePicks board unavailable: {pp_err}; DraftKings lines used as the proxy.")
    if pm is not None and pk_inputs:
        pp_source_rows = pp_rows
        if pp_rows:
            pk_source = "prizepicks"
        elif odds_events and odds_budget is not None:
            # Odds API fallback: PrizePicks lines (region us_dfs) for at most 3 games with the most DK candidates
            ranked = sorted(pk_inputs.items(), key=lambda kv: -sum(1 for r in kv[1][1] if r.get("kind") == "ou"))[:3]
            rows = []
            for gid, (tp, _pl, _gm) in ranked:
                eid = espn_to_event.get(gid)
                if not eid:
                    continue
                try:
                    props_ev = oddsapi.fetch_event_props(f, eid, budget=odds_budget)
                except Exception:  # noqa: BLE001
                    props_ev = {}
                for (pname, stat), books in props_ev.items():
                    for bk in ("prizepicks",):        # Underdog / Pick6 lines are not PrizePicks lines
                        if bk in books and books[bk].get("line") is not None:
                            rows.append({"name": pname, "team": None, "stat": stat, "line": books[bk]["line"],
                                         "odds_type": "standard", "status": "pre_game", "supported": True,
                                         "projection_id": None})
                            break
            if rows:
                pp_source_rows, pk_source = rows, "oddsapi_prizepicks"
                pk_notes.append("PrizePicks lines via the Odds API (us_dfs) for the top 3 games; the rest use the DK proxy.")
        legs_all: list[dict] = []
        sims_all: dict[str, dict] = {}
        for gid, (team_players, plines, gmeta) in pk_inputs.items():
            id_to_name = {pid: t[0]["name"] for pid, t in team_players.items()}
            pp_game = []
            if pp_source_rows:
                pp_game, _ = prizepicks.match_players(pp_source_rows, id_to_name,
                                                      team_of={pid: t[2] for pid, t in team_players.items()})
                pp_game = [r for r in pp_game if r.get("player_id")]
            legs = pickem.build_legs([t[0] for t in team_players.values()], {pid: t[1] for pid, t in team_players.items()},
                                     plines, pp_game, {"s": policy.shrink_factor("pickem"), "payouts": PICKEM_PAYOUTS,
                                                       "lean": lean["value"]},
                                     game=gmeta)
            if pk_source == "oddsapi_prizepicks":
                for l in legs:
                    if l.get("line_source") == "prizepicks":
                        l["line_source"] = "oddsapi_prizepicks"
            legs_all += legs
            for pid in {l["player_id"] for l in legs}:
                sims_all[pid] = team_players[pid][1]
        legs_all.sort(key=lambda l: (-l["p_side_shrunk"], l["id"]))
        legs_all = legs_all[:MAX_PICKEM_LEGS]
        keep_pids = {l["player_id"] for l in legs_all}
        sims_all = {pid: s for pid, s in sims_all.items() if pid in keep_pids}
        if n > MAX_PICKEM_DRAWS:
            sims_all = {pid: {k: v[:MAX_PICKEM_DRAWS] for k, v in s.items()} for pid, s in sims_all.items()}
        pk_notes.append(f"The reference ledger records at most {int(policy.NIGHT_CAP * REF_BANKROLL // PICKEM_MIN_ENTRY)} "
                        f"${PICKEM_MIN_ENTRY:g} entries per night (the 10% night cap on $100); slips are ranked.")
        pk_notes.append("Pick'em edges are PROVISIONAL: no historical PrizePicks line archive exists, so the leg "
                        "probabilities are market-anchored (DraftKings) and shrunk at s = 0.5 until the live ledger "
                        "grades enough slips.")
        pickem_block = pickem.build_pickem_block(legs_all, sims_all, source=pk_source, fetched_at=pp_fetched,
                                                 payouts=PICKEM_PAYOUTS, min_entry=PICKEM_MIN_ENTRY,
                                                 s=policy.shrink_factor("pickem"), notes=pk_notes,
                                                 max_sim_legs=MAX_PICKEM_LEGS, bankroll_ref=REF_BANKROLL)
    pickem_block["props_regime"] = regime["regime"]
    pickem_block["lean"] = lean
    # pick'em status joins the nightly verdict as a reason (it never raises the Fliff score)
    pk_legs = pickem_block.get("legs", [])
    if pickem_block.get("mode") == "check_lines" and pk_legs:
        pk_take = [l for l in pk_legs if l.get("more_if_at_most") is not None or l.get("less_if_at_least") is not None]
        night.setdefault("reasons", []).append(
            f"PrizePicks: the board could not be read from the server, so {len(pk_take)} player legs come with take-it "
            "thresholds instead of picks. Check them in the app; a slip is worth playing only where its lines meet the thresholds.")
        night["pickem_status"] = "check_lines"
    elif pk_legs:
        n_play = sum(1 for x in pickem_block.get("slips", []) if x.get("verdict") == "play")
        night.setdefault("reasons", []).append(
            f"PrizePicks: {n_play} slip(s) grow a $100 bankroll at the ${PICKEM_MIN_ENTRY:g} entry on live lines." if n_play
            else "PrizePicks: no slip grows a $100 bankroll at the minimum entry tonight.")
        night["pickem_status"] = "play" if n_play else "pass"
    else:
        night["pickem_status"] = "none"

    # ----------------------------------------------------------- lines snapshot + movement
    lines_block = {"snapshots_file": f"data/lines/{day}.json", "label": label, "moved": []}
    if not replay and slate:
        models = {gm["id"]: {"margin": gm["model"]["margin_final"], "total": gm["model"]["total_final"]} for gm in games_out}
        lines_mod.record(day, label, slate, models, at=now.isoformat(timespec="seconds"))
        from . import ledger as ledger_mod
        lines_block["moved"] = moved_past(day, bets, {b["id"] for b in kept}, ledger_mod.load())
        log_props(day, props_log_rows)

    # ----------------------------------------------------------- output
    eligible_ids = {b["id"] for b in kept}
    shown = [b for b in bets if b["kind"] != "prop"] + sorted(
        [b for b in bets if b["kind"] == "prop"], key=lambda b: b["ev"], reverse=True)[:MAX_PROPS_SHOWN]
    out_bets = []
    for b in shown:
        out_bets.append({k: (_r(v, 4) if isinstance(v, float) else v) for k, v in b.items()} |
                        {"american": _r(b["price"], 0), "eligible": b["id"] in eligible_ids})

    teams_tbl = sorted(({"team": t, "name": TEAMS[t].name, "adj_o": _r(st["mu"] + v["O"], 1),
                         "adj_d": _r(st["mu"] + v["D"], 1), "adj_net": _r(v["net"], 1),
                         "adj_pace": _r(st["mu_pace"] + v["P"], 1), "games": v["games"],
                         "elo": _r(eng.elo_now.get(t), 0)} for t, v in st["teams"].items()),
                       key=lambda r: -r["adj_net"])
    growth_block = growth_block_cached() if not replay else {"basis": "not computed in replay", "nights": 0, "grid": []}
    result = {
        "brand": BRAND, "version": __version__,
        "generated": now.isoformat(timespec="seconds"), "date": str(day), "season": season, "run_label": label,
        "n_sims": n, "games": games_out, "bets": out_bets,
        "parlays": [{k: (_r(v, 4) if isinstance(v, float) else v) for k, v in p.items() if k != "payoff"}
                    for p in parlays],
        "same_game_fair": sgp, "allocations": allocations, "picks": picks, "teams": teams_tbl,
        "portfolio": portfolio, "_R": R, "_R_shrunk": R_s, "_meta": meta,
        "policy": policy.POLICY, "night": night,
        "pickem": pickem_block,
        "fliff": fliff_block(ticket_bets, ticket_parlays, bool(odds_events)),
        "growth": growth_block, "lines": lines_block,
        "props_regime": regime, "context": {"kill_switch": kill, "params": ctx.CONTEXT_PARAMS},
        # everything the page needs to re-price a total at a live line the way this run did (template liveReprice)
        "live_pricing": {"total_sd": float(params["total_sd"]), "w_early": float(params.get("w_total_early", 0.30)),
                         "w_late": float(params["w_model_vs_market_total"]),
                         "resid": [round(float(x), 3) for x in centred_table(
                             params.get("total_resid_q") or _gauss_quantiles(params["total_sd"]))],
                         "min_ev": float(params["min_ev"]), "max_edge_prob": float(params["max_edge_prob"]),
                         "floor": float(policy.MIN_SHRUNK_EV), "s": float(policy.shrink_factor("total"))},
        "params": {k: params[k] for k in ("margin_sd", "total_sd", "w_ratings", "w_model_vs_market_margin",
                                          "w_model_vs_market_total", "w_total_early", "w_model_vs_market_props", "hca_points",
                                          "min_ev", "max_edge_prob")},
        "counts": {"games": len(games_out), "bets_priced": len(bets), "eligible": len(kept),
                   "placeable_ref": int((ref_units > 0).sum()) if ids else 0,
                   "props_priced": sum(1 for b in bets if b["kind"] == "prop"),
                   "pickem_legs": len(pickem_block.get("legs", [])),
                   "pickem_slips_play": sum(1 for s_ in pickem_block.get("slips", []) if s_.get("verdict") == "play")},
    }
    return result


def write_outputs(result: dict | None, name: str = "latest") -> None:
    """Write a priced night for the page: `latest` is the main slate, `next` the following day's (the page's
    Tomorrow option). `None` removes the file, so a stale 'next' never outlives its night."""
    SITE_DATA.mkdir(parents=True, exist_ok=True)
    (SITE_DATA / "slates").mkdir(exist_ok=True)
    if result is None:
        (SITE_DATA / f"{name}.json").unlink(missing_ok=True)
        return
    result = {k: v for k, v in result.items() if not k.startswith("_")}
    txt = json.dumps(result, separators=(",", ":"), default=_json_default)
    (SITE_DATA / f"{name}.json").write_text(txt)
    # dated snapshot without the full props board or the packed pick'em sims (keeps the repo small)
    keep = {i["ref"] for a in result["allocations"].values() for i in a["items"]}
    keep |= {l for p in result["parlays"] for l in p["legs"]} | set(result["picks"].values())
    slim = {**result, "bets": [b for b in result["bets"] if b["id"] in keep or b["kind"] != "prop"],
            "pickem": {**result.get("pickem", {}), "sims": {"n": 0, "legs": [], "bits": ""}}}
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
