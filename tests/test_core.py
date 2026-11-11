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


def test_parlay_closed_form_ev():
    n = 20000
    rng = np.random.default_rng(9)
    legs, pay = [], {}
    for i, (p, d) in enumerate(((0.55, 1.91), (0.53, 1.95), (0.60, 1.70))):
        legs.append({"id": f"l{i}", "game_id": f"g{i}", "ev": p * d - 1, "p_final": p, "dec": d})
        pay[f"l{i}"] = np.where(rng.random(n) < p, d - 1, -1.0)
    out = build_parlays(legs, pay, max_legs=3, min_leg_prob=0.4)
    three = [x for x in out if x["n_legs"] == 3][0]
    pw = [float(np.mean(pay[l] > 0)) for l in three["legs"]]
    expect = np.prod([w * d for w, d in zip(pw, [1.91, 1.95, 1.70])]) - 1
    assert abs(three["ev"] - expect) < 1e-9
    assert abs(three["p_hit"] - np.prod(pw)) < 1e-9
    assert abs(three["payoff"].mean() - three["ev"]) < 1e-6      # sized payoff matches the closed form


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


def test_discrete_allocator_small_bankroll():
    from jghedge.portfolio import discrete
    rng = np.random.default_rng(6)
    n = 30000
    R = np.column_stack([np.where(rng.random(n) < p, d - 1, -1.0) for p, d in ((0.58, 1.91), (0.52, 1.91), (0.40, 1.91))])
    # $10 at quarter Kelly with $0.50 units: a 5% unit is too big for a small edge -> few or no bets
    u = discrete.discrete_allocate(R, 10, 0.5, 0.25, 0.05, 0.20)
    assert u.sum() <= 1 and u[2] == 0
    # full Kelly on $100: best bet gets the most units, negative-EV bet gets none
    u = discrete.discrete_allocate(R, 100, 0.5, 1.0, 0.15, 0.5)
    assert u[0] > u[1] >= 0 and u[2] == 0
    assert u.max() * 0.5 <= 0.15 * 100 + 1e-9
    mu, cov = discrete.moments(R)
    q = discrete.quadratic_allocate(mu, cov, 100, 0.5, 1.0, 0.15, 0.5)
    assert abs(int(q.sum()) - int(u.sum())) <= 2          # quadratic greedy tracks the exact one


def test_shared_issue_parse():
    from jghedge.shared import _parse_issue
    body = ("J&G ticket\n\n```json\n" + '{"date":"2026-11-07","bets":[{"ref":"x-sp-h","stake":2.5,"dec":1.91,"price":-110,'
            '"label":"Warriors -5.5","legs":[{"game_id":"g","kind":"spread","side":"home","line":-5.5,"dec":1.91}]},'
            '{"ref":"bad","stake":0,"legs":[]}]}' + "\n```")
    rows = _parse_issue({"number": 12, "title": "Bets 2026-11-07", "body": body, "user": {"login": "jackson"}})
    assert len(rows) == 1 and rows[0]["stake"] == 2.5 and rows[0]["type"] == "single" and rows[0]["by"] == "jackson"
    assert _parse_issue({"number": 1, "title": "Something else", "body": body}) == []


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


def test_simulation_returns_the_market_probability_at_zero_model_weight():
    """With the market's own centre and no model weight, an even-priced total must come out near 50/50
    and an even-priced spread too. The fitted residual tables are centred on their mean, totals are
    right-skewed, and a tied draw used to add up to five points: Unders started 0.4 points ahead."""
    import numpy as np
    from jghedge.config import load_params
    from jghedge.model.odds import market_total
    from jghedge.model.simulate import GameSim
    p = load_params()
    for line in (228.5, 224.0, 236.5):
        mt = market_total(line, -110, -110, p["total_sd"])
        s = GameSim(4.0, mt, p, 400_000, np.random.default_rng(5))
        over = float(np.mean(s.total > line) + 0.5 * np.mean(s.total == line))
        assert abs(over - 0.5) < 0.0035, (line, over)
        assert not np.any(s.margin == 0)                                     # every tie is settled
    s = GameSim(3.5, 228.5, p, 400_000, np.random.default_rng(6))            # the market makes home -3.5 a coin flip
    assert abs(float(np.mean(s.margin - 3.5 > 0)) - 0.5) < 0.006
    # a symmetric table is unchanged by the centring
    q = {"margin_sd": 12.0, "total_sd": 18.0}
    g = GameSim(0.0, 220.0, q, 200_000, np.random.default_rng(1))
    assert abs(float(np.mean(g.total > 219.5)) - float(np.mean(g.total < 220.5))) < 0.01


def test_total_probabilities_match_the_simulation():
    """The page re-prices a total at a live line with simulate.total_probabilities' formula (template
    liveProbs). It must agree with the simulator, be 50/50 at an even half-point line, and allow a push
    only on a whole-number line."""
    import numpy as np
    from jghedge.config import load_params
    from jghedge.model.simulate import GameSim, centred_table, total_probabilities
    p = load_params()
    tab = centred_table(p["total_resid_q"])
    for line, off in ((228.5, 0.0), (224.0, 0.0), (220.5, 1.7), (231.0, -2.3), (226.0, 3.0)):
        c = line + off
        s = GameSim(4.0, c, p, 600_000, np.random.default_rng(5))
        so, su = float(np.mean(s.total > line)), float(np.mean(s.total < line))
        ao, au, ap = total_probabilities(c, line, tab)
        assert abs(so - ao) < 0.004 and abs(su - au) < 0.004, (line, off, so, ao, su, au)
        assert abs(ao + au + ap - 1.0) < 1e-9
        assert (ap == 0.0) == (line != round(line))
    o, u, _ = total_probabilities(228.5, 228.5, tab)
    assert abs(o - 0.5) < 0.002 and abs(u - 0.5) < 0.002
    assert total_probabilities(231.5, 228.5, tab)[0] > total_probabilities(229.5, 228.5, tab)[0] > 0.5


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} tests passed")
