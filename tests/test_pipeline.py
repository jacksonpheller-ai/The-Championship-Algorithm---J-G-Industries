"""Integration tests for the growth-policy wiring (pipeline helpers, engine context hook,
backtest mirror, ledger pick'em grading, replay seed combination, CLI label logic).
Run: python tests/test_pipeline.py"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from jghedge import ledger, pipeline, replay  # noqa: E402
from jghedge.config import load_params  # noqa: E402
from jghedge.model.engine import Engine  # noqa: E402
from jghedge.portfolio import policy  # noqa: E402


def test_run_label_and_slate_day():
    assert pipeline.run_label(datetime(2026, 11, 8, 2, 31, tzinfo=timezone.utc)) == "opener"      # 21:31 ET
    assert pipeline.run_label(datetime(2026, 11, 7, 15, 41, tzinfo=timezone.utc)) == "morning"    # 10:41 ET
    assert pipeline.run_label(datetime(2026, 11, 7, 20, 41, tzinfo=timezone.utc)) == "pretip"     # 15:41 ET
    assert pipeline.run_label(datetime(2026, 11, 7, 22, 17, tzinfo=timezone.utc)) == "afternoon"  # 17:17 ET
    os.environ["JG_RUN_LABEL"] = "manual"
    try:
        assert pipeline.run_label() == "manual"
    finally:
        del os.environ["JG_RUN_LABEL"]
    # the evening opener run prices TOMORROW's slate; a morning run prices today's
    assert str(pipeline.slate_day("opener", datetime(2026, 11, 8, 2, 31, tzinfo=timezone.utc))) == "2026-11-08"
    assert str(pipeline.slate_day("morning", datetime(2026, 11, 7, 15, 41, tzinfo=timezone.utc))) == "2026-11-07"


def _engine_stub():
    """A minimal Engine whose params are the defaults (no history needed for blend_with_market)."""
    eng = Engine.__new__(Engine)
    eng.params = load_params()
    eng.params["w_model_vs_market_total"] = 0.0
    return eng


def test_blend_adds_context_after_market_blend():
    eng = _engine_stub()
    proj = {"m_model": 3.0, "t_model": 240.0}
    odds = {"close": {"spread_home": -3.0, "spread_home_price": -110, "spread_away_price": -110,
                      "total": 225.0, "over_price": -110, "under_price": -110}}
    base = eng.blend_with_market(proj, odds)
    assert abs(base["t_final"] - 225.0) < 0.5 and base["t_context"] == 0.0   # weight 0: t_model discarded
    late = eng.blend_with_market(proj, odds, {"home_gp": 72, "away_gp": 71, "season_type": 2,
                                               "home_winpct": 0.5, "away_winpct": 0.5})
    from jghedge.model.context import CONTEXT_PARAMS
    adj = CONTEXT_PARAMS["late_adj"]
    assert abs(late["t_final"] - base["t_final"] - adj) < 1e-9 and late["t_context"] == adj
    killed = eng.blend_with_market(proj, odds, {"home_gp": 72, "away_gp": 71, "season_type": 2, "kill": True})
    assert killed["t_context"] == 0.0
    playoff = eng.blend_with_market(proj, odds, {"home_gp": 82, "away_gp": 82, "season_type": 3})
    assert playoff["t_context"] == 0.0


def test_backtest_mirror_adds_same_term():
    from jghedge import backtest
    p = load_params()
    p["w_model_vs_market_total"] = 0.0
    feats = {f: 0.0 for f in backtest.SIT_FEATURES}
    row = {"m_ratings_base": 2.0, "m_elo_base": 2.0, "t_model": 230.0, "b2b_any": 0, "pace": 100.0,
           "spread_home": -3.0, "spread_home_price": -110.0, "spread_away_price": -110.0,
           "total": 225.0, "over_price": -110.0, "under_price": -110.0, **feats}
    preds = pd.DataFrame([{**row, "games_h": 10, "games_a": 12}, {**row, "games_h": 75, "games_a": 71},
                          {**row, "games_h": 80, "games_a": 80}])
    d = backtest.with_params(preds, p)
    from jghedge.model.context import CONTEXT_PARAMS
    late, very = CONTEXT_PARAMS["late_adj"], CONTEXT_PARAMS["very_late_adj"]
    assert list(d["t_context"]) == [0.0, late, very]
    assert abs((d["t_final"].iloc[1] - d["t_final"].iloc[0]) - late) < 1e-9


def test_context_kill_switch_counts_late_games():
    rows = []
    teams = ["AAA", "BBB"]
    day0 = pd.Timestamp("2026-01-01")
    for i in range(80):
        rows.append({"id": str(i), "date": day0 + pd.Timedelta(days=i), "season": 2027, "season_type": 2,
                     "home": teams[i % 2], "away": teams[(i + 1) % 2], "total": 220.0,
                     "total_pts": 230.0 if i % 3 else 210.0})
    gdf = pd.DataFrame(rows)
    ks = pipeline.context_kill_switch(gdf, 2027, pd.Timestamp("2026-06-01"))
    assert ks["n"] == 10 and ks["kill"] is False          # games 71-80 qualify; window of 100 not reached
    ks2 = pipeline.context_kill_switch(gdf, 2027, pd.Timestamp("2026-06-01"), {"kill_switch_window": 9})
    assert ks2["over_rate"] is not None and ks2["kill"] is False   # 2 of 3 over
    gdf["total_pts"] = 210.0
    ks3 = pipeline.context_kill_switch(gdf, 2027, pd.Timestamp("2026-06-01"), {"kill_switch_window": 9})
    assert ks3["kill"] is True


def test_props_regime_from_logs_and_results():
    with tempfile.TemporaryDirectory() as td:
        logd, resd = Path(td) / "log", Path(td) / "res"
        logd.mkdir(), resd.mkdir()
        rows = [{"game_id": "g", "player_id": str(i), "stat": "pts", "line": 20.5, "over": -115, "under": -105}
                for i in range(40)]
        for d in range(12):
            day = f"2026-10-{10 + d:02d}"
            (logd / f"{day}.json").write_text(json.dumps(rows))
            # 70% unders
            players = {str(i): {"pts": 15.0 if i < 28 else 25.0} for i in range(40)}
            (resd / f"{day}.json").write_text(json.dumps({"date": day, "games": {}, "players": {"g": players}}))
        out = pipeline.props_regime("2026-10-25", log_dir=logd, results_dir=resd)
        assert out["regime"] == "lean_under" and out["n"] == 480 and out["nights"] == 12
        assert abs(out["under_rate"] - 0.7) < 1e-9
        few = pipeline.props_regime("2026-10-25", {"min_lines": 1000}, log_dir=logd, results_dir=resd)
        assert few["regime"] == "neutral"
        # overs crushing -> stand down
        for d in range(12):
            day = f"2026-10-{10 + d:02d}"
            players = {str(i): {"pts": 25.0} for i in range(40)}
            (resd / f"{day}.json").write_text(json.dumps({"date": day, "games": {}, "players": {"g": players}}))
        assert pipeline.props_regime("2026-10-25", log_dir=logd, results_dir=resd)["regime"] == "stand_down"
        # missing dirs degrade to neutral
        assert pipeline.props_regime("2026-10-25", log_dir=Path(td) / "nope", results_dir=resd)["regime"] == "neutral"


def test_growth_cache_recomputes_only_on_change():
    with tempfile.TemporaryDirectory() as td:
        rp, cp = Path(td) / "replay_open.json", Path(td) / "growth_grid.json"
        nightly = [{"date": f"d{i}", "games": 3, "bets": 2, "staked": 0.04, "ret": 0.01 * ((-1) ** i), "bank": 1000}
                   for i in range(60)]
        rp.write_text(json.dumps({"2026": {"nightly": nightly}}))
        g1 = pipeline.growth_block_cached(rp, cp)
        assert cp.exists() and len(g1["grid"]) == 84 and "p_loss" not in g1["grid"][0]
        assert {"bankroll", "unit", "k", "median", "p10", "p90", "p_bust", "p_dd50"} <= set(g1["grid"][0])
        key1 = json.loads(cp.read_text())["key"]
        g2 = pipeline.growth_block_cached(rp, cp)
        assert g2 == g1 and json.loads(cp.read_text())["key"] == key1
        rp.write_text(json.dumps({"2026": {"nightly": nightly[:30]}}))
        g3 = pipeline.growth_block_cached(rp, cp)
        assert json.loads(cp.read_text())["key"] != key1 and "30 nights" in g3["basis"]
        missing = pipeline.growth_block_cached(Path(td) / "none.json", cp)
        assert missing["grid"] == []


def test_fliff_instruction_text():
    b = {"id": "x", "kind": "total", "side": "over", "line": 228.5, "max_line": 229.5, "american": -110}
    assert pipeline.fliff_instruction(b) == "Over 228.5 at -110 or better; skip above 229.5"
    u = {"id": "y", "kind": "total", "side": "under", "line": 221.0, "max_line": 219.5, "american": -105}
    assert pipeline.fliff_instruction(u) == "Under 221 at -105 or better; skip below 219.5"
    blk = pipeline.fliff_block([{**b, "label": "A/B Over 228.5"}], [{"id": "parlay-1", "legs": ["x"], "leg_labels": ["A/B Over 228.5"],
                                                                     "american": 264}], False)
    assert blk["platform"] == "Fliff" and blk["min_bet"] == 0.25 and len(blk["instructions"]) == 2
    assert any("ASSUMPTION" in n for n in blk["notes"])


def test_ledger_pickem_grading_rules():
    pay = {"power": {"2": 3, "3": 5, "4": 10}, "flex": {"3": [2.25, 1.25], "4": [5, 1.5], "5": [10, 2, 0.4], "6": [25, 2, 0.4]}}
    assert ledger.settle_pickem("power3", ["win", "win", "win"], pay) == ("win", 4.0, 3)
    assert ledger.settle_pickem("power3", ["win", "loss", "win"], pay) == ("loss", -1.0, 2)
    assert ledger.settle_pickem("flex4", ["win", "win", "win", "loss"], pay) == ("win", 0.5, 3)
    assert ledger.settle_pickem("flex3", ["win", "void", "win"], pay) == ("win", 2.0, 2)       # reduced to 2-pick power
    assert ledger.settle_pickem("power2", ["win", "void"], pay) == ("push", 0.0, 1)             # below 2 picks: refund
    assert ledger.settle_pickem("flex6", ["win"] * 4 + ["loss", "void"], pay) == ("win", 1.0, 4)  # -> flex5, 4 of 5
    assert ledger.settle_pickem("power3", ["win", "push", "win"], pay) == ("win", 2.0, 2)       # tie drops the pick like a void
    # a Flex tier that pays back less than the entry is a loss of the difference, not a push
    r, u, h = ledger.settle_pickem("flex5", ["win", "win", "win", "loss", "loss"], pay)
    assert r == "loss" and abs(u + 0.6) < 1e-12 and h == 3
    assert ledger.settle_pickem("flex3", ["win", "win", "loss"], pay)[0] == "win"                # 1.25x here
    assert ledger.settle_pickem("flex3", ["win", "win", "loss"], {"power": {}, "flex": {"3": [3.0, 1.0]}}) == ("push", 0.0, 2)
    rows = [{"type": "pickem", "kind": "pickem", "paper": True, "result": r, "units": u, "frac": 0.0, "date": "2099-01-01", "legs": []}]
    assert ledger.summary(rows)["pickem_paper"]["losses"] == 1
    games = {"g": {"home_score": 110, "away_score": 104}}
    players = {("g", "7"): {"pts": 22, "reb": 5}}
    leg = {"game_id": "g", "kind": "pickem", "side": "more", "line": 21.5, "player_id": "7", "stat": "pts"}
    assert ledger._settle_leg(leg, games, players) == "win"
    assert ledger._settle_leg({**leg, "side": "less"}, games, players) == "loss"
    assert ledger._settle_leg({**leg, "line": 22.0}, games, players) == "void"                   # exactly on the line
    assert ledger._settle_leg({**leg, "player_id": "9"}, games, players) == "void"               # did not play


def test_ledger_record_reference_ticket_and_pickem():
    res = {"date": "2099-01-01",
           "bets": [{"id": "g-to-o", "game_id": "g", "kind": "total", "label": "A/B Over 220.5", "side": "over", "line": 220.5,
                     "price": -110, "dec": 1.9091, "p_final": 0.56, "ev": 0.04, "shrunk_ev": 0.02, "max_line": 221.5}],
           "parlays": [],
           "allocations": {"reference": {"items": [{"ref": "g-to-o", "units": 4, "stake": 2.0, "frac": 0.02}]}},
           "pickem": {"platform": "PrizePicks", "source": "dk_proxy", "min_entry": 5.0,
                      "payouts": {"power": {"2": 3.0}, "flex": {}},
                      "legs": [{"id": "l1", "game_id": "g", "player_id": "7", "player": "A", "stat": "pts", "line": 21.5,
                                "side": "more", "label": "A More 21.5 Pts", "p_side": 0.6},
                               {"id": "l2", "game_id": "g", "player_id": "8", "player": "B", "stat": "reb", "line": 6.5,
                                "side": "less", "label": "B Less 6.5 Reb", "p_side": 0.6}],
                      "slips": [{"id": "pk-1-power2", "type": "power2", "legs": ["l1", "l2"], "verdict": "play",
                                 "p_all": 0.36, "ev": 0.08, "ev_shrunk": 0.04, "label": "A + B"},
                                {"id": "pk-2-power2", "type": "power2", "legs": ["l1", "l2"], "verdict": "skip"}]}}
    old = ledger.LEDGER_FILE
    with tempfile.TemporaryDirectory() as td:
        ledger.LEDGER_FILE = Path(td) / "ledger.json"
        try:
            n = ledger.record(res)
            rows = ledger.load()
            assert n == 2 and {r["type"] for r in rows} == {"single", "pickem"}
            pk = next(r for r in rows if r["type"] == "pickem")
            assert pk["frac"] == 0.05 and pk["slip_type"] == "power2" and len(pk["legs"]) == 2
            # grade by hand with the settlement rules used in grade()
            games = {"g": {"home_score": 110, "away_score": 104}}
            players = {("g", "7"): {"pts": 25}, ("g", "8"): {"reb": 4}}
            legs_res = [ledger._settle_leg(l, games, players) for l in pk["legs"]]
            assert ledger.settle_pickem(pk["slip_type"], legs_res, pk["payouts"]) == ("win", 2.0, 2)
            s = ledger.summary(rows)
            assert s["pending"] == 2
        finally:
            ledger.LEDGER_FILE = old


def test_replay_combine_seeds_layout():
    def run(off, end):
        return {"season": 2026, "seed_offset": off, "start_bankroll": 1000.0, "end_bankroll": end, "max_drawdown": 0.1,
                "stake_weighted_roi": 0.02, "flat_roi": 0.01, "bets": 100, "wins": 55, "losses": 45, "nights_bet": 80,
                "winning_nights": 40, "losing_nights": 40, "avg_predicted_ev": 0.05, "avg_shrunk_ev": 0.025,
                "verdicts": {"PASS": 50, "LIGHT": 60, "BET": 50},
                "nightly": [{"date": "d", "games": 1, "bets": 1, "staked": 0.02, "ret": 0.01, "bank": end}],
                "rows": [{"ref": "x"}], "by_kind": {}, "not_replayed": replay.NOT_REPLAYED,
                "small": {"10@0.5": {"start": 10, "unit": 0.5, "profile": "adaptive", "end": 9.0, "bets": 10, "wins": 5,
                                     "max_drawdown": 0.2, "bust_date": None, "tier_nights": {}}}}
    out = replay.combine_seeds({"0": run(0, 1100.0), "1000": run(1000, 900.0)})
    assert out["end_bankroll"] == 1000.0 and out["per_seed_end"] == {"0": 1100.0, "1000": 900.0}
    assert out["seeds_run"] == [0, 1000] and "nightly" in out["seeds"]["1000"] and "nightly" not in out["seeds"]["0"]
    assert out["small"]["10@0.5"]["end"] == 9.0
    # the growth loader sees two paths for the season
    from jghedge import growth
    data = growth.load_replay_nightly({"2026": json.loads(json.dumps(out))})
    assert data["seeds"]["2026"] == 2 and data["nights"] == 2


def test_policy_contract_keys_in_pipeline_blocks():
    assert set(policy.POLICY) == {"s", "tiers", "per_bet_cap", "night_cap", "opt_in_k"}
    assert policy.POLICY["s"] == {"total": 0.5, "parlay": 0.5, "prop": 0.0, "pickem": 0.5}
    # JSON-safe rounding helper
    assert pipeline._r4({"a": 0.123456, "b": [np.float64(1.0), float("inf")], "c": np.int64(3)}) == \
        {"a": 0.1235, "b": [1.0, None], "c": 3}


def test_pickem_lean_blends_prior_with_live_sample():
    prm = {"prior": 0.03, "prior_n": 4000, "max": 0.05}
    assert pipeline.pickem_lean({"regime": "neutral", "n": 120, "lean": None}, prm)["value"] == 0.03      # too few graded lines: prior only
    assert pipeline.pickem_lean(None, prm)["value"] == 0.03
    got = pipeline.pickem_lean({"n": 4000, "lean": 0.05}, prm)
    assert abs(got["value"] - 0.04) < 1e-9 and got["live_n"] == 4000 and got["live"] == 0.05               # equal weights
    assert pipeline.pickem_lean({"n": 100000, "lean": 0.20}, prm)["value"] == 0.05                         # clipped above
    assert pipeline.pickem_lean({"n": 100000, "lean": -0.10}, prm)["value"] == 0.0                         # overs beating their price: no lean


def _night(date, label, gen, items, bets, slips=(), verdict="BET", source="prizepicks"):
    legs = [{"id": f"l{i}", "game_id": "g", "player_id": str(i), "player": f"P{i}", "stat": "pts", "line": 20.5, "side": "less",
             "label": f"P{i} Less 20.5 Pts", "p_side": 0.62} for i in range(8)]
    return {"date": date, "generated": gen, "run_label": label, "bets": bets, "parlays": [], "night": {"verdict": verdict},
            "allocations": {"reference": {"items": items}},
            "pickem": {"platform": "PrizePicks", "source": source, "min_entry": 5.0, "payouts": {"power": {"2": 3.0}, "flex": {}}, "legs": legs,
                       "slips": [{"id": f"pk-{k}", "type": "power2", "legs": list(pair), "verdict": v, "verdict_if_lines_match": "play",
                                  "p_all": 0.38, "ev": 0.1, "ev_shrunk": 0.05, "label": "+".join(pair)} for k, (pair, v) in enumerate(slips)]}}


def _total(gid, side, line, max_line):
    return {"id": f"{gid}-to-{side[0]}", "game_id": gid, "kind": "total", "label": f"{gid} {side} {line}", "side": side, "line": line,
            "price": -110, "dec": 1.9091, "p_final": 0.56, "ev": 0.05, "shrunk_ev": 0.025, "max_line": max_line}


def test_ledger_caps_hold_for_the_night_not_for_each_run():
    """Several runs price one night. The reference ticket must stop at the 10% night cap and two pick'em
    entries in total, and a later run must not add the other side of a market the night already holds."""
    A_o, A_u, B_o, C_o = _total("A", "over", 228.5, 229.5), _total("A", "under", 236.5, 235.5), _total("B", "over", 220.5, 221.5), _total("C", "over", 231.5, 232.5)
    bets = [A_o, A_u, B_o, C_o]
    old = ledger.LEDGER_FILE
    with tempfile.TemporaryDirectory() as td:
        ledger.LEDGER_FILE = Path(td) / "ledger.json"
        try:
            r1 = _night("2099-01-01", "opener", "2099-01-01T02:31:00+00:00", [{"ref": "A-to-o", "frac": 0.04}, {"ref": "B-to-o", "frac": 0.04}], bets,
                        [(("l0", "l1"), "play"), (("l2", "l3"), "play")])
            assert ledger.record(r1) == 4
            r2 = _night("2099-01-01", "morning", "2099-01-01T15:41:00+00:00", [{"ref": "A-to-u", "frac": 0.02}, {"ref": "C-to-o", "frac": 0.05}], bets,
                        [(("l0", "l2"), "play"), (("l4", "l5"), "play")])
            n2 = ledger.record(r2)
            rows = ledger.load()
            refs = [r["ref"] for r in rows if r["type"] == "single"]
            assert "A-to-u" not in refs                                   # the night already holds the Over on game A
            assert "C-to-o" not in refs                                   # 4% + 4% + 5% would pass the 10% night cap
            assert n2 == 0 and sum(1 for r in rows if r["type"] == "pickem") == 2    # two slips for the night, not two per run
            r3 = _night("2099-01-01", "pretip", "2099-01-01T20:41:00+00:00", [{"ref": "C-to-o", "frac": 0.02}], bets)
            assert ledger.record(r3) == 1 and abs(sum(r["frac"] for r in ledger.load() if r["type"] == "single") - 0.10) < 1e-12
            # a new night starts again
            r4 = _night("2099-01-02", "opener", "2099-01-02T02:31:00+00:00", [{"ref": "A-to-u", "frac": 0.05}], bets, [(("l6", "l7"), "play")])
            assert ledger.record(r4) == 2
        finally:
            ledger.LEDGER_FILE = old


def test_ledger_records_live_slip_on_a_pass_night():
    """Pick'em does not depend on the game verdict: the page offers a live 'play' slip on a PASS night, so the ledger records it."""
    old = ledger.LEDGER_FILE
    with tempfile.TemporaryDirectory() as td:
        ledger.LEDGER_FILE = Path(td) / "ledger.json"
        try:
            res = _night("2099-01-03", "morning", "2099-01-03T15:00:00+00:00", [{"ref": "A-to-o", "frac": 0.02}], [_total("A", "over", 228.5, 229.5)],
                         [(("l0", "l1"), "play")], verdict="PASS")
            assert ledger.record(res) == 1
            row = ledger.load()[0]
            assert row["type"] == "pickem" and row["paper"] is False and row["frac"] == 0.05     # the game bet is not staked on a PASS
        finally:
            ledger.LEDGER_FILE = old


