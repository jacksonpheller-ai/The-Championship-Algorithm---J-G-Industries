"""Player prop model.

Projection per player and stat k (pts, reb, ast, 3PM, stl, blk, tov):

    E[k] = E[minutes] * rate_k * opp_factor_k * pace_factor * usage_bump_k

* rate_k: recency-weighted per-minute production (this season + last season at reduced
  weight), shrunk toward the league per-minute rate with `K` pseudo-minutes.
* E[minutes]: recency-weighted minutes when playing, times P(plays), plus a share of
  minutes vacated by injured teammates.
* opp_factor_k: how much of stat k the opponent allows per 100 possessions vs league
  (points use the opponent's adjusted defensive rating), shrunk toward 1.
* usage_bump: vacated scoring/creation from absent teammates partly redistributed.

Simulation draws a minutes shock (and blowout-driven minute cuts from the simulated
margin), scales by the simulated team score, then samples each stat from a negative
binomial (gamma-Poisson) so variance exceeds the mean as it does in real box scores.
Combos (PRA, P+R, P+A, R+A) are summed inside each draw, so their correlations are
preserved, and props are correlated with the game sim for same-game parlay pricing.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

STATS = ["pts", "reb", "ast", "fg3m", "stl", "blk", "tov"]
COMBOS = {"pra": ("pts", "reb", "ast"), "pr": ("pts", "reb"), "pa": ("pts", "ast"), "ra": ("reb", "ast")}
TEAM_BETA = {"pts": 1.0, "ast": 0.8, "fg3m": 0.9, "reb": 0.25, "stl": 0.2, "blk": 0.2, "tov": 0.3}
DEFAULT_DISPERSION = {"pts": 9.0, "reb": 9.0, "ast": 7.0, "fg3m": 5.0, "stl": 14.0, "blk": 5.0, "tov": 9.0}
SHRINK_MIN = 120.0


def league_rates(pg: pd.DataFrame) -> dict[str, float]:
    m = pg["min"].sum()
    return {k: float(pg[k].sum() / m) for k in STATS}


def opponent_factors(tg: pd.DataFrame, season: int, asof: pd.Timestamp, half_life: float = 40.0) -> dict:
    """team -> stat -> multiplier for players FACING that team."""
    df = tg[(tg["season"] == season) & (tg["date"] < asof)].copy()
    if df.empty:
        return {}
    df["w"] = 0.5 ** ((asof - df["date"]).dt.days / half_life)
    # stats allowed by `team` = opponent box columns in the team's rows
    allowed = {
        "reb": (df["opp_oreb"] + df["opp_dreb"]) / df["poss"],
        "ast": df["opp_ast"] / df["poss"],
        "fg3m": df["opp_fg3m"] / df["poss"],
        "stl": df["tov"] / df["poss"],          # steals by players facing team ~ team's turnovers
        "blk": df["fga"] / df["poss"],          # blocks by players facing team ~ team's shot volume
        "tov": df["opp_tov"] / df["poss"],
    }
    out: dict = {}
    lg = {k: float(np.average(v, weights=df["w"])) for k, v in allowed.items()}
    for team, g in df.groupby("team"):
        n = len(g)
        shrink = n / (n + 12.0)
        out[team] = {}
        for k, v in allowed.items():
            raw = float(np.average(v.loc[g.index], weights=g["w"])) / lg[k]
            out[team][k] = 1 + (raw - 1) * shrink
    return out


def player_rates(logs: pd.DataFrame, lg: dict, season: int, half_life_games: float = 20.0) -> dict:
    """Per-minute rates from a player's game log (sorted newest first internally)."""
    g = logs.sort_values("date", ascending=False)
    k = np.arange(len(g))
    w = 0.5 ** (k / half_life_games) * np.where(g["season"].to_numpy() == season, 1.0, 0.35)
    mins = g["min"].to_numpy()
    wm = float((w * mins).sum())
    rates = {}
    for s in STATS:
        rates[s] = float(((w * g[s].to_numpy()).sum() + SHRINK_MIN * lg[s]) / (wm + SHRINK_MIN))
    return rates


