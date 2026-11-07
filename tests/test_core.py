"""Core math tests. Run: python -m pytest -q  (or: python tests/test_core.py)"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from jghedge.ledger import _settle_leg  # noqa: E402
from jghedge.model import odds  # noqa: E402
from jghedge.model.simulate import GameSim, win_prob  # noqa: E402
from jghedge.portfolio import kelly  # noqa: E402
from jghedge.portfolio.parlay import build_parlays, leg_multiplier  # noqa: E402


def test_odds_conversions():
    assert math.isclose(odds.american_to_decimal(-110), 1.909090, rel_tol=1e-5)
    assert math.isclose(odds.american_to_decimal(150), 2.5)
    assert math.isclose(odds.decimal_to_american(2.5), 150)
    assert math.isclose(odds.decimal_to_american(1.5), -200)


def test_novig_sums_to_one_and_power_shades_longshot():
    a, b = odds.novig_multiplicative(-150, 130)
    assert math.isclose(a + b, 1.0)
    pa, pb = odds.novig_power(-400, 320)
    ma, mb = odds.novig_multiplicative(-400, 320)
    assert math.isclose(pa + pb, 1.0, rel_tol=1e-6)
    assert pb < mb  # power method removes more vig from the longshot


def test_normal_helpers_inverse():
    for p in (0.01, 0.2, 0.5, 0.8, 0.99):
        assert abs(float(odds.norm_cdf(odds.norm_ppf(p))) - p) < 1e-6


def test_market_margin_uses_price_skew():
    even = odds.market_margin(-5.5, -110, -110, 13.0)
    juiced = odds.market_margin(-5.5, -130, 110, 13.0)
    assert math.isclose(even, 5.5, abs_tol=1e-6)
    assert juiced > even  # home -5.5 at -130 implies a bigger expected margin


def test_single_bet_kelly_matches_closed_form():
    rng = np.random.default_rng(0)
    p, dec = 0.55, 2.0
    win = rng.random(200000) < p
    R = np.where(win, dec - 1, -1.0)[:, None]
    f = kelly.optimal_kelly(R)
    closed = (p * (dec - 1) - (1 - p)) / (dec - 1)   # 0.10
    assert abs(f[0] - closed) < 0.01


def test_kelly_zero_for_negative_ev():
    rng = np.random.default_rng(1)
    R = np.where(rng.random(50000) < 0.45, 1.0, -1.0)[:, None]
    assert kelly.optimal_kelly(R)[0] < 1e-3


def test_profile_caps():
    rng = np.random.default_rng(2)
    R = np.column_stack([np.where(rng.random(40000) < 0.6, 1.0, -1.0) for _ in range(5)])
    prof = kelly.PROFILES["balanced"]
    f = kelly.allocate(R, prof)
    assert f.max() <= prof["max_bet"] + 1e-9
    assert f.sum() <= prof["max_total"] + 1e-9


def test_parlay_push_shrinks_payout():
    pay = np.array([0.91, 0.0, -1.0])
    assert list(leg_multiplier(pay, 1.91)) == [1.91, 1.0, 0.0]


def test_parlays_only_cross_game():
    n = 1000
    legs = [{"id": "a", "game_id": "g1", "ev": 0.05, "p_final": 0.55, "dec": 1.91},
            {"id": "b", "game_id": "g1", "ev": 0.05, "p_final": 0.55, "dec": 1.91},
            {"id": "c", "game_id": "g2", "ev": 0.05, "p_final": 0.55, "dec": 1.91}]
    rng = np.random.default_rng(3)
    pay = {l["id"]: np.where(rng.random(n) < 0.56, 0.91, -1.0) for l in legs}
    for p in build_parlays(legs, pay, max_legs=3):
        games = [next(l["game_id"] for l in legs if l["id"] == x) for x in p["legs"]]
        assert len(set(games)) == len(games)


def test_game_sim_centres_and_settles():
    params = {"margin_sd": 13.0, "total_sd": 18.0, "margin_total_corr": 0.0}
    sim = GameSim(5.0, 225.0, params, 40000, np.random.default_rng(4))
    assert abs(sim.margin.mean() - 5.0) < 0.3
    assert abs(sim.total.mean() - 225.0) < 0.4
    assert not np.any(sim.margin == 0)             # no ties in basketball
    p_cover = win_prob(sim.spread("home", -5.0, 1.91))
    assert 0.45 < p_cover < 0.55                   # line at the projection ~ coin flip
    ml = sim.ml_anchored("home", 1.5, 0.66, np.random.default_rng(5))
    assert abs(np.mean(ml > 0) - 0.66) < 0.01


def test_ledger_settlement():
    games = {"g": {"home_score": 110, "away_score": 104}}
    players = {("g", "7"): {"pts": 22, "reb": 5}}
    leg = lambda **k: {"game_id": "g", "kind": "spread", "side": "home", "line": -6.0, **k}
    assert _settle_leg(leg(), games, players) == "push"
    assert _settle_leg(leg(line=-5.5), games, players) == "win"
    assert _settle_leg(leg(kind="moneyline", side="away", line=None), games, players) == "loss"
    assert _settle_leg(leg(kind="total", side="under", line=214.5), games, players) == "win"
    assert _settle_leg(leg(kind="total", side="over", line=214.5), games, players) == "loss"
    assert _settle_leg(leg(kind="prop", side="over", line=21.5, player_id="7", stat="pts"), games, players) == "win"
    assert _settle_leg(leg(kind="prop", side="over", line=1.5, player_id="9", stat="pts"), games, players) == "void"


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} tests passed")
