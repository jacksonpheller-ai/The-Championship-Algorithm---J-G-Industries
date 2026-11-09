"""Sizing/verdict policy and growth bootstrap tests. Run: python tests/test_policy.py"""
import math
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from jghedge import growth  # noqa: E402
from jghedge.portfolio import policy  # noqa: E402

DEC110 = 1 + 100 / 110


def _bet(rng, p, dec, n, p_push=0.0):
    u = rng.random(n)
    return np.where(u < p, dec - 1, np.where(u < p + p_push, 0.0, -1.0))


def test_shrink_hits_target_win_rate():
    rng = np.random.default_rng(0)
    n = 200000
    R = _bet(rng, 0.56, DEC110, n)
    p = float(np.mean(R > 0))
    be = policy.breakeven(DEC110)
    u = np.random.default_rng(1).random(n)
    out = policy.shrink_payoff(R, None, "total", u=u)
    target = be + 0.5 * (p - be)
    assert abs(float(np.mean(out > 0)) - target) < 1e-5              # P(win) -> be + s (p - be)
    assert abs(float(out.mean()) - 0.5 * float(R.mean())) < 1e-4     # EV -> s x EV
    assert np.all(out <= R)                                          # only winners are thinned, to -1
    # closed-form path with the strict win probability gives the same draws
    out2 = policy.shrink_payoff(R, p, "total", dec=DEC110, u=u)
    assert np.array_equal(out, out2)
    # pushes untouched
    Rp = _bet(rng, 0.56, DEC110, n, p_push=0.03)
    outp = policy.shrink_payoff(Rp, None, "total", u=u)
    assert int(np.sum(outp == 0)) == int(np.sum(Rp == 0))
    assert abs(float(outp.mean()) - 0.5 * float(Rp.mean())) < 1e-4
    # negative-EV bet is never improved; s = 0 (props) removes the edge entirely
    Rn = _bet(rng, 0.50, DEC110, n)
    assert np.array_equal(policy.shrink_payoff(Rn, None, "total", u=u), Rn)
    assert abs(float(policy.shrink_payoff(R, None, "prop", u=u).mean())) < 1e-4
    assert policy.shrink_factor("spread") == 0.0 and policy.shrink_factor("parlay") == 0.5


def test_shrink_matrix_parlay_and_determinism():
    rng = np.random.default_rng(2)
    n = 100000
    a = _bet(rng, 0.56, DEC110, n, p_push=0.02)
    b = _bet(rng, 0.55, 1.95, n, p_push=0.02)
    ma = np.where(a > 0, DEC110, np.where(a == 0, 1.0, 0.0))
    mb = np.where(b > 0, 1.95, np.where(b == 0, 1.0, 0.0))
    parlay = ma * mb - 1                                   # multi-valued winners (pushed legs)
    prop = _bet(rng, 0.56, DEC110, n)
    R = np.column_stack([a, b, parlay, prop])
    kinds = ["total", "total", "parlay", "prop"]
    out1 = policy.shrink_matrix(R, kinds, decs=[DEC110, 1.95, DEC110 * 1.95, DEC110], seed=5)
    out2 = policy.shrink_matrix(R, kinds, decs=[DEC110, 1.95, DEC110 * 1.95, DEC110], seed=5)
    assert np.array_equal(out1, out2)                                  # deterministic
    for j, s in enumerate((0.5, 0.5, 0.5, 0.0)):
        assert abs(float(out1[:, j].mean()) - s * float(R[:, j].mean())) < 3e-4, j
    assert np.all(out1 <= R)
    assert int(np.sum(out1[:, 2] == 0)) == int(np.sum(R[:, 2] == 0))
    # common random numbers: a draw thinned in the parlay is usually thinned in one of its legs
    flipped_p = (R[:, 2] > 0) & (out1[:, 2] < 0)
    flipped_leg = ((R[:, 0] > 0) & (out1[:, 0] < 0)) | ((R[:, 1] > 0) & (out1[:, 1] < 0))
    assert flipped_p.sum() > 500 and float(np.mean(flipped_leg[flipped_p])) > 0.4
    assert policy.shrink_matrix(np.zeros((10, 0)), []).shape == (10, 0)


