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


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} tests passed")
