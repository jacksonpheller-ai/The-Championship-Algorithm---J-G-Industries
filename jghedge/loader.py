"""Assemble data + Engine. Basketball-Reference tables are snapshotted to data/bbref/ so a
blocked or rate-limited BBRef never stops the nightly run."""
from __future__ import annotations

import pandas as pd

from .config import CURRENT_SEASON, DATA_DIR, load_params
from .data import bbref, history
from .data.http import Fetcher
from .model.engine import Engine
from .model.players import enrich_with_ratings

BBREF_DIR = DATA_DIR / "bbref"


def bbref_table(f: Fetcher, kind: str, season: int, current: int = CURRENT_SEASON) -> pd.DataFrame:
    path = BBREF_DIR / f"{kind}_{season}.csv"
    if path.exists() and season < current:
        df = pd.read_csv(path, dtype={"bbref_id": str})
        return df.set_index("espn") if kind == "team" else df
    try:
        df = (bbref.team_advanced(f, season) if kind == "team" else
              bbref.player_per_poss(f, season) if kind == "per_poss" else bbref.player_advanced(f, season))
        if df is not None and not df.empty:
            BBREF_DIR.mkdir(parents=True, exist_ok=True)
            (df.reset_index() if kind == "team" else df).to_csv(path, index=False)
            return df
    except Exception as e:  # noqa: BLE001
        print(f"  BBRef {kind} {season} unavailable ({e}); using snapshot")
    if path.exists():
        df = pd.read_csv(path, dtype={"bbref_id": str})
        return df.set_index("espn") if kind == "team" else df
    return pd.DataFrame()


def build_engine(f: Fetcher, seasons: list[int], params: dict | None = None) -> Engine:
    params = params or load_params()
    gdf, tg, pg = history.load_all(seasons)
    first = min(seasons)
    adv = {s: enrich_with_ratings(bbref_table(f, "player", s), bbref_table(f, "per_poss", s))
           for s in range(first - 2, max(seasons) + 1)}
    team_base = {first - 1: bbref_table(f, "team", first - 1)}
    return Engine(gdf, tg, pg, adv, team_base, params)