def test_tiers_boundaries():
    t = policy.tiers(12.4, 0.5)
    assert t["label"] == "tiny" and t["k"] == 0.5 and t["ask_for_smaller_unit"] and t["opt_in_k"] is None
    t = policy.tiers(12.5, 0.5)
    assert t["label"] == "small" and t["k"] == 0.35 and not t["ask_for_smaller_unit"] and t["opt_in_k"] is None
    assert policy.tiers(49.99, 0.5)["label"] == "small"
    t = policy.tiers(50, 0.5)
    assert t["label"] == "standard" and t["k"] == 0.25 and t["opt_in_k"] == 0.5 and t["units"] == 100
    assert policy.tiers(10, 0.1)["label"] == "standard"
    assert policy.tiers(10, 0.5)["max_parlay_legs"] == 2 and policy.tiers(1000, 0.5)["max_parlay_legs"] == 4
    assert policy.tiers(0, 0.5)["label"] == "tiny"
    pol = policy.POLICY
    assert pol["s"] == {"total": 0.5, "parlay": 0.5, "prop": 0.0, "pickem": 0.5}
    assert pol["tiers"] == [{"max_units": 25, "k": 0.5, "label": "tiny"}, {"max_units": 100, "k": 0.35, "label": "small"},
                            {"max_units": None, "k": 0.25, "label": "standard"}]
    assert pol["per_bet_cap"] == 0.05 and pol["night_cap"] == 0.10 and pol["opt_in_k"] == 0.5


def test_growth_gate_sign_and_b_min():
    rng = np.random.default_rng(3)
    n = 400000
    R = _bet(rng, 1.04 / DEC110, DEC110, n)                 # +4% claimed EV at -110
    Rs = policy.shrink_payoff(R, None, "total", rng=np.random.default_rng(4))
    ev_s = float(Rs.mean())
    assert abs(ev_s - 0.02) < 2e-3
    assert policy.growth_gate(Rs, 10, 0.5) < 0 < policy.growth_gate(Rs, 100, 0.5)   # memo: $10/$0.50 fails, $100 passes
    g = policy.growth_gate_matrix(np.column_stack([Rs, Rs]), 100, 0.5)
    assert g.shape == (2,) and abs(g[0] - policy.growth_gate(Rs, 100, 0.5)) < 1e-12
    assert abs(policy.b_min(DEC110, 0.02, 0.5) - 11.36) < 0.01                      # memo s.2 figure
    assert policy.b_min(DEC110, 0.0, 0.5) == math.inf and policy.b_min(DEC110, -0.01, 0.5) == math.inf
    bm, bx = policy.b_min(DEC110, ev_s, 0.5), policy.b_min_exact(Rs, 0.5)
    assert abs(bx - bm) / bm < 0.05                                                  # closed form tracks the exact root
    assert policy.growth_gate(Rs, bx * 0.98, 0.5) < 0 < policy.growth_gate(Rs, bx * 1.02, 0.5)
    assert policy.growth_gate(R, 0.5, 0.5) == -math.inf                              # a unit equal to the bankroll
    m = policy.bet_metrics(Rs, DEC110, unit=0.5, bankroll=10)
    assert set(m) == {"shrunk_ev", "p_shrunk", "b_min", "growth_1u_per_100", "growth_1u"}
    assert m["growth_1u"] < 0 < m["growth_1u_per_100"] and abs(m["b_min"] - bm) < 1e-9
    # JSON safety: a negative-edge bet has no b_min (None, not inf) and a unit >= bankroll has no growth
    neg = policy.bet_metrics(_bet(rng, 0.45, DEC110, 20000), DEC110, unit=0.5, bankroll=0.5)
    assert neg["b_min"] is None and neg["growth_1u"] is None and neg["shrunk_ev"] < 0
    import json
    json.dumps(neg, allow_nan=False)


