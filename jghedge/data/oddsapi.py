"""Optional multi-book prices from The Odds API (https://the-odds-api.com), v4.

Enabled only when the environment variable ODDS_API_KEY is set (add it as a GitHub Actions
secret named ODDS_API_KEY). Without it every function returns empty results and the desk
prices from ESPN's DraftKings feed as before.

What it buys us: the same bet at the best available price across US books, and, if the
provider lists them, the bettors' own books (Fliff is reported under region "us2" and the
DFS pick'em sites PrizePicks/Underdog under region "us_dfs" for player markets). Those
bookmaker keys and the credit costs below were written from the provider's documentation as
remembered on 2026-11-07 and could NOT be verified from this sandbox (the host is blocked
without a key): treat them as assumptions and check the first live response.

Credit model (assumption): one call to /sports/basketball_nba/odds costs
(number of markets) x (number of regions) credits; per-event player-prop calls likewise.
Free tier ~500 credits/month. The budget guard stops below `MIN_CREDITS_LEFT`.
The research memo's verdict: the paid plan is not worth it below roughly $5,000 of bankroll.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from ..teams import TEAMS, canon

BASE = "https://api.the-odds-api.com/v4/sports/basketball_nba"
DEFAULT_REGIONS = "us,us2"
FEATURED_MARKETS = ("spreads", "totals", "h2h")
PROP_MARKETS = ("player_points", "player_rebounds", "player_assists", "player_threes",
                "player_points_rebounds_assists", "player_points_rebounds", "player_points_assists",
                "player_rebounds_assists", "player_steals", "player_blocks", "player_turnovers")
PROP_MARKET_TO_STAT = {"player_points": "pts", "player_rebounds": "reb", "player_assists": "ast", "player_threes": "fg3m",
                       "player_points_rebounds_assists": "pra", "player_points_rebounds": "pr",
                       "player_points_assists": "pa", "player_rebounds_assists": "ra", "player_steals": "stl",
                       "player_blocks": "blk", "player_turnovers": "tov"}
MIN_CREDITS_LEFT = 60
FLIFF_KEYS = ("fliff",)
PICKEM_KEYS = ("prizepicks", "underdog", "pick6")

_NAME_TO_ABBR = {t.name.lower(): a for a, t in TEAMS.items()}
_NAME_TO_ABBR.update({"los angeles clippers": "LAC", "la clippers": "LAC"})


def api_key() -> str | None:
    return os.environ.get("ODDS_API_KEY") or None


def available() -> bool:
    return api_key() is not None


class Budget:
    """Tracks the provider's remaining-credit headers across calls."""

    def __init__(self, floor: int = MIN_CREDITS_LEFT):
        self.remaining: int | None = None
        self.used: int | None = None
        self.floor = floor

    def update(self, headers) -> None:
        try:
            if headers.get("x-requests-remaining") is not None:
                self.remaining = int(float(headers.get("x-requests-remaining")))
            if headers.get("x-requests-used") is not None:
                self.used = int(float(headers.get("x-requests-used")))
        except (TypeError, ValueError):
            pass

    def allows(self, cost: int) -> bool:
        return self.remaining is None or (self.remaining - cost) >= self.floor


def team_abbr(name: str | None) -> str | None:
    if not name:
        return None
    key = name.strip().lower()
    if key in _NAME_TO_ABBR:
        return _NAME_TO_ABBR[key]
    for full, ab in _NAME_TO_ABBR.items():
        if key.split()[-1] == full.split()[-1]:
            return ab
    try:
        return canon(name)
    except Exception:  # noqa: BLE001
        return None


def parse_slate(payload: list[dict]) -> dict[str, dict]:
    """Normalise /odds output -> {event_id: {home, away, commence, books: {book: {...}}}}."""
    out: dict[str, dict] = {}
    for ev in payload or []:
        home, away = team_abbr(ev.get("home_team")), team_abbr(ev.get("away_team"))
        books: dict[str, dict] = {}
        for bk in ev.get("bookmakers", []):
            rec: dict = {"last_update": bk.get("last_update")}
            for m in bk.get("markets", []):
                key = m.get("key")
                outs = m.get("outcomes", [])
                if key == "h2h":
                    for o in outs:
                        ab = team_abbr(o.get("name"))
                        if ab == home:
                            rec["ml_home"] = o.get("price")
                        elif ab == away:
                            rec["ml_away"] = o.get("price")
                elif key == "spreads":
                    for o in outs:
                        ab = team_abbr(o.get("name"))
                        if ab == home:
                            rec["spread_home"], rec["spread_home_price"] = o.get("point"), o.get("price")
                        elif ab == away:
                            rec["spread_away_price"] = o.get("price")
                elif key == "totals":
                    for o in outs:
                        if (o.get("name") or "").lower() == "over":
                            rec["total"], rec["over_price"] = o.get("point"), o.get("price")
                        elif (o.get("name") or "").lower() == "under":
                            rec["under_price"] = o.get("price")
            books[bk.get("key")] = rec
        out[str(ev.get("id"))] = {"home": home, "away": away, "commence": ev.get("commence_time"), "books": books}
    return out


