"""Static team reference data: ESPN/BBRef codes, arena locations, altitude, time zone."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Team:
    espn: str        # ESPN abbreviation (canonical key in this codebase)
    espn_id: str
    bbref: str
    name: str
    lat: float
    lon: float
    tz: int          # standard-time UTC offset of home arena
    altitude_ft: int


TEAMS: dict[str, Team] = {t.espn: t for t in [
    Team("ATL", "1", "ATL", "Atlanta Hawks", 33.757, -84.396, -5, 1050),
    Team("BOS", "2", "BOS", "Boston Celtics", 42.366, -71.062, -5, 20),
    Team("BKN", "17", "BRK", "Brooklyn Nets", 40.683, -73.976, -5, 30),
    Team("CHA", "30", "CHO", "Charlotte Hornets", 35.225, -80.839, -5, 750),
    Team("CHI", "4", "CHI", "Chicago Bulls", 41.881, -87.674, -6, 600),
    Team("CLE", "5", "CLE", "Cleveland Cavaliers", 41.497, -81.688, -5, 650),
    Team("DAL", "6", "DAL", "Dallas Mavericks", 32.790, -96.810, -6, 430),
    Team("DEN", "7", "DEN", "Denver Nuggets", 39.749, -105.008, -7, 5280),
    Team("DET", "8", "DET", "Detroit Pistons", 42.341, -83.055, -5, 600),
    Team("GS", "9", "GSW", "Golden State Warriors", 37.768, -122.388, -8, 10),
    Team("HOU", "10", "HOU", "Houston Rockets", 29.751, -95.362, -6, 50),
    Team("IND", "11", "IND", "Indiana Pacers", 39.764, -86.156, -5, 715),
    Team("LAC", "12", "LAC", "LA Clippers", 33.945, -118.343, -8, 100),
    Team("LAL", "13", "LAL", "Los Angeles Lakers", 34.043, -118.267, -8, 300),
    Team("MEM", "29", "MEM", "Memphis Grizzlies", 35.138, -90.051, -6, 340),
    Team("MIA", "14", "MIA", "Miami Heat", 25.781, -80.188, -5, 10),
    Team("MIL", "15", "MIL", "Milwaukee Bucks", 43.045, -87.917, -6, 620),
    Team("MIN", "16", "MIN", "Minnesota Timberwolves", 44.980, -93.276, -6, 830),
    Team("NO", "3", "NOP", "New Orleans Pelicans", 29.949, -90.082, -6, 10),
    Team("NY", "18", "NYK", "New York Knicks", 40.751, -73.993, -5, 30),
    Team("OKC", "25", "OKC", "Oklahoma City Thunder", 35.463, -97.515, -6, 1200),
    Team("ORL", "19", "ORL", "Orlando Magic", 28.539, -81.384, -5, 100),
    Team("PHI", "20", "PHI", "Philadelphia 76ers", 39.901, -75.172, -5, 40),
    Team("PHX", "21", "PHO", "Phoenix Suns", 33.446, -112.071, -7, 1086),
    Team("POR", "22", "POR", "Portland Trail Blazers", 45.532, -122.667, -8, 50),
    Team("SAC", "23", "SAC", "Sacramento Kings", 38.580, -121.500, -8, 30),
    Team("SA", "24", "SAS", "San Antonio Spurs", 29.427, -98.438, -6, 650),
    Team("TOR", "28", "TOR", "Toronto Raptors", 43.643, -79.379, -5, 250),
    Team("UTAH", "26", "UTA", "Utah Jazz", 40.768, -111.901, -7, 4226),
    Team("WSH", "27", "WAS", "Washington Wizards", 38.898, -77.021, -5, 50),
]}

ESPN_ID_TO_ABBR = {t.espn_id: t.espn for t in TEAMS.values()}
BBREF_TO_ESPN = {t.bbref: t.espn for t in TEAMS.values()}
# Historical BBRef codes
BBREF_TO_ESPN.update({"NJN": "BKN", "CHA": "CHA", "CHH": "CHA", "NOH": "NO", "NOK": "NO", "SEA": "OKC"})
ESPN_ALIASES = {"GSW": "GS", "NYK": "NY", "NOP": "NO", "SAS": "SA", "UTA": "UTAH", "WAS": "WSH", "PHO": "PHX", "BRK": "BKN"}

# Neutral / international venues: city -> (lat, lon, tz, altitude_ft)
NEUTRAL_SITES = {
    "Mexico City": (19.404, -99.096, -6, 7350),
    "Paris": (48.839, 2.378, 1, 115),
    "London": (51.508, 0.003, 0, 20),
    "Berlin": (52.508, 13.443, 1, 110),
    "Abu Dhabi": (24.467, 54.600, 4, 10),
    "Las Vegas": (36.103, -115.178, -8, 2030),
    "Montreal": (45.496, -73.569, -5, 120),
}

HIGH_ALTITUDE_FT = 4000


def canon(abbr: str) -> str:
    abbr = abbr.upper()
    return ESPN_ALIASES.get(abbr, abbr)


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def venue_location(home_abbr: str, neutral: bool, city: str | None) -> tuple[float, float, int, int]:
    if neutral and city:
        for key, loc in NEUTRAL_SITES.items():
            if key.lower() in city.lower():
                return loc
    t = TEAMS[home_abbr]
    return t.lat, t.lon, t.tz, t.altitude_ft
