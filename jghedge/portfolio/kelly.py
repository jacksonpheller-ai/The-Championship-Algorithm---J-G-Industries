"""Simultaneous Kelly bankroll allocation over correlated bets.

Given a payoff matrix R (draws x bets; per $1 staked) from the joint Monte Carlo, find

    f* = argmax_f  E[ log(1 + R f) ]   s.t.  f >= 0,  sum(f) <= 1

by projected gradient ascent. Bets on the same game are correlated through the shared
simulation, so the optimiser automatically sizes down stacked exposure. The full-Kelly
vector is then scaled by a risk profile (fractional Kelly) and capped per bet and in total.
"""
from __future__ import annotations

import numpy as np

PROFILES = {
    "conservative": {"kelly": 0.15, "max_bet": 0.03, "max_total": 0.12, "label": "Conservative (0.15× Kelly)"},
    "balanced": {"kelly": 0.25, "max_bet": 0.05, "max_total": 0.20, "label": "Balanced (¼ Kelly)"},
    "aggressive": {"kelly": 0.50, "max_bet": 0.08, "max_total": 0.35, "label": "Aggressive (½ Kelly)"},
    "max_growth": {"kelly": 1.00, "max_bet": 0.15, "max_total": 0.50, "label": "Max growth (full Kelly)"},
}


def _project_simplex_cap(v: np.ndarray, cap: float = 1.0) -> np.ndarray:
    v = np.maximum(v, 0)
    s = v.sum()
    if s <= cap:
        return v
    # Euclidean projection onto {f >= 0, sum f = cap}
    u = np.sort(v)[::-1]
    css = np.cumsum(u)
    rho = np.nonzero(u * np.arange(1, len(u) + 1) > (css - cap))[0][-1]
    theta = (css[rho] - cap) / (rho + 1.0)
    return np.maximum(v - theta, 0)


def growth(R: np.ndarray, f: np.ndarray) -> float:
    w = 1 + R @ f
    if np.any(w <= 0):
        return -np.inf
    return float(np.mean(np.log(w)))


def optimal_kelly(R: np.ndarray, iters: int = 400) -> np.ndarray:
    n_bets = R.shape[1]
    if n_bets == 0:
        return np.zeros(0)
    ev = R.mean(axis=0)
    f = np.where(ev > 0, np.minimum(ev / np.maximum(R.var(axis=0), 1e-6), 0.2), 0.0)
    f = _project_simplex_cap(f, 0.95)
    g = growth(R, f)
    step = 0.05
    for _ in range(iters):
        w = 1 + R @ f
        grad = (R / w[:, None]).mean(axis=0)
        while step > 1e-7:
            cand = _project_simplex_cap(f + step * grad, 0.95)
            gc = growth(R, cand)
            if gc > g:
                f, g = cand, gc
                step *= 1.3
                break
            step *= 0.5
        else:
            break
    f[f < 1e-5] = 0.0
    return f


def allocate(R: np.ndarray, profile: dict) -> np.ndarray:
    f = optimal_kelly(R) * profile["kelly"]
    f = np.minimum(f, profile["max_bet"])
    tot = f.sum()
    if tot > profile["max_total"]:
        f *= profile["max_total"] / tot
    return f


def risk_summary(R: np.ndarray, f: np.ndarray) -> dict:
    if len(f) == 0 or f.sum() == 0:
        return {"exposure": 0.0, "exp_return": 0.0, "p_profit": 0.0, "p_loss": 0.0,
                "p5": 0.0, "p50": 0.0, "p95": 0.0, "max_loss": 0.0}
    ret = R @ f
    return {"exposure": float(f.sum()), "exp_return": float(ret.mean()),
            "p_profit": float(np.mean(ret > 1e-9)), "p_loss": float(np.mean(ret < -1e-9)),
            "p5": float(np.percentile(ret, 5)), "p50": float(np.percentile(ret, 50)),
            "p95": float(np.percentile(ret, 95)), "max_loss": float(-f.sum())}