def test_closing_line_value_uses_the_real_close():
    """grade() takes the close from the game's history record; the desk's last snapshot can be hours old."""
    from jghedge.data import lines as lines_mod
    leg = {"game_id": "g", "kind": "total", "side": "over", "line": 226.5}
    assert lines_mod.clv(leg, {"total": 227.5}) == 1.0 and lines_mod.clv({**leg, "side": "under"}, {"total": 227.5}) == -1.0
    old_file, old_load, old_close = ledger.LEDGER_FILE, ledger.history.load_season, lines_mod.closing_line
    with tempfile.TemporaryDirectory() as td:
        ledger.LEDGER_FILE = Path(td) / "ledger.json"
        try:
            ledger.save([{"date": "2099-01-01", "ref": "g-to-o", "type": "single", "kind": "total", "dec": 1.9091, "frac": 0.02, "result": None,
                          "legs": [{"id": "g-to-o", "game_id": "g", "kind": "total", "side": "over", "line": 226.5}]}])
            ledger.history.load_season = lambda season: [{"id": "g", "home_score": 120.0, "away_score": 110.0, "players": [],
                                                          "odds": {"close": {"total": 227.5, "spread_home": -3.5}}}]
            lines_mod.closing_line = lambda date, gid: {"total": 226.5}      # the desk's own last look: the bet's own line
            assert ledger.grade(2099) == 1
            e = ledger.load()[0]
            assert e["result"] == "win" and e["legs"][0]["close_line"] == 227.5 and e["legs"][0]["clv"] == 1.0
        finally:
            ledger.LEDGER_FILE, ledger.history.load_season, lines_mod.closing_line = old_file, old_load, old_close


