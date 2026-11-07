"""Monte Carlo game simulation.

Margin and total residuals are drawn from the *empirical* walk-forward residual
distribution (quantile tables fit by the backtest), joined by a Gaussian copula. NBA
margins have fatter tails and a denser centre than a normal with the same RMSE, which
matters for moneyline and alternate-line pricing. Scores are rounded; ties go to overtime.
"""
from __future__ import annotations

import numpy as np

QGRID = np.linspace(0.0, 1.0, 201)


def _gauss_quantiles(sd: float) -> np.ndarray:
    from .odds import norm_ppf
    q = np.array([norm_ppf(min(max(u, 1e-4), 1 - 1e-4)) for u in QGRID])
    return q * sd


class GameSim:
    def __init__(self, m_final: float, t_final: float, params: dict, n: int, rng: np.random.Generator):
        mq = np.asarray(params.get("margin_resid_q") or _gauss_quantiles(params["margin_sd"]))
        tq = np.asarray(params.get("total_resid_q") or _gauss_quantiles(params["total_sd"]))
        rho = float(params.get("margin_total_corr", 0.0))
        z1 = rng.standard_normal(n)
        z2 = rho * z1 + np.sqrt(max(1 - rho ** 2, 0)) * rng.standard_normal(n)
        from .odds import norm_cdf
        u1, u2 = norm_cdf(z1), norm_cdf(z2)
        margin = m_final + np.interp(u1, QGRID, mq)
        total = t_final + np.interp(u2, QGRID, tq)
        hs = np.rint((total + margin) / 2)
        as_ = np.rint((total - margin) / 2)
        tie = hs == as_
        if tie.any():
            ot = rng.integers(0, 2, tie.sum()) * 2 - 1
            hs[tie] += np.where(ot > 0, rng.integers(1, 6, tie.sum()), 0)
            as_[tie] += np.where(ot < 0, rng.integers(1, 6, tie.sum()), 0)
        self.hs, self.as_ = hs, as_
        self.margin = hs - as_
        self.total = hs + as_
        self.n = n

    # payoff per $1 staked for every draw: win -> dec-1, push -> 0, loss -> -1
    def ml(self, side: str, dec: float) -> np.ndarray:
        win = self.margin > 0 if side == "home" else self.margin < 0
        return np.where(win, dec - 1, -1.0)

    def ml_anchored(self, side: str, dec: float, p_home: float, rng: np.random.Generator) -> np.ndarray:
        """Moneyline payoff whose win rate equals `p_home` (market-anchored), keeping the
        draw-by-draw ordering of the simulated margin so correlations with other bets survive."""
        jit = self.margin + rng.random(self.n) - 0.5
        thr = float(np.quantile(jit, 1 - p_home))
        home_win = jit > thr
        win = home_win if side == "home" else ~home_win
        return np.where(win, dec - 1, -1.0)

    def spread(self, side: str, line: float, dec: float) -> np.ndarray:
        v = (self.margin + line) if side == "home" else (-self.margin + line)
        return np.where(v > 0, dec - 1, np.where(v == 0, 0.0, -1.0))

    def total_bet(self, side: str, line: float, dec: float) -> np.ndarray:
        v = (self.total - line) if side == "over" else (line - self.total)
        return np.where(v > 0, dec - 1, np.where(v == 0, 0.0, -1.0))


def win_prob(payoff: np.ndarray) -> float:
    """P(win) treating pushes as half."""
    return float(np.mean(payoff > 0) + 0.5 * np.mean(payoff == 0))
