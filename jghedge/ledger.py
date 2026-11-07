"""Live track record: every night's recommended bets (balanced profile) are recorded before
tip-off and graded from final box scores on the next run."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import DATA_DIR, SITE_DATA
from .data import history

LEDGER_FILE = DATA_DIR / "ledger.json"


def load() -> list[dict]:
    if LEDGER_FILE.exists():
        return json.loads(LEDGER_FILE.read_text())
    return []


def save(entries: list[dict]) -> None:
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_FILE.write_text(json.dumps(entries, indent=1))


def _leg(b: dict) -> dict:
    return {k: b.get(k) for k in ("id", "game_id", "kind", "label", "side", "line", "price", "dec",
                                  "p_final", "player_id", "stat", "matchup", "tip")}


def record(result: dict, profile: str = "balanced") -> int:
    """Store tonight's allocation (replacing any earlier run for the same date)."""
    bets = {b["id"]: b for b in result["bets"]}
    parlays = {p["id"]: p for p in result["parlays"]}
    alloc = result["allocations"].get(profile, {})
    rows = []
    for it in alloc.get("items", []):
        ref = it["ref"]
        if ref in bets:
            b = bets[ref]
            rows.append({"date": result["date"], "ref": ref, "type": "single", "label": b["label"],
                         "kind": b["kind"], "price": b["price"], "dec": b["dec"], "p_final": b["p_final"],
                         "ev": b["ev"], "frac": it["frac"], "legs": [_leg(b)], "result": None})
        elif ref in parlays:
            p = parlays[ref]
            legs = [_leg(bets[l]) for l in p["legs"] if l in bets]
            rows.append({"date": result["date"], "ref": ref, "type": "parlay",
                         "label": " + ".join(l["label"] for l in legs), "kind": "parlay",
                         "price": p.get("american"), "dec": p["dec"], "p_final": p["p_hit"], "ev": p["ev"],
                         "frac": it["frac"], "legs": legs, "result": None})
    entries = [e for e in load() if e["date"] != result["date"] or e.get("result") is not None]
    # don't overwrite already-graded entries; replace ungraded ones for the date
    entries = [e for e in entries if e["date"] != result["date"]] + rows
    save(entries)
    return len(rows)


def _settle_leg(leg: dict, games: dict, players: dict) -> str | None:
    g = games.get(leg["game_id"])
    if not g:
        return None
    m = g["home_score"] - g["away_score"]
    t = g["home_score"] + g["away_score"]
    k, side, line = leg["kind"], leg["side"], leg["line"]
    if k == "moneyline":
        return "win" if (m > 0) == (side == "home") else "loss"
    if k == "spread":
        v = (m + line) if side == "home" else (-m + line)
    elif k == "total":
        v = (t - line) if side == "over" else (line - t)
    elif k == "prop":
        row = players.get((leg["game_id"], str(leg["player_id"])))
        if row is None:
            return "void"   # did not play
        val = row.get(leg["stat"])
        if val is None:
            return None
        v = (val - line) if side == "over" else (line - val)
    else:
        return None
    return "win" if v > 0 else ("push" if v == 0 else "loss")


def grade(season: int) -> int:
    entries = load()
    if not entries:
        return 0
    games = {g["id"]: g for g in history.load_season(season)}
    players = {}
    from .data.espn import PLAYER_COLS
    for g in games.values():
        for p in g.get("players", []):
            r = dict(zip(PLAYER_COLS, p))
            r["pra"] = r["pts"] + r["reb"] + r["ast"]
            r["pr"], r["pa"], r["ra"] = r["pts"] + r["reb"], r["pts"] + r["ast"], r["reb"] + r["ast"]
            players[(g["id"], str(r["id"]))] = r
    n = 0
    for e in entries:
        if e.get("result") is not None:
            continue
        res = [_settle_leg(l, games, players) for l in e["legs"]]
        if any(r is None for r in res):
            continue
        if e["type"] == "single":
            r = res[0]
            e["result"] = r
            e["units"] = (e["dec"] - 1) if r == "win" else (-1.0 if r == "loss" else 0.0)
        else:
            if "loss" in res:
                e["result"], e["units"] = "loss", -1.0
            else:
                dec = 1.0
                for l, r in zip(e["legs"], res):
                    dec *= l["dec"] if r == "win" else 1.0
                e["result"] = "win" if dec > 1.0 else "push"
                e["units"] = dec - 1.0
        e["graded"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        n += 1
    save(entries)
    return n


def summary(entries: list[dict] | None = None) -> dict:
    entries = entries if entries is not None else load()
    done = [e for e in entries if e.get("result") in ("win", "loss", "push", "void")]
    w = sum(e["result"] == "win" for e in done)
    l = sum(e["result"] == "loss" for e in done)
    staked = sum(1 for e in done if e["result"] in ("win", "loss"))
    units = sum(e.get("units", 0.0) for e in done)
    bank = sum(e["frac"] * e.get("units", 0.0) for e in done)   # return on bankroll (balanced profile)
    by_kind: dict = {}
    for e in done:
        d = by_kind.setdefault(e["kind"], {"n": 0, "w": 0, "l": 0, "units": 0.0})
        d["n"] += 1
        d["w"] += e["result"] == "win"
        d["l"] += e["result"] == "loss"
        d["units"] += e.get("units", 0.0)
    nights: dict = {}
    for e in done:
        nights[e["date"]] = nights.get(e["date"], 0.0) + e["frac"] * e.get("units", 0.0)
    return {"bets": len(done), "wins": w, "losses": l, "pushes": len(done) - w - l,
            "units": round(units, 3), "roi_flat": round(units / max(staked, 1), 4),
            "bankroll_return": round(bank, 4), "by_kind": by_kind,
            "nightly": [{"date": d, "ret": round(v, 4)} for d, v in sorted(nights.items())],
            "pending": sum(1 for e in entries if e.get("result") is None),
            "recent": sorted(entries, key=lambda e: e["date"], reverse=True)[:60]}


def publish() -> None:
    SITE_DATA.mkdir(parents=True, exist_ok=True)
    (SITE_DATA / "ledger.json").write_text(json.dumps(summary(), separators=(",", ":")))
