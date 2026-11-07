"""ESPN public API: schedule, scores, box scores, odds (incl. closing), injuries, props, rosters."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..config import CACHE_DIR
from ..teams import ESPN_ID_TO_ABBR, canon
from .http import FetchError, Fetcher

SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"

GAME_CACHE = CACHE_DIR / "games"


def _f(x):
    """Parse American odds / lines like '+7.5', 'o227.5', '-110', 'EVEN'."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().lower().lstrip("ou")
    if s in ("even", "ev", "pk", "pick"):
        return 100.0 if s.startswith("ev") else 0.0
    try:
        return float(s)
    except ValueError:
        return None


def parse_odds(o: dict | None) -> dict | None:
    """Normalise an ESPN odds object (scoreboard `odds[0]` or summary `pickcenter[0]`)."""
    if not o:
        return None
    ml, ps, tt = o.get("moneyline") or {}, o.get("pointSpread") or {}, o.get("total") or {}

    def side(block, key, which, field):
        return _f(((block.get(key) or {}).get(which) or {}).get(field))

    out = {"provider": (o.get("provider") or {}).get("name")}
    for which in ("close", "open"):
        d = {
            "ml_home": side(ml, "home", which, "odds"),
            "ml_away": side(ml, "away", which, "odds"),
            "spread_home": side(ps, "home", which, "line"),
            "spread_home_price": side(ps, "home", which, "odds"),
            "spread_away_price": side(ps, "away", which, "odds"),
            "total": side(tt, "over", which, "line"),
            "over_price": side(tt, "over", which, "odds"),
            "under_price": side(tt, "under", which, "odds"),
        }
        out[which] = d
    c = out["close"]
    # Fallbacks to the flat fields ESPN also provides.
    if c["total"] is None:
        c["total"] = _f(o.get("overUnder"))
    if c["ml_home"] is None:
        c["ml_home"] = _f((o.get("homeTeamOdds") or {}).get("moneyLine"))
        c["ml_away"] = _f((o.get("awayTeamOdds") or {}).get("moneyLine"))
    if c["spread_home"] is None and o.get("spread") is not None:
        # In pickcenter `spread` is the home line; in scoreboard it's ambiguous, so use details.
        det = o.get("details") or ""
        m = re.match(r"([A-Z]+)\s+([-+]?\d+\.?\d*)", det)
        if m:
            fav, line = canon(m.group(1)), float(m.group(2))
            out["_fav_line"] = (fav, line)
    if c["over_price"] is None:
        c["over_price"] = _f(o.get("overOdds"))
        c["under_price"] = _f(o.get("underOdds"))
    if c["spread_home_price"] is None:
        c["spread_home_price"] = _f((o.get("homeTeamOdds") or {}).get("spreadOdds"))
        c["spread_away_price"] = _f((o.get("awayTeamOdds") or {}).get("spreadOdds"))
    if all(v is None for v in c.values()):
        return None
    return out


def _parse_event(e: dict) -> dict:
    c = e["competitions"][0]
    teams = {x["homeAway"]: x for x in c["competitors"]}
    st = c["status"]["type"]

    def ab(x):
        return ESPN_ID_TO_ABBR.get(str(x["team"]["id"]), canon(x["team"]["abbreviation"]))

    g = {
        "id": e["id"],
        "date": c.get("date") or e["date"],
        "season": e.get("season", {}).get("year"),
        "season_type": e.get("season", {}).get("type"),
        "home": ab(teams["home"]),
        "away": ab(teams["away"]),
        "neutral": bool(c.get("neutralSite")),
        "venue_city": ((c.get("venue") or {}).get("address") or {}).get("city"),
        "state": st.get("state"),
        "completed": bool(st.get("completed")),
        "home_score": _f(teams["home"].get("score")),
        "away_score": _f(teams["away"].get("score")),
        "odds": parse_odds((c.get("odds") or [None])[0]),
        "notes": [n.get("headline") for n in c.get("notes") or []],
    }
    return g