def parse_event_props(payload: dict) -> dict[tuple, dict[str, dict]]:
    """Normalise /events/<id>/odds player markets -> {(player_name, stat): {book: {line, over, under}}}."""
    out: dict[tuple, dict[str, dict]] = {}
    for bk in (payload or {}).get("bookmakers", []):
        for m in bk.get("markets", []):
            stat = PROP_MARKET_TO_STAT.get(m.get("key"))
            if not stat:
                continue
            for o in m.get("outcomes", []):
                player = o.get("description") or o.get("participant")
                if not player:
                    continue
                rec = out.setdefault((player, stat), {}).setdefault(bk.get("key"), {})
                rec["line"] = o.get("point")
                side = (o.get("name") or "").lower()
                if side == "over":
                    rec["over"] = o.get("price")
                elif side == "under":
                    rec["under"] = o.get("price")
    return out


def fetch_slate_odds(fetcher, regions: str = DEFAULT_REGIONS, markets=FEATURED_MARKETS, budget: Budget | None = None) -> dict[str, dict]:
    key = api_key()
    if not key:
        return {}
    budget = budget or Budget()
    cost = len(markets) * len(regions.split(","))
    if not budget.allows(cost):
        return {}
    url = (f"{BASE}/odds?regions={regions}&markets={','.join(markets)}&oddsFormat=american&dateFormat=iso&apiKey={key}")
    r = fetcher.session.get(url, timeout=30)
    budget.update(r.headers)
    if r.status_code != 200:
        return {}
    return parse_slate(r.json())


def fetch_event_props(fetcher, event_id: str, regions: str = "us,us_dfs", markets=PROP_MARKETS[:4], budget: Budget | None = None) -> dict:
    key = api_key()
    if not key:
        return {}
    budget = budget or Budget()
    cost = len(markets) * len(regions.split(","))
    if not budget.allows(cost):
        return {}
    url = (f"{BASE}/events/{event_id}/odds?regions={regions}&markets={','.join(markets)}&oddsFormat=american&apiKey={key}")
    r = fetcher.session.get(url, timeout=30)
    budget.update(r.headers)
    if r.status_code != 200:
        return {}
    return parse_event_props(r.json())


def map_to_espn(events: dict[str, dict], slate: list[dict]) -> dict[str, str]:
    """{odds-api event id: ESPN game id} by (home, away) and a same-day tip."""
    by_pair = {}
    for g in slate:
        by_pair[(g["home"], g["away"])] = g["id"]
    out = {}
    for eid, ev in events.items():
        gid = by_pair.get((ev.get("home"), ev.get("away")))
        if gid:
            out[eid] = gid
    return out


def best_price(books: dict[str, dict], market: str, side: str) -> tuple[str | None, float | None, float | None]:
    """Best American price for a side; for spreads/totals the line is returned too (lines may
    differ across books, so compare like with like before taking the best price)."""
    best = (None, None, None)
    for book, rec in books.items():
        if market == "ml":
            price = rec.get("ml_home" if side == "home" else "ml_away")
            line = None
        elif market == "spread":
            price = rec.get("spread_home_price" if side == "home" else "spread_away_price")
            line = rec.get("spread_home")
        else:
            price = rec.get("over_price" if side == "over" else "under_price")
            line = rec.get("total")
        if price is None:
            continue
        if best[1] is None or _dec(price) > _dec(best[1]):
            best = (book, price, line)
    return best


def fliff_lines(books: dict[str, dict]) -> dict | None:
    for k in FLIFF_KEYS:
        if k in books:
            return books[k]
    return None


def _dec(american: float) -> float:
    a = float(american)
    return 1 + (a / 100 if a > 0 else 100 / -a)


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
