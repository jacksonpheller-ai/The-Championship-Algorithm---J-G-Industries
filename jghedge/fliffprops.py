"""Player props as straight bets on Fliff.

Fliff is a sportsbook: each side of a prop carries its own price. The PrizePicks edge (a fixed
payout on a side the sportsbook prices at 60%) does not exist here; what is left is the price.

Evidence (8,009 pre-game DraftKings main lines with results, Jan-Nov 2026, 133 games): every
Under bet at DraftKings' own price returned -0.2% per dollar (standard error 1.0%), every Over
-13.1%. The under lean is real (unders won 54.6%) but the book's margin (median 6.8% across the
two sides) uses it up, and a rule that bet the leaned side at DraftKings' price whenever it
claimed an edge lost 1.0% to 3.3%. So a prop is a bet only where Fliff's line or price is better
than the fair price implied by DraftKings, and the page asks for Fliff's number before it stakes
anything on a prop (the server cannot read Fliff's board without an Odds API key).

Fair chance: DraftKings' no-vig price moved by the under lean (config.PICKEM_LEAN), with the
player's simulated stat line anchored to it (jghedge.pickem.build_legs) so the chance can be read
at whatever line Fliff shows. A prop qualifies at an expected profit of `min_ev` per dollar (the
rule game totals use) and is sized at quarter Kelly on the chance after the safety cut.
The page mirrors `evaluate` (template propEval); keep the two in step.
"""
from __future__ import annotations

from .config import PER_BET_CAP
from .model.odds import american_to_decimal, decimal_to_american

PARAMS: dict = {
    "s": 0.5,                # safety cut: half of the claimed edge is kept (the totals and parlay setting)
    "min_ev": 0.03,          # expected profit per dollar a prop needs at Fliff's number (as for totals)
    "max_edge": 0.12,        # a chance more than this above the price's break-even is treated as this much
    "kelly": 0.25,           # quarter Kelly, as on the reference ticket
    "per_bet_cap": float(PER_BET_CAP),
    "props_cap": 0.05,       # all props together: at most this share of the reference bankroll (half the night cap)
    "max_rows": 400,         # rows published, best first
    "q_halfwidth": 3.0,      # lines published around DraftKings' line for re-pricing in the page
    "min_take": -400.0,      # no 'bet at X or better' price is published beyond this
    # The lean is applied only where DraftKings' no-vig under is at least this. Where the under is the
    # longshot the lean was not found: +0.6 points (se 1.5) on 1,080 lines priced below 45%, against
    # +3.8 (se 0.6) on 6,657 lines priced 45-65% and +3.4 on 272 above (same sign in both seasons).
    "lean_min_under": 0.45,
}


def probs_at(row: dict, line: float) -> tuple[float, float, float] | None:
    """(p_over, p_under, p_tie) at `line` from the row's table; p_over and p_under are conditional
    on no tie (a tie is a push: the stake comes back). None when the line is outside the table."""
    for ln, p_over, p_tie in row.get("q") or []:        # [line, p_over, p_tie]; p_under = 1 - p_over
        if abs(float(ln) - float(line)) < 1e-9:
            return float(p_over), 1.0 - float(p_over), float(p_tie)
    return None


def evaluate(p: float, p_tie: float, american: float, prm: dict | None = None) -> dict:
    """One side at one price. `p` is the chance of winning given no tie.

    ev      expected profit per dollar staked at the claimed chance
    p_cut   the chance after the safety cut (edge over the price's break-even times s; an edge
            above max_edge counts as max_edge and is flagged `big_gap`)
    frac    quarter-Kelly share of the bankroll on the cut chance, capped per bet
    ok      ev >= min_ev and the cut chance still beats the price
    """
    prm = {**PARAMS, **(prm or {})}
    dec = american_to_decimal(american)
    be = 1.0 / dec
    live = 1.0 - float(p_tie)
    ev = live * (p * dec - 1.0)
    big = (p - be) > prm["max_edge"]
    p_use = be + prm["max_edge"] if big else p
    p_cut = be + prm["s"] * (p_use - be) if p_use > be else p_use
    ev_cut = live * (p_cut * dec - 1.0)
    frac = prm["kelly"] * (p_cut * dec - 1.0) / (dec - 1.0) if dec > 1.0 else 0.0
    frac = float(min(max(frac, 0.0), prm["per_bet_cap"]))
    return {"dec": dec, "be": be, "ev": ev, "p_cut": p_cut, "ev_cut": ev_cut, "frac": frac,
            "big_gap": bool(big), "ok": bool(ev >= prm["min_ev"] - 1e-12 and frac > 0.0)}


def take_american(p: float, p_tie: float, prm: dict | None = None) -> float | None:
    """The worst American price at which this side still qualifies (ev = min_ev), or None when
    that price would be longer than `min_take` (a chance that high has no playable price)."""
    prm = {**PARAMS, **(prm or {})}
    live = 1.0 - float(p_tie)
    if p <= 0.0 or live <= 0.0:
        return None
    dec = (1.0 + prm["min_ev"] / live) / p
    if dec <= 1.0:
        return None
    a = decimal_to_american(dec)
    a = float(int(a) + 1) if a > int(a) else float(int(a))      # round toward the bettor: the price must be this or better
    if 0 < a < 100:
        a = 100.0
    return None if a < prm["min_take"] else a