def scoreboard(f: Fetcher, day: date) -> list[dict]:
    ds = day.strftime("%Y%m%d")
    today = datetime.now(timezone.utc).date()
    ttl = None if day < today - timedelta(days=2) else 600
    data = f.get(f"{SITE}/scoreboard?dates={ds}&limit=100", ttl=ttl)
    return [_parse_event(e) for e in data.get("events", [])]


# ---------------------------------------------------------------- box scores
_PSTAT = ["min", "pts", "fg", "fg3", "ft", "reb", "ast", "tov", "stl", "blk", "oreb", "dreb", "pf", "pm"]
_KEYMAP = {"minutes": "min", "points": "pts", "fieldGoalsMade-fieldGoalsAttempted": "fg",
           "threePointFieldGoalsMade-threePointFieldGoalsAttempted": "fg3",
           "freeThrowsMade-freeThrowsAttempted": "ft", "rebounds": "reb", "assists": "ast",
           "turnovers": "tov", "steals": "stl", "blocks": "blk", "offensiveRebounds": "oreb",
           "defensiveRebounds": "dreb", "fouls": "pf", "plusMinus": "pm"}
PLAYER_COLS = ["id", "name", "team", "starter", "min", "pts", "fgm", "fga", "fg3m", "fg3a",
               "ftm", "fta", "oreb", "dreb", "reb", "ast", "stl", "blk", "tov", "pf", "pm"]


def _split(v):
    try:
        a, b = str(v).split("-")
        return float(a), float(b)
    except ValueError:
        return 0.0, 0.0


def _num(v):
    try:
        return float(str(v).replace("+", ""))
    except ValueError:
        return 0.0


def _team_box(t: dict) -> dict:
    s = {x["name"]: x.get("displayValue") for x in t.get("statistics", [])}
    fgm, fga = _split(s.get("fieldGoalsMade-fieldGoalsAttempted", "0-0"))
    f3m, f3a = _split(s.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted", "0-0"))
    ftm, fta = _split(s.get("freeThrowsMade-freeThrowsAttempted", "0-0"))
    return {"fgm": fgm, "fga": fga, "fg3m": f3m, "fg3a": f3a, "ftm": ftm, "fta": fta,
            "oreb": _num(s.get("offensiveRebounds", 0)), "dreb": _num(s.get("defensiveRebounds", 0)),
            "ast": _num(s.get("assists", 0)), "stl": _num(s.get("steals", 0)),
            "blk": _num(s.get("blocks", 0)),
            "tov": _num(s.get("totalTurnovers", s.get("turnovers", 0))),
            "pf": _num(s.get("fouls", 0))}


def game_record(f: Fetcher, event_id: str, refresh: bool = False) -> dict | None:
    """Compact record of a completed game: team + player box score and closing odds.

    Cached permanently once the game is final (raw summaries are ~1MB; records ~8KB).
    """
    path = GAME_CACHE / f"{event_id}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    try:
        s = f.get(f"{SITE}/summary?event={event_id}", ttl=0, store=False)
    except FetchError:
        return None
    hdr = s.get("header", {}).get("competitions", [{}])[0]
    st = hdr.get("status", {}).get("type", {})
    comps = {x["homeAway"]: x for x in hdr.get("competitors", [])}
    if "home" not in comps:
        return None
    ids = {k: ESPN_ID_TO_ABBR.get(str(v["team"]["id"]), canon(v["team"]["abbreviation"]))
           for k, v in comps.items()}
    rec = {"id": event_id, "date": hdr.get("date"), "completed": bool(st.get("completed")),
           "home": ids["home"], "away": ids["away"], "neutral": bool(hdr.get("neutralSite")),
           "venue_city": ((s.get("gameInfo") or {}).get("venue") or {}).get("address", {}).get("city"),
           "season": s.get("header", {}).get("season", {}).get("year"),
           "season_type": s.get("header", {}).get("season", {}).get("type"),
           "home_score": _num(comps["home"].get("score", 0)),
           "away_score": _num(comps["away"].get("score", 0)),
           "box": {}, "players": [],
           "odds": parse_odds((s.get("pickcenter") or [None])[0])}
    bs = s.get("boxscore", {})
    for t in bs.get("teams", []):
        ab = ESPN_ID_TO_ABBR.get(str(t["team"]["id"]), canon(t["team"]["abbreviation"]))
        rec["box"][ab] = _team_box(t)
    for p in bs.get("players", []):
        ab = ESPN_ID_TO_ABBR.get(str(p["team"]["id"]), canon(p["team"]["abbreviation"]))
        for grp in p.get("statistics", []):
            keys = [_KEYMAP.get(k, k) for k in grp.get("keys", [])]
            for a in grp.get("athletes", []):
                if a.get("didNotPlay") or not a.get("stats"):
                    continue
                v = dict(zip(keys, a["stats"]))
                mins = _num(v.get("min", 0))
                if mins <= 0:
                    continue
                fgm, fga = _split(v.get("fg", "0-0"))
                f3m, f3a = _split(v.get("fg3", "0-0"))
                ftm, fta = _split(v.get("ft", "0-0"))
                rec["players"].append([
                    a["athlete"]["id"], a["athlete"].get("displayName"), ab, int(bool(a.get("starter"))),
                    mins, _num(v.get("pts")), fgm, fga, f3m, f3a, ftm, fta, _num(v.get("oreb")),
                    _num(v.get("dreb")), _num(v.get("reb")), _num(v.get("ast")), _num(v.get("stl")),
                    _num(v.get("blk")), _num(v.get("tov")), _num(v.get("pf")), _num(v.get("pm"))])
    if rec["completed"] and len(rec["box"]) == 2:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rec, separators=(",", ":")))
    return rec


