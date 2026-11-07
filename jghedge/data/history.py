"""Season game database built from ESPN: results, team + player box scores, closing odds.

Stored compactly at data/history/season_<year>.json.gz (committed to the repo so the
nightly job only fetches new games).
"""
from __future__ import annotations

import gzip
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from ..config import DATA_DIR
from ..teams import TEAMS
from . import espn
from .http import Fetcher

HIST_DIR = DATA_DIR / "history"


def game_day(iso: str) -> date:
    """US game-day for an ESPN UTC timestamp (UTC-6 keeps late West-coast tips on the right day)."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (dt - timedelta(hours=6)).date()


def season_window(season: int) -> tuple[date, date]:
    return date(season - 1, 10, 1), date(season, 6, 30)


def _path(season: int):
    return HIST_DIR / f"season_{season}.json.gz"


def load_season(season: int) -> list[dict]:
    p = _path(season)
    if not p.exists():
        return []
    with gzip.open(p, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def save_season(season: int, games: list[dict]) -> None:
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    games = sorted(games, key=lambda g: (g["date"], g["id"]))
    with gzip.open(_path(season), "wt", encoding="utf-8", compresslevel=9) as fh:
        json.dump(games, fh, separators=(",", ":"))


def update_season(f: Fetcher, season: int, through: date | None = None, workers: int = 8,
                  verbose: bool = True) -> list[dict]:
    """Add any completed games missing from the stored season file."""
    have = {g["id"]: g for g in load_season(season)}
    start, end = season_window(season)
    today = datetime.now(timezone.utc).date()
    end = min(end, through or today)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    def sb(d):
        try:
            return espn.scoreboard(f, d)
        except Exception:  # noqa: BLE001 - a missing day must not kill the update
            return []

    with ThreadPoolExecutor(workers) as ex:
        boards = list(ex.map(sb, days))
    todo = []
    for evs in boards:
        for e in evs:
            if e["season_type"] not in (2, 3, 5) or not e["completed"]:
                continue
            if e["home"] not in TEAMS or e["away"] not in TEAMS:
                continue  # All-Star / exhibition
            if e["id"] not in have:
                todo.append(e)
    if verbose:
        print(f"season {season}: {len(have)} stored, {len(todo)} new games to fetch")

    def rec(e):
        r = espn.game_record(f, e["id"])
        if r and r.get("completed") and len(r.get("box", {})) == 2:
            r["season_type"] = e["season_type"]
            r["neutral"] = r.get("neutral") or e["neutral"]
            r["venue_city"] = r.get("venue_city") or e["venue_city"]
            if not r.get("odds") and e.get("odds"):
                r["odds"] = e["odds"]
            return r
        return None

    with ThreadPoolExecutor(workers) as ex:
        for i, r in enumerate(ex.map(rec, todo)):
            if r:
                have[r["id"]] = r
            if verbose and i and i % 200 == 0:
                print(f"  {i}/{len(todo)}")
    games = list(have.values())
    save_season(season, games)
    return games


# ------------------------------------------------------------ flat tables
def possessions(b: dict, o: dict) -> float:
    """Basketball-Reference team possession estimate (averaged over both sides)."""
    def one(t, u):
        orb_pct = t["oreb"] / max(t["oreb"] + u["dreb"], 1)
        return t["fga"] + 0.4 * t["fta"] - 1.07 * orb_pct * (t["fga"] - t["fgm"]) + t["tov"]
    return 0.5 * (one(b, o) + one(o, b))


def games_frame(games: list[dict]) -> pd.DataFrame:
    rows = []
    for g in games:
        h, a = g["home"], g["away"]
        bh, ba = g["box"].get(h), g["box"].get(a)
        if not bh or not ba:
            continue
        poss = possessions(bh, ba)
        o = (g.get("odds") or {}).get("close") or {}
        op = (g.get("odds") or {}).get("open") or {}
        rows.append({
            "id": g["id"], "date": pd.Timestamp(game_day(g["date"])), "tip": g["date"],
            "season": g.get("season"), "season_type": g.get("season_type"),
            "home": h, "away": a, "neutral": bool(g.get("neutral")), "venue_city": g.get("venue_city"),
            "hs": g["home_score"], "as": g["away_score"], "poss": poss,
            "ml_home": o.get("ml_home"), "ml_away": o.get("ml_away"),
            "spread_home": o.get("spread_home"), "spread_home_price": o.get("spread_home_price"),
            "spread_away_price": o.get("spread_away_price"), "total": o.get("total"),
            "over_price": o.get("over_price"), "under_price": o.get("under_price"),
            "open_spread_home": op.get("spread_home"), "open_total": op.get("total"),
            **{f"h_{k}": v for k, v in bh.items()}, **{f"a_{k}": v for k, v in ba.items()},
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["margin"] = df["hs"] - df["as"]
    df["total_pts"] = df["hs"] + df["as"]
    return df.sort_values(["date", "tip", "id"]).reset_index(drop=True)


def team_games_frame(gdf: pd.DataFrame) -> pd.DataFrame:
    """One row per team per game with offensive and defensive box components."""
    cols = ["fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "ast", "stl", "blk", "tov", "pf"]
    parts = []
    for side, opp in (("h", "a"), ("a", "h")):
        d = pd.DataFrame({
            "id": gdf["id"], "date": gdf["date"], "season": gdf["season"],
            "team": gdf["home"] if side == "h" else gdf["away"],
            "opp": gdf["away"] if side == "h" else gdf["home"],
            "home": (side == "h") & ~gdf["neutral"], "neutral": gdf["neutral"],
            "pts": gdf["hs"] if side == "h" else gdf["as"],
            "opp_pts": gdf["as"] if side == "h" else gdf["hs"], "poss": gdf["poss"],
        })
        for c in cols:
            d[c] = gdf[f"{side}_{c}"]
            d[f"opp_{c}"] = gdf[f"{opp}_{c}"]
        parts.append(d)
    tg = pd.concat(parts, ignore_index=True)
    tg["ortg"] = 100 * tg["pts"] / tg["poss"]
    tg["drtg"] = 100 * tg["opp_pts"] / tg["poss"]
    tg["efg"] = (tg["fgm"] + 0.5 * tg["fg3m"]) / tg["fga"]
    tg["opp_efg"] = (tg["opp_fgm"] + 0.5 * tg["opp_fg3m"]) / tg["opp_fga"]
    tg["tov_pct"] = tg["tov"] / tg["poss"]
    tg["opp_tov_pct"] = tg["opp_tov"] / tg["poss"]
    tg["orb_pct"] = tg["oreb"] / (tg["oreb"] + tg["opp_dreb"])
    tg["drb_pct"] = tg["dreb"] / (tg["dreb"] + tg["opp_oreb"])
    tg["ftr"] = tg["ftm"] / tg["fga"]
    tg["opp_ftr"] = tg["opp_ftm"] / tg["opp_fga"]
    tg["fg3_rate"] = tg["fg3a"] / tg["fga"]
    return tg.sort_values(["date", "id"]).reset_index(drop=True)


def player_games_frame(games: list[dict]) -> pd.DataFrame:
    rows = []
    for g in games:
        d = pd.Timestamp(game_day(g["date"]))
        h, a = g["home"], g["away"]
        for p in g.get("players", []):
            r = dict(zip(espn.PLAYER_COLS, p))
            r["game_id"], r["date"], r["season"] = g["id"], d, g.get("season")
            r["opp"] = a if r["team"] == h else h
            r["home"] = r["team"] == h and not g.get("neutral")
            rows.append(r)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["id"] = df["id"].astype(str)
    df["fg3m"] = df["fg3m"].astype(float)
    df["pra"] = df["pts"] + df["reb"] + df["ast"]
    df["pr"] = df["pts"] + df["reb"]
    df["pa"] = df["pts"] + df["ast"]
    df["ra"] = df["reb"] + df["ast"]
    return df.sort_values(["date", "game_id"]).reset_index(drop=True)


def load_all(seasons: list[int]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    games: list[dict] = []
    for s in seasons:
        games.extend(load_season(s))
    gdf = games_frame(games)
    return gdf, team_games_frame(gdf) if len(gdf) else pd.DataFrame(), player_games_frame(games)


def np_float(x):
    return float(x) if x is not None and not (isinstance(x, float) and np.isnan(x)) else None
