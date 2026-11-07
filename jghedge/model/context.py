"""Game-context adjustments applied to the blended total AFTER the market blend.

Evidence (research memo B4, four seasons of closing totals): late in the regular season the
actual total exceeds the closing total by +2.56 points on average (n=528, t=3.25, 56% overs,
same sign and size in each of three seasons). Nothing else among 48 candidate features
(referees, pace, 3PA rate, rest, streaks, day of week, altitude alone...) survived a
leave-one-season-out test. The coefficient is applied shrunk (about 70% of the estimate).

A kill switch zeroes the adjustment if the rolling over rate of qualifying bets drops
below 50% over the last 100, so a regime change cannot bleed for a whole spring.
"""
from __future__ import annotations

import pandas as pd

CONTEXT_PARAMS: dict = {
    "late_gp": 70,          # mean games played by the two teams at which the effect switches on
    "late_adj": 1.8,        # points added to the blended total (shrunk from +2.56)
    "very_late_gp": 79,     # games 79-82
    "very_late_adj": 2.5,
    "tank_spread": 8.0,     # |closing spread| at which a bad underdog is treated as a tanker
    "tank_winpct": 0.35,
    "regular_season_type": 2,
    "kill_switch_window": 100,
    "kill_switch_floor": 0.50,
}


def total_context_adjust(home_gp: int, away_gp: int, season_type: int | None, spread_home: float | None,
                         home_winpct: float | None, away_winpct: float | None,
                         params: dict | None = None) -> float:
    """Points to add to the blended game total. Zero outside the late regular season."""
    p = {**CONTEXT_PARAMS, **(params or {})}
    if season_type is not None and int(season_type) != p["regular_season_type"]:
        return 0.0
    mean_gp = (float(home_gp or 0) + float(away_gp or 0)) / 2.0
    if mean_gp < p["late_gp"]:
        return 0.0
    adj = float(p["late_adj"])
    tank = False
    if spread_home is not None and abs(float(spread_home)) >= p["tank_spread"]:
        dog_winpct = home_winpct if float(spread_home) > 0 else away_winpct
        if dog_winpct is not None and float(dog_winpct) < p["tank_winpct"]:
            tank = True
    if mean_gp >= p["very_late_gp"] or tank:
        adj = float(p["very_late_adj"])
    return adj


def games_played(tg: pd.DataFrame, team: str, date, season: int) -> int:
    """Regular-season games a team has played strictly before `date`."""
    if tg is None or len(tg) == 0:
        return 0
    m = (tg["team"] == team) & (tg["season"] == season) & (tg["date"] < pd.Timestamp(date))
    return int(m.sum())


def team_winpct(tg: pd.DataFrame, team: str, date, season: int) -> float | None:
    if tg is None or len(tg) == 0:
        return None
    g = tg[(tg["team"] == team) & (tg["season"] == season) & (tg["date"] < pd.Timestamp(date))]
    if len(g) == 0:
        return None
    return float((g["pts"] > g["opp_pts"]).mean())


def kill_switch(recent_over_results: list[bool], params: dict | None = None) -> bool:
    """True when the adjustment should be switched off: rolling over rate below the floor."""
    p = {**CONTEXT_PARAMS, **(params or {})}
    w = int(p["kill_switch_window"])
    if len(recent_over_results) < w:
        return False
    tail = recent_over_results[-w:]
    return (sum(1 for x in tail if x) / w) < p["kill_switch_floor"]
