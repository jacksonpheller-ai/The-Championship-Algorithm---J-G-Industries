"""PrizePicks boards saved from the page: pack/unpack, archive by date and time, latest pointer, issue parsing."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jghedge import boards  # noqa: E402

PAYLOAD = {"v": 1, "read_at": "2026-11-16T23:41:05Z", "date": "2026-11-16", "n": 5, "skipped": {"unsupported": 1},
           "games": ["2026-11-17T00:10:00Z"], "players": [["Nikola Jokić", "DEN"], ["LeBron James", "LAL"]],
           "rows": [[0, "pts", 27.5, 0, 0, None], [0, "reb", 14.5, 1, 0, None], [1, "ast", 5.5, 2, 0, 0.75], [1, "pts", 22, 3, 0, None]]}


def _swap_dirs(tmp):
    boards.BOARD_DIR = Path(tmp) / "data" / "boards"
    boards.LATEST = Path(tmp) / "docs" / "data" / "board.json"
    boards.SITE_DATA = Path(tmp) / "docs" / "data"
    boards.DATA_DIR = Path(tmp) / "data"


def test_roundtrip_both_encodings():
    for comp in (True, False):
        t = boards.encode(PAYLOAD, compress=comp)
        assert t.startswith("JGB1." if comp else "JGB0.")
        assert boards.decode("text before\n```\n" + t + "\n```\n") == PAYLOAD


def test_bad_input_is_refused():
    assert boards.decode("") is None and boards.decode("hello") is None and boards.decode("JGB1.@@@") is None
    bad = dict(PAYLOAD, v=2)
    assert boards.decode(boards.encode(bad)) is None
    assert boards.decode(boards.encode(dict(PAYLOAD, date="16 Nov"))) is None


def test_expand_types_and_multiplier():
    rows = boards.expand(PAYLOAD)
    assert [r["odds_type"] for r in rows] == ["standard", "demon", "goblin", "other"]
    assert rows[2]["multiplier"] == 0.75 and rows[0]["multiplier"] is None   # never assumed
    assert rows[0]["name"] == "Nikola Jokić" and rows[0]["team"] == "DEN" and rows[0]["start_time"] == "2026-11-17T00:10:00Z"
    junk = dict(PAYLOAD, rows=[[9, "pts", 1, 0], ["x"], [0, "pts", "nan?", 0]])
    assert boards.expand(junk) == []


def test_archive_by_date_and_time_and_latest():
    with tempfile.TemporaryDirectory() as tmp:
        _swap_dirs(tmp)
        packed = boards.encode(PAYLOAD)
        path, wrote = boards.archive(PAYLOAD, packed, {"issue": 7, "by": "friend"})
        assert wrote and path.name == "20261116T234105Z.json" and path.parent.name == "2026-11-16"
        again, wrote2 = boards.archive(PAYLOAD, packed)
        assert again == path and not wrote2   # the same board is archived once
        later = dict(PAYLOAD, read_at="2026-11-17T01:02:03Z")
        boards.archive(later, boards.encode(later))
        out = boards.publish_latest()
        assert out["read_at"] == "2026-11-17T01:02:03Z" and boards.decode(out["packed"])["read_at"] == later["read_at"]
        assert json.loads(boards.LATEST.read_text())["date"] == "2026-11-16"
        assert len(boards.load_archive("2026-11-16")) == 2
        rec = json.loads(path.read_text())
        assert rec["summary"]["by_type"] == {"standard": 1, "demon": 1, "goblin": 1, "other": 1} and rec["source"]["by"] == "friend"


def test_issue_and_event():
    with tempfile.TemporaryDirectory() as tmp:
        _swap_dirs(tmp)
        body = "Saved from the J&G desk.\n\n```\n" + boards.encode(PAYLOAD) + "\n```\n"
        issue = {"number": 12, "title": "Board 2026-11-16 · 4 lines", "body": body, "user": {"login": "friend"}}
        assert boards.from_issue(dict(issue, title="Bets 2026-11-16")) is None
        assert boards.from_issue(dict(issue, title="Board 2026-11-15")) is None   # title date must match the board's
        ev = Path(tmp) / "event.json"
        ev.write_text(json.dumps({"issue": issue}))
        res = boards.from_event(str(ev))
        assert res["ok"] and res["written"] and res["issue"] == 12 and res["summary"]["lines"] == 4
        txt = boards.comment_text(res)
        assert txt.startswith("Saved for both: 4 lines") and "1 demon" in txt and "1 goblin" in txt
        assert boards.comment_text({"ok": False, "why": "no board"}).startswith("Not saved")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in fns:
        f()
    print(f"{len(fns)} tests passed")
