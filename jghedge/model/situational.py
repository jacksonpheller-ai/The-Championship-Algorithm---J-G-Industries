"""Schedule-derived situational features: rest, back-to-backs, 3-in-4, travel, time zones, altitude."""
from __future__ import annotations

import pandas as pd

from ..teams import HIGH_ALTITUDE_FT, TEAMS, haversine_miles, venue_location


def schedule_features(sched: pd.DataFrame) -> pd.DataFrame:
    """`sched` needs id, date, tip, home, away, neutral, venue_city — completed AND upcoming games."""
    s = sched.sort_values(["tip", "id"]).reset_index(drop=True)
    last: dict[str, tuple] = {}            # team -> (date, lat, lon, tz)
    recent: dict[str, list] = {}           # team -> list of dates
    rows = []
    for g in s.itertuples(index=False):
        lat, lon, tz, alt = venue_location(g.home, bool(g.neutral), g.venue_city)
        feat = {"id": g.id}
        for side, team in (("h", g.home), ("a", g.away)):
            prev = last.get(team)
            if prev is None or (g.date - prev[0]).days > 20:
                rest, travel, tzs = 3, 0.0, 0
            else:
                rest = max(0, (g.date - prev[0]).days - 1)
                travel = haversine_miles(prev[1], prev[2], lat, lon)
                tzs = abs(tz - prev[3])
            ds = [d for d in recent.get(team, []) if 0 < (g.date - d).days <= 3]
            home_alt = TEAMS[team].altitude_ft if team in TEAMS else 0
            feat.update({
                f"{side}_rest": min(rest, 3),
                f"{side}_b2b": int(rest == 0),
                f"{side}_3in4": int(len(ds) >= 2),
                f"{side}_travel": travel,
                f"{side}_tz": tzs,
                f"{side}_alt": int(alt >= HIGH_ALTITUDE_FT and home_alt < HIGH_ALTITUDE_FT),
            })
            last[team] = (g.date, lat, lon, tz)
            recent.setdefault(team, []).append(g.date)
            recent[team] = recent[team][-4:]
        rows.append(feat)
    return pd.DataFrame(rows).set_index("id")


SIT_FEATURES = ["b2b_diff", "rest_diff", "travel_diff", "tz_diff", "alt_edge", "three_in_four_diff"]


def feature_vector(f: dict | pd.Series) -> dict:
    """Signed so that positive values favour the HOME team."""
    return {
        "b2b_diff": f["a_b2b"] - f["h_b2b"],
        "rest_diff": min(f["h_rest"], 2) - min(f["a_rest"], 2),
        "travel_diff": (f["a_travel"] - f["h_travel"]) / 1000.0,
        "tz_diff": f["a_tz"] - f["h_tz"],
        "alt_edge": f["a_alt"] - f["h_alt"],
        "three_in_four_diff": f["a_3in4"] - f["h_3in4"],
    }


def situational_margin(f: dict | pd.Series, params: dict) -> float:
    v = feature_vector(f)
    return (params["b2b_points"] * v["b2b_diff"]
            + params["rest_advantage_points"] * v["rest_diff"]
            + params["travel_points_per_1000mi"] * v["travel_diff"]
            + params["tz_points_per_hour"] * v["tz_diff"]
            + params["altitude_points"] * v["alt_edge"]
            + params.get("three_in_four_points", 0.5) * v["three_in_four_diff"])
