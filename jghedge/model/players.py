"""Player value (BPM family) and lineup-availability adjustments.

Value of a player = Box Plus/Minus split into OBPM/DBPM, Bayesian-blended between the
current season (weight = minutes played) and last season (weight = `bpm_prior_minutes`).

Availability adjustment for tonight, per team (points per 100 possessions):

    dO = scale * sum_p (OBPM_p - repl_o) * (E[min_p tonight] - avg_min_p in sample) / 48
    dD = -scale * sum_p (DBPM_p - repl_d) * (E[min_p tonight] - avg_min_p in sample) / 48

`avg_min_p in sample` is the recency-weighted minutes per team game the player actually
gave during the period the team ratings were estimated from, so the adjustment only
captures *changes* relative to what the ratings already contain (no double counting).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..data.bbref import norm_name

REPL_O, REPL_D = -1.4, -0.6   # replacement level split (sums to -2.0 BPM)
W_RTG_O, W_RTG_D = 0.25, 0.30  # weight of individual ORtg/DRtg in the blended value


def enrich_with_ratings(adv: pd.DataFrame | None, per_poss: pd.DataFrame | None) -> pd.DataFrame | None:
    """Fold individual Offensive/Defensive Rating (Basketball-Reference, Dean Oliver method)
    into the box plus/minus value.

    Individual ORtg (points produced per 100 possessions used) is converted to points above
    average per 100 team possessions via (ORtg - lg) * USG%. Individual DRtg (points allowed
    per 100 while on court) gives (lg - DRtg). Each is mapped onto the OBPM/DBPM scale by a
    minutes-weighted regression across the league that season, then blended with BPM.
    """
    if adv is None or adv.empty:
        return adv
    df = adv.copy()
    for c in ("off_rtg", "def_rtg"):
        if c in df:
            df = df.drop(columns=c)
    if per_poss is not None and not per_poss.empty and "bbref_id" in per_poss:
        df = df.merge(per_poss[["bbref_id", "off_rtg", "def_rtg"]].drop_duplicates("bbref_id"),
                      on="bbref_id", how="left")
    else:
        df["off_rtg"] = np.nan
        df["def_rtg"] = np.nan
    df["obpm_box"], df["dbpm_box"] = df["obpm"], df["dbpm"]
    m = (df["mp"] >= 250) & df["off_rtg"].notna() & df["def_rtg"].notna() & df["usg_pct"].notna()
    if m.sum() >= 60:
        w = np.sqrt(df.loc[m, "mp"].to_numpy())
        lg_o = float(np.average(df.loc[m, "off_rtg"], weights=df.loc[m, "mp"]))
        lg_d = float(np.average(df.loc[m, "def_rtg"], weights=df.loc[m, "mp"]))
        xo = (df["off_rtg"] - lg_o) * df["usg_pct"] / 100.0
        xd = lg_d - df["def_rtg"]
        bo = np.polyfit(xo[m], df.loc[m, "obpm"], 1, w=w)
        bd = np.polyfit(xd[m], df.loc[m, "dbpm"], 1, w=w)
        o_rtg = np.polyval(bo, xo)
        d_rtg = np.polyval(bd, xd)
        ok = df["off_rtg"].notna() & df["def_rtg"].notna() & df["usg_pct"].notna() & (df["mp"] >= 100)
        df.loc[ok, "obpm"] = (1 - W_RTG_O) * df.loc[ok, "obpm_box"] + W_RTG_O * o_rtg[ok]
        df.loc[ok, "dbpm"] = (1 - W_RTG_D) * df.loc[ok, "dbpm_box"] + W_RTG_D * d_rtg[ok]
        df["bpm_blend"] = df["obpm"] + df["dbpm"]
    return df


def build_values(prev: pd.DataFrame | None, cur: pd.DataFrame | None, id_names: dict[str, str],
                 prior_minutes: float = 600.0, prev2: pd.DataFrame | None = None) -> dict[str, dict]:
    """Map ESPN athlete id -> {bpm, obpm, dbpm, ...} from Basketball-Reference tables."""
    def index(df):
        if df is None or df.empty:
            return {}
        out = {}
        for r in df.itertuples(index=False):
            out.setdefault(r.key, r)
        return out

    ip, ic, ip2 = index(prev), index(cur), index(prev2)
    vals = {}
    for pid, name in id_names.items():
        k = norm_name(name)
        p, c, p2 = ip.get(k), ic.get(k), ip2.get(k)
        # prior: last season, else two seasons ago, else rookie/unknown prior
        if p is not None and (p.mp or 0) >= 150:
            po, pd_, pm = float(p.obpm), float(p.dbpm), float(p.mp)
        elif p2 is not None and (p2.mp or 0) >= 150:
            po, pd_, pm = 0.8 * float(p2.obpm), 0.8 * float(p2.dbpm), float(p2.mp)
        else:
            po, pd_, pm = -1.2, -0.6, 0.0
        pw = prior_minutes * min(1.0, 0.4 + pm / 1500.0) if pm else prior_minutes * 0.5
        if c is not None and not np.isnan(c.obpm if c.obpm is not None else np.nan):
            cm = float(c.mp or 0)
            o = (cm * float(c.obpm) + pw * po) / (cm + pw)
            d = (cm * float(c.dbpm) + pw * pd_) / (cm + pw)
            extra = {"per": _f(c.per), "ts_pct": _f(c.ts_pct), "usg_pct": _f(c.usg_pct),
                     "ws_per_48": _f(c.ws_per_48), "vorp": _f(c.vorp), "bpm_cur": _f(c.bpm),
                     "obpm_cur": _f(getattr(c, "obpm_box", c.obpm)), "dbpm_cur": _f(getattr(c, "dbpm_box", c.dbpm)),
                     "ortg": _f(getattr(c, "off_rtg", None)), "drtg": _f(getattr(c, "def_rtg", None)),
                     "mp_cur": cm}
        else:
            o, d, extra = po, pd_, {}
        vals[str(pid)] = {"name": name, "obpm": o, "dbpm": d, "bpm": o + d,
                          "obpm_prior": po, "dbpm_prior": pd_, **extra}
    return vals


def prior_values(prev: pd.DataFrame | None, id_names: dict[str, str]) -> dict[str, dict]:
    """Prior-season-only values (no current-season information) for talent priors."""
    if prev is None or prev.empty:
        return {}
    idx = {}
    for r in prev.itertuples(index=False):
        idx.setdefault(r.key, r)
    out = {}
    for pid, name in id_names.items():
        r = idx.get(norm_name(name))
        if r is not None and (r.mp or 0) >= 150:
            # shrink small samples toward replacement-ish prior
            w = min(1.0, float(r.mp) / 1200.0)
            out[str(pid)] = {"obpm": w * float(r.obpm) + (1 - w) * -1.2,
                             "dbpm": w * float(r.dbpm) + (1 - w) * -0.6}
    return out


def _f(x):
    try:
        x = float(x)
        return None if np.isnan(x) else x
    except (TypeError, ValueError):
        return None


def rotation(pg_season: pd.DataFrame, team: str, asof: pd.Timestamp, half_life_games: float = 8.0,
             window: int = 25) -> pd.DataFrame:
    """Recency-weighted minutes profile of players whose latest team is `team`."""
    df = pg_season[pg_season["date"] < asof]
    if df.empty:
        return pd.DataFrame()
    latest = df.sort_values("date").groupby("id")["team"].last()
    roster_ids = set(latest[latest == team].index)
    tgames = df[df["team"] == team][["game_id", "date"]].drop_duplicates().sort_values("date")
    tgames = tgames.tail(window)
    if tgames.empty:
        return pd.DataFrame()
    n = len(tgames)
    gw = 0.5 ** ((n - 1 - np.arange(n)) / half_life_games)
    gidx = {g: i for i, g in enumerate(tgames["game_id"])}
    sub = df[(df["team"] == team) & df["game_id"].isin(gidx) & df["id"].isin(roster_ids)]
    rows = []
    for pid, g in sub.groupby("id"):
        played = np.zeros(n)
        mins = np.zeros(n)
        for r in g.itertuples(index=False):
            i = gidx[r.game_id]
            played[i] = 1
            mins[i] = r.min
        avg_min = float((gw * mins).sum() / gw.sum())
        typ = float((gw * mins * played).sum() / max((gw * played).sum(), 1e-9))
        last3 = int(played[-3:].sum()) if n >= 3 else int(played.sum())
        rows.append({"id": pid, "name": g["name"].iloc[-1], "avg_min": avg_min, "typ_min": typ,
                     "gp": int(played.sum()), "last3": last3, "starter_rate": float(g["starter"].mean())})
    # players traded in: count from first game with team
    return pd.DataFrame(rows).sort_values("avg_min", ascending=False).reset_index(drop=True)


def availability_adjustment(rot: pd.DataFrame, values: dict[str, dict], play_prob: dict[str, float],
                            params: dict, unlisted_absent_prob: float = 0.35) -> tuple[float, float, list[dict]]:
    """Returns (dO, dD, detail rows). `play_prob` maps ESPN id -> P(plays tonight);
    players not present default to 1.0 (or `unlisted_absent_prob` if they missed the last 3)."""
    if rot is None or rot.empty:
        return 0.0, 0.0, []
    scale = params["injury_scale"]
    dO = dD = 0.0
    detail = []
    for r in rot.itertuples(index=False):
        if r.typ_min < 6 and r.avg_min < 4:
            continue
        pid = str(r.id)
        if pid in play_prob:
            pp = play_prob[pid]
        elif r.last3 == 0 and r.typ_min >= 10:
            pp = unlisted_absent_prob
        else:
            pp = 1.0
        exp_min = pp * r.typ_min
        dmin = exp_min - r.avg_min
        if abs(dmin) < 0.5:
            continue
        v = values.get(pid, {"obpm": -1.2, "dbpm": -0.6, "bpm": -1.8})
        o = scale * (v["obpm"] - REPL_O) * dmin / 48
        d = -scale * (v["dbpm"] - REPL_D) * dmin / 48
        dO += o
        dD += d
        detail.append({"id": pid, "name": r.name, "play_prob": round(pp, 2), "typ_min": round(r.typ_min, 1),
                       "avg_min": round(r.avg_min, 1), "bpm": round(v.get("bpm", v["obpm"] + v["dbpm"]), 1),
                       "net_impact": round(o - d, 2)})
    dO = float(np.clip(dO, -9, 9))
    dD = float(np.clip(dD, -9, 9))
    detail.sort(key=lambda x: x["net_impact"])
    return dO, dD, detail