# ------------------------------------------------------------------ injuries
STATUS_PLAY_PROB = {"out": 0.0, "suspension": 0.0, "doubtful": 0.15, "questionable": 0.5,
                    "day-to-day": 0.5, "game time decision": 0.5, "probable": 0.9}
FANTASY_PLAY_PROB = {"OUT": 0.0, "O": 0.0, "OFS": 0.0, "IR": 0.0, "SUSP": 0.0, "D": 0.15, "DTD": 0.5,
                     "GTD": 0.5, "Q": 0.5, "P": 0.9}


def play_probability(item: dict, game_date: str | None = None) -> tuple[float, str, str | None]:
    """P(plays tonight) from an ESPN injury item.

    ESPN's top-level status is only 'Out' or 'Day-To-Day'; the useful detail is in
    details.fantasyStatus (OUT / GTD / OFS ...), details.returnDate and the comment wording.
    Returns (play_prob, fantasy_status, return_date). Rules (research memo B5): a player
    listed Out but fantasy GTD, or whose return date is today's game, is a 50/50; comment
    words 'probable'/'expected to play' -> 0.9, 'doubtful' -> 0.15, 'questionable' -> 0.5,
    'out for <this> game' -> 0.0.
    """
    status = (item.get("status") or "").strip().lower()
    det = item.get("details") or {}
    fs = ((det.get("fantasyStatus") or {}).get("abbreviation") or "").upper() or None
    ret = det.get("returnDate")
    comment = ((item.get("shortComment") or "") + " " + (item.get("longComment") or "")).lower()
    p = STATUS_PLAY_PROB.get(status, 0.5)
    if fs in FANTASY_PLAY_PROB:
        p = FANTASY_PLAY_PROB[fs]
    if ret and game_date and str(ret)[:10] == str(game_date)[:10] and fs not in ("OFS", "IR", "SUSP"):
        p = max(p, 0.5)
    if "expected to play" in comment or "will play" in comment or "is probable" in comment or "(probable)" in comment:
        p = max(p, 0.9)
    elif "doubtful" in comment:
        p = min(p, 0.15) if fs not in ("GTD",) else 0.15
    elif "questionable" in comment or "game-time decision" in comment or "game time decision" in comment:
        p = 0.5 if p > 0 else p
    if re.search(r"\b(is|are|remains?) out\b", comment) and "game-time" not in comment and fs != "GTD":
        p = 0.0
    return float(p), fs, ret


