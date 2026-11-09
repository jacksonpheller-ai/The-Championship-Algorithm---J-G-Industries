"""Live track record: every night's recommended bets (balanced profile) are recorded before
tip-off and graded from final box scores on the next run."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import DATA_DIR, NIGHT_CAP, REF_BANKROLL, SITE_DATA
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


def _pickem_rows(result: dict) -> list[dict]:
    """Reference pick'em entries: one minimum entry per slip with verdict 'play' (stake
    min_entry / REF_BANKROLL, i.e. $5 on $100), with the payout table so it can be graded.
    At most NIGHT_CAP x REF_BANKROLL / min_entry entries (two $5 entries on $100) so the
    reference ledger respects the same 10% night cap as the Fliff ticket; slips come ranked."""
    pk = result.get("pickem") or {}
    legs = {l["id"]: l for l in pk.get("legs", [])}
    min_entry = float(pk.get("min_entry") or 5.0)
    max_slips = max(int(NIGHT_CAP * REF_BANKROLL // min_entry), 1)
    rows = []
    for s in pk.get("slips", []):
        if len(rows) >= max_slips:
            break
        paper = s.get("verdict") == "check_lines" and s.get("verdict_if_lines_match") == "play"
        if (s.get("verdict") != "play" and not paper) or not all(l in legs for l in s.get("legs", [])):
            continue
        lrows = [{"id": lid, "game_id": legs[lid].get("game_id"), "kind": "pickem", "label": legs[lid].get("label"),
                  "side": legs[lid].get("side"), "line": legs[lid].get("line"), "stat": legs[lid].get("stat"),
                  "player_id": legs[lid].get("player_id"), "player": legs[lid].get("player"),
                  "p_final": legs[lid].get("p_side"), "matchup": legs[lid].get("matchup"), "tip": legs[lid].get("tip")}
                 for lid in s["legs"]]
        rows.append({"date": result["date"], "ref": s["id"], "type": "pickem", "label": s.get("label") or s["id"],
                     "kind": "pickem", "slip_type": s["type"], "platform": pk.get("platform", "PrizePicks"),
                     "line_source": pk.get("source"), "payouts": pk.get("payouts"), "price": None,
                     "dec": None, "p_final": s.get("p_all"), "ev": s.get("ev"), "ev_shrunk": s.get("ev_shrunk"),
                     "frac": 0.0 if paper else round(min_entry / REF_BANKROLL, 5), "paper": paper,
                     "legs": lrows, "result": None})
    return rows


def _key(row: dict) -> str:
    """Stable identity of a recorded bet across runs (parlay/slip ids are per-run labels)."""
    if row.get("type") in ("parlay", "pickem"):
        return (row.get("slip_type") or row["type"]) + ":" + "+".join(sorted(l.get("id") or "" for l in row.get("legs", [])))
    return row["ref"]


def record(result: dict, profile: str = "reference") -> int:
    """Record tonight's reference ticket ($100 bankroll, $0.50 units): singles, parlays and
    pick'em slips. Each bet is recorded ONCE, by the first run that bets it, at that run's line,
    so early tips are kept and closing-line value is measured against the close. No game bet is
    staked on a PASS night (the replay skips PASS nights too). Pick'em slips do not depend on the
    game verdict: a live 'play' slip is recorded at the minimum entry, a stand-in slip as a
    zero-stake paper entry so the pick'em model is graded even without the board.

    Several runs price the same night, so the caps are applied to the NIGHT, not to each run:
    game bets stop at NIGHT_CAP of the bankroll, pick'em at the slip count _pickem_rows allows,
    and a later run never adds the other side of a market the night already holds."""
    bets = {b["id"]: b for b in result["bets"]}
    parlays = {p["id"]: p for p in result["parlays"]}
    alloc = result["allocations"].get(profile) or result["allocations"].get("balanced", {})
    verdict = (result.get("night") or {}).get("verdict")
    rows = _pickem_rows(result)
    if verdict != "PASS":
        for it in alloc.get("items", []):
            ref = it["ref"]
            if ref in bets:
                b = bets[ref]
                rows.append({"date": result["date"], "ref": ref, "type": "single", "label": b["label"],
                             "kind": b["kind"], "price": b["price"], "dec": b["dec"], "p_final": b["p_final"],
                             "ev": b["ev"], "ev_shrunk": b.get("shrunk_ev"), "max_line": b.get("max_line"),
                             "units": None, "frac": it["frac"], "legs": [_leg(b)], "result": None})
            elif ref in parlays:
                p = parlays[ref]
                legs = [_leg(bets[l]) for l in p["legs"] if l in bets]
                rows.append({"date": result["date"], "ref": ref, "type": "parlay",
                             "label": " + ".join(l["label"] for l in legs), "kind": "parlay",
                             "price": p.get("american"), "dec": p["dec"], "p_final": p["p_hit"], "ev": p["ev"],
                             "ev_shrunk": p.get("shrunk_ev"), "frac": it["frac"], "legs": legs, "result": None})
    entries = load()
    have = {(e["date"], e.get("key") or _key(e)) for e in entries}
    night = [e for e in entries if e["date"] == result["date"]]
    pk = result.get("pickem") or {}
    max_slips = max(int(NIGHT_CAP * REF_BANKROLL // float(pk.get("min_entry") or 5.0)), 1)
    n_slips = sum(1 for e in night if e.get("type") == "pickem")
    staked = sum(float(e.get("frac") or 0.0) for e in night if e.get("type") != "pickem")
    sides = {(l.get("game_id"), l.get("kind")): l.get("side") for e in night if e.get("type") != "pickem" for l in e.get("legs", [])}
    new = []
    for r in rows:
        r["key"] = _key(r)
        r["recorded_at"] = result.get("generated")
        r["run_label"] = result.get("run_label")
        if (r["date"], r["key"]) in have:
            continue
        if r.get("type") == "pickem":
            if n_slips >= max_slips:
                continue
            n_slips += 1
        else:
            frac = float(r.get("frac") or 0.0)
            if staked + frac > NIGHT_CAP + 1e-9:
                continue                      # the night's cap is already used by earlier runs
            if any(sides.get((l.get("game_id"), l.get("kind")), l.get("side")) != l.get("side") for l in r.get("legs", [])):
                continue                      # the night already holds the other side of this market
            staked += frac
            for l in r.get("legs", []):
                sides[(l.get("game_id"), l.get("kind"))] = l.get("side")
        new.append(r)
        have.add((r["date"], r["key"]))
    save(entries + new)
    return len(new)


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
    elif k in ("prop", "pickem"):
        row = players.get((leg["game_id"], str(leg["player_id"])))
        if row is None:
            return "void"   # did not play
        val = row.get(leg["stat"])
        if val is None:
            return None
        if k == "pickem" and val == line:
            return "void"   # PrizePicks voids a pick that lands exactly on the line
        v = (val - line) if side in ("over", "more") else (line - val)
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
    from .data import lines as lines_mod
    n = 0
    for e in entries:
        if e.get("type") == "pickem" and e.get("result") == "push" and (e.get("units") or 0.0) < 0:
            e["result"] = "loss"              # rows graded before a 0.4x Flex payout counted as a loss
        if e.get("result") is not None:
            continue
        res = [_settle_leg(l, games, players) for l in e["legs"]]
        if any(r is None for r in res):
            continue
        for l in e["legs"]:
            if l.get("kind") in ("total", "spread") and l.get("clv") is None:
                # the real close is in the game's history record; the desk's last snapshot can be hours
                # before tip (and equals the bet's own line when the last run placed it)
                close = ((games.get(l.get("game_id")) or {}).get("odds") or {}).get("close") or {}
                field = "total" if l["kind"] == "total" else "spread_home"
                closing = close if close.get(field) is not None else lines_mod.closing_line(e["date"], l.get("game_id"))
                if closing:
                    l["close_line"] = closing.get("total") if l["kind"] == "total" else closing.get("spread_home")
                    l["clv"] = lines_mod.clv(l, closing)
        if e["type"] == "single":
            r = res[0]
            e["result"] = r
            e["units"] = (e["dec"] - 1) if r == "win" else (-1.0 if r == "loss" else 0.0)
        elif e["type"] == "pickem":
            e["result"], e["units"], e["hits"] = settle_pickem(e.get("slip_type"), res, e.get("payouts"))
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


def settle_pickem(slip_type: str | None, leg_results: list[str], payouts: dict | None) -> tuple[str, float, int]:
    """Grade a pick'em slip from its leg results per PrizePicks rules (ASSUMPTION, from the
    public house rules as remembered: a void or tied pick drops the entry to the next size down
    with that size's payout; an entry that falls below 2 picks is refunded). Returns
    (result, units per $1 of entry, hits among non-void legs)."""
    from .pickem import payout_vector
    live = [r for r in leg_results if r not in ("void", "push")]
    hits = sum(1 for r in live if r == "win")
    n = len(live)
    if n < 2 or not slip_type or not payouts:
        return "push", 0.0, hits
    family = "flex" if str(slip_type).startswith("flex") else "power"
    table = {"power": {int(k): float(v) for k, v in (payouts.get("power") or {}).items()},
             "flex": {int(k): list(map(float, v)) for k, v in (payouts.get("flex") or {}).items()}}
    if n not in table[family]:
        family = "power" if n in table["power"] else ("flex" if n in table["flex"] else None)
        if family is None:
            return "push", 0.0, hits
    mult = float(payout_vector(f"{family}{n}", table)[hits])
    if mult > 1:
        return "win", mult - 1.0, hits
    if mult == 1:
        return "push", 0.0, hits
    return "loss", mult - 1.0, hits           # includes Flex tiers that pay back less than the entry (0.4x)


def summary(entries: list[dict] | None = None) -> dict:
    entries = entries if entries is not None else load()
    paper = [e for e in entries if e.get("paper") and e.get("result") in ("win", "loss", "push", "void")]
    entries = [e for e in entries if not e.get("paper")]
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
    clvs = [lg["clv"] for e in done for lg in e["legs"] if lg.get("clv") is not None]
    paper_summary = {"slips": len(paper), "wins": sum(e["result"] == "win" for e in paper),
                     "losses": sum(e["result"] == "loss" for e in paper),
                     "units": round(sum(e.get("units", 0.0) or 0.0 for e in paper), 3)}
    return {"bets": len(done), "wins": w, "losses": l, "pushes": len(done) - w - l, "pickem_paper": paper_summary,
            "avg_clv": round(sum(clvs) / len(clvs), 3) if clvs else None,
            "beat_close_pct": round(sum(1 for c in clvs if c > 0) / len(clvs), 4) if clvs else None,
            "clv_n": len(clvs),
            "units": round(units, 3), "roi_flat": round(units / max(staked, 1), 4),
            "bankroll_return": round(bank, 4), "by_kind": by_kind,
            "nightly": [{"date": d, "ret": round(v, 4)} for d, v in sorted(nights.items())],
            "pending": sum(1 for e in entries if e.get("result") is None),
            "recent": sorted(entries, key=lambda e: e["date"], reverse=True)[:60]}


def publish() -> None:
    SITE_DATA.mkdir(parents=True, exist_ok=True)
    (SITE_DATA / "ledger.json").write_text(json.dumps(summary(), separators=(",", ":")))