def test_moved_past_compares_with_the_number_the_desk_took():
    bets = [_total("A", "over", 232.5, 233.0), _total("B", "over", 221.0, 222.0), _total("C", "under", 230.5, 229.5)]
    entries = [{"date": "2099-01-01", "ref": "A-to-o", "type": "single", "kind": "total", "max_line": 229.5, "recorded_at": "t0",
                "legs": [{"side": "over", "line": 228.5}]},
               {"date": "2099-01-01", "ref": "B-to-o", "type": "single", "kind": "total", "max_line": 221.5, "recorded_at": "t0",
                "legs": [{"side": "over", "line": 220.5}]},
               {"date": "2099-01-01", "ref": "C-to-u", "type": "single", "kind": "total", "max_line": 231.5, "recorded_at": "t0",
                "legs": [{"side": "under", "line": 232.5}]},
               {"date": "2098-12-31", "ref": "A-to-o", "type": "single", "kind": "total", "max_line": 200.0, "legs": [{"side": "over", "line": 199.5}]}]
    mv = {m["bet_id"]: m for m in pipeline.moved_past("2099-01-01", bets, set(), entries)}
    assert set(mv) == {"A-to-o", "B-to-o", "C-to-u"}                       # yesterday's row is ignored
    assert mv["A-to-o"]["still_ok"] is False and mv["A-to-o"]["line_then"] == 228.5 and mv["A-to-o"]["line_now"] == 232.5
    assert mv["B-to-o"]["still_ok"] is True                                 # 221.0 is inside the 221.5 the desk accepted
    assert mv["C-to-u"]["still_ok"] is False                                # an Under taken at 232.5 down to 231.5; now 230.5
    # a bet that still qualifies at the new line is not "moved past", whatever the old number was
    assert pipeline.moved_past("2099-01-01", bets, {"A-to-o"}, entries)[0]["still_ok"] is True


