"""PrizePicks pick-em module tests. Run: python tests/test_pickem.py"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from jghedge import pickem  # noqa: E402
from jghedge.data import prizepicks  # noqa: E402
from jghedge.data.http import FetchError  # noqa: E402
from jghedge.model.props import COMBOS  # noqa: E402


# ------------------------------------------------------------------ helpers
def _sims(rng, n, means, play_prob=1.0, disp=9.0):
    plays = rng.random(n) < play_prob
    out = {}
    for st, mu in means.items():
        lam = rng.gamma(disp, mu / disp, n)
        out[st] = np.where(plays, rng.poisson(lam), 0).astype(np.int16)
    for c, parts in COMBOS.items():
        if all(p in out for p in parts):
            out[c] = sum(out[p] for p in parts).astype(np.int16)
    out["_plays"] = plays
    return out


def _player(pid, name, means, play_prob=1.0, exp_min=34.0, team="DEN"):
    exp_line = {**means, **{c: sum(means[p] for p in parts) for c, parts in COMBOS.items() if all(p in means for p in parts)}}
    return {"id": pid, "name": name, "team": team, "opp": "LAL", "play_prob": play_prob, "exp_min": exp_min,
            "exp_line": exp_line, "typ_min": exp_min, "starter": True}


def _dk(pid, stat, line, over, under):
    return {"athlete_id": pid, "stat": stat, "kind": "ou", "line": line, "over": over, "under": under}


# ------------------------------------------------------------------ payouts
def test_payout_breakevens():
    be2 = pickem.breakeven_probability("power2")
    assert abs(be2 - 3 ** -0.5) < 1e-6                    # 0.5774
    assert abs(pickem.breakeven_probability("power3") - 6 ** (-1 / 3)) < 1e-6   # official 6x (help centre, 2026-11-07)
    assert abs(pickem.breakeven_probability("power4") - 0.1 ** 0.25) < 1e-6
    assert abs(pickem.breakeven_probability("power6") - 37.5 ** (-1 / 6)) < 1e-6
    be = pickem.breakevens()
    assert set(be) == set(pickem.SLIP_TYPES)
    assert be["flex6"] < be["flex5"] < be["flex4"] < be["power2"]   # 6-pick flex is the cheapest leg
    assert abs(be["flex3"] - be["power2"]) < 1e-3                    # flex3 3x/1x has the same break-even as power2
    for t in pickem.SLIP_TYPES:
        assert abs(pickem.slip_ev_at(be[t], t)) < 1e-6
    # custom table: 2-pick at 4x needs p = 0.5
    assert abs(pickem.breakeven_probability("power2", {"power": {2: 4.0}, "flex": {}}) - 0.5) < 1e-6
    assert list(pickem.payout_vector("flex5")) == [0, 0, 0, 0.4, 2.0, 10.0]
    assert pickem.payouts_json()["power"]["2"] == 3.0 and pickem.payouts_json()["flex"]["6"] == [25.0, 2.0, 0.4]


def test_shrink_toward_breakeven():
    be = 0.5774
    assert abs(pickem.shrink(0.60, be, 0.5) - (be + 0.5 * (0.60 - be))) < 1e-12
    assert abs(pickem.shrink(be, be, 0.5) - be) < 1e-12
    assert pickem.shrink(0.55, be, 0.5) == 0.55         # below break-even: never moved up (shrink never improves a bet)
    arr = pickem.shrink(np.array([0.6, 0.5]), be, 0.0)
    assert np.allclose(arr, [be, 0.5])


# ------------------------------------------------------------------ anchoring
def test_anchoring_hits_target_and_preserves_ranks():
    rng = np.random.default_rng(1)
    x = _sims(rng, 20000, {"pts": 20.0})["pts"]
    raw = float(np.mean(x > 22.5))
    xa, m = pickem.anchor_to_market(x, 22.5, 0.55, seed=3)
    assert abs(float(np.mean(xa > 22.5)) - 0.55) <= 0.005
    assert m > 1.0 and raw < 0.55                      # market centre above ours -> scale up
    # rank preservation across distinct counts
    order = np.argsort(x, kind="stable")
    xs, xas = x[order].astype(float), xa[order]
    assert np.all(np.diff(xas)[np.diff(xs) > 0] > 0)
    # shrink the other way too
    xb, mb = pickem.anchor_to_market(x, 22.5, 0.30, seed=3)
    assert abs(float(np.mean(xb > 22.5)) - 0.30) <= 0.005 and mb < 1.0
    assert abs(pickem.fair_median(xa) - np.median(xa)) < 1e-12


def test_anchoring_degenerate_falls_back_to_shift():
    x = np.full(5000, 10, dtype=np.int16)              # constant draws
    xa, m = pickem.anchor_to_market(x, 14.5, 0.40, seed=9)
    assert abs(float(np.mean(xa > 14.5)) - 0.40) <= 0.005
    assert m == 1.0                                     # additive fallback


def test_anchoring_prefers_shift_when_scaling_would_strain():
    # blocks: mean 0.4, DK line 0.5 priced at 55% over. Scaling would need m ~ 3 (tripling the
    # whole distribution to push zero draws across the line); the shift keeps the shape.
    rng = np.random.default_rng(4)
    x = rng.poisson(0.4, 20000).astype(np.int16)
    xa, m = pickem.anchor_to_market(x, 0.5, 0.55, seed=4)
    assert abs(float(np.mean(np.rint(xa) > 0.5)) - 0.55) <= 0.005
    assert m == 1.0                                               # additive fallback chosen
    assert float(np.mean(np.rint(xa) > 2.5)) < 0.1                # tail not inflated
    xm, mm = pickem.anchor_to_market(x, 0.5, 0.55, seed=4, m_band=(0.1, 10.0))
    assert mm > 1.6 and float(np.mean(np.rint(xm) > 2.5)) > 0.2   # what scaling would have done


def test_leg_probabilities_ties_are_void():
    x = np.array([23.0, 24.0, 24.0, 25.0, 26.0])
    p_more, p_less, p_tie = pickem.leg_probabilities(x, 24)
    assert abs(p_tie - 0.4) < 1e-12
    assert abs(p_more - 2 / 3) < 1e-12 and abs(p_less - 1 / 3) < 1e-12
    p_more, p_less, p_tie = pickem.leg_probabilities(x, 24.5)
    assert p_tie == 0.0 and abs(p_more - 0.4) < 1e-12 and abs(p_more + p_less - 1) < 1e-12


# ------------------------------------------------------------------ slips
def test_two_leg_power_ev_from_independent_legs():
    # exactly independent 60% legs: P(both) = 0.36 -> EV = 3*0.36 - 1 = +8%
    n = 100
    j = np.arange(n)
    hits = np.stack([(j < 60), (j % 10 < 6)]).astype(np.uint8)
    legs = [{"id": "a", "player_id": "1", "p_side": 0.6, "p_side_shrunk": 0.6},
            {"id": "b", "player_id": "2", "p_side": 0.6, "p_side_shrunk": 0.6}]
    slips = pickem.enumerate_slips(legs, hits, s=1.0, max_legs=2)
    assert len(slips) == 1 and slips[0]["type"] == "power2"
    sl = slips[0]
    assert abs(sl["ev"] - 0.08) < 1e-9 and abs(sl["ev_mult"] - 1.08) < 1e-9
    assert abs(sl["ev_shrunk"] - 0.08) < 1e-9                       # s = 1 keeps the model's number
    assert np.allclose(sl["p_dist"], [0.16, 0.48, 0.36])
    # growth at $5 on $100: 0.36*ln(1.10) + 0.64*ln(0.95)
    assert abs(sl["growth_at_min_entry_per_100"] - (0.36 * math.log(1.10) + 0.64 * math.log(0.95))) < 1e-9
    assert sl["verdict"] == "play" and sl["growth_at_min_entry_per_100"] > 0
    assert sl["b_min"] is not None and sl["b_min"] <= 100


def test_shrunk_ev_is_about_half_the_edge():
    n = 100000                                           # still exactly independent 60% legs
    j = np.arange(n)
    hits = np.stack([(j < 60000), (j % 10 < 6)]).astype(np.uint8)
    legs = [{"id": "a", "player_id": "1", "p_side": 0.6, "p_side_shrunk": 0.6},
            {"id": "b", "player_id": "2", "p_side": 0.6, "p_side_shrunk": 0.6}]
    be = pickem.breakeven_probability("power2")
    sl = pickem.enumerate_slips(legs, hits, s=0.5, max_legs=2)[0]
    target = pickem.shrink(0.6, be, 0.5)
    assert abs(sl["ev"] - 0.08) < 1e-9                         # raw unchanged
    assert 0 < sl["ev_shrunk"] < sl["ev"]
    assert abs(sl["ev_shrunk"] - (3 * target ** 2 - 1)) < 0.03  # thinning is random on 100 draws
    assert sl["verdict"] in ("play", "marginal")


def test_flex_partial_payouts():
    # three independent legs at 0.7, 0.7, 0.8: hit-count distribution by hand
    ps = [0.7, 0.7, 0.8]
    p_dist = np.array([0.3 * 0.3 * 0.2,
                       0.7 * 0.3 * 0.2 + 0.3 * 0.7 * 0.2 + 0.3 * 0.3 * 0.8,
                       0.7 * 0.7 * 0.2 + 0.7 * 0.3 * 0.8 + 0.3 * 0.7 * 0.8,
                       0.7 * 0.7 * 0.8])
    legs = [{"id": f"l{i}", "player_id": str(i), "p_side": ps[i], "p_side_shrunk": ps[i]} for i in range(3)]
    slips = pickem.enumerate_slips(legs, None, s=1.0, max_legs=3, top=50)
    flex = next(x for x in slips if x["type"] == "flex3" and len(x["legs"]) == 3)
    power = next(x for x in slips if x["type"] == "power3")
    assert np.allclose(flex["p_dist"], p_dist)
    assert abs(flex["ev_mult"] - (p_dist[3] * 3.0 + p_dist[2] * 1.0)) < 1e-12
    assert abs(power["ev_mult"] - p_dist[3] * 6.0) < 1e-12
    # five-leg flex: payout vector indexes misses from the top
    pay = pickem.payout_vector("flex5")
    assert pay[5] == 10 and pay[4] == 2 and pay[3] == 0.4 and pay[2] == 0
    assert abs(pickem.growth_of(np.array([0, 0, 0, 0, 0, 1.0]), pay, 5, 100) - math.log(1 + 9 * 0.05)) < 1e-12
    assert pickem.min_bankroll_for_growth(np.array([1.0, 0, 0]), pickem.payout_vector("power2"), 5) is None


def test_no_two_legs_of_same_player_and_top_selection():
    rng = np.random.default_rng(5)
    n = 4000
    legs, rows = [], []
    for i in range(8):
        p = 0.66 - 0.01 * i
        rows.append((rng.random(n) < p).astype(np.uint8))
        legs.append({"id": f"l{i}", "player_id": str(i // 2), "p_side": p, "p_side_shrunk": p})
    hits = np.stack(rows)
    slips = pickem.enumerate_slips(legs, hits, s=0.5, max_legs=4, pool=8, top=12)
    assert 0 < len(slips) <= 12 + len(pickem.SLIP_TYPES)
    for sl in slips:
        players = [legs[int(l[1:])]["player_id"] for l in sl["legs"]]
        assert len(set(players)) == len(players)
        assert len(sl["p_dist"]) == len(sl["legs"]) + 1 and abs(sum(sl["p_dist"]) - 1) < 1e-9
        assert sl["verdict"] in ("play", "marginal", "skip")
    g = [x["growth_at_min_entry_per_100"] for x in slips]
    assert g == sorted(g, reverse=True)
    assert all(x["id"].startswith("pk-") for x in slips)


def test_thinning_marginals():
    rng = np.random.default_rng(11)
    rows = (rng.random((3, 50000)) < np.array([[0.62], [0.58], [0.50]])).astype(np.uint8)
    p_cond = rows.mean(1)
    be = pickem.breakeven_probability("power2")
    U = rng.random(rows.shape)
    thinned = pickem._thin(rows, p_cond, be, 0.5, U)
    for i in range(3):
        assert abs(thinned[i].mean() - pickem.shrink(p_cond[i], be, 0.5)) < 0.006
    assert np.all(thinned[0] <= rows[0])          # above break-even: wins only removed
    assert np.all(thinned[2] >= rows[2])          # below break-even: losses promoted


# ------------------------------------------------------------------ packing
def test_pack_unpack_round_trip():
    rng = np.random.default_rng(2)
    m = (rng.random((5, 1003)) < 0.4).astype(np.uint8)
    b = pickem.pack_bits(m)
    assert isinstance(b, str)
    back = pickem.unpack_bits(b, 5, 1003)
    assert back.shape == m.shape and np.array_equal(back, m)
    # layout: bit k of the flat stream is row k // n, col k % n (row-major), MSB first
    flat = np.unpackbits(np.frombuffer(__import__("base64").b64decode(b), dtype=np.uint8))[: 5 * 1003]
    assert flat[1003 * 2 + 7] == m[2, 7]


# ------------------------------------------------------------------ PrizePicks board
def _payload():
    return {
        "data": [
            {"type": "projection", "id": "101",
             "attributes": {"line_score": "24.5", "stat_type": "Points", "odds_type": "standard",
                            "start_time": "2026-11-07T19:00:00-05:00", "status": "pre_game", "description": "LAL"},
             "relationships": {"new_player": {"data": {"type": "new_player", "id": "p1"}}}},
            {"type": "projection", "id": "102",
             "attributes": {"line_score": 8.5, "stat_type": "Rebounds", "odds_type": "goblin",
                            "start_time": "2026-11-07T19:00:00-05:00", "status": "pre_game"},
             "relationships": {"new_player": {"data": {"type": "new_player", "id": "p1"}}}},
            {"type": "projection", "id": "103",
             "attributes": {"line_score": 41.5, "stat_type": "Fantasy Score", "odds_type": "standard",
                            "start_time": "2026-11-07T19:00:00-05:00", "status": "pre_game"},
             "relationships": {"new_player": {"data": {"type": "new_player", "id": "p2"}}}},
            {"type": "projection", "id": "104",
             "attributes": {"line_score": 2.5, "stat_type": "Blocked Shots", "odds_type": "standard",
                            "start_time": "2026-11-07T21:30:00-05:00", "status": "pre_game"},
             "relationships": {"new_player": {"data": {"type": "new_player", "id": "p3"}}}},
            {"type": "projection", "id": "105", "attributes": {"line_score": None, "stat_type": "Points"},
             "relationships": {"new_player": {"data": {"type": "new_player", "id": "p3"}}}},
        ],
        "included": [
            {"type": "new_player", "id": "p1", "attributes": {"name": "Nikola Jokić", "team": "DEN", "position": "C"}},
            {"type": "new_player", "id": "p2", "attributes": {"name": "Jaren Jackson Jr.", "team": "MEM", "position": "PF"}},
            {"type": "new_player", "id": "p3", "attributes": {"display_name": "Victor Wembanyama", "team": "SAS", "position": "C"}},
            {"type": "league", "id": "7", "attributes": {"name": "NBA"}},
        ],
    }


def test_parse_projections_and_stat_map():
    rows = prizepicks.parse_projections(_payload())
    assert len(rows) == 4                                   # the null line is dropped
    by_id = {r["projection_id"]: r for r in rows}
    assert by_id["101"]["stat"] == "pts" and by_id["101"]["line"] == 24.5 and by_id["101"]["name"] == "Nikola Jokić"
    assert by_id["101"]["team"] == "DEN" and by_id["101"]["opponent"] == "LAL" and by_id["101"]["odds_type"] == "standard"
    assert by_id["102"]["odds_type"] == "goblin" and by_id["102"]["stat"] == "reb"
    assert by_id["103"]["stat"] is None and by_id["103"]["supported"] is False
    assert by_id["104"]["stat"] == "blk" and by_id["104"]["team"] == "SA"    # SAS -> ESPN 'SA'
    assert prizepicks.map_stat("Pts+Rebs+Asts") == "pra" and prizepicks.map_stat("3-PT Made") == "fg3m"
    assert prizepicks.map_stat("Turnovers") == "tov" and prizepicks.map_stat("Fantasy Score") is None
    assert prizepicks.parse_projections({"data": "nope"}) == [] and prizepicks.parse_projections(None) == []


def test_name_mapping():
    rows = prizepicks.parse_projections(_payload())
    id_to_name = {"1": "Nikola Jokic", "2": "Jaren Jackson", "3": "Victor Wembanyama", "4": "Nikola Jokic"}
    matched, unmatched = prizepicks.match_players(rows, id_to_name, team_of={"1": "DEN", "4": "LAL"})
    by_id = {r["projection_id"]: r for r in matched}
    assert by_id["101"]["player_id"] == "1"                 # accent dropped, duplicate resolved by team
    assert by_id["103"]["player_id"] == "2"                 # 'Jr.' suffix dropped
    assert by_id["104"]["player_id"] == "3"
    assert unmatched == []
    matched, unmatched = prizepicks.match_players(rows, {"1": "Nikola Jokic", "4": "Nikola Jokic"})
    assert matched[0]["player_id"] is None                  # ambiguous without teams
    assert "Nikola Jokić" in unmatched and "Jaren Jackson Jr." in unmatched
    assert prizepicks.board_summary(matched)["rows"] == 4


class _Blocked:
    class _S:
        headers = {"User-Agent": "x"}

    def __init__(self, exc):
        self.exc, self.session, self.calls = exc, self._S(), 0

    def get(self, url, ttl=None, kind="json", store=True):
        self.calls += 1
        assert store is False
        raise self.exc


class _Good(_Blocked):
    def get(self, url, ttl=None, kind="json", store=True):
        self.calls += 1
        assert "league_id=7" in url and "single_stat=true" in url
        assert self.session.headers.get("Origin") == "https://app.prizepicks.com"   # browser-like headers applied
        return _payload()


def test_fetch_projections_failure_paths():
    f = _Blocked(FetchError("HTTP 403 for https://api.prizepicks.com/projections"))
    rows, reason = prizepicks.fetch_projections(f)
    assert rows == [] and "403" in reason
    f = _Blocked(FetchError("HTTPSConnectionPool: Read timed out"))
    rows, reason = prizepicks.fetch_projections(f)
    assert rows == [] and "timeout" in reason
    g = _Good(None)
    rows, reason = prizepicks.fetch_projections(g)
    assert reason is None and len(rows) == 4 and g.calls == 1
    assert "Origin" not in g.session.headers                 # headers restored


# ------------------------------------------------------------------ end to end
def test_build_legs_hit_matrix_and_slips():
    rng = np.random.default_rng(21)
    n = 20000
    players = [_player("1", "Nikola Jokic", {"pts": 24.0, "reb": 12.0, "ast": 9.0}),
               _player("2", "Jamal Murray", {"pts": 20.0, "reb": 4.0, "ast": 6.0}, team="LAL"),   # a slip needs two teams
               _player("3", "Bench Guy", {"pts": 8.0, "reb": 3.0, "ast": 1.0}, exp_min=10.0),
               _player("4", "Doubtful Star", {"pts": 25.0, "reb": 5.0, "ast": 5.0}, play_prob=0.8)]
    sims = {p["id"]: _sims(rng, n, {k: v for k, v in p["exp_line"].items() if k in ("pts", "reb", "ast")},
                           play_prob=p["play_prob"]) for p in players}
    dk = [_dk("1", "pts", 24.5, -140, 110),      # market median above the number
          _dk("1", "pts", 27.5, 150, -180),      # alt line must be ignored
          _dk("1", "reb", 11.5, -110, -110),
          _dk("1", "ast", 8.5, -115, -105),
          _dk("2", "pts", 20.5, -110, -110),
          _dk("2", "ast", 5.5, -130, 100),
          _dk("3", "pts", 7.5, -110, -110),
          _dk("4", "pts", 24.5, -110, -110)]
    pp = [{"player_id": "1", "stat": "pts", "line": 23.5, "odds_type": "standard", "status": "pre_game", "projection_id": "a"},
          {"player_id": "1", "stat": "pra", "line": 44.5, "odds_type": "standard", "status": "pre_game", "projection_id": "b"},
          {"player_id": "1", "stat": "reb", "line": 11.5, "odds_type": "goblin", "status": "pre_game", "projection_id": "c"},
          {"player_id": "2", "stat": "pts", "line": 30.5, "odds_type": "standard", "status": "pre_game", "projection_id": "d"},
          {"player_id": "2", "stat": "reb", "line": 3.5, "odds_type": "standard", "status": "pre_game", "projection_id": "e"},
          {"player_id": "2", "stat": "ast", "line": 4.5, "odds_type": "standard", "status": "pre_game", "projection_id": "f"}]
    game = {"id": "g1", "matchup": "LAL @ DEN", "tip": "2026-11-08T02:00Z"}
    legs = pickem.build_legs(players, sims, dk, pp, {"s": 0.5}, game=game)
    by = {(l["player_id"], l["stat"]): l for l in legs}
    assert all(l["player_id"] in ("1", "2") for l in legs)               # bench (<12 min) and doubtful (<0.9) dropped
    assert ("2", "pts") not in by                                        # 30.5 vs 20.5: data error, dropped
    assert ("2", "reb") not in by                                        # PP line but no DK anchor and not a combo
    j = by[("1", "pts")]
    assert j["line"] == 23.5 and j["line_source"] == "prizepicks" and j["dk_line"] == 24.5 and j["dk_over_price"] == -140
    assert j["side"] == "more" and j["p_side"] > j["p_over_mkt"] > 0.5  # one point under a skewed DK number
    assert j["market_median"] > 24.0 and abs(j["p_more"] + j["p_less"] - 1) < 1e-9
    assert j["anchor"] == "dk" and j["anchor_m"] > 1.0 and j["proj"] == 24.0
    assert abs(j["p_side_shrunk"] - pickem.shrink(j["p_side"], j["be_ref"], 0.5)) < 1e-12
    pra = by[("1", "pra")]
    assert pra["anchor"] == "components" and pra["dk_line"] is None and pra["line_source"] == "prizepicks"
    assert len(pra["anchor_components"]) == 3 and pra["line"] == 44.5
    assert ("1", "reb") not in by   # live board offers this stat only as a Goblin: not playable at standard payouts -> no leg
    assert ("1", "ast") not in by   # live board does not list it at all -> no DK stand-in leg on a live night
    assert by[("2", "ast")]["line_source"] == "prizepicks" and by[("2", "ast")]["line"] == 4.5
    assert abs(by[("2", "pts")]["p_side"] - 0.5) < 0.1 if ("2", "pts") in by else True
    assert legs == sorted(legs, key=lambda l: (-l["p_side_shrunk"], l["id"]))
    assert pickem.fmt_leg_label(j) == "Nikola Jokic More 23.5 Pts" and j["label"] == pickem.fmt_leg_label(j)
    # hit matrix reproduces the leg probabilities (all players play here)
    H = pickem.hit_matrix(legs, sims)
    assert H.shape == (len(legs), n) and H.dtype == np.uint8
    for i, l in enumerate(legs):
        assert abs(H[i].mean() - l["p_side"] * (1 - l["p_void"])) < 0.01
    # the DK-anchored leg reproduces the market probability at the DK line
    xa = pickem._anchored_draws(j, sims["1"])[sims["1"]["_plays"]]
    assert abs(float(np.mean(np.rint(xa) > 24.5)) - j["p_over_mkt"]) <= 0.005
    slips = pickem.enumerate_slips(legs, H, s=0.5)
    assert slips and all(len(s["legs"]) >= 2 for s in slips)
    for s in slips:
        players_in = [by_id for by_id in (next(l["player_id"] for l in legs if l["id"] == x) for x in s["legs"])]
        assert len(set(players_in)) == len(players_in)
    block = pickem.build_pickem_block(legs, sims, source="prizepicks", fetched_at="2026-11-07T15:00:00Z")
    assert block["platform"] == "PrizePicks" and block["source"] == "prizepicks" and block["min_entry"] == 5.0
    assert set(block["breakeven"]) == set(pickem.SLIP_TYPES) and block["payouts"]["power"]["2"] == 3.0
    assert block["sims"] == {"n": 0, "legs": [], "bits": ""}          # slips are priced exactly; no joint draws are published
    back = pickem.unpack_bits(pickem.pack_bits(H), len(legs), n)
    assert np.array_equal(back, H)
    assert all(isinstance(l["id"], str) for l in block["legs"]) and all(x["verdict"] in ("play", "marginal", "skip") for x in block["slips"])
    assert any("assumption" in note.lower() for note in block["notes"])
    assert set(block["slips"][0]) >= {"id", "type", "legs", "p_dist", "ev_mult", "ev", "ev_shrunk",
                                      "growth_at_min_entry_per_100", "verdict"}
    import json
    json.dumps(block)                                                    # serialisable as is
    empty = pickem.build_pickem_block([], {}, source="dk_proxy")
    assert empty["legs"] == [] and empty["slips"] == [] and empty["sims"]["bits"] == ""


def test_lean_moves_a_component_anchored_combo_once():
    """A combo with no DraftKings line of its own is anchored through its components. The lean must move
    it by the lean, as it moves a stat DraftKings does post, not once per component."""
    rng = np.random.default_rng(9)
    n = 20000
    players = [_player("7", "Combo Guy", {"pts": 21.0, "reb": 6.0, "ast": 4.5})]
    sims = {"7": _sims(rng, n, {"pts": 21.0, "reb": 6.0, "ast": 4.5})}
    dk = [_dk("7", "pts", 21.5, -110, -110), _dk("7", "reb", 6.5, -110, -110), _dk("7", "ast", 4.5, -110, -110)]
    pp = [{"player_id": "7", "stat": "pra", "line": 33.5, "odds_type": "standard", "status": "pre_game", "projection_id": "x"},
          {"player_id": "7", "stat": "pts", "line": 21.5, "odds_type": "standard", "status": "pre_game", "projection_id": "y"}]
    game = {"id": "g", "matchup": "LAL @ DEN", "tip": "2026-11-08T02:00Z"}
    base = {l["stat"]: l for l in pickem.build_legs(players, sims, dk, pp, {"lean": 0.0}, game=game)}
    lean = {l["stat"]: l for l in pickem.build_legs(players, sims, dk, pp, {"lean": 0.03}, game=game)}
    assert base["pra"]["anchor"] == "components" and lean["pra"]["anchor"] == "components"
    assert abs((base["pra"]["p_more"] - lean["pra"]["p_more"]) - 0.03) < 0.004
    assert abs((base["pts"]["p_more"] - lean["pts"]["p_more"]) - 0.03) <= 0.011       # a posted stat: two anchoring tolerances
    # the stored anchoring parameters still rebuild the leaned draws
    xa = pickem._anchored_draws(lean["pra"], sims["7"])[sims["7"]["_plays"]]
    assert abs(float(np.mean(np.rint(xa) > 33.5)) - lean["pra"]["p_more"]) < 1e-9


def test_slips_are_priced_exactly_not_from_the_luckiest_draws():
    """Every leg truly 0.57: by exact binomial no slip grows $100 at a $5 entry (best is flex5 at +9.3%
    after the shrink). Pricing thousands of combinations on one set of draws used to list a dozen 'play'
    slips at +12% to +14%."""
    be = pickem.breakevens()
    legs = [{"id": f"L{i}", "player_id": str(i), "team": f"T{i % 4}", "p_side": 0.57,
             "p_side_shrunk": float(pickem.shrink(0.57, be["power2"], 0.5)), "label": f"L{i}"} for i in range(9)]
    rng = np.random.default_rng(200)
    noisy = (rng.random((9, 20000)) < 0.57).astype(np.uint8)          # the draws are ignored when legs carry p_side
    slips = pickem.enumerate_slips(legs, noisy, pool=9)
    assert slips and not any(x["verdict"] == "play" for x in slips)
    top = slips[0]
    assert top["type"] == "flex5"
    assert abs(top["ev_shrunk"] - pickem.slip_ev_at(float(pickem.shrink(0.57, be["flex5"], 0.5)), "flex5")) < 1e-9
    assert abs(top["ev_shrunk"] - 0.0930) < 5e-4 and top["growth_at_min_entry_per_100"] < 0
    assert all(abs(x["ev_shrunk"] - top["ev_shrunk"]) < 1e-12 for x in slips if x["type"] == "flex5")   # identical legs, identical value


def test_void_pick_drops_the_slip_a_size():
    """A pick that does not play drops the slip one size at that size's payout; under two live picks
    the entry is refunded (the rule ledger.settle_pickem grades by)."""
    o = pickem.slip_outcomes([0.6, 0.6], [0.1, 0.1], "power2")
    assert abs(o["probs"].sum() - 1) < 1e-12
    ev = float(o["probs"] @ o["mults"]) - 1
    assert abs(ev - 0.81 * (3 * 0.36 - 1)) < 1e-12                   # both live 81% of the time, refunded otherwise
    # flex5 with one void becomes a 4-pick flex: all four hit pays 6x
    o5 = pickem.slip_outcomes([1.0, 1.0, 1.0, 1.0, 1.0], [1.0, 0.0, 0.0, 0.0, 0.0], "flex5")
    assert abs(float(o5["probs"] @ o5["mults"]) - 6.0) < 1e-12
    # a 2-pick flex does not exist, so a 3-pick flex with one void pays the 2-pick power multiple
    o3 = pickem.slip_outcomes([1.0, 1.0, 1.0], [1.0, 0.0, 0.0], "flex3")
    assert abs(float(o3["probs"] @ o3["mults"]) - 3.0) < 1e-12
    # the slip value seen by enumerate_slips includes the void chance and a tie on a whole-number line
    legs = [{"id": "a", "player_id": "1", "team": "A", "p_side": 0.6, "p_side_shrunk": 0.6, "p_void": 0.1},
            {"id": "b", "player_id": "2", "team": "B", "p_side": 0.6, "p_side_shrunk": 0.6, "p_void": 0.0, "p_tie": 0.1}]
    sl = pickem.enumerate_slips(legs, None, s=1.0, max_legs=2)[0]
    assert abs(sl["ev"] - 0.81 * (3 * 0.36 - 1)) < 1e-12 and abs(sl["p_all"] - 0.81 * 0.36) < 1e-12


def test_single_team_slips_are_not_offered():
    legs = [{"id": f"l{i}", "player_id": str(i), "team": t, "p_side": 0.66, "p_side_shrunk": 0.62} for i, t in enumerate(["DEN", "DEN", "DEN"])]
    assert pickem.enumerate_slips(legs, None, max_legs=3) == []
    legs[2]["team"] = "LAL"
    slips = pickem.enumerate_slips(legs, None, max_legs=3)
    assert slips and all("l2" in x["legs"] for x in slips)


def test_under_lean_moves_more_to_less():
    """config.PICKEM_LEAN: the lean comes off P(more) and goes onto P(less) at the DraftKings line,
    the market price itself is reported unchanged, and the take-it lines follow."""
    rng = np.random.default_rng(5)
    n = 20000
    players = [_player("1", "Juiced Over", {"pts": 20.0, "reb": 6.0, "ast": 5.0}),
               _player("2", "Juiced Under", {"pts": 18.0, "reb": 5.0, "ast": 4.0})]
    sims = {p["id"]: _sims(rng, n, {k: v for k, v in p["exp_line"].items() if k in ("pts", "reb", "ast")}) for p in players}
    dk = [_dk("1", "ast", 4.5, -165, 130), _dk("2", "ast", 4.5, 130, -165)]     # no-vig 0.589 on the priced side
    game = {"id": "g1", "matchup": "LAL @ DEN", "tip": "2026-11-08T02:00Z"}
    base = {l["player_id"]: l for l in pickem.build_legs(players, sims, dk, None, {"s": 0.5}, game=game)}
    lean = {l["player_id"]: l for l in pickem.build_legs(players, sims, dk, None, {"s": 0.5, "lean": 0.03}, game=game)}
    for pid in ("1", "2"):
        assert base[pid]["lean"] == 0.0 and lean[pid]["lean"] == 0.03
        assert abs(base[pid]["p_over_mkt"] - lean[pid]["p_over_mkt"]) < 1e-12
        assert abs(lean[pid]["p_over_anchor"] - (lean[pid]["p_over_mkt"] - 0.03)) < 1e-12
        assert abs((base[pid]["p_more"] - lean[pid]["p_more"]) - 0.03) <= 0.011    # two anchoring tolerances
    assert base["1"]["side"] == "more" and base["2"]["side"] == "less"
    assert lean["1"]["p_side"] < base["1"]["p_side"] and lean["2"]["p_side"] > base["2"]["p_side"]
    # the Less pick is a clear take (60%+) at the sportsbook's own line once the lean is in; it was not before
    assert base["2"]["p_less"] < 0.60 <= lean["2"]["p_less"]
    assert lean["2"]["less_if_at_least"] is not None and lean["2"]["less_if_at_least"] <= 4.5
    assert base["2"]["less_if_at_least"] is None or base["2"]["less_if_at_least"] > 4.5
    # the More pick falls below the 2-pick break-even, so it is no longer lifted or ranked first
    assert lean["1"]["p_side"] < pickem.breakeven_probability("power2") < base["1"]["p_side"]
    ranked = pickem.build_legs(players, sims, dk, None, {"s": 0.5, "lean": 0.03}, game=game)
    assert ranked[0]["player_id"] == "2"


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} tests passed")
