"""Integer-unit (small bankroll) Kelly allocation.

A $10 bankroll cannot hold thirty $0.04 positions. Stakes must be whole units (say $0.50),
so the question becomes: which few bets, and how many units each, maximise expected log
growth? Fractional Kelly is implemented as "bet as if the bankroll were k x B": a unit is
`unit / (k * B)` of the effective bankroll, and a unit is only added while it still raises
E[log(1 + R f)] on the joint simulation. Correlated legs (same game, parlay sharing a leg)
are handled naturally because growth is evaluated on the joint draws.

The dashboard runs the same greedy with a quadratic (mean/covariance) approximation so the
ticket re-optimises instantly when the bankroll, unit or selection changes.
"""
from __future__ import annotations

import numpy as np


def discrete_allocate(R: np.ndarray, bankroll: float, unit: float, kelly: float,
                      max_bet: float, max_total: float, max_steps: int = 600) -> np.ndarray:
    """Return integer units per bet (columns of R: payoff per $1 staked, one row per draw)."""
    n_bets = R.shape[1] if R.ndim == 2 else 0
    units = np.zeros(n_bets, dtype=int)
    if n_bets == 0 or bankroll <= 0 or unit <= 0:
        return units
    b_eff = max(bankroll * kelly, 1e-9)
    fu = unit / b_eff                                   # fraction of effective bankroll per unit
    cap_each = int(np.floor(max_bet * bankroll / unit + 1e-9))
    cap_total = int(np.floor(max_total * bankroll / unit + 1e-9))
    if cap_each < 1 or cap_total < 1:
        return units
    w = np.ones(R.shape[0])
    base = 0.0
    for _ in range(min(cap_total, max_steps)):
        cand = np.flatnonzero(units < cap_each)
        if cand.size == 0:
            break
        trial = w[:, None] + fu * R[:, cand]
        gains = np.log(np.maximum(trial, 1e-9)).mean(axis=0) - base
        j = int(np.argmax(gains))
        if gains[j] <= 1e-7:
            break
        units[cand[j]] += 1
        w = trial[:, j]
        base = float(np.log(np.maximum(w, 1e-9)).mean())
    return units


def moments(R: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Mean payoff per $ and covariance matrix for the client-side optimiser."""
    if R.ndim != 2 or R.shape[1] == 0:
        return np.zeros(0), np.zeros((0, 0))
    mu = R.mean(axis=0)
    cov = np.cov(R, rowvar=False, bias=True)
    cov = np.atleast_2d(cov)
    return mu, cov


def quadratic_allocate(mu: np.ndarray, cov: np.ndarray, bankroll: float, unit: float, kelly: float,
                       max_bet: float, max_total: float) -> np.ndarray:
    """Reference implementation of the dashboard's greedy (used by tests to check agreement)."""
    n = len(mu)
    units = np.zeros(n, dtype=int)
    if n == 0 or bankroll <= 0 or unit <= 0:
        return units
    du = unit / bankroll
    cap_each = int(np.floor(max_bet * bankroll / unit + 1e-9))
    cap_total = int(np.floor(max_total * bankroll / unit + 1e-9))
    f = np.zeros(n)
    for _ in range(cap_total):
        sig_f = cov @ f
        gains = du * (mu - sig_f / kelly) - du * du * np.diag(cov) / (2 * kelly)
        gains[units >= cap_each] = -np.inf
        j = int(np.argmax(gains))
        if gains[j] <= 0:
            break
        units[j] += 1
        f[j] += du
    return units