def _best_side(row: dict, line: float, over, under, prm: dict) -> dict | None:
    pr = probs_at(row, line)
    if pr is None:
        return None
    best = None
    for side, p, price in (("over", pr[0], over), ("under", pr[1], under)):
        if price is None:
            continue
        e = evaluate(p, pr[2], float(price), prm)
        cand = {"side": side, "line": float(line), "american": float(price), "p": p, "p_tie": pr[2], **e}
        if best is None or cand["ev"] > best["ev"]:
            best = cand
    return best


def build_rows(legs: list[dict], fliff: dict | None = None, prm: dict | None = None) -> list[dict]:
    """One row per (player, stat) with a DraftKings main line.

    legs  : jghedge.pickem.build_legs output (anchored to DraftKings minus the under lean)
    fliff : {(player_id, stat): {"line", "over", "under"}} when Fliff's own board is known
    Each row is priced at the reference number: Fliff's when known, else DraftKings' as the stand-in.
    """
    prm = {**PARAMS, **(prm or {})}
    fliff = fliff or {}
    rows = []
    for l in legs:
        if l.get("dk_line") is None or l.get("dk_over_price") is None or l.get("dk_under_price") is None:
            continue
        dk_line = float(l["dk_line"])
        row = {"id": f"{l.get('game_id') or 'na'}-fp-{l['player_id']}-{l['stat']}",
               "player": l.get("player"), "player_id": l["player_id"], "team": l.get("team"),
               "matchup": l.get("matchup"), "game_id": l.get("game_id"), "tip": l.get("tip"), "stat": l["stat"],
               "line": dk_line, "over": float(l["dk_over_price"]), "under": float(l["dk_under_price"]),
               "p_over_mkt": l.get("p_over_mkt"),
               "lean": (float(l["p_over_mkt"]) - float(l["p_over_anchor"])
                        if l.get("p_over_mkt") is not None and l.get("p_over_anchor") is not None else 0.0),
               "q": [[float(q["line"]), float(q["p_more"]), float(q["p_tie"])] for q in (l.get("q") or [])
                     if abs(float(q["line"]) - dk_line) <= prm["q_halfwidth"] + 1e-9],
               "play_prob": l.get("play_prob"), "exp_min": l.get("exp_min"), "proj": l.get("proj"),
               "strain": bool(l.get("anchor_strain"))}
        at_dk = probs_at(row, dk_line)
        if at_dk is None:
            continue
        row["p_over"], row["p_under"], row["p_tie"] = at_dk
        row["take_over"] = take_american(at_dk[0], at_dk[2], prm)
        row["take_under"] = take_american(at_dk[1], at_dk[2], prm)
        fl = fliff.get((str(l["player_id"]), l["stat"]))
        ref = None
        if fl and fl.get("line") is not None and (fl.get("over") is not None or fl.get("under") is not None):
            ref = _best_side(row, float(fl["line"]), fl.get("over"), fl.get("under"), prm)
            if ref is not None:
                row["fliff"] = {"line": float(fl["line"]), "over": fl.get("over"), "under": fl.get("under")}
        row["ref"] = "fliff" if ref is not None else "dk"
        if ref is None:
            row["fliff"] = None
            ref = _best_side(row, dk_line, row["over"], row["under"], prm)
        row.update({"side": ref["side"], "ev": ref["ev"], "ok": bool(ref["ok"] and row["ref"] == "fliff")})
        rows.append(row)
    rows.sort(key=lambda r: (not r["ok"], -r["ev"], r["id"]))
    return rows[:int(prm["max_rows"])]


def fliff_board(props_ev: dict, id_to_name: dict[str, str], team_of: dict[str, str] | None = None,
                keys: tuple = ("fliff",)) -> dict:
    """Fliff's own prop lines from an Odds API event response (oddsapi.parse_event_props output),
    matched to ESPN athlete ids by name: {(player_id, stat): {"line", "over", "under"}}."""
    from .data.prizepicks import match_players
    rows = []
    for (name, stat), books in (props_ev or {}).items():
        for k in keys:
            rec = books.get(k)
            if rec and rec.get("line") is not None:
                rows.append({"name": name, "team": None, "stat": stat, "line": rec["line"],
                             "over": rec.get("over"), "under": rec.get("under")})
                break
    matched, _ = match_players(rows, id_to_name, team_of=team_of)
    return {(str(r["player_id"]), r["stat"]): {"line": r["line"], "over": r["over"], "under": r["under"]}
            for r in matched if r.get("player_id")}


def empty_block(notes: list[str] | None = None) -> dict:
    return {"platform": "Fliff", "source": "dk_proxy", "rows": [], "rules": _rules(PARAMS), "notes": list(notes or [])}


def _rules(prm: dict) -> dict:
    return {k: prm[k] for k in ("s", "min_ev", "max_edge", "kelly", "per_bet_cap", "props_cap")}


def build_block(legs: list[dict], fliff: dict | None = None, lean: dict | None = None,
                notes: list[str] | None = None, prm: dict | None = None) -> dict:
    prm = {**PARAMS, **(prm or {})}
    rows = build_rows(legs, fliff, prm)
    n_feed = sum(1 for r in rows if r["ref"] == "fliff")
    return {"platform": "Fliff", "source": "oddsapi_fliff" if n_feed else "dk_proxy", "rows": _round(rows),
            "rules": _rules(prm), "lean": lean, "n_fliff": n_feed, "n_ok": sum(1 for r in rows if r["ok"]),
            "notes": list(notes or [])}


def _round(obj, nd: int = 4):
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return round(obj, nd)
    if isinstance(obj, dict):
        return {k: _round(v, nd) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round(v, nd) for v in obj]
    return obj