def test_props_log_is_merged_by_game():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        pipeline.log_props("2099-01-01", [{"game_id": "g1", "player_id": "1", "stat": "pts", "line": 20.5, "over": -110, "under": -110},
                                          {"game_id": "g2", "player_id": "2", "stat": "pts", "line": 10.5, "over": -110, "under": -110}], log_dir=d)
        # a later run sees only the game that has not tipped, with a moved line
        pipeline.log_props("2099-01-01", [{"game_id": "g2", "player_id": "2", "stat": "pts", "line": 11.5, "over": -115, "under": -105}], log_dir=d)
        rows = json.loads((d / "2099-01-01.json").read_text())
        assert {r["game_id"] for r in rows} == {"g1", "g2"} and len(rows) == 2
        assert next(r for r in rows if r["game_id"] == "g2")["line"] == 11.5


def test_pending_games_restore_back_to_backs_on_an_evening_run():
    """An evening run prices tomorrow while tonight's games are unfinished: they must still count for rest."""
    from datetime import date
    from jghedge.model.situational import schedule_features
    done = pd.DataFrame([{"id": "old", "date": pd.Timestamp("2098-12-29"), "tip": "2098-12-30T00:00Z", "home": "LAL", "away": "DEN",
                          "neutral": False, "venue_city": "Los Angeles"}])
    slate = [{"id": "tom", "date": "2099-01-02T00:00Z", "home": "ATL", "away": "LAL", "neutral": False, "venue_city": "Atlanta"}]
    board = {date(2098, 12, 30): [],
             date(2098, 12, 31): [{"id": "ton", "date": "2099-01-01T02:00Z", "home": "LAL", "away": "BOS", "neutral": False, "venue_city": "Los Angeles", "state": "in", "completed": False},
                                  {"id": "ppd", "date": "2099-01-01T00:00Z", "home": "ATL", "away": "MIA", "neutral": False, "venue_city": "Atlanta", "state": "post", "completed": False},
                                  {"id": "old", "date": "2098-12-30T00:00Z", "home": "LAL", "away": "DEN", "neutral": False, "venue_city": "Los Angeles", "state": "post", "completed": True}]}
    pend = pipeline.pending_games(None, date(2099, 1, 1), {"old", "tom"}, boards=board)
    assert [g["id"] for g in pend] == ["ton"]                                # the postponed game and the known one are left out
    without = schedule_features(pd.concat([done, pipeline._schedule_rows(slate)], ignore_index=True))
    with_p = schedule_features(pd.concat([done, pipeline._schedule_rows(pend), pipeline._schedule_rows(slate)], ignore_index=True))
    assert int(without.loc["tom", "a_b2b"]) == 0 and int(with_p.loc["tom", "a_b2b"]) == 1


