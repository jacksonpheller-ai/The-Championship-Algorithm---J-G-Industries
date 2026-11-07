"""Opponent-adjusted, recency-weighted team efficiency ratings.

Weighted ridge regression on every team-game of the season:

    ORtg(team i vs j) = mu + O_i + D_j + h * venue_i + e
    Poss(i vs j)      = mu_p + P_i + P_j + e

O_i: points/100 created above average (higher = better offence).
D_j: points/100 allowed above average (lower = better defence).
P_i: possessions/game above average.

Each game is weighted by 0.5 ** (age_days / half_life). Every team coefficient is shrunk
toward a prior (last season's ratings regressed to the mean, blended with a roster-talent
prior built from player BPM and current minutes), with prior strength `rating_ridge`
measured in games. Early in a season the prior dominates; by ~20 games the data does.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..teams import TEAMS

ABBRS = list(TEAMS)
IDX = {a: i for i, a in enumerate(ABBRS)}
N = len(ABBRS)


def _solve(X, y, w):
    W = w[:, None]
    A = X.T @ (X * W)
    b = X.T @ (y * w)
    return np.linalg.solve(A + 1e-9 * np.eye(A.shape[0]), b)


def fit_ratings(tg: pd.DataFrame, asof: pd.Timestamp, season: int, params: dict,
                prior: dict[str, dict] | None = None, hca_prior: float | None = None) -> dict:
    """Ratings using only games of `season` strictly before `asof`."""
    prior = prior or {}
    df = tg[(tg["season"] == season) & (tg["date"] < asof)]
    hl = params["rating_half_life_days"]
    lam = params["rating_ridge"]
    games = {a: 0 for a in ABBRS}

    # ---------- efficiency
    rows_X, rows_y, rows_w = [], [], []
    if len(df):
        age = (asof - df["date"]).dt.days.to_numpy()
        w = 0.5 ** (age / hl)
        w_games = w
        ti = df["team"].map(IDX).to_numpy()
        oj = df["opp"].map(IDX).to_numpy()
        venue = np.where(df["neutral"].to_numpy(), 0.0, np.where(df["home"].to_numpy(), 0.5, -0.5))
        n = len(df)
        X = np.zeros((n, 2 + 2 * N))
        X[:, 0] = 1
        X[:, 1] = venue
        X[np.arange(n), 2 + ti] = 1
        X[np.arange(n), 2 + N + oj] = 1
        rows_X.append(X)
        rows_y.append(df["ortg"].to_numpy())
        rows_w.append(w)
        for a, c in df["team"].value_counts().items():
            games[a] = int(c)
    # priors
    P = np.zeros((2 * N + 2, 2 + 2 * N))
    py = np.zeros(2 * N + 2)
    pw = np.full(2 * N + 2, lam)
    for a, i in IDX.items():
        P[i, 2 + i] = 1
        py[i] = prior.get(a, {}).get("O", 0.0)
        P[N + i, 2 + N + i] = 1
        py[N + i] = prior.get(a, {}).get("D", 0.0)
    # HCA prior (strong: estimated across seasons), weak league-mean prior
    hca_eff = hca_prior if hca_prior is not None else params["hca_points"]
    P[2 * N, 1] = 1
    py[2 * N] = hca_eff
    pw[2 * N] = 400.0
    P[2 * N + 1, 0] = 1
    py[2 * N + 1] = 114.0
    pw[2 * N + 1] = 0.5
    # sum-to-zero constraints (identifiability)
    C = np.zeros((2, 2 + 2 * N))
    C[0, 2:2 + N] = 1
    C[1, 2 + N:] = 1
    X = np.vstack(rows_X + [P, C])
    y = np.concatenate(rows_y + [py, np.zeros(2)])
    w = np.concatenate(rows_w + [pw, np.full(2, 1e4)])
    sol = _solve(X, y, w)
    mu, h = sol[0], sol[1]
    O, D = sol[2:2 + N], sol[2 + N:]

    # ---------- pace
    if len(df):
        n = len(df)
        Xp = np.zeros((n, 1 + N))
        Xp[:, 0] = 1
        Xp[np.arange(n), 1 + ti] += 1
        Xp[np.arange(n), 1 + oj] += 1
        yp = df["poss"].to_numpy()
        wp = w_games
    else:
        Xp, yp, wp = np.zeros((0, 1 + N)), np.zeros(0), np.zeros(0)
    Pp = np.zeros((N + 1, 1 + N))
    pyp = np.zeros(N + 1)
    pwp = np.full(N + 1, lam)
    for a, i in IDX.items():
        Pp[i, 1 + i] = 1
        pyp[i] = prior.get(a, {}).get("P", 0.0)
    Pp[N, 0] = 1
    pyp[N] = 99.0
    pwp[N] = 0.5
    Cp = np.zeros((1, 1 + N))
    Cp[0, 1:] = 1
    solp = _solve(np.vstack([Xp, Pp, Cp]), np.concatenate([yp, pyp, [0.0]]),
                  np.concatenate([wp, pwp, [1e4]]))
    mu_p, Pc = solp[0], solp[1:]

    teams = {a: {"O": float(O[i]), "D": float(D[i]), "P": float(Pc[i]),
                 "net": float(O[i] - D[i]), "games": games[a]} for a, i in IDX.items()}
    return {"mu": float(mu), "hca": float(h), "mu_pace": float(mu_p), "teams": teams,
            "asof": str(asof.date()), "n_games": int(len(df) // 2)}


def season_end_ratings(tg: pd.DataFrame, season: int, params: dict) -> dict:
    p = dict(params)
    p["rating_ridge"] = 1.0
    p["rating_half_life_days"] = 120.0
    end = tg[tg["season"] == season]["date"].max()
    if pd.isna(end):
        return {}
    return fit_ratings(tg, end + pd.Timedelta(days=1), season, p)


def carry_prior(last: dict, carry: float) -> dict[str, dict]:
    if not last:
        return {}
    return {a: {"O": carry * t["O"], "D": carry * t["D"], "P": 0.7 * t["P"]}
            for a, t in last["teams"].items()}


def talent_prior(pg_season: pd.DataFrame, asof: pd.Timestamp, values: dict[str, dict],
                 half_life_days: float = 30.0) -> dict[str, dict]:
    """Roster-talent prior: minutes-weighted OBPM/DBPM of the players each team is using now.

    BPM is scaled so that a team's minutes-weighted sum approximates its net rating per 100
    (5 players on court). Unknown players (rookies, unmatched) are assigned a below-average
    prior. Output is centred across the league.
    """
    df = pg_season[pg_season["date"] < asof]
    if df.empty:
        return {}
    age = (asof - df["date"]).dt.days.to_numpy()
    df = df.assign(w=0.5 ** (age / half_life_days) * df["min"].to_numpy())
    # player's current team = latest team in the data
    last_team = df.sort_values("date").groupby("id")["team"].last()
    df = df[df["team"].to_numpy() == last_team.reindex(df["id"]).to_numpy()]
    out = {}
    for team, g in df.groupby("team"):
        mins = g.groupby("id")["w"].sum()
        share = mins / mins.sum() * 5
        o = d = 0.0
        for pid, s in share.items():
            v = values.get(str(pid))
            ob, db = (v["obpm"], v["dbpm"]) if v else (-1.2, -0.6)
            o += s * ob
            d += s * db
        out[team] = {"O": o, "D": -d}
    if out:
        mo = np.mean([v["O"] for v in out.values()])
        md = np.mean([v["D"] for v in out.values()])
        out = {k: {"O": v["O"] - mo, "D": v["D"] - md} for k, v in out.items()}
    return out


def combine_priors(carry: dict, talent: dict, games_played: dict[str, int]) -> dict[str, dict]:
    out = {}
    for a in ABBRS:
        c = carry.get(a, {"O": 0.0, "D": 0.0, "P": 0.0})
        t = talent.get(a)
        if t is None or games_played.get(a, 0) < 3:
            out[a] = dict(c)
        else:
            out[a] = {"O": 0.4 * c["O"] + 0.6 * t["O"], "D": 0.4 * c["D"] + 0.6 * t["D"], "P": c["P"]}
    return out
