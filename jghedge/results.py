"""Publish final results per game day so the dashboard can grade bets people actually placed.

docs/data/results/<date>.json  ->  {"date", "games": {id: {home, away, hs, as}},
                                     "players": {game_id: {player_id: {pts, reb, ast, ...}}}}
docs/data/results/index.json   ->  list of dates available
"""
from __future__ import annotations

import json
from datetime import date

from .config import SITE_DATA
from .data import history
from .data.espn import PLAYER_COLS

RES_DIR = SITE_DATA / "results"
STATS = ["min", "pts", "reb", "ast", "fg3m", "stl", "blk", "tov"]


def publish(season: int, since: date | None = None) -> int:
    RES_DIR.mkdir(parents=True, exist_ok=True)
    by_day: dict[str, dict] = {}
    for g in history.load_season(season):
        d = str(history.game_day(g["date"]))
        if since and date.fromisoformat(d) < since:
            continue
        day = by_day.setdefault(d, {"date": d, "games": {}, "players": {}})
        day["games"][g["id"]] = {"home": g["home"], "away": g["away"], "hs": g["home_score"], "as": g["away_score"]}
        pl = {}
        for p in g.get("players", []):
            r = dict(zip(PLAYER_COLS, p))
            pl[str(r["id"])] = {k: r[k] for k in STATS}
            pl[str(r["id"])]["pra"] = r["pts"] + r["reb"] + r["ast"]
            pl[str(r["id"])]["pr"] = r["pts"] + r["reb"]
            pl[str(r["id"])]["pa"] = r["pts"] + r["ast"]
            pl[str(r["id"])]["ra"] = r["reb"] + r["ast"]
        day["players"][g["id"]] = pl
    n = 0
    for d, day in by_day.items():
        path = RES_DIR / f"{d}.json"
        txt = json.dumps(day, separators=(",", ":"))
        if not path.exists() or path.read_text() != txt:
            path.write_text(txt)
            n += 1
    dates = sorted(p.stem for p in RES_DIR.glob("*.json") if p.stem != "index")
    (RES_DIR / "index.json").write_text(json.dumps(dates))
    return n