def test_injury_wording_negation():
    from jghedge.data.espn import play_probability as pp

    def item(status, fs, comment):
        return {"status": status, "details": {"fantasyStatus": {"abbreviation": fs}} if fs else {}, "shortComment": comment}
    assert pp(item("Out", "OUT", "Embiid (knee) is not expected to play Monday against the Celtics."))[0] == 0.0
    assert pp(item("Out", "OUT", "Leonard (knee) isn't expected to play in Tuesday's game."))[0] == 0.0
    assert pp(item("Day-To-Day", None, "He is not expected to play Monday."))[0] == 0.15
    assert pp(item("Day-To-Day", "GTD", "Doncic is unlikely to play Wednesday."))[0] == 0.15
    assert pp(item("Day-To-Day", None, "James (foot) will not play Tuesday."))[0] == 0.0
    assert pp(item("Day-To-Day", "GTD", "It is unclear if he will play again this season."))[0] == 0.5
    assert pp(item("Day-To-Day", "GTD", "Young is expected to play Friday."))[0] == 0.9
    assert pp(item("Day-To-Day", "GTD", "Smith (finger) is probable for Sunday's game."))[0] == 0.9


def test_run_plan_prices_tonight_and_tomorrow():
    from datetime import date
    at = lambda h, m=7: datetime(2026, 11, 12, h, m, tzinfo=timezone.utc)      # Eastern is UTC-5 in November
    d12, d13 = date(2026, 11, 12), date(2026, 11, 13)
    assert pipeline.run_plan(at(13)) == [(d12, "morning"), (d13, "opener")]            # 8:07 am ET
    assert pipeline.run_plan(at(19)) == [(d12, "pretip"), (d13, "opener")]             # 2:07 pm ET
    assert pipeline.run_plan(at(22)) == [(d12, "afternoon"), (d13, "opener")]          # 5:07 pm ET
    # 9:07 pm ET: tonight's late games are still tonight's slate (an evening look, not an opener); tomorrow is the opener
    assert pipeline.run_plan(datetime(2026, 11, 13, 2, 7, tzinfo=timezone.utc)) == [(d12, "afternoon"), (d13, "opener")]
    # 12:07 am ET: the calendar has turned, so "tonight" is the new day and its first look is an opener
    assert pipeline.run_plan(datetime(2026, 11, 13, 5, 7, tzinfo=timezone.utc)) == [(d13, "opener"), (date(2026, 11, 14), "opener")]