def injuries(f: Fetcher, game_date: str | None = None) -> dict[str, list[dict]]:
    data = f.get(f"{SITE}/injuries", ttl=900)
    out: dict[str, list[dict]] = defaultdict(list)
    for t in data.get("injuries", []):
        for i in t.get("injuries", []):
            a = i.get("athlete", {})
            team = a.get("team", {}) or {}
            ab = ESPN_ID_TO_ABBR.get(str(team.get("id")), None) if team else None
            if ab is None:
                ab = ESPN_ID_TO_ABBR.get(str(t.get("id")), canon(t.get("abbreviation", "")) if t.get("abbreviation") else None)
            if ab is None:
                continue
            status = (i.get("status") or "").strip()
            pid = None
            for ln in a.get("links", []):
                m = re.search(r"/id/(\d+)", ln.get("href", ""))
                if m:
                    pid = m.group(1)
                    break
            pp, fs, ret = play_probability(i, game_date)
            out[ab].append({"id": pid, "name": a.get("displayName"), "status": status, "play_prob": pp,
                            "fantasy_status": fs, "return_date": ret,
                            "detail": i.get("shortComment") or "", "date": i.get("date")})
    return dict(out)


def team_injuries_block_ids(f: Fetcher) -> dict:
    data = f.get(f"{SITE}/injuries", ttl=900)
    return {t.get("displayName"): t.get("id") for t in data.get("injuries", [])}


# ---------------------------------------------------------------- rosters
def roster(f: Fetcher, team_espn_id: str) -> list[dict]:
    data = f.get(f"{SITE}/teams/{team_espn_id}/roster", ttl=6 * 3600)
    out = []
    for a in data.get("athletes", []):
        out.append({"id": str(a.get("id")), "name": a.get("displayName"),
                    "pos": (a.get("position") or {}).get("abbreviation"),
                    "injuries": [x.get("status") for x in a.get("injuries", [])]})
    return out


# ------------------------------------------------------------------- props
PROP_TYPES = {
    "Total Points": "pts", "Total Rebounds": "reb", "Total Assists": "ast",
    "Total 3-Point Field Goals": "fg3m", "Total Points, Rebounds, and Assists": "pra",
    "Total Points and Rebounds": "pr", "Total Points and Assists": "pa",
    "Total Assists and Rebounds": "ra", "Total Steals": "stl", "Total Blocks": "blk",
}
MILESTONE_TYPES = {
    "Points Milestones": "pts", "Rebounds Milestones": "reb", "Assists Milestones": "ast",
    "3-Point Field Goals Milestones": "fg3m", "Points + Assists + Rebounds Milestones": "pra",
    "Points + Rebounds Milestones": "pr", "Points + Assists Milestones": "pa",
    "Rebounds + Assists Milestones": "ra", "Steals Milestones": "stl", "Blocks Milestones": "blk",
}


def _imp(am: float) -> float:
    return 100 / (am + 100) if am > 0 else -am / (-am + 100)


