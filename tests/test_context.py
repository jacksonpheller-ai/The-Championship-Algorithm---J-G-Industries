"""Tests for game-context adjustment, line snapshots/CLV, injury probabilities, odds-API parsing.
Run: python tests/test_context.py"""
import json
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from jghedge.data import lines, oddsapi  # noqa: E402
from jghedge.data.espn import play_probability  # noqa: E402
from jghedge.model import context  # noqa: E402


def test_total_context_adjust_rules():
    assert context.total_context_adjust(30, 35, 2, -3.0, 0.5, 0.5) == 0.0
    assert context.total_context_adjust(70, 72, 2, -3.0, 0.5, 0.5) == 1.8
    assert context.total_context_adjust(80, 79, 2, -3.0, 0.5, 0.5) == 2.5
    assert context.total_context_adjust(72, 73, 2, 9.0, 0.2, 0.6) == 2.5   # home dog at +9 with .200 record: tanker
    assert context.total_context_adjust(72, 73, 2, -9.0, 0.6, 0.2) == 2.5  # away dog
    assert context.total_context_adjust(72, 73, 2, -9.0, 0.6, 0.5) == 1.8  # dog is decent: no tank bump
    assert context.total_context_adjust(75, 75, 3, -3.0, 0.5, 0.5) == 0.0  # playoffs


def test_games_played_and_kill_switch():
    tg = pd.DataFrame({"team": ["BOS"] * 3 + ["NY"], "season": [2027] * 4,
                       "date": pd.to_datetime(["2026-10-22", "2026-10-24", "2026-10-26", "2026-10-22"]),
                       "pts": [100, 90, 110, 95], "opp_pts": [95, 99, 100, 90]})
    assert context.games_played(tg, "BOS", "2026-10-25", 2027) == 2
    assert context.team_winpct(tg, "BOS", "2026-10-27", 2027) == 2 / 3
    assert not context.kill_switch([True] * 50)
    assert context.kill_switch([False] * 60 + [True] * 40)
    assert not context.kill_switch([True] * 60 + [False] * 40)


def test_lines_snapshots_movement_and_clv(monkeypatch=None):
    tmp = Path(tempfile.mkdtemp())
    lines.LINES_DIR = tmp
    games = [{"id": "g1", "odds": {"close": {"spread_home": -4.5, "spread_home_price": -110, "spread_away_price": -110,
                                             "total": 224.5, "over_price": -110, "under_price": -110, "ml_home": -190, "ml_away": 160}}}]
    lines.record("2026-11-07", "morning", games, {"g1": {"margin": 5.1, "total": 226.0}}, at="2026-11-07T15:40:00+00:00")
    games[0]["odds"]["close"]["total"] = 226.5
    games[0]["odds"]["close"]["spread_home"] = -5.5
    lines.record("2026-11-07", "pretip", games, at="2026-11-07T22:30:00+00:00")
    assert len(lines.load("2026-11-07")) == 2
    first, close = lines.first_snapshot("2026-11-07", "g1"), lines.closing_line("2026-11-07", "g1")
    assert first["total"] == 224.5 and close["total"] == 226.5 and close["label"] == "pretip"
    over = {"kind": "total", "side": "over", "line": 224.5, "game_id": "g1"}
    under = {"kind": "total", "side": "under", "line": 224.5, "game_id": "g1"}
    mv = lines.movement("2026-11-07", over)
    assert mv["line_then"] == 224.5 and mv["line_now"] == 226.5 and mv["still_ok"] is False
    assert lines.movement("2026-11-07", under)["still_ok"] is True
    assert lines.clv(over, close) == 2.0 and lines.clv(under, close) == -2.0
    home = {"kind": "spread", "side": "home", "line": -4.5, "game_id": "g1"}
    away = {"kind": "spread", "side": "away", "line": 4.5, "game_id": "g1"}
    assert lines.clv(home, close) == 1.0 and lines.clv(away, close) == -1.0   # close moved to -5.5: laying -4.5 beat the close
    assert lines.closing_line("2026-11-07", "nope") is None


def test_play_probability_mapping():
    gtd_out = {"status": "Out", "shortComment": "Johnson (ankle) is probable for Saturday's game against Brooklyn.",
               "details": {"fantasyStatus": {"abbreviation": "GTD"}, "returnDate": "2026-11-07"}}
    assert play_probability(gtd_out, "2026-11-07")[0] == 0.9
    plain_out = {"status": "Out", "shortComment": "Gilbert (knee) is out for Saturday's game.",
                 "details": {"fantasyStatus": {"abbreviation": "OUT"}, "returnDate": "2026-11-09"}}
    assert play_probability(plain_out, "2026-11-07")[0] == 0.0
    ofs = {"status": "Out", "shortComment": "Out for the season.", "details": {"fantasyStatus": {"abbreviation": "OFS"}, "returnDate": "2026-11-07"}}
    assert play_probability(ofs, "2026-11-07")[0] == 0.0
    q = {"status": "Day-To-Day", "shortComment": "McConnell (hamstring) is questionable for Saturday.", "details": {"fantasyStatus": {"abbreviation": "GTD"}}}
    assert play_probability(q, "2026-11-07")[0] == 0.5
    d = {"status": "Day-To-Day", "shortComment": "Smith is doubtful for Saturday.", "details": {"fantasyStatus": {"abbreviation": "GTD"}}}
    assert play_probability(d, "2026-11-07")[0] == 0.15
    legacy = {"status": "Doubtful", "shortComment": "", "details": {}}
    assert play_probability(legacy)[0] == 0.15