def test_allocate_gate_and_caps():
    rng = np.random.default_rng(6)
    n = 60000
    R = np.column_stack([_bet(rng, 0.58, DEC110, n), _bet(rng, 1.04 / DEC110, DEC110, n), _bet(rng, 0.50, DEC110, n)])
    Rs = policy.shrink_matrix(R, ["total", "total", "total"], seed=1)
    u10 = policy.allocate(Rs, 10, 0.5, 0.5)
    assert u10[1] == 0 and u10[2] == 0                       # +2% shrunk bet fails the gate at $10/$0.50; negative never bets
    assert u10[0] == 1                                       # 5% per-bet cap = exactly one unit
    u100 = policy.allocate(Rs, 100, 0.5, 0.25)
    assert u100[0] > u100[1] >= 1 and u100[2] == 0
    assert u100.max() * 0.5 <= 0.05 * 100 + 1e-9 and u100.sum() * 0.5 <= 0.10 * 100 + 1e-9
    assert policy.allocate(Rs[:, 2:], 100, 0.5, 0.25).sum() == 0
    assert policy.allocate(np.zeros((n, 0)), 100, 0.5, 0.25).shape == (0,)
    assert np.array_equal(policy.reference_units(Rs), u100)
    tk = policy.size_ticket(Rs, 10, 0.5)
    assert tk["k"] == 0.5 and np.array_equal(tk["units"], u10) and tk["staked"] == 0.5 and abs(tk["staked_frac"] - 0.05) < 1e-12
    assert policy.size_ticket(Rs, 100, 0.5, opt_in=True)["k"] == 0.5 and policy.size_ticket(Rs, 100, 0.5)["k"] == 0.25
    assert policy.size_ticket(Rs, 40, 0.5, opt_in=True)["k"] == 0.35   # no opt-in below 100 units
    # the opt-in ceiling is only offered on BET nights (memo s.4: LIGHT = default fraction only)
    assert policy.size_ticket(Rs, 100, 0.5, opt_in=True, verdict="BET")["opt_in"] is True
    for v in ("LIGHT", "PASS"):
        tk = policy.size_ticket(Rs, 100, 0.5, opt_in=True, verdict=v)
        assert tk["k"] == 0.25 and tk["opt_in"] is False
    # a large bankroll with tiny units can need more than discrete_allocate's default 600 greedy
    # steps (full Kelly here wants ~700 units): the per-bet and night caps, not the step limit,
    # must bound the ticket
    big = policy.allocate(Rs, 1000, 0.1, 1.0)
    assert big[2] == 0 and big[1] > 0 and abs(big[0] * 0.1 - 0.05 * 1000) < 1e-9       # best bet at the 5% cap
    assert big.sum() > 600 and big.sum() * 0.1 <= 0.10 * 1000 + 1e-9
    assert policy.allocate(Rs, 1000, 0.1, 0.25).sum() < 600                                # quarter Kelly sits well inside


def test_night_score_and_verdict_thresholds():
    R = np.column_stack([np.full(1000, 0.02), np.full(1000, 0.05), np.full(1000, -0.01)])
    assert abs(policy.night_score(R, [2, 0, 1], 0.5, 100) - (0.01 * 0.02 + 0.005 * -0.01)) < 1e-12
    assert policy.night_score(np.zeros((10, 0)), [], 0.5) == 0.0
    assert policy.verdict(0.0004, 3) == "PASS"
    assert policy.verdict(0.0005, 3) == "LIGHT"
    assert policy.verdict(0.0014, 2) == "LIGHT"
    assert policy.verdict(0.0015, 2) == "BET"
    assert policy.verdict(0.01, 0) == "PASS"
    assert policy.verdict(0.01, 2, {"provisional": True}) == "LIGHT"
    assert policy.SCORE_THRESHOLDS == {"light": 0.0005, "bet": 0.0015}


