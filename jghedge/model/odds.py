"""Odds math: conversions, vig removal, normal distribution helpers (NumPy only)."""
from __future__ import annotations

import math

import numpy as np


def american_to_decimal(a: float) -> float:
    return 1 + (a / 100 if a > 0 else 100 / -a)


def decimal_to_american(d: float) -> float:
    if d <= 1:
        return float("nan")
    return (d - 1) * 100 if d >= 2 else -100 / (d - 1)


def implied(a: float) -> float:
    return 1 / american_to_decimal(a)


def fmt_american(a: float | None) -> str:
    if a is None or (isinstance(a, float) and math.isnan(a)):
        return "—"
    a = round(a)
    return f"+{a}" if a > 0 else str(a)


def novig_multiplicative(a1: float, a2: float) -> tuple[float, float]:
    p1, p2 = implied(a1), implied(a2)
    s = p1 + p2
    return p1 / s, p2 / s


def novig_power(a1: float, a2: float, iters: int = 60) -> tuple[float, float]:
    """Power method: find k with p1^k + p2^k = 1. Removes more vig from longshots,
    matching the favourite-longshot bias observed in moneyline markets."""
    p1, p2 = implied(a1), implied(a2)
    lo, hi = 1.0, 3.0
    for _ in range(iters):
        k = (lo + hi) / 2
        s = p1 ** k + p2 ** k
        if s > 1:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    q1 = p1 ** k
    return q1, 1 - q1


def overround(a1: float, a2: float) -> float:
    return implied(a1) + implied(a2) - 1


# ---------------------------------------------------------- normal helpers
def norm_cdf(x):
    x = np.asarray(x, dtype=float)
    return 0.5 * (1 + _erf(x / math.sqrt(2)))


def _erf(x):
    # Abramowitz & Stegun 7.1.26 (|err| < 1.5e-7)
    s = np.sign(x)
    x = np.abs(x)
    t = 1 / (1 + 0.3275911 * x)
    y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t
             + 0.254829592) * t * np.exp(-x * x)
    return s * y


def norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation, |rel err| < 1.2e-9)."""
    if p <= 0:
        return -math.inf
    if p >= 1:
        return math.inf
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]
    plow = 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > 1 - plow:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def logit(p):
    p = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def inv_logit(x):
    return 1 / (1 + np.exp(-np.asarray(x, dtype=float)))


# ------------------------------------------------- market-implied centres
def market_margin(spread_home: float | None, home_price: float | None, away_price: float | None,
                  sd: float, ml_home: float | None = None, ml_away: float | None = None) -> float | None:
    """Expected home margin implied by the spread (price-skew adjusted); falls back to moneyline."""
    if spread_home is not None and not _nan(spread_home):
        m = -spread_home
        if home_price and away_price and not _nan(home_price) and not _nan(away_price):
            p_cover, _ = novig_multiplicative(home_price, away_price)
            # P(margin > -spread) = p_cover  =>  mu = -spread + sd * z(p_cover)
            m = -spread_home + sd * norm_ppf(p_cover)
        return m
    if ml_home and ml_away and not _nan(ml_home) and not _nan(ml_away):
        ph, _ = novig_power(ml_home, ml_away)
        return sd * norm_ppf(ph)
    return None


def market_total(total: float | None, over_price: float | None, under_price: float | None,
                 sd: float) -> float | None:
    if total is None or _nan(total):
        return None
    if over_price and under_price and not _nan(over_price) and not _nan(under_price):
        p_over, _ = novig_multiplicative(over_price, under_price)
        return total + sd * norm_ppf(p_over)
    return total


def _nan(x) -> bool:
    return isinstance(x, float) and math.isnan(x)
