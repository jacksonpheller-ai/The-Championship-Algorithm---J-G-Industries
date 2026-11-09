"""Monte Carlo game simulation.

Margin and total residuals are drawn from the *empirical* walk-forward residual
distribution (quantile tables fit by the backtest), joined by a Gaussian copula. NBA
margins have fatter tails and a denser centre than a normal with the same RMSE, which
matters for moneyline and alternate-line pricing. Scores are rounded; a tie is settled by one point.
"""
from __future__ import annotations

import numpy as np

QGRID = np.linspace(0.0, 1.0, 201)


def _gauss_quantiles(sd: float) -> np.ndarray:
    from .odds import norm_ppf
    q = np.array([norm_ppf(min(max(u, 1e-4), 1 - 1e-4)) for u in QGRID])
    return q * sd


_CENTRE_CACHE: dict = {}


def _balanced_centre(q: np.ndarray) -> float:
    """The shift c for which a draw r from the quantile table `q`, less c, plus the rounding error of two
    integer scores, is equally likely to be at least half a point up as half a point down. The tables are
    lumpy (final scores are whole numbers against half-point lines), so the plain median can sit anywhere
    inside a half-point step; balancing the rounded outcome is what makes an even-priced line 50/50."""
    key = (len(q), round(float(q[0]), 6), round(float(q[-1]), 6), round(float(q.sum()), 6))
    if key in _CENTRE_CACHE:
        return _CENTRE_CACHE[key]
    u = (np.arange(8001) + 0.5) / 8001
    r = np.interp(u, QGRID, q)

    def tail(x):            # P(e >= x), e the sum of two independent rounding errors uniform on (-0.5, 0.5)
        x = np.clip(x, -1.0, 1.0)
        return np.where(x <= 0, 1.0 - (1.0 + x) ** 2 / 2.0, (1.0 - x) ** 2 / 2.0)

    def gap(c):             # P(up half a point) - P(down half a point); falls as c rises
        return float(tail(0.5 - (r - c)).mean() - tail(0.5 + (r - c)).mean())

    mid = float(np.interp(0.5, QGRID, q))
    lo, hi = mid - 2.0, mid + 2.0
    for _ in range(60):
        c = (lo + hi) / 2.0
        if gap(c) > 0:
            lo = c
        else:
            hi = c
    _CENTRE_CACHE[key] = (lo + hi) / 2.0
    return _CENTRE_CACHE[key]


class GameSim:
    def __init__(self, m_final: float, t_final: float, params: dict, n: int, rng: np.random.Generator):
        mq = np.asarray(params.get("margin_resid_q") or _gauss_quantiles(params["margin_sd"]), dtype=float)
        tq = np.asarray(params.get("total_resid_q") or _gauss_quantiles(params["total_sd"]), dtype=float)
        # The centres passed in are 50/50 points (the market's line and price, blended with our number),
        # but the fitted tables are centred on their MEAN, and totals are right-skewed (overtime), so the
        # table's median sat about 0.4 points low and every Under started 0.4 points of probability ahead.
        # Re-centre each table so that a rounded draw is as likely to finish half a point above the centre
        # as half a point below (_balanced_centre): with no model weight the simulation then returns the
        # market's own no-vig probability (closing totals have landed over their centre 50.0% of the time).
        mq = mq - _balanced_centre(mq)
        tq = tq - _balanced_centre(tq)
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
            # A tied draw is a margin that rounded to zero. The residual tables already hold overtime
            # results, so the tie is settled by one point either way without moving the expected total:
            # the winner gains a point or the loser gives one up, each half the time. (Adding 1-5 points
            # to the winner, as before, put 0.3 points of probability on every Over and bent short spreads.)
            k = int(tie.sum())
            home = rng.integers(0, 2, k) > 0
            up = rng.integers(0, 2, k) > 0
            hs[tie] += np.where(home & up, 1, 0) - np.where(~home & ~up, 1, 0)
            as_[tie] += np.where(~home & up, 1, 0) - np.where(home & ~up, 1, 0)
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
