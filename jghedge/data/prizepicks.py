"""PrizePicks board (NBA = league_id 7) from the public JSON:API projections endpoint.

The endpoint is undocumented. The parser follows the shape observed in the wild:

    data[]      type "projection"; attributes {line_score, stat_type, odds_type, start_time,
                status, description (opponent), projection_type, is_promo, ...};
                relationships.new_player.data.id
    included[]  type "new_player"; attributes {name | display_name, team, position, ...}

PrizePicks refuses many cloud networks (HTTP 403 / Cloudflare challenge) and this sandbox
cannot reach the host at all, so the live call is untested here: every failure path returns
an empty board plus a human-readable reason and the pipeline falls back to DraftKings lines.
"""
from __future__ import annotations

import requests

from ..teams import canon
from .bbref import norm_name
from .http import FetchError, Fetcher

API = "https://api.prizepicks.com/projections"
NBA_LEAGUE_ID = 7
TTL = 120            # seconds: the board moves; a stale copy is worse than none
MAX_PAGES = 8        # follow links.next at most this far (NBA fits in one or two pages)

# A plain request: the server does not present itself as PrizePicks' own web app. PrizePicks refuses cloud
# servers (HTTP 403), so the board is normally read by the reader's own browser instead (the page's
# "Load PrizePicks' board" button), and this fetch is only a chance that it is ever allowed.
HEADERS = {"Accept": "application/json"}

# PrizePicks stat_type -> our stat keys (jghedge.model.props.STATS + COMBOS).
STAT_MAP = {
    "Points": "pts", "Rebounds": "reb", "Assists": "ast", "3-PT Made": "fg3m",
    "Pts+Rebs+Asts": "pra", "Pts+Rebs": "pr", "Pts+Asts": "pa", "Rebs+Asts": "ra",
    "Steals": "stl", "Blocked Shots": "blk", "Turnovers": "tov",
    # spelling variants seen on the board over time
    "3-Pointers Made": "fg3m", "3PT Made": "fg3m", "Blocks": "blk", "Pts+Reb+Ast": "pra",
    "Points + Rebounds + Assists": "pra", "Points + Rebounds": "pr", "Points + Assists": "pa",
    "Rebounds + Assists": "ra",
}
# Markets we cannot price from the box-score simulation (no leg is ever built for them).
UNSUPPORTED = {
    "Fantasy Score", "Free Throws Made", "FG Made", "FG Attempted", "3-PT Attempted",
    "Offensive Rebounds", "Defensive Rebounds", "Blks+Stls", "Dunks", "Personal Fouls",
    "Minutes Played", "Two Pointers Made", "Points (Combo)", "Rebounds (Combo)", "Assists (Combo)",
    "First Basket", "Double-Double", "Triple-Double",
}


def map_stat(stat_type: str | None) -> str | None:
    """PrizePicks stat_type -> our stat key, or None when unsupported/unknown."""
    if not stat_type:
        return None
    s = " ".join(str(stat_type).split())
    if s in STAT_MAP:
        return STAT_MAP[s]
    low = s.lower()
    for k, v in STAT_MAP.items():
        if k.lower() == low:
            return v
    return None