def props(f: Fetcher, event_id: str, provider: str = "100", ttl: float | None = 900) -> list[dict]:
    """Player props for one game: over/under lines and one-sided milestone ('N+') ladders.

    ESPN lists O/U as an unlabeled pair; the over is identified as the price closer to the
    milestone ladder's P(stat >= line+0.5), falling back to list order (over first).
    """
    items: list[dict] = []
    for page in (1, 2, 3, 4):
        try:
            d = f.get(f"{CORE}/events/{event_id}/competitions/{event_id}/odds/{provider}/propBets"
                      f"?limit=1000&page={page}", ttl=ttl)
        except FetchError:
            break
        items.extend(d.get("items", []))
        if page >= int(d.get("pageCount", 1) or 1):
            break

    def aid(i):
        return i.get("athlete", {}).get("$ref", "").split("/athletes/")[-1].split("?")[0] or None

    pairs: dict[tuple, list[float]] = defaultdict(list)
    ladders: dict[tuple, dict[float, float]] = defaultdict(dict)
    updated: dict[tuple, str] = {}
    for i in items:
        t = i.get("type", {}).get("name")
        a = aid(i)
        if not a:
            continue
        am = _f(i.get("odds", {}).get("american", {}).get("value"))
        if am is None:
            continue
        tgt = (i.get("current") or {}).get("target", {}).get("value")
        tot = _f(str(i.get("odds", {}).get("total", {}).get("value", "")).replace("+", ""))
        if t in PROP_TYPES and tot is not None:
            pairs[(a, PROP_TYPES[t], tot)].append(am)
            k = (a, PROP_TYPES[t], tot)
            updated[k] = max(updated.get(k, ""), i.get("lastUpdated") or "")
        elif t in MILESTONE_TYPES:
            n = _f(tgt) if tgt is not None else tot
            if n is not None:
                ladders[(a, MILESTONE_TYPES[t])][n] = am
    out = []
    for (a, stat, line), prices in pairs.items():
        if len(prices) < 2:
            continue
        p1, p2 = prices[0], prices[1]
        lad = ladders.get((a, stat), {})
        need = line + 0.5
        if need in lad:
            ref = _imp(lad[need])
            if abs(_imp(p2) - ref) < abs(_imp(p1) - ref):
                p1, p2 = p2, p1
        out.append({"athlete_id": a, "stat": stat, "kind": "ou", "line": line,
                    "over": p1, "under": p2, "updated": updated.get((a, stat, line))})
    for (a, stat), lad in ladders.items():
        for n, am in lad.items():
            out.append({"athlete_id": a, "stat": stat, "kind": "milestone", "line": n - 0.5,
                        "over": am, "under": None})
    return out


def core_odds(f: Fetcher, event_id: str) -> dict | None:
    """Odds from the core API (has closing lines for past seasons, e.g. ESPN BET 2024-25)."""
    try:
        d = f.get(f"{CORE}/events/{event_id}/competitions/{event_id}/odds", ttl=None)
    except FetchError:
        return None
    items = sorted(d.get("items", []), key=lambda i: (i.get("provider", {}).get("priority", 9),))
    for it in items:
        name = (it.get("provider") or {}).get("name", "")
        if "live" in name.lower():
            continue
        h, a = it.get("homeTeamOdds") or {}, it.get("awayTeamOdds") or {}

        def am(block, *path):
            x = block
            for p in path:
                x = (x or {}).get(p)
            return _f(x)

        out = {"provider": name}
        for which, key in (("close", "current"), ("open", "open")):
            out[which] = {
                "ml_home": am(h, key, "moneyLine", "american"),
                "ml_away": am(a, key, "moneyLine", "american"),
                "spread_home": am(h, key, "pointSpread", "american"),
                "spread_home_price": am(h, key, "spread", "american"),
                "spread_away_price": am(a, key, "spread", "american"),
                "total": am(it, key, "total", "american"),
                "over_price": am(it, key, "over", "american"),
                "under_price": am(it, key, "under", "american"),
            }
        c = out["close"]
        c["ml_home"] = c["ml_home"] if c["ml_home"] is not None else _f(h.get("moneyLine"))
        c["ml_away"] = c["ml_away"] if c["ml_away"] is not None else _f(a.get("moneyLine"))
        c["spread_home"] = c["spread_home"] if c["spread_home"] is not None else _f(it.get("spread"))
        c["spread_home_price"] = c["spread_home_price"] if c["spread_home_price"] is not None else _f(h.get("spreadOdds"))
        c["spread_away_price"] = c["spread_away_price"] if c["spread_away_price"] is not None else _f(a.get("spreadOdds"))
        c["total"] = c["total"] if c["total"] is not None else _f(it.get("overUnder"))
        c["over_price"] = c["over_price"] if c["over_price"] is not None else _f(it.get("overOdds"))
        c["under_price"] = c["under_price"] if c["under_price"] is not None else _f(it.get("underOdds"))
        if c["spread_home"] is not None or c["ml_home"] is not None:
            return out
    return None