def test_pick_slates():
    today, tomorrow, empty = {"date": "a", "games": [1]}, {"date": "b", "games": [1]}, {"date": "c", "games": []}
    assert pipeline.pick_slates(today, tomorrow) == (today, tomorrow)                   # both: tonight leads, tomorrow rides along
    assert pipeline.pick_slates(today, None) == (today, None)
    assert pipeline.pick_slates(empty, tomorrow) == (tomorrow, None)                    # tonight is over: tomorrow is the page
    assert pipeline.pick_slates(None, tomorrow) == (tomorrow, None)
    assert pipeline.pick_slates(empty, None) == (empty, None)                           # nothing anywhere: the empty night is published


def test_line_snapshots_skip_games_without_a_line():
    from jghedge.data import lines
    with tempfile.TemporaryDirectory() as td:
        old_dir = lines.LINES_DIR
        lines.LINES_DIR = Path(td)
        try:
            posted = {"id": "1", "odds": {"close": {"total": 221.5, "over_price": -110, "under_price": -110}}}
            waiting = {"id": "2", "odds": {"close": {}}}
            none = {"id": "3", "odds": None}
            lines.record("2099-01-02", "opener", [waiting, none])                 # tomorrow before any line: no snapshot at all
            assert lines.load("2099-01-02") == []
            lines.record("2099-01-02", "opener", [posted, waiting, none])
            snaps = lines.load("2099-01-02")
            assert len(snaps) == 1 and list(snaps[0]["games"]) == ["1"]
            assert lines.first_snapshot("2099-01-02", "2") is None and lines.first_snapshot("2099-01-02", "1")["total"] == 221.5
        finally:
            lines.LINES_DIR = old_dir


