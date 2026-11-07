"""Game projection engine shared by the walk-forward backtest and the nightly run.

Projection for home team h vs away team a:

    pace      = mu_p + P_h + P_a
    ORtg_h    = mu + O_h + dO_h + D_a + dD_a + hca/2      (dO/dD = availability adjustments)
    pts_h     = ORtg_h * pace / 100                       (same for away, -hca/2)
    m_ratings = pts_h - pts_a + situational(rest, b2b, 3-in-4, travel, time zone, altitude)
    m_elo     = (Elo_h + HCA_elo - Elo_a) / elo_points_per_pt + availability + situational
    m_model   = w_r * m_ratings + (1 - w_r) * m_elo
    m_final   = w_m * m_model + (1 - w_m) * m_market      (market = price-adjusted spread)

Totals follow the same path with the market total. Weights and standard deviations are fit
out-of-sample by `jghedge.backtest`.
"""
from __future__ import annotations

import pandas as pd

from ..teams import TEAMS
from . import elo as elo_mod
from . import players as pl
from . import ratings as rt
from .odds import market_margin, market_total
from .situational import situational_margin


class Engine:
    def __init__(self, gdf: pd.DataFrame, tg: pd.DataFrame, pg: pd.DataFrame,
                 adv: dict[int, pd.DataFrame], team_adv_base: dict[int, pd.DataFrame], params: dict):
        self.gdf, self.tg, self.pg, self.adv, self.params = gdf, tg, pg, adv, params
        self.id_names = (pg.sort_values("date").groupby("id")["name"].last().to_dict()
                         if len(pg) else {})
        self.seasons = sorted(gdf["season"].dropna().unique().astype(int)) if len(gdf) else []
        self._season_end: dict[int, dict] = {}
        for s, tdf in team_adv_base.items():
            if tdf is not None and not tdf.empty:
                lg_o, lg_p = tdf["off_rtg"].mean(), tdf["pace"].mean()
                self._season_end[s] = {"teams": {
                    a: {"O": float(r.off_rtg - lg_o), "D": float(r.def_rtg - lg_o), "P": float(r.pace - lg_p)}
                    for a, r in tdf.iterrows() if a in TEAMS}}
        init = None
        first = self.seasons[0] if self.seasons else None
        if first and (first - 1) in team_adv_base and team_adv_base[first - 1] is not None:
            init = elo_mod.init_from_srs(team_adv_base[first - 1]["srs"].to_dict())
        self.elo_now, self.elo_pre = elo_mod.run_elo(gdf, params, init)
        self._prior_vals: dict[int, dict] = {}
        self._state_cache: dict = {}

    # ------------------------------------------------------------- state
    def season_end(self, season: int) -> dict:
        if season not in self._season_end:
            self._season_end[season] = rt.season_end_ratings(self.tg, season, self.params)
        return self._season_end[season]

    def prior_vals(self, season: int) -> dict:
        if season not in self._prior_vals:
            self._prior_vals[season] = pl.prior_values(self.adv.get(season - 1), self.id_names)
        return self._prior_vals[season]

    def injury_values(self, season: int, live: bool = False) -> dict:
        key = ("vals", season, live)
        if key not in self._state_cache:
            self._state_cache[key] = pl.build_values(
                self.adv.get(season - 1), self.adv.get(season) if live else None, self.id_names,
                self.params["bpm_prior_minutes"], prev2=self.adv.get(season - 2))
        return self._state_cache[key]

    def state(self, asof: pd.Timestamp, season: int) -> dict:
        key = (asof, season)
        if key in self._state_cache:
            return self._state_cache[key]
        carry = rt.carry_prior(self.season_end(season - 1), self.params["prior_season_carry"])
        pg_s = self.pg[self.pg["season"] == season]
        talent = rt.talent_prior(pg_s, asof, self.prior_vals(season))
        tg_s = self.tg[(self.tg["season"] == season) & (self.tg["date"] < asof)]
        gp = tg_s["team"].value_counts().to_dict()
        prior = rt.combine_priors(carry, talent, gp)
        st = rt.fit_ratings(self.tg, asof, season, self.params, prior,
                            hca_prior=self.params["hca_points"])
        st["prior"] = prior
        st["talent"] = talent
        self._state_cache[key] = st
        return st

    # ---------------------------------------------------------- predict
    def project(self, st: dict, home: str, away: str, neutral: bool, sit: dict | None,
                avail: dict | None = None, elo_pair: tuple[float, float] | None = None) -> dict:
        p = self.params
        th, ta = st["teams"][home], st["teams"][away]
        av = avail or {}
        dOh, dDh = av.get("dO_h", 0.0), av.get("dD_h", 0.0)
        dOa, dDa = av.get("dO_a", 0.0), av.get("dD_a", 0.0)
        pace = st["mu_pace"] + th["P"] + ta["P"]
        h = 0.0 if neutral else p["hca_points"]
        ortg_h = st["mu"] + th["O"] + dOh + ta["D"] + dDa + h / 2
        ortg_a = st["mu"] + ta["O"] + dOa + th["D"] + dDh - h / 2
        pts_h, pts_a = ortg_h * pace / 100, ortg_a * pace / 100
        sit_m = situational_margin(sit, p) if sit is not None else 0.0
        m_ratings = pts_h - pts_a + sit_m
        t_ratings = pts_h + pts_a + p.get("total_b2b_points", -0.8) * (
            (sit or {}).get("h_b2b", 0) + (sit or {}).get("a_b2b", 0))
        avail_m = ((dOh - dDh) - (dOa - dDa)) * pace / 100
        if elo_pair is None:
            elo_pair = (self.elo_now.get(home, elo_mod.BASE), self.elo_now.get(away, elo_mod.BASE))
        elo_diff = elo_pair[0] - elo_pair[1] + (0.0 if neutral else p["elo_hca"])
        m_elo = elo_diff / (100.0 / p["elo_points_per_100"]) + avail_m + sit_m
        wr = p["w_ratings"]
        return {
            "pace": pace, "pts_h": pts_h, "pts_a": pts_a, "m_ratings": m_ratings, "m_elo": m_elo,
            "m_model": wr * m_ratings + (1 - wr) * m_elo, "t_model": t_ratings,
            "sit_margin": sit_m, "avail_margin": avail_m, "elo_h": elo_pair[0], "elo_a": elo_pair[1],
            "ortg_h": ortg_h, "ortg_a": ortg_a,
        }

    def blend_with_market(self, proj: dict, odds: dict | None) -> dict:
        p = self.params
        o = (odds or {}).get("close") or {}
        mm = market_margin(o.get("spread_home"), o.get("spread_home_price"), o.get("spread_away_price"),
                           p["margin_sd"], o.get("ml_home"), o.get("ml_away"))
        mt = market_total(o.get("total"), o.get("over_price"), o.get("under_price"), p["total_sd"])
        wm, wt = p["w_model_vs_market_margin"], p["w_model_vs_market_total"]
        m = proj["m_model"] if mm is None else wm * proj["m_model"] + (1 - wm) * mm
        t = proj["t_model"] if mt is None else wt * proj["t_model"] + (1 - wt) * mt
        return {**proj, "m_market": mm, "t_market": mt, "m_final": m, "t_final": t}


def availability_from_box(engine: Engine, st_season_pg: pd.DataFrame, game_id: str, team: str,
                          asof: pd.Timestamp, values: dict) -> tuple[float, float]:
    """Backtest availability: players who actually appeared are 'available' (a proxy for the
    pre-game injury report, which identifies the large majority of absences)."""
    rot = pl.rotation(st_season_pg, team, asof)
    if rot.empty:
        return 0.0, 0.0
    played = set(st_season_pg.loc[(st_season_pg["game_id"] == game_id) & (st_season_pg["team"] == team), "id"])
    pp = {str(pid): (1.0 if pid in played else 0.0) for pid in rot["id"]}
    dO, dD, _ = pl.availability_adjustment(rot, values, pp, engine.params)
    return dO, dD