def test_text_helpers_phrasing():
    best = [{"id": "g1-to-o", "label": "DEN/IND Over 228.5", "kind": "total", "shrunk_ev": 0.012,
             "growth_1u": -0.0001, "b_min": 22.7}]
    h = policy.headline("PASS", best, {"bankroll": 14, "unit": 0.5, "score": 0.0002, "n_bets": 0})
    assert h.startswith("Pass tonight") and "cuts variance" in h and "1.2% shrunk edge" in h
    assert "$0.50 unit on $14" in h and "$22.70" in h and "no stake grows the bankroll" in h
    for bad in ("raises growth", "increases growth", "improves growth"):
        assert bad not in h
    assert policy.headline("PASS", [], {}).startswith("Pass tonight")
    # the memo's realised-return clause appears only when comparable history is supplied
    hh = policy.headline("PASS", best, {"bankroll": 14, "unit": 0.5, "history_roi": -0.05, "history_n": 40,
                                        "history_se": 0.04})
    assert "returned about -5% per dollar (not distinguishable from zero); skipping costs no expected growth" in hh
    hs = policy.headline("PASS", best, {"history_roi": 0.12, "history_n": 40, "history_se": 0.02})
    assert "about 12% per dollar;" in hs and "not distinguishable" not in hs
    assert "Historically" not in policy.headline("PASS", best, {"history_roi": -0.05, "history_n": 4})
    hb = policy.headline("BET", best, {"score": 0.002, "n_bets": 3})
    assert hb.startswith("Bet tonight") and "3 bets" in hb and "full ticket" in hb
    hl = policy.headline("LIGHT", best, {"score": 0.001, "n_bets": 1, "provisional": True})
    assert hl.startswith("Light night") and "1 bet " in hl and "no opt-in" in hl and "Provisional" in hl
    ticket = [{"id": "a", "label": "DEN/IND Over 228.5", "kind": "total", "side": "over", "line": 228.5,
               "max_line": 229.5, "shrunk_ev": 0.02, "frac_ref": 0.02},
              {"id": "b", "label": "LAL/BOS Under 215", "kind": "total", "side": "under", "line": 215.0,
               "max_line": 214.0, "shrunk_ev": 0.018, "frac_ref": 0.01}]
    wc = policy.would_change("BET", ticket, 0.0016, {"lineup_uncertainty": ["g9"], "props_regime": "stand_down",
                                                     "hours_to_first_tip": 1.0})
    text = " ".join(wc)
    assert "Over 228.5 at -110 or better; skip above 229.5" in text and "Under 215 at -110 or better; skip below 214" in text
    assert "drops the night to LIGHT" in text and "GTD starters" in text and "stand down" in text and "First tip" in text
    assert "BET holds" in text
    wp = policy.would_change("PASS", [{"id": "c", "label": "X Over 230", "kind": "total", "side": "over", "line": 230.0,
                                       "shrunk_ev": 0.008, "frac_ref": 0}], 0.0, {})
    text = " ".join(wp)
    assert "below the 1.5% floor" in text and "Over 229.5 or better" in text and "assuming" in text
    wm = policy.would_change("PASS", [{"id": "c", "label": "X Over 230", "kind": "total", "side": "over", "line": 230.0,
                                       "max_line": 228.5, "shrunk_ev": 0.008, "frac_ref": 0}], 0.0, {})
    assert "Over 228.5 or better would clear it." in " ".join(wm)
    assert policy.would_change("PASS", [], 0.0, {}) == []