def test_oddsapi_parsing_and_best_price():
    payload = [{"id": "e1", "home_team": "Boston Celtics", "away_team": "New York Knicks", "commence_time": "2026-11-08T00:00:00Z",
                "bookmakers": [
                    {"key": "draftkings", "last_update": "x", "markets": [
                        {"key": "totals", "outcomes": [{"name": "Over", "point": 224.5, "price": -110}, {"name": "Under", "point": 224.5, "price": -110}]},
                        {"key": "spreads", "outcomes": [{"name": "Boston Celtics", "point": -4.5, "price": -108}, {"name": "New York Knicks", "point": 4.5, "price": -112}]},
                        {"key": "h2h", "outcomes": [{"name": "Boston Celtics", "price": -190}, {"name": "New York Knicks", "price": 160}]}]},
                    {"key": "fliff", "last_update": "x", "markets": [
                        {"key": "totals", "outcomes": [{"name": "Over", "point": 224.5, "price": -105}, {"name": "Under", "point": 224.5, "price": -115}]}]}]}]
    ev = oddsapi.parse_slate(payload)
    assert ev["e1"]["home"] == "BOS" and ev["e1"]["away"] == "NY"
    assert ev["e1"]["books"]["draftkings"]["spread_home"] == -4.5 and ev["e1"]["books"]["draftkings"]["ml_away"] == 160
    assert oddsapi.best_price(ev["e1"]["books"], "total", "over") == ("fliff", -105, 224.5)
    assert oddsapi.best_price(ev["e1"]["books"], "total", "under") == ("draftkings", -110, 224.5)
    assert oddsapi.fliff_lines(ev["e1"]["books"])["over_price"] == -105
    assert oddsapi.map_to_espn(ev, [{"id": "401", "home": "BOS", "away": "NY"}]) == {"e1": "401"}
    props = oddsapi.parse_event_props({"bookmakers": [{"key": "prizepicks", "markets": [{"key": "player_points", "outcomes": [
        {"name": "Over", "description": "Jayson Tatum", "point": 27.5, "price": -119}, {"name": "Under", "description": "Jayson Tatum", "point": 27.5, "price": -119}]}]}]})
    assert props[("Jayson Tatum", "pts")]["prizepicks"]["line"] == 27.5
    b = oddsapi.Budget(floor=60)
    b.update({"x-requests-remaining": "70"})
    assert b.allows(6) and not b.allows(12)
    assert oddsapi.fetch_slate_odds(None) == {}   # no key -> no network, empty


def test_pickem_line_table_thresholds():
    import numpy as np
    from jghedge.pickem import line_table
    rng = np.random.default_rng(3)
    draws = rng.poisson(20.0, 20000).astype(float)
    q, more_max, less_min = line_table(draws, 20.5, halfwidth=4.0, take_p=0.60)
    lines = [r["line"] for r in q]
    assert lines[0] == 16.5 and lines[-1] == 24.5
    pm = {r["line"]: r["p_more"] for r in q}
    assert pm[16.5] > pm[20.5] > pm[24.5]
    # thresholds sit on half lines and respect the 60% rule
    assert more_max is not None and pm[more_max] >= 0.60 and pm[more_max + 1.0] < 0.60
    assert less_min is not None and 1 - pm[less_min] >= 0.60 and 1 - pm[less_min - 1.0] < 0.60
    assert more_max < 20.5 < less_min


def test_pickem_proxy_slips_need_line_checks():
    from jghedge import pickem
    leg = lambda i, p: {"id": f"l{i}", "player_id": str(i), "line_source": "dk", "p_side_shrunk": p}
    import numpy as np
    legs = [leg(i, 0.6) for i in range(3)]
    sims = {str(i): {"_plays": np.ones(400, bool)} for i in range(3)}
    # stub the heavy parts: no sims columns needed when every slip is post-processed
    orig_hm, orig_en = pickem.hit_matrix, pickem.enumerate_slips
    try:
        pickem.hit_matrix = lambda legs, sims: np.ones((len(legs), 400), dtype=np.uint8)
        pickem.enumerate_slips = lambda *a, **k: [{"id": "s1", "type": "power2", "legs": ["l0", "l1"], "verdict": "play"}]
        out = pickem.build_pickem_block(legs, sims, source="dk_proxy")
    finally:
        pickem.hit_matrix, pickem.enumerate_slips = orig_hm, orig_en
    assert out["mode"] == "check_lines"
    assert out["slips"][0]["verdict"] == "check_lines" and out["slips"][0]["verdict_if_lines_match"] == "play"


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} tests passed")