def _float(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def parse_projections(payload: dict) -> list[dict]:
    """JSON:API payload -> flat rows. Rows for unsupported markets are kept with stat=None
    (supported=False) so callers can count what the board offered."""
    if not isinstance(payload, dict):
        return []
    players: dict[str, dict] = {}
    for inc in payload.get("included") or []:
        if not isinstance(inc, dict) or inc.get("type") != "new_player":
            continue
        a = inc.get("attributes") or {}
        team = a.get("team") or a.get("team_name") or ""
        players[str(inc.get("id"))] = {
            "name": a.get("display_name") or a.get("name"),
            "team": canon(str(team)) if team else None,
            "position": a.get("position"),
        }
    rows: list[dict] = []
    for d in payload.get("data") or []:
        if not isinstance(d, dict) or d.get("type") not in (None, "projection"):
            continue
        a = d.get("attributes") or {}
        rel = ((d.get("relationships") or {}).get("new_player") or {}).get("data") or {}
        pp_pid = str(rel.get("id")) if rel.get("id") is not None else None
        pl = players.get(pp_pid or "", {})
        line = _float(a.get("line_score"))
        if line is None:
            continue
        stat_type = a.get("stat_type")
        stat = map_stat(stat_type)
        rows.append({
            "projection_id": str(d.get("id")),
            "pp_player_id": pp_pid,
            "name": pl.get("name"),
            "team": pl.get("team"),
            "position": pl.get("position"),
            "opponent": a.get("description"),
            "stat_type": stat_type,
            "stat": stat,
            "supported": stat is not None,
            "line": line,
            "odds_type": a.get("odds_type") or "standard",
            "start_time": a.get("start_time"),
            "status": a.get("status"),
            "is_promo": bool(a.get("is_promo", False)),
            "updated_at": a.get("updated_at"),
        })
    return rows


def _reason(msg: str) -> str:
    m = msg or "unknown error"
    if "403" in m:
        return "blocked (HTTP 403): PrizePicks refuses this network"
    if "429" in m:
        return "rate limited (HTTP 429)"
    low = m.lower()
    if "timed out" in low or "timeout" in low:
        return "timeout contacting api.prizepicks.com"
    if "offline" in low:
        return "offline: PrizePicks board is never cached"
    return f"fetch failed: {m[:160]}"


def fetch_projections(fetcher: Fetcher, league_id: int = NBA_LEAGUE_ID, per_page: int = 250,
                      single_stat: bool = True, ttl: float = TTL) -> tuple[list[dict], str | None]:
    """Fetch the board. Returns (rows, None) on success or ([], reason) on any failure.
    Never stores to the disk cache (store=False) and never raises."""
    url = (f"{API}?league_id={int(league_id)}&per_page={int(per_page)}"
           f"&single_stat={'true' if single_stat else 'false'}")
    rows: list[dict] = []
    session_headers = getattr(fetcher, "session", None)
    saved = dict(session_headers.headers) if session_headers is not None else None
    if session_headers is not None:
        session_headers.headers.update(HEADERS)
    try:
        for _ in range(MAX_PAGES):
            payload = fetcher.get(url, ttl=ttl, kind="json", store=False)
            if not isinstance(payload, dict) or "data" not in payload:
                return [], "unexpected response shape (no data[])"
            rows.extend(parse_projections(payload))
            url = (payload.get("links") or {}).get("next")
            if not url:
                break
    except FetchError as e:
        return [], _reason(str(e))
    except (requests.RequestException, ValueError, TypeError) as e:  # noqa: PERF203
        return [], _reason(str(e))
    finally:
        if session_headers is not None and saved is not None:
            session_headers.headers.clear()
            session_headers.headers.update(saved)
    return rows, None


def match_players(rows: list[dict], id_to_name: dict[str, str],
                  team_of: dict[str, str] | None = None) -> tuple[list[dict], list[str]]:
    """Attach ESPN athlete ids to board rows by normalised name (bbref.norm_name).
    Ambiguous names are resolved by team when `team_of` ({id: ESPN abbr}) is given, else left
    unmatched. Returns (rows with 'player_id', sorted unique unmatched names)."""
    by_key: dict[str, list[str]] = {}
    for pid, name in (id_to_name or {}).items():
        if name:
            by_key.setdefault(norm_name(str(name)), []).append(str(pid))
    out, unmatched = [], set()
    for r in rows:
        name = r.get("name") or ""
        ids = list(by_key.get(norm_name(name), [])) if name else []
        if len(ids) > 1 and team_of and r.get("team"):
            narrowed = [i for i in ids if canon(str(team_of.get(i) or "")) == canon(str(r["team"]))]
            if len(narrowed) == 1:
                ids = narrowed
        pid = ids[0] if len(ids) == 1 else None
        if pid is None and name:
            unmatched.add(name)
        out.append({**r, "player_id": pid})
    return out, sorted(unmatched)


def board_summary(rows: list[dict]) -> dict:
    """Counts for the dashboard notes: how much of the board we can price."""
    n = len(rows)
    supported = sum(1 for r in rows if r.get("supported"))
    matched = sum(1 for r in rows if r.get("supported") and r.get("player_id"))
    standard = sum(1 for r in rows if r.get("odds_type", "standard") == "standard")
    return {"rows": n, "supported": supported, "matched": matched, "standard": standard}