def _slate(rng, n=40000):
    strong1 = _bet(rng, 0.60, DEC110, n, p_push=0.02)
    strong2 = _bet(rng, 0.60, 1.95, n, p_push=0.02)
    weak = _bet(rng, 1.04 / DEC110, DEC110, n)
    prop = _bet(rng, 0.58, DEC110, n)
    R = np.column_stack([strong1, strong2, weak, prop])
    meta = [{"id": "g1-to-o", "kind": "total", "label": "A/B Over 228.5", "game_id": "g1", "dec": DEC110,
             "side": "over", "line": 228.5, "max_line": 230.5},
            {"id": "g2-to-u", "kind": "total", "label": "C/D Under 215", "game_id": "g2", "dec": 1.95,
             "side": "under", "line": 215.0, "max_line": 213.0},
            {"id": "g3-to-o", "kind": "total", "label": "E/F Over 231", "game_id": "g3", "dec": DEC110,
             "side": "over", "line": 231.0, "max_line": 231.0},
            {"id": "g1-pp-7-pts", "kind": "prop", "label": "Someone Over 21.5 Points", "game_id": "g1", "dec": DEC110}]
    return policy.shrink_matrix(R, [m["kind"] for m in meta], [m["dec"] for m in meta], seed=3), meta


def test_night_block_contract():
    Rs, meta = _slate(np.random.default_rng(8))
    flags = {"lineup_uncertainty": [], "props_regime": "neutral", "hours_to_first_tip": 6.2}
    nb = policy.night_block(Rs, meta, bankroll=14, unit=0.5, flags=flags)
    assert set(nb) == {"verdict", "score", "score_thresholds", "headline", "reasons", "would_change", "flags", "best_available"}
    assert nb["verdict"] == "BET" and nb["score"] >= 0.0015 and nb["score_thresholds"] == {"light": 0.0005, "bet": 0.0015}
    assert isinstance(nb["headline"], str) and nb["headline"].startswith("Bet tonight")
    assert isinstance(nb["reasons"], list) and nb["reasons"] and isinstance(nb["would_change"], list)
    assert 1 <= len(nb["best_available"]) <= 3 and all(isinstance(i, str) for i in nb["best_available"])
    assert set(nb["best_available"]) <= {m["id"] for m in meta} and "g1-pp-7-pts" not in nb["best_available"]  # s = 0 prop has no edge
    assert nb["flags"] == {"props_regime": "neutral", "props_regime_n": None, "props_regime_min": None, "lineup_uncertainty": [], "hours_to_first_tip": 6.2}
    assert any("small (28 units" in r for r in nb["reasons"])
    tiny = policy.night_block(Rs, meta, bankroll=10, unit=0.5, flags=flags)          # 20 units -> tiny tier
    assert any("tiny" in r for r in tiny["reasons"]) and any("$0.10 units" in r for r in tiny["reasons"])
    # lineups unconfirmed on the games carrying the ticket cap the verdict at LIGHT
    prov = policy.night_block(Rs, meta, 14, 0.5, {**flags, "lineup_uncertainty": ["g1", "g2"]})
    assert prov["verdict"] == "LIGHT" and prov["flags"]["lineup_uncertainty"] == ["g1", "g2"]
    assert "Provisional" in prov["headline"]
    # empty slate degrades to a clean PASS
    empty = policy.night_block(np.zeros((1000, 0)), [], 14, 0.5)
    assert empty["verdict"] == "PASS" and empty["score"] == 0 and empty["best_available"] == [] and empty["would_change"] == []
    assert empty["flags"]["hours_to_first_tip"] is None
    # a slate of only the weak total passes at every bankroll (score below the floor)
    weak = policy.night_block(Rs[:, 2:3], meta[2:3], 100, 0.5)
    assert weak["verdict"] == "PASS" and weak["headline"].startswith("Pass tonight") and "cuts variance" in weak["headline"]
    hist = policy.night_block(Rs[:, 2:3], meta[2:3], 100, 0.5, history={"roi": -0.03, "n": 55, "se": 0.05})
    assert "Historically nights like this returned about -3% per dollar (not distinguishable from zero)" in hist["headline"]
    # the whole block (with a negative-edge column present) is strict JSON: no Infinity/NaN anywhere
    import json
    neg_meta = meta + [{"id": "g4-sp-h", "kind": "spread", "label": "Spread", "game_id": "g4", "dec": DEC110}]
    neg_R = np.column_stack([Rs, _bet(np.random.default_rng(5), 0.45, DEC110, Rs.shape[0])])
    blk = policy.night_block(neg_R, neg_meta, 0.5, 0.5, flags)        # a unit equal to the bankroll
    json.dumps(blk, allow_nan=False)
    # why nothing is placeable: the three hurdles are told apart (weak total alone: +2% shrunk, b_min ~ $11.40)
    assert any("above the 5% per-bet cap ($0.40)" in r for r in policy.night_block(Rs, meta, 8, 0.5, flags)["reasons"])
    nogo = policy.night_block(Rs[:, 2:3], meta[2:3], 11, 0.5, flags)  # $11 < b_min: no $0.50 stake grows the bankroll
    assert any("no stake of that size grows the bankroll" in r and "about $11.40 is the smallest bankroll" in r
               for r in nogo["reasons"])
    frozen = policy.night_block(Rs[:, 2:3], meta[2:3], 14, 0.5, flags)   # 28 units: growth-positive, > 0.35-Kelly stake
    assert any("growth-positive on 1 bet but larger than the 0.35-Kelly stake" in r and "smaller unit" in r
               for r in frozen["reasons"])
    assert any("2 bets placeable for $1 (7.1% of bankroll)" in r for r in nb["reasons"])   # the full slate at $14 bets
    assert "0.050% floor" in weak["headline"]


