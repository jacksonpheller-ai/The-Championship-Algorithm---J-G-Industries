"""Margin-of-victory Elo (FiveThirtyEight-style) as an independent second opinion."""
from __future__ import annotations

import math

import pandas as pd

BASE = 1505.0


def expected(diff: float) -> float:
    return 1 / (1 + 10 ** (-diff / 400))


def run_elo(gdf: pd.DataFrame, params: dict, init: dict[str, float] | None = None):
    """Returns (current ratings, {game_id: (home_pre, away_pre)})."""
    k, hca, revert = params["elo_k"], params["elo_hca"], params["elo_season_revert"]
    r: dict[str, float] = dict(init or {})
    pre: dict[str, tuple[float, float]] = {}
    season = None
    for g in gdf.itertuples(index=False):
        if season is not None and g.season != season:
            r = {t: BASE + (1 - revert) * (v - BASE) for t, v in r.items()}
        season = g.season
        h, a = r.get(g.home, BASE), r.get(g.away, BASE)
        pre[g.id] = (h, a)
        adv = 0.0 if g.neutral else hca
        diff = h + adv - a
        e = expected(diff)
        mov = g.margin
        won = 1.0 if mov > 0 else 0.0
        wdiff = diff if mov > 0 else -diff
        mult = ((abs(mov) + 3) ** 0.8) / (7.5 + 0.006 * wdiff)
        delta = k * mult * (won - e)
        r[g.home] = h + delta
        r[g.away] = a - delta
    return r, pre


def init_from_srs(srs: dict[str, float]) -> dict[str, float]:
    return {t: BASE + 20.0 * s for t, s in srs.items() if s is not None and not math.isnan(s)}
