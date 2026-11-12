"""Fliff player props (straight bets against a DraftKings-based fair price). Run: python tests/test_fliffprops.py"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from jghedge import fliffprops as fp  # noqa: E402
from jghedge import pickem  # noqa: E402
from jghedge.data import oddsapi  # noqa: E402
from jghedge.model.odds import american_to_decimal, novig_multiplicative  # noqa: E402
from jghedge.model.props import COMBOS  # noqa: E402


def _sims(rng, n, means, disp=9.0):
    out = {}
    for st, mu in means.items():
        out[st] = rng.poisson(rng.gamma(disp, mu / disp, n)).astype(np.int16)
    for c, parts in COMBOS.items():
        if all(p in out for p in parts):
            out[c] = sum(out[p] for p in parts).astype(np.int16)
    out["_plays"] = np.ones(n, dtype=bool)
    return out


def _legs(lines, lean=0.03, seed=3):
    """lines: [(pid, stat, line, over, under)] for players who all score about 20 / 6 / 5."""
    rng = np.random.default_rng(seed)
    pids = sorted({x[0] for x in lines})
    means = {"pts": 20.0, "reb": 6.0, "ast": 5.0}
    players = [{"id": pid, "name": f"Player {pid}", "team": "DEN" if i % 2 else "LAL", "play_prob": 1.0, "exp_min": 33.0,
                "exp_line": dict(means)} for i, pid in enumerate(pids)]
    sims = {pid: _sims(rng, 20000, means) for pid in pids}
    dk = [{"athlete_id": pid, "stat": st, "kind": "ou", "line": ln, "over": o, "under": u} for pid, st, ln, o, u in lines]
    return pickem.build_legs(players, sims, dk, [], {"s": 0.5, "lean": lean, "proxy_p_band": (0.02, 0.98),
                                                     "lean_min_under": fp.PARAMS["lean_min_under"], "q_halfwidth": 3.0},
                             game={"id": "g1", "matchup": "LAL @ DEN", "tip": "2099-01-01T00:00Z"})


def test_evaluate_is_the_price_arithmetic():
    e = fp.evaluate(0.56, 0.0, -110)
    dec = american_to_decimal(-110)
    assert math.isclose(e["ev"], 0.56 * dec - 1) and math.isclose(e["be"], 1 / dec)
    assert math.isclose(e["p_cut"], 1 / dec + 0.5 * (0.56 - 1 / dec))                 # half of the edge over break-even is kept
    assert math.isclose(e["frac"], 0.25 * (e["p_cut"] * dec - 1) / (dec - 1))         # quarter Kelly on the cut chance
    assert e["ok"] and not e["big_gap"]
    # 3% is the bar: 53.9% at -110 just misses, 54.0% clears
    assert not fp.evaluate(0.539, 0.0, -110)["ok"] and fp.evaluate(0.540, 0.0, -110)["ok"]
    # below break-even nothing is cut, nothing is staked
    lo = fp.evaluate(0.50, 0.0, -110)
    assert lo["p_cut"] == 0.50 and lo["frac"] == 0.0 and not lo["ok"]
    # a tie (whole-number line) returns the stake: the edge applies only to the games that are decided
    t = fp.evaluate(0.56, 0.20, -110)
    assert math.isclose(t["ev"], 0.8 * (0.56 * dec - 1)) and math.isclose(t["frac"], e["frac"])
    # an edge of more than 12 points counts as 12 (and is flagged), so a stale line cannot take a big stake
    big = fp.evaluate(0.75, 0.0, 100)
    assert big["big_gap"] and math.isclose(big["p_cut"], 0.5 + 0.5 * 0.12) and big["frac"] <= fp.PARAMS["per_bet_cap"]


def test_take_price_is_the_worst_price_that_still_qualifies():
    for p in (0.45, 0.50, 0.53, 0.56, 0.62):
        a = fp.take_american(p, 0.0)
        assert fp.evaluate(p, 0.0, a)["ok"], (p, a)                                    # at the take price it is a bet
        worse = a - 1 if (a > 100 or a <= -100) and a - 1 != 99 else -101
        if a == 100:
            worse = -101
        assert not fp.evaluate(p, 0.0, worse)["ok"], (p, a, worse)                     # one cent worse it is not
    assert fp.take_american(0.56, 0.0) == -119          # 1.03 / 0.56 = 1.8393 -> -119.1, rounded toward the bettor
    assert fp.take_american(0.95, 0.0) is None          # no playable price for a near-certain side


def test_rows_at_draftkings_own_price_are_never_a_bet():
    lines = [("1", "pts", 19.5, -115, -115), ("2", "pts", 21.5, -120, -110), ("3", "reb", 5.5, -130, 100),
             ("4", "ast", 4.5, 105, -135), ("5", "pts", 18.5, -110, -120)]
    rows = fp.build_rows(_legs(lines))
    assert len(rows) == 5
    for r in rows:
        assert r["ref"] == "dk" and r["ok"] is False and r["fliff"] is None            # the stand-in number is never staked
        p_over_mkt = novig_multiplicative(r["over"], r["under"])[0]
        assert abs(r["p_over"] - (p_over_mkt - r["lean"])) < 0.012                       # fair chance = no-vig price minus the lean
        assert abs(r["p_over"] + r["p_under"] - 1.0) < 1e-9
        assert r["ev"] < fp.PARAMS["min_ev"]                                           # a 3-point lean does not beat a 7% margin by 3%
        # the take price is better than DraftKings' own price on that side
        assert american_to_decimal(r["take_under"]) > american_to_decimal(r["under"])
    assert rows == sorted(rows, key=lambda r: (not r["ok"], -r["ev"], r["id"]))


def test_lean_is_not_applied_where_the_under_is_the_longshot():
    rows = {r["player_id"]: r for r in fp.build_rows(_legs([("1", "pts", 16.5, -230, 180), ("2", "pts", 20.5, -110, -110)]))}
    assert rows["1"]["lean"] == 0.0 and abs(rows["1"]["p_over"] - novig_multiplicative(-230, 180)[0]) < 0.012
    assert abs(rows["2"]["lean"] - 0.03) < 1e-9
    # without the lean neither side of the longshot row is close to a bet at DraftKings' price
    assert rows["1"]["ev"] < 0


def test_a_softer_fliff_line_is_the_edge():
    legs = _legs([("1", "pts", 19.5, -115, -115), ("2", "pts", 19.5, -115, -115)])
    fliff = {("1", "pts"): {"line": 21.5, "over": -115, "under": -115},        # two points softer on the under, same price
             ("2", "pts"): {"line": 19.5, "over": -120, "under": -120}}        # same line, worse price
    rows = {r["player_id"]: r for r in fp.build_rows(legs, fliff)}
    a, b = rows["1"], rows["2"]
    assert a["ref"] == "fliff" and a["side"] == "under" and a["ok"] and a["ev"] > 0.03 and a["fliff"]["line"] == 21.5
    assert b["ref"] == "fliff" and not b["ok"] and b["ev"] < 0
    # the chance at Fliff's line comes from the row's own table
    p = fp.probs_at(a, 21.5)
    assert p is not None and p[1] > fp.probs_at(a, 19.5)[1] + 0.05
    assert fp.probs_at(a, 40.5) is None                                         # outside the table: not priced
    blk = fp.build_block(legs, fliff)
    assert blk["source"] == "oddsapi_fliff" and blk["n_ok"] == 1 and blk["n_fliff"] == 2 and blk["rows"][0]["player_id"] == "1"
    assert fp.build_block(legs)["source"] == "dk_proxy" and fp.build_block(legs)["n_ok"] == 0


def test_fliff_board_from_an_odds_api_response():
    payload = {"bookmakers": [
        {"key": "fliff", "markets": [{"key": "player_points", "outcomes": [
            {"name": "Over", "description": "Nikola Jokic", "point": 27.5, "price": -112},
            {"name": "Under", "description": "Nikola Jokic", "point": 27.5, "price": -108},
            {"name": "Over", "description": "Nobody Known", "point": 9.5, "price": -110}]}]},
        {"key": "draftkings", "markets": [{"key": "player_points", "outcomes": [
            {"name": "Over", "description": "Jamal Murray", "point": 20.5, "price": -115}]}]}]}
    board = fp.fliff_board(oddsapi.parse_event_props(payload), {"3112335": "Nikola Jokić", "3936299": "Jamal Murray"})
    assert board == {("3112335", "pts"): {"line": 27.5, "over": -112, "under": -108}}     # Fliff only, matched by name


def test_published_rows_are_compact_and_complete():
    blk = fp.build_block(_legs([("1", "pts", 19.5, -115, -115), ("1", "reb", 6.0, -105, -125)]))
    for r in blk["rows"]:
        assert {"id", "player", "player_id", "team", "game_id", "tip", "stat", "line", "over", "under", "p_over", "p_under",
                "p_tie", "q", "side", "ev", "ok", "take_over", "take_under", "ref", "lean"} <= set(r)
        assert all(len(q) == 3 and abs(q[0] - r["line"]) <= 3.0 for q in r["q"])
        assert any(q[0] == r["line"] for q in r["q"])
    whole = next(r for r in blk["rows"] if r["stat"] == "reb")
    assert whole["p_tie"] > 0.05                                                 # a whole-number line can land exactly
    assert blk["rules"]["min_ev"] == 0.03 and blk["rules"]["s"] == 0.5 and blk["rules"]["props_cap"] == 0.05


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("ok:", t.__name__)
    print(f"{len(tests)} tests passed")