class PropModel:
    def __init__(self, pg: pd.DataFrame, tg: pd.DataFrame, season: int, asof: pd.Timestamp, params: dict):
        self.season, self.asof, self.params = season, asof, params
        hist = pg[(pg["date"] < asof) & (pg["season"].isin([season, season - 1]))]
        self.hist = hist
        self.lg = league_rates(hist[hist["min"] >= 10]) if len(hist) else {k: 0.0 for k in STATS}
        self.opp = opponent_factors(tg, season, asof)
        self.disp = {**DEFAULT_DISPERSION, **(params.get("prop_dispersion") or {})}
        self._by_player = {pid: g for pid, g in hist.groupby("id")}

    def project_team(self, rot: pd.DataFrame, team: str, opp: str, play_prob: dict[str, float],
                     pace_factor: float, opp_D: float, mu: float) -> list[dict]:
        """Project every rotation player of `team` for tonight."""
        if rot is None or rot.empty:
            return []
        players = []
        for r in rot.itertuples(index=False):
            pid = str(r.id)
            logs = self._by_player.get(pid)
            if logs is None or r.typ_min < 8:
                continue
            pp = play_prob.get(pid, 1.0 if r.last3 > 0 else 0.35)
            players.append({"id": pid, "name": r.name, "team": team, "opp": opp, "play_prob": pp,
                            "typ_min": float(r.typ_min), "avg_min": float(r.avg_min),
                            "starter": r.starter_rate >= 0.5,
                            "rates": player_rates(logs, self.lg, self.season)})
        if not players:
            return []
        # Minutes vacated *relative to the recent baseline*: teammates' recent minutes already
        # reflect long-term absences, so only tonight's change is redistributed.
        def vac(p):
            return max(0.0, p["avg_min"] - p["play_prob"] * p["typ_min"])
        lost_min = sum(vac(p) for p in players)
        lost_pts = sum(vac(p) * p["rates"]["pts"] for p in players)
        lost_ast = sum(vac(p) * p["rates"]["ast"] for p in players)
        healthy = [p for p in players if p["play_prob"] >= 0.5]
        tot_typ = sum(p["typ_min"] for p in healthy) or 1.0
        team_pts = sum(p["play_prob"] * p["typ_min"] * p["rates"]["pts"] for p in players) or 1.0
        recovered = 0.0
        for p in players:
            boost = 0.0
            if p in healthy:
                boost = min(lost_min * p["typ_min"] / tot_typ, 0.22 * p["typ_min"], max(0.0, 40 - p["typ_min"]))
            p["exp_min"] = p["typ_min"] + boost
            recovered += p["play_prob"] * boost * p["rates"]["pts"]
        residual_pts_share = max(0.0, lost_pts - recovered) / team_pts
        ast_share = lost_ast / max(sum(p["typ_min"] * p["rates"]["ast"] for p in players), 1.0)
        usage = {"pts": 1 + 0.55 * residual_pts_share, "fg3m": 1 + 0.45 * residual_pts_share,
                 "ast": 1 + 0.35 * ast_share}
        of = self.opp.get(opp, {})
        pts_factor = (mu + opp_D) / mu if mu else 1.0
        bias = self.params.get("prop_bias") or {}
        # empirical projection correction from the prop backtest, applied at 60% strength
        bfac = {s: 1 + 0.6 * (float(bias.get(s, 1.0)) - 1) for s in STATS}
        for p in players:
            mean = {}
            for s in STATS:
                f = pts_factor if s == "pts" else of.get(s, 1.0)
                mean[s] = p["exp_min"] * p["rates"][s] * f * pace_factor * usage.get(s, 1.0) * bfac[s]
            p["mean"] = mean
            p["exp_line"] = {**{s: round(mean[s], 2) for s in STATS},
                             **{c: round(sum(mean[x] for x in parts), 2) for c, parts in COMBOS.items()}}
        return players

    def simulate(self, players: list[dict], team_pts_draw: np.ndarray, team_pts_exp: float,
                 margin_for_team: np.ndarray, rng: np.random.Generator) -> dict[str, dict[str, np.ndarray]]:
        n = len(team_pts_draw)
        cv = self.params.get("prop_minutes_cv", 0.14)
        tf = np.clip(team_pts_draw / max(team_pts_exp, 1.0), 0.6, 1.5)
        blow = np.abs(margin_for_team)
        out = {}
        for p in players:
            plays = rng.random(n) < p["play_prob"]
            mf = np.clip(rng.normal(1.0, cv, n), 0.35, 1.35)
            if p["starter"]:
                mf = mf * np.clip(1 - 0.012 * np.maximum(blow - 15, 0), 0.72, 1.0)
            sims = {}
            for s in STATS:
                mu = p["mean"][s] * mf * tf ** TEAM_BETA[s]
                mu = np.maximum(mu, 1e-6)
                r = self.disp[s]
                lam = rng.gamma(r, mu / r)
                sims[s] = np.where(plays, rng.poisson(lam), 0).astype(np.int16)
            for c, parts in COMBOS.items():
                sims[c] = sum(sims[x] for x in parts).astype(np.int16)
            sims["_plays"] = plays
            out[p["id"]] = sims
        return out