def _rows(rng, n, with_score=False):
    out = []
    for i in range(n):
        st = float(rng.uniform(0.01, 0.06)) if rng.random() < 0.6 else 0.0
        row = {"date": f"2025-01-{i % 28 + 1:02d}", "games": 5, "bets": int(rng.integers(1, 5)) if st else 0,
               "staked": round(st, 4), "ret": round(st * float(rng.choice([-1.0, 0.91])), 5), "bank": 1000.0}
        if with_score:
            row["score"] = float(rng.uniform(0, 0.003))
        out.append(row)
    return out


def test_replay_loader_finds_every_seed_and_history_band():
    rng = np.random.default_rng(21)
    raw = {"2024": {"nightly": _rows(rng, 30), "nightly_seed2": _rows(rng, 30), "small": {"balanced": {"end": 9.5}},
                    "bet_calibration": [{"bin": "0.5-0.6", "pred": 0.55, "actual": 0.52, "n": 100}],
                    "by_kind": {"total": {"bets": 10}}},
            "2025": {"nightly": _rows(rng, 30), "seeds": [{"seed": 1, "nightly": _rows(rng, 30)}]},
            "2026": {"nightly": _rows(rng, 30), "runs": {"s1": {"nightly": _rows(rng, 30)}}},
            "2027": {"nightly": _rows(rng, 10, with_score=True)},
            "2027_seed2": {"nightly": _rows(rng, 10, with_score=True)},
            "meta": {"note": "ignored"}}
    data = growth.load_replay_nightly(raw)
    assert data["seasons"] == ["2024", "2025", "2026", "2027"] and data["seeds"] == {"2024": 2, "2025": 2, "2026": 2, "2027": 2}
    assert data["nights"] == 200 and len(data["ret"]["2024"]) == 60 and len(data["bets"]["2027"]) == 20
    assert growth.load_replay_nightly(raw, seasons=["2025"])["nights"] == 60
    blk = growth.growth_block(raw, paths=100, nights=40)
    assert "both seeds" in blk["basis"] and "replay_open.json" in blk["basis"] and len(blk["grid"]) == 84
    assert growth.growth_block({"meta": {}})["grid"] == []
    # realised return per dollar on past nights in a score band (the PASS headline's clause)
    rows = raw["2027"]["nightly"] + raw["2027_seed2"]["nightly"]
    band = growth.historical_band_roi(rows, 0.0, 0.0005, min_n=1)
    staked = [r for r in rows if r["staked"] > 0 and 0.0 <= r["score"] < 0.0005]
    assert band is not None and band["n"] == len(staked) and -1 <= band["roi"] <= 0.91 and band["se"] >= 0
    assert growth.historical_band_roi(rows, 0.0, 0.0005, min_n=50) is None
    assert growth.historical_band_roi(raw["2024"]["nightly"], 0.0, 1.0) is None      # no stored score