def test_write_outputs_main_and_next():
    from jghedge import site
    res = lambda d, gen: {"date": d, "generated": gen, "games": [{"id": "1"}], "bets": [], "allocations": {}, "parlays": [], "picks": {}, "_R": 1}
    with tempfile.TemporaryDirectory() as td:
        old_p, old_s = pipeline.SITE_DATA, site.SITE_DATA
        pipeline.SITE_DATA = site.SITE_DATA = Path(td)
        try:
            pipeline.write_outputs(res("2099-01-01", "2099-01-01T00:00:10+00:00"))
            pipeline.write_outputs(res("2099-01-02", "2099-01-01T00:00:25+00:00"), name="next")
            main, nxt, build = site._slates()
            assert main["date"] == "2099-01-01" and nxt["date"] == "2099-01-02" and "_R" not in main
            assert build == "2099-01-01T00:00:25+00:00"                                  # the page's build stamp is the newer of the two
            assert (Path(td) / "slates" / "2099-01-02.json").exists()
            pipeline.write_outputs(None, name="next")                                    # tomorrow is gone: the file must not linger
            assert not (Path(td) / "next.json").exists() and site._slates()[1] is None
            pipeline.write_outputs(res("2099-01-01", "x"), name="next")                  # a 'next' for the same date as the main slate is ignored
            assert site._slates()[1] is None
        finally:
            pipeline.SITE_DATA, site.SITE_DATA = old_p, old_s


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} tests passed")
