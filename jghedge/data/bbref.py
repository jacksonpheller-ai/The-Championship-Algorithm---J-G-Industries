"""Basketball-Reference: player BPM/OBPM/DBPM/VORP/PER/WS48/TS%/USG%, individual
ORtg/DRtg (per-100), and team ratings + four factors. Rate limited to <20 req/min."""
from __future__ import annotations

import re
import unicodedata

import pandas as pd
from lxml import html as lhtml

from ..teams import BBREF_TO_ESPN
from .http import Fetcher

BASE = "https://www.basketball-reference.com"
TTL = 6 * 3600


def norm_name(name: str) -> str:
    """'Nikola Jokić' -> 'nikola jokic'; drops suffixes and punctuation for cross-site joins."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'`\-]", "", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _table(page: str, table_id: str) -> list[dict]:
    # Some tables ship inside HTML comments.
    m = re.search(r'(<table[^>]*id="%s".*?</table>)' % re.escape(table_id), page, re.S)
    if not m:
        return []
    tree = lhtml.fromstring(m.group(1))
    rows = []
    for tr in tree.xpath(".//tbody/tr"):
        cls = tr.get("class") or ""
        if "thead" in cls or "over_header" in cls:
            continue
        row = {}
        for cell in tr.xpath("./th|./td"):
            k = cell.get("data-stat")
            if not k:
                continue
            row[k] = cell.text_content().strip()
            if k in ("name_display", "player"):
                link = cell.xpath(".//a/@href")
                row["bbref_id"] = link[0].split("/")[-1].replace(".html", "") if link else None
            if k in ("team", "team_name"):
                link = cell.xpath(".//a/@href")
                if link:
                    mm = re.search(r"/teams/([A-Z]{3})/", link[0])
                    row["team_code"] = mm.group(1) if mm else None
        if row:
            rows.append(row)
    return rows


def _num(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for c in cols:
        if c in df:
            df[c] = pd.to_numeric(df[c].astype(str).str.replace("%", ""), errors="coerce")
    return df


PLAYER_ADV = ["age", "games", "games_started", "mp", "per", "ts_pct", "fg3a_per_fga_pct",
              "fta_per_fga_pct", "orb_pct", "drb_pct", "trb_pct", "ast_pct", "stl_pct", "blk_pct",
              "tov_pct", "usg_pct", "ows", "dws", "ws", "ws_per_48", "obpm", "dbpm", "bpm", "vorp"]


def player_advanced(f: Fetcher, season: int) -> pd.DataFrame:
    page = f.get(f"{BASE}/leagues/NBA_{season}_advanced.html", ttl=TTL, kind="html")
    df = pd.DataFrame(_table(page, "advanced"))
    if df.empty:
        return df
    df = df.rename(columns={"name_display": "name", "team_name_abbr": "team"})
    df = _num(df, PLAYER_ADV)
    return _collapse_traded(df)


def player_per_poss(f: Fetcher, season: int) -> pd.DataFrame:
    page = f.get(f"{BASE}/leagues/NBA_{season}_per_poss.html", ttl=TTL, kind="html")
    df = pd.DataFrame(_table(page, "per_poss"))
    if df.empty:
        return df
    df = df.rename(columns={"name_display": "name", "team_name_abbr": "team"})
    keep = ["bbref_id", "name", "team", "games", "mp", "off_rtg", "def_rtg", "pts_per_poss",
            "trb_per_poss", "ast_per_poss", "fg3_per_poss", "stl_per_poss", "blk_per_poss"]
    df = df[[c for c in keep if c in df]]
    df = _num(df, [c for c in keep if c not in ("bbref_id", "name", "team")])
    return _collapse_traded(df)


def _collapse_traded(df: pd.DataFrame) -> pd.DataFrame:
    """Traded players get a '2TM'/'3TM'/'TOT' total row plus one row per team.
    Keep the total row's stats, tagged with the most recent team."""
    df = df[df["name"].notna() & (df["name"] != "")].copy()
    out = []
    for pid, g in df.groupby("bbref_id", sort=False):
        tot = g[g["team"].astype(str).str.match(r"^(\dTM|TOT)$")]
        teams = g[~g.index.isin(tot.index)]
        row = (tot.iloc[0] if len(tot) else g.iloc[0]).copy()
        last_team = teams.iloc[-1]["team"] if len(teams) else row["team"]
        row["team"] = last_team
        row["espn_team"] = BBREF_TO_ESPN.get(str(last_team))
        if len(teams) > 1 and "mp" in teams:
            row["mp_current_team"] = float(teams.iloc[-1]["mp"] or 0)
        else:
            row["mp_current_team"] = row.get("mp")
        out.append(row)
    res = pd.DataFrame(out).reset_index(drop=True)
    res["key"] = res["name"].map(norm_name)
    return res


TEAM_ADV = ["age", "wins", "losses", "wins_pyth", "losses_pyth", "mov", "sos", "srs", "off_rtg",
            "def_rtg", "net_rtg", "pace", "fta_per_fga_pct", "fg3a_per_fga_pct", "ts_pct",
            "efg_pct", "tov_pct", "orb_pct", "ft_rate", "opp_efg_pct", "opp_tov_pct", "drb_pct",
            "opp_ft_rate"]


def team_advanced(f: Fetcher, season: int) -> pd.DataFrame:
    page = f.get(f"{BASE}/leagues/NBA_{season}.html", ttl=TTL, kind="html")
    rows = _table(page, "advanced-team")
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df[df["team"].astype(str) != "League Average"].copy()
    df["espn"] = df["team_code"].map(BBREF_TO_ESPN)
    df = _num(df, TEAM_ADV)
    return df.dropna(subset=["espn"]).set_index("espn")