def test_max_line_fair_line_still_ok():
    rng = np.random.default_rng(11)
    draws = np.round(rng.normal(228, 18, 200000))
    assert abs(policy.fair_line(draws) - 228) <= 0.5
    ml = policy.max_line(draws, "over", 224.0)
    assert ml is not None and 224 <= ml <= 227 and ml % 0.5 == 0
    mu = policy.max_line(draws, "under", 232.0)
    assert mu is not None and 229 <= mu <= 232
    assert policy.max_line(draws, "over", 240.0, span=2.0) is None
    ev_now = policy.total_ev_at_line(draws, "over", 224.0)
    assert 0.5 * ev_now >= policy.MIN_SHRUNK_EV and 0.5 * policy.total_ev_at_line(draws, "over", ml + 0.5) < policy.MIN_SHRUNK_EV
    assert policy.still_ok("over", 226.0, 226.5) and not policy.still_ok("over", 227.0, 226.5)
    assert policy.still_ok("under", 231.0, 230.5) and not policy.still_ok("under", 230.0, 230.5)
    assert not policy.still_ok("over", None, 226.5) and not policy.still_ok("over", 226.0, None)


def _nightly():
    if growth.REPLAY_FILE.exists():
        data = growth.load_replay_nightly()
        if data["nights"] > 100:
            return data
    rng = np.random.default_rng(0)                     # synthetic fallback: same shape as the replay file
    ret, staked, bets = {}, {}, {}
    for s in ("2024", "2025"):
        st = np.where(rng.random(160) < 0.6, rng.uniform(0.005, 0.08, 160), 0.0)
        ret[s] = list(np.round(st * rng.choice([-1.0, 0.91], 160, p=[0.47, 0.53]), 5))
        staked[s] = list(np.round(st, 4))
        bets[s] = [int(rng.integers(1, 6)) if x > 0 else 0 for x in st]
    return {"ret": ret, "staked": staked, "bets": bets, "seeds": {"2024": 1, "2025": 1}, "seasons": ["2024", "2025"],
            "nights": 320}


def test_bootstrap_determinism_and_grid_timing():
    data = _nightly()
    kw = {"nightly_staked_by_season": data["staked"], "nightly_bets_by_season": data["bets"]}
    a = growth.bootstrap(data["ret"], 100, 0.5, 0.25, nights=160, paths=500, seed=1, **kw)
    b = growth.bootstrap(data["ret"], 100, 0.5, 0.25, nights=160, paths=500, seed=1, **kw)
    assert a == b                                                         # deterministic in the seed
    c = growth.bootstrap(data["ret"], 100, 0.5, 0.25, nights=160, paths=500, seed=2, **kw)
    assert c["median"] != a["median"]
    assert {"bankroll", "unit", "k", "median", "p10", "p90", "p_bust", "p_dd50", "basis"} <= set(a)
    assert a["p10"] <= a["median"] <= a["p90"] and 0 <= a["p_bust"] <= 1 and a["integer_units"] and a["per_bet_split"]
    assert "k/0.25 = 1.00" in a["basis"] and "whole-unit" in a["basis"]
    assert "k/0.25 = 2.00 for k=0.5" in growth.bootstrap(data["ret"], 100, 0.5, 0.5, nights=20, paths=50, **kw)["basis"]
    # a unit larger than the bankroll freezes every path: nothing is ever staked
    d = growth.bootstrap(data["ret"], 10, 20, 0.5, nights=50, paths=200, seed=1, **kw)
    assert d["median"] == 10 and d["p10"] == 10 and d["p_bust"] == 1.0 and d["skip_share"] == 1.0
    # $10 at $0.50 units: most bets round below one unit (the memo's "frozen" bankroll), so the
    # path stays near $10; at $0.10 units the same nights are placeable and the spread opens up
    f = growth.bootstrap(data["ret"], 10, 0.5, 0.25, nights=160, paths=500, seed=1, **kw)
    assert f["skip_share"] > 0.5 and 5 <= f["median"] <= 15
    f_small = growth.bootstrap(data["ret"], 10, 0.1, 0.25, nights=160, paths=500, seed=1, **kw)
    assert f_small["skip_share"] < f["skip_share"] and (f_small["p90"] - f_small["p10"]) > (f["p90"] - f["p10"])
    # continuous fallback when no stake data is given
    e = growth.bootstrap(data["ret"], 100, 0.5, 0.25, nights=160, paths=200, seed=1)
    assert not e["integer_units"] and e["p10"] < e["p90"]
    t0 = time.time()
    g = growth.grid(data["ret"], data["staked"], nights=160, paths=2000, seed=1, nightly_bets_by_season=data["bets"])
    dt = time.time() - t0
    assert len(g) == 7 * 4 * 3 and dt < 60, dt
    for row in g:
        assert {"bankroll", "unit", "k", "median", "p10", "p90", "p_bust", "p_dd50"} <= set(row)
        assert 0 <= row["p_bust"] <= 1 and 0 <= row["p_dd50"] <= 1
    big = [r for r in g if r["bankroll"] == 1000 and r["unit"] == 0.5 and r["k"] == 0.25][0]
    assert big["p_bust"] == 0.0                                           # caps make bust impossible at $1000
    assert big["median"] > 0
    block = growth.growth_block(paths=200) if growth.REPLAY_FILE.exists() else {"basis": "replay_open.json k/0.25", "nights": 160, "grid": []}
    assert set(block) == {"basis", "nights", "grid"} and "replay_open.json" in block["basis"] and "k/0.25" in block["basis"]
    assert growth.growth_block(path="/nonexistent/replay.json")["grid"] == []


def test_lineup_cap_counts_parlay_stake():
    """A parlay is exposed to a late scratch in any leg's game, so its stake counts toward 'most of the
    ticket is provisional', which caps a BET night at LIGHT. A parlay's game_id is None, so it used to
    count for nothing."""
    rng = np.random.default_rng(3)
    n = 6000
    R = np.column_stack([np.where(rng.random(n) < 0.36, 2.645, -1.0), np.where(rng.random(n) < 0.36, 2.645, -1.0)])
    meta = [{"id": "parlay-1", "kind": "parlay", "label": "A + C", "game_id": None, "game_ids": ["a", "c"], "dec": 3.645},
            {"id": "parlay-2", "kind": "parlay", "label": "B + C", "game_id": None, "game_ids": ["b", "c"], "dec": 3.645}]
    assert policy.night_block(R, meta, 100, 0.5, {})["verdict"] == "BET"
    assert policy.night_block(R, meta, 100, 0.5, {"lineup_uncertainty": ["c"]})["verdict"] == "LIGHT"       # every unit touches game c
    assert policy.night_block(R, meta, 100, 0.5, {"lineup_uncertainty": ["z"]})["verdict"] == "BET"
    old_meta = [{k: v for k, v in m.items() if k != "game_ids"} for m in meta]
    assert policy.night_block(R, old_meta, 100, 0.5, {"lineup_uncertainty": ["c"]})["verdict"] == "BET"      # the old undercount


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} tests passed")
