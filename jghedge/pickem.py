"""PrizePicks pick-em pricing: market-anchored leg probabilities, exact slip hit-count
distributions from the joint simulation, shrunk EV and log growth at the minimum entry.

Why it is built this way (research memo, 2026-11-07): our player projections add nothing
beyond the sportsbook line *at the same line*. Whatever edge a pick-em entry can have must
come from (a) the shape of the distribution around a market-anchored centre when the
PrizePicks line differs from the DraftKings line, (b) skewed DraftKings prices (over -140 says
the market median sits above the posted number) and (c) the payout table. Every probability
here is therefore a rank-preserving rescaling of the simulated stat line that reproduces the
no-vig DraftKings probability at the DraftKings line; the simulation only supplies the shape.

Pipeline use (one call per game, then one call per night):

    legs  = build_legs(players, prop_sims, dk_props, pp_rows, params, game={"id":..,"matchup":..,"tip":..})
    block = build_pickem_block(all_legs, all_prop_sims, source="prizepicks", fetched_at=iso)

`build_pickem_block` returns the `pickem` object of the data contract (legs, packed sims,
slips, break-evens, notes), floats rounded to 4 dp.
"""
from __future__ import annotations

import base64
import math
import zlib

import numpy as np

from .model.odds import implied, novig_multiplicative
from .model.props import COMBOS

# ------------------------------------------------------------------ assumptions
# PrizePicks payout table. ASSUMPTION, verification attempted 2026-11-07: this sandbox's network
# blocks prizepicks.com, intercom.help and api.prizepicks.com, so the official table could not be
# read. Web-search summaries of the official pages contradicted each other (Power 3-pick 5x vs 6x,
# 5/6-pick Power 20x/37.5x on one page; Flex 3/4-pick 2.25x/5x vs 3x/6x; the 6-pick Flex at
# 25x/2x/0.4x was consistent everywhere). The long-standing table below is the default; it is a
# parameter of every function, and the dashboard must label it as unverified.
PAYOUTS: dict = {
    "power": {2: 3.0, 3: 6.0, 4: 10.0, 5: 20.0, 6: 37.5},          # all picks must hit
    "flex": {3: [3.0, 1.0], 4: [6.0, 1.5], 5: [10.0, 2.0, 0.4], 6: [25.0, 2.0, 0.4]},  # [all, 1 miss, 2 misses]
}
MIN_ENTRY = 5.0    # ASSUMPTION: $5 standard minimum (official help centre, via web search 2026-11-07)
PAYOUT_NOTES = [
    "Payout table (partly an assumption): Power 2/3/4/5/6 picks pay 3x/6x/10x/20x/37.5x and Flex 3/4/5/6 picks pay 3x/6x/10x/25x when all hit, "
    "per PrizePicks' help centre as summarised by web search on 2026-11-07 (the site itself is blocked from the build host). "
    "Flex partial tiers (3-pick 1x with one miss, 4-pick 1.5x, 5-pick 2x/0.4x, 6-pick 2x/0.4x) are from secondary sources. "
    "PrizePicks notes some lineups (e.g. several players from one game) pay differently: check the multiplier the app shows.",
    "Minimum entry assumed $5 (official help centre, via web search 2026-11-07); Happy Hour promos can be lower.",
    "Standard (non-Demon/Goblin) projections only. A voided pick (the player sits, or a tie on a whole-number "
    "line) is assumed to drop the slip one size at that size's payout; under two live picks it is refunded.",
    "A slip needs players from at least two teams (assumed PrizePicks rule), and its picks are treated as independent.",
]

SLIP_TYPES = ("power2", "power3", "power4", "power5", "power6", "flex3", "flex4", "flex5", "flex6")
STAT_LABEL = {"pts": "Pts", "reb": "Reb", "ast": "Ast", "fg3m": "3PM", "stl": "Stl", "blk": "Blk",
              "tov": "TO", "pra": "Pts+Reb+Ast", "pr": "Pts+Reb", "pa": "Pts+Ast", "ra": "Reb+Ast"}

DEFAULT_PARAMS = {
    "min_play_prob": 0.9,       # leg requires P(plays) >= this
    "proxy_p_band": (0.40, 0.60),  # DK-proxy legs only from near-even DK markets (PrizePicks posts median lines)
    "take_p": 0.60,             # 'take it if the line is at most/least X' threshold on the raw anchored p
    "q_halfwidth": 6.0,         # line table spans dk_line +/- this, step 0.5
    "min_exp_min": 12.0,        # and projected minutes >= this
    "max_line_gap": 6.0,        # |PrizePicks line - DK line| above this is a data error -> drop
    "s": 0.5,                   # shrinkage toward break-even (memo: edge is about half the model's)
    "odds_types": ("standard",),
    "min_play_draws": 200,      # need this many playing draws to anchor
    "tol": 0.005,               # anchoring tolerance on P(X' > dk_line)
    "be_ref": None,             # leg-level reference break-even; None -> power2's
    "strain_band": (0.6, 1.6),  # anchoring multipliers outside this are flagged
    "lean": 0.0,                # under lean: points moved from P(more) to P(less) at the DK line (config.PICKEM_LEAN)
    "lean_min_under": 0.0,      # the lean applies only where DK's no-vig under is at least this (Fliff props: 0.45)
}


# ------------------------------------------------------------------- payouts
def payouts_json(payouts: dict = PAYOUTS) -> dict:
    """Contract shape: string keys."""
    return {"power": {str(k): float(v) for k, v in payouts.get("power", {}).items()},
            "flex": {str(k): [float(x) for x in v] for k, v in payouts.get("flex", {}).items()}}


def parse_slip_type(slip_type: str) -> tuple[str, int]:
    for kind in ("power", "flex"):
        if slip_type.startswith(kind):
            return kind, int(slip_type[len(kind):])
    raise ValueError(f"unknown slip type {slip_type!r}")


def available_slip_types(payouts: dict = PAYOUTS, max_legs: int = 6) -> list[str]:
    out = [f"power{n}" for n in sorted(payouts.get("power", {})) if n <= max_legs]
    out += [f"flex{n}" for n in sorted(payouts.get("flex", {})) if n <= max_legs]
    return out


def slip_types_for(n_legs: int, payouts: dict = PAYOUTS) -> list[str]:
    out = []
    if n_legs in payouts.get("power", {}):
        out.append(f"power{n_legs}")
    if n_legs in payouts.get("flex", {}):
        out.append(f"flex{n_legs}")
    return out


def payout_vector(slip_type: str, payouts: dict = PAYOUTS) -> np.ndarray:
    """Multiple of the entry returned for k hits, k = 0..n (index n = all correct)."""
    kind, n = parse_slip_type(slip_type)
    v = np.zeros(n + 1)
    if kind == "power":
        v[n] = float(payouts["power"][n])
    else:
        for miss, mult in enumerate(payouts["flex"][n]):
            if n - miss >= 0:
                v[n - miss] = float(mult)
    return v


def slip_ev_at(p: float, slip_type: str, payouts: dict = PAYOUTS) -> float:
    """EV per $1 of a slip whose legs are independent with equal probability p."""
    _, n = parse_slip_type(slip_type)
    pay = payout_vector(slip_type, payouts)
    k = np.arange(n + 1)
    pmf = np.array([math.comb(n, int(i)) for i in k]) * p ** k * (1 - p) ** (n - k)
    return float(pmf @ pay) - 1.0


def breakeven_probability(slip_type: str, payouts: dict = PAYOUTS, tol: float = 1e-9) -> float:
    """Per-leg p at which EV = 0 for independent equal legs (bisection; EV is increasing in p
    because the payout is non-decreasing in the number of hits)."""
    lo, hi = 0.0, 1.0
    if slip_ev_at(1.0, slip_type, payouts) <= 0:
        return 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if slip_ev_at(mid, slip_type, payouts) < 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2


def breakevens(payouts: dict = PAYOUTS, max_legs: int = 6) -> dict[str, float]:
    return {t: breakeven_probability(t, payouts) for t in available_slip_types(payouts, max_legs)}


def shrink(p, be, s):
    """Shrink only the edge ABOVE the break-even: be + s*(p - be) when p > be, p otherwise.
    s=1 keeps the model's number, s=0 says the market is right. A leg below break-even is never
    moved up (shrinkage must never improve a bet)."""
    if np.ndim(p):
        x = np.asarray(p, dtype=float)
        return np.where(x > be, be + s * (x - be), x)
    x = float(p)
    return be + s * (x - be) if x > be else x


# ------------------------------------------------------------------ anchoring
M_LO, M_HI = 0.2, 5.0


def _seed_for(*parts) -> int:
    return zlib.crc32(":".join(str(p) for p in parts).encode())


def _jitter(n: int, seed: int) -> np.ndarray:
    """Deterministic U(-0.5, 0.5) continuity jitter: integer counts become a continuous latent
    quantity so the anchoring can hit any target probability. Rank preserving across distinct
    counts (|u_i - u_j| < 1)."""
    return np.random.default_rng(int(seed) % (2 ** 32)).random(int(n)) - 0.5


def _transform(x, u, m: float, shift: float) -> np.ndarray:
    return m * (np.asarray(x, dtype=float) + u) + shift


def anchor_params(draws, dk_line: float, p_over_mkt: float, jitter: np.ndarray,
                  tol: float = 0.005, m_band: tuple[float, float] = (0.6, 1.6)) -> tuple[float, float]:
    """Find (m, shift) with P(m*(X+U) + shift > dk_line) == p_over_mkt. Multiplicative scaling
    first (shift 0): a minutes/usage story scales every counting stat. Additive shift (m = 1)
    when scaling cannot bracket the target, the draws are degenerate, or the multiplier leaves
    `m_band` (on low-count stats such as blocks a 0.5 line can only be crossed by tripling the
    whole distribution, which is the wrong shape; a shift moves the centre instead)."""
    x = np.asarray(draws, dtype=float)
    if x.size == 0:
        return 1.0, 0.0
    target = float(np.clip(p_over_mkt, 0.001, 0.999))

    def p_at(m: float, c: float = 0.0) -> float:
        return float(np.mean(_transform(x, jitter, m, c) > dk_line))

    if np.ptp(x) > 0 and p_at(M_LO) <= target <= p_at(M_HI):
        lo, hi = M_LO, M_HI
        for _ in range(60):
            mid = (lo + hi) / 2
            if p_at(mid) < target:
                lo = mid
            else:
                hi = mid
        m = (lo + hi) / 2
        if abs(p_at(m) - target) <= tol and m_band[0] <= m <= m_band[1]:
            return m, 0.0
    lo, hi = dk_line - float(x.max()) - 1.0, dk_line - float(x.min()) + 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if p_at(1.0, mid) < target:
            lo = mid
        else:
            hi = mid
    return 1.0, (lo + hi) / 2


def anchor_to_market(draws, dk_line: float, p_over_mkt: float, jitter=None, seed: int = 0,
                     tol: float = 0.005, m_band: tuple[float, float] = (0.6, 1.6)) -> tuple[np.ndarray, float]:
    """Rank-preserving transform of integer simulated draws (one player's stat, conditional on
    playing) so that P(X' > dk_line) == p_over_mkt within `tol`. X' = m*(X+U) with U a
    deterministic continuity jitter; m by bisection. Falls back to an additive shift when X is
    degenerate or the multiplier would leave `m_band` (then m == 1.0 and the shift is baked
    into the returned draws). Returns (float draws, m)."""
    x = np.asarray(draws, dtype=float)
    u = np.asarray(jitter, dtype=float) if jitter is not None else _jitter(x.size, seed)
    m, c = anchor_params(x, dk_line, p_over_mkt, u, tol, m_band)
    return _transform(x, u, m, c), m


def fair_median(draws) -> float:
    """Median of (anchored) draws: the market-consistent centre of the distribution."""
    x = np.asarray(draws, dtype=float)
    return float(np.median(x)) if x.size else float("nan")


def leg_probabilities(draws_anchored, line: float) -> tuple[float, float, float]:
    """(p_more, p_less, p_tie). The simulated count is round(X'); a tie is possible only on a
    whole-number line and voids the leg on PrizePicks, so p_more and p_less are conditional on
    no tie (they sum to 1) and p_tie is reported separately."""
    c = np.rint(np.asarray(draws_anchored, dtype=float))
    if c.size == 0:
        return 0.5, 0.5, 0.0
    line = float(line)
    tie = float(np.mean(c == line)) if line.is_integer() else 0.0
    more, less = float(np.mean(c > line)), float(np.mean(c < line))
    live = 1.0 - tie
    if live <= 0:
        return 0.5, 0.5, 1.0
    return more / live, less / live, tie


def line_table(draws_anchored, centre: float, halfwidth: float = 6.0, take_p: float = 0.60) -> tuple[list[dict], float | None, float | None]:
    """P(more)/P(less) at every half-point line around the market centre, so a line typed from the
    PrizePicks app can be re-priced in the browser, plus the take-it thresholds:
    more_if_at_most = highest half-line where P(more) >= take_p (take More at that line or lower),
    less_if_at_least = lowest half-line where P(less) >= take_p (take Less at that line or higher).
    Validation (DraftKings opening vs closing prop lines, n=2,866): a distribution anchored at the sharp
    line and priced at a stale line predicted 0.62 in the 0.60-0.65 band and realised 0.66 (n=589)."""
    c = np.rint(np.asarray(draws_anchored, dtype=float))
    out, more_max, less_min = [], None, None
    if c.size == 0:
        return out, None, None
    lo = max(0.0, math.floor(centre - halfwidth) + 0.5) if centre - halfwidth > 0 else 0.0
    line = lo
    while line <= centre + halfwidth + 1e-9:
        pm, pl, pt = leg_probabilities(c, line)
        out.append({"line": round(line, 1), "p_more": round(pm, 4), "p_less": round(pl, 4), "p_tie": round(pt, 4)})
        if not float(line).is_integer():
            if pm >= take_p:
                more_max = line
            if pl >= take_p and less_min is None:
                less_min = line
        line += 0.5
    return out, more_max, less_min


# ------------------------------------------------------------------ legs
def fmt_leg_label(leg: dict) -> str:
    """'Nikola Jokic More 25.5 Pts+Reb+Ast'."""
    side = "More" if leg.get("side") == "more" else "Less"
    line = leg.get("line")
    line_s = f"{float(line):g}" if line is not None else "?"
    return f"{leg.get('player', '?')} {side} {line_s} {STAT_LABEL.get(leg.get('stat'), leg.get('stat', ''))}".strip()


def _main_dk_lines(dk_props: list[dict]) -> dict[tuple[str, str], dict]:
    """(player_id, stat) -> the DK over/under row with the most balanced prices (the main line)."""
    best: dict[tuple[str, str], tuple[float, dict]] = {}
    for r in dk_props or []:
        if r.get("kind", "ou") != "ou" or r.get("over") is None or r.get("under") is None:
            continue
        if r.get("line") is None or r.get("stat") is None or r.get("athlete_id") is None:
            continue
        key = (str(r["athlete_id"]), r["stat"])
        bal = abs(implied(r["over"]) - implied(r["under"]))
        if key not in best or bal < best[key][0]:
            best[key] = (bal, r)
    return {k: v[1] for k, v in best.items()}


def _pp_lines(pp_lines: list[dict], odds_types) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for r in pp_lines or []:
        pid, st = r.get("player_id"), r.get("stat")
        if not pid or not st or r.get("line") is None:
            continue
        if (r.get("odds_type") or "standard") not in odds_types:
            continue
        if r.get("status") not in (None, "pre_game"):
            continue
        out.setdefault((str(pid), st), r)
    return out


def build_legs(players: list[dict], prop_sims: dict[str, dict], dk_props: list[dict],
               pp_lines: list[dict] | None = None, params: dict | None = None,
               game: dict | None = None) -> list[dict]:
    """One leg per (player, stat) that has a market anchor, sorted by shrunk edge.

    players   : PropModel.project_team output (id, name, team, play_prob, exp_min, exp_line, ...)
    prop_sims : PropModel.simulate output, {player_id: {stat: int16 draws, "_plays": bool}}
    dk_props  : espn.props rows (kind 'ou', athlete_id, stat, line, over, under)
    pp_lines  : prizepicks.match_players rows (player_id, stat, line, odds_type, status); when
                empty the DK line is used as the proxy and line_source is 'dk'
    game      : {"id", "matchup", "tip"} for labelling (one call per game)

    A base stat is anchored on its own DK line. A combo (pra/pr/pa/ra) without a DK line is
    anchored through its components when each has a DK line (the simulated within-player
    correlation is kept because the anchored component draws are summed draw by draw).
    No anchor -> no leg: the memo found no edge in the raw projection.
    """
    prm = {**DEFAULT_PARAMS, **(params or {})}
    payouts = prm.get("payouts") or PAYOUTS
    be_ref = prm["be_ref"] if prm.get("be_ref") is not None else breakeven_probability("power2", payouts)
    s = float(prm["s"])
    game = game or {}
    dk = _main_dk_lines(dk_props)
    pp = _pp_lines(pp_lines or [], tuple(prm["odds_types"]))
    lo_m, hi_m = prm["strain_band"]
    legs: list[dict] = []
    for p in players:
        pid = str(p["id"])
        sims = prop_sims.get(pid)
        if sims is None or "_plays" not in sims:
            continue
        if float(p.get("play_prob", 1.0)) < prm["min_play_prob"] or float(p.get("exp_min", 0.0)) < prm["min_exp_min"]:
            continue
        plays = np.asarray(sims["_plays"], dtype=bool)
        n = plays.size
        if int(plays.sum()) < prm["min_play_draws"]:
            continue
        stats = sorted({st for (q, st) in list(dk) + list(pp) if q == pid})
        cache: dict[str, dict | None] = {}

        def anchor_stat(st: str, lean: bool = True) -> dict | None:
            key = (st, lean)
            if key in cache:
                return cache[key]
            row, x = dk.get((pid, st)), sims.get(st)
            if row is None or x is None:
                cache[key] = None
                return None
            seed = _seed_for(pid, st)
            u = _jitter(n, seed)
            p_over, _ = novig_multiplicative(row["over"], row["under"])
            # sportsbook overs hit less often than their no-vig price: anchor to the price minus the lean
            lean_v = float(prm["lean"]) if lean and (1.0 - p_over) >= float(prm["lean_min_under"]) else 0.0
            p_anchor = float(min(max(p_over - lean_v, 0.02), 0.98))
            m, c = anchor_params(np.asarray(x, dtype=float)[plays], float(row["line"]), p_anchor, u[plays],
                                 prm["tol"], tuple(prm["strain_band"]))
            cache[key] = {"x": _transform(x, u, m, c), "m": m, "shift": c, "seed": seed, "dk": row,
                          "p_over_mkt": p_over, "p_over_anchor": p_anchor, "anchor": "dk", "components": None}
            return cache[key]

        for st in stats:
            a = anchor_stat(st)
            if a is None and st in COMBOS:
                # components are anchored WITHOUT the lean and the lean is applied once to the sum below;
                # leaning each component first would move a Pts+Reb+Ast pick by about 1.5 times the lean
                parts = [anchor_stat(x, lean=False) for x in COMBOS[st]]
                if all(parts):
                    a = {"x": sum(q["x"] for q in parts), "m": None, "shift": None, "seed": None, "dk": None,
                         "p_over_mkt": None, "p_over_anchor": None, "anchor": "components", "lean_pending": True,
                         "components": [{"stat": c_st, "m": q["m"], "shift": q["shift"], "seed": q["seed"]}
                                        for c_st, q in zip(COMBOS[st], parts)]}
            if a is None:
                continue
            pprow, dk_row = pp.get((pid, st)), a["dk"]
            if pp and pprow is None:
                continue   # live board: a leg PrizePicks does not offer cannot be played
            if pprow is not None:
                line, src = float(pprow["line"]), "prizepicks"
            elif dk_row is not None:
                line, src = float(dk_row["line"]), "dk"
            else:
                continue
            if dk_row is not None and abs(line - float(dk_row["line"])) > prm["max_line_gap"]:
                continue
            if src == "dk":
                lo_p, hi_p = prm["proxy_p_band"]
                if a["p_over_mkt"] is None or not (lo_p <= a["p_over_mkt"] <= hi_p):
                    continue   # lopsided DK market: PrizePicks would post a different number (or none)
            if a.get("lean_pending") and float(prm["lean"]) > 0:
                # one shift of the summed draws so that P(more) at this line falls by exactly the lean
                xs = a["x"][plays]
                edge = line if not float(line).is_integer() else line + 0.5      # round(x) > line  <=>  x >= edge
                target = max(float(np.mean(np.rint(xs) > line)) - float(prm["lean"]), 0.02)
                a = {**a, "shift": float(edge - np.quantile(xs, 1.0 - target))}
                a["x"] = a["x"] + a["shift"]
            xa = a["x"][plays]
            p_more, p_less, p_tie = leg_probabilities(xa, line)
            side = "more" if p_more >= p_less else "less"
            p_side = max(p_more, p_less)
            p_shr = float(shrink(p_side, be_ref, s))
            gid = game.get("id") or "na"
            leg = {
                "id": f"{gid}-pk-{pid}-{st}-{line:g}-{side}",
                "player": p.get("name"), "player_id": pid, "team": p.get("team"),
                "matchup": game.get("matchup"), "game_id": game.get("id"),
                "tip": game.get("tip") or (pprow or {}).get("start_time"),
                "stat": st, "line": line, "line_source": src,
                "dk_line": float(dk_row["line"]) if dk_row is not None else None,
                "dk_over_price": dk_row.get("over") if dk_row is not None else None,
                "dk_under_price": dk_row.get("under") if dk_row is not None else None,
                "p_over_mkt": a["p_over_mkt"], "p_over_anchor": a["p_over_anchor"], "lean": float(prm["lean"]),
                "market_median": fair_median(xa),
                "p_more": p_more, "p_less": p_less, "p_tie": p_tie,
                "side": side, "p_side": p_side, "p_side_shrunk": p_shr,
                "edge": p_side - be_ref, "edge_shrunk": p_shr - be_ref, "be_ref": be_ref,
                "play_prob": float(p.get("play_prob", 1.0)),
                "p_void": float(1.0 - plays.mean()),
                "proj": (p.get("exp_line") or {}).get(st),
                "exp_min": float(p.get("exp_min", 0.0)),
                "anchor": a["anchor"], "anchor_m": a["m"], "anchor_shift": a["shift"], "anchor_seed": a["seed"],
                "anchor_components": a["components"],
                # True when the market and the simulation disagree by more than a minutes story
                # (additive shift used, or a multiplier outside the band): look for news
                "anchor_strain": bool(a["m"] is not None and not (lo_m <= a["m"] <= hi_m)) or bool(a["shift"])
                or bool(a["components"] and any(q["shift"] for q in a["components"])),
                "odds_type": (pprow or {}).get("odds_type", "standard") if pprow else None,
                "pp_projection_id": (pprow or {}).get("projection_id") if pprow else None,
            }
            centre = float(dk_row["line"]) if dk_row is not None else line
            leg["q"], leg["more_if_at_most"], leg["less_if_at_least"] = line_table(xa, centre, prm["q_halfwidth"], prm["take_p"])
            leg["label"] = fmt_leg_label(leg)
            legs.append(leg)
    legs.sort(key=lambda l: (-l["p_side_shrunk"], l["id"]))
    return legs


# ------------------------------------------------------------------ joint draws
def _anchored_draws(leg: dict, sims: dict) -> np.ndarray:
    n = np.asarray(sims["_plays"]).size
    if leg.get("anchor") == "components" and leg.get("anchor_components"):
        tot = np.zeros(n)
        for c in leg["anchor_components"]:
            tot += _transform(sims[c["stat"]], _jitter(n, c["seed"]), float(c["m"]), float(c["shift"]))
        return tot + float(leg.get("anchor_shift") or 0.0)      # the lean, applied once to the sum
    return _transform(sims[leg["stat"]], _jitter(n, leg["anchor_seed"]), float(leg["anchor_m"]), float(leg["anchor_shift"]))


def hit_matrix(legs: list[dict], prop_sims: dict[str, dict]) -> np.ndarray:
    """(n_legs, n) uint8: 1 where the leg's side wins in that joint draw. Draws where the player
    does not play are 0 (a void is treated as a loss; the leg carries p_void and p_tie)."""
    if not legs:
        return np.zeros((0, 0), dtype=np.uint8)
    n = np.asarray(prop_sims[str(legs[0]["player_id"])]["_plays"]).size
    H = np.zeros((len(legs), n), dtype=np.uint8)
    for i, leg in enumerate(legs):
        sims = prop_sims[str(leg["player_id"])]
        c = np.rint(_anchored_draws(leg, sims))
        win = (c > leg["line"]) if leg["side"] == "more" else (c < leg["line"])
        H[i] = (win & np.asarray(sims["_plays"], dtype=bool)).astype(np.uint8)
    return H


def pack_bits(matrix: np.ndarray) -> str:
    """base64(numpy.packbits(matrix.ravel())) — row-major, bit k = row k // n, column k % n,
    most significant bit first within each byte (numpy default)."""
    m = np.ascontiguousarray(np.asarray(matrix, dtype=np.uint8))
    return base64.b64encode(np.packbits(m.ravel()).tobytes()).decode("ascii")


def unpack_bits(b64: str, n_legs: int, n: int) -> np.ndarray:
    raw = np.frombuffer(base64.b64decode(b64), dtype=np.uint8)
    bits = np.unpackbits(raw)[: n_legs * n]
    return bits.reshape(n_legs, n)


# ------------------------------------------------------------------ slips
def _thin(rows: np.ndarray, p_cond: np.ndarray, be: float, s: float, U: np.ndarray) -> np.ndarray:
    """Flip wins to losses (or losses to wins) so each row's marginal equals its shrunk value.
    p_cond is the leg's probability conditional on playing; the unconditional marginal is scaled
    by the same ratio so voids stay voids."""
    out = rows.copy()
    p_u = rows.mean(axis=1)
    for i in range(rows.shape[0]):
        pc = float(p_cond[i]) if p_cond[i] > 0 else float(p_u[i])
        target = float(shrink(pc, be, s))
        if pc <= 0 or p_u[i] <= 0:
            continue
        ratio = target / pc
        if ratio < 1.0:                         # keep each win with prob ratio; never promote losses
            out[i] = rows[i] & (U[i] < ratio)
    return out


def _dist(cum: np.ndarray, n_legs: int) -> np.ndarray:
    return np.bincount(cum.astype(np.intp), minlength=n_legs + 1)[: n_legs + 1] / cum.size


def growth_of(q_dist: np.ndarray, pay: np.ndarray, stake: float, bankroll: float) -> float:
    """E[ln(1 + (payout-1) * stake/bankroll)] under the hit-count distribution q."""
    f = stake / bankroll
    q = np.asarray(q_dist, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        lg = np.log1p((np.asarray(pay, dtype=float) - 1.0) * f)
    lg = np.where(q > 0, lg, 0.0)          # a zero-probability total loss must not make 0 * -inf
    return float(q @ lg)


def min_bankroll_for_growth(q_dist: np.ndarray, pay: np.ndarray, stake: float) -> float | None:
    """Smallest bankroll at which one entry of `stake` has positive expected log growth
    (None when the shrunk EV is not positive). g(f) is concave with g(0)=0, so the root in f
    is unique."""
    if float(q_dist @ pay) - 1.0 <= 0:
        return None
    lo, hi = 1e-6, 1.0 - 1e-9    # bankroll fraction f = stake / B (f = 1 would be a total loss)
    if growth_of(q_dist, pay, hi, 1.0) > 0:
        return float(stake / hi)
    for _ in range(80):
        mid = (lo + hi) / 2
        if growth_of(q_dist, pay, mid, 1.0) > 0:
            lo = mid
        else:
            hi = mid
    return float(stake / lo)


def _too_similar(x: dict, chosen: list[dict], max_overlap: float) -> bool:
    """Near-duplicate filter within a slip type: shared legs / smaller slip >= max_overlap."""
    if max_overlap >= 1.0:
        return False
    a = set(x["legs"])
    for c in chosen:
        if c["type"] != x["type"]:
            continue
        b = set(c["legs"])
        if len(a & b) / max(min(len(a), len(b)), 1) >= max_overlap:
            return True
    return False


def _step_down_vector(kind: str, m: int, payouts: dict) -> np.ndarray | None:
    """Payout vector for a slip left with `m` live picks: the same family at that size, else the other
    family, else None (refund). Mirrors ledger.settle_pickem."""
    if m < 2:
        return None
    for fam in (kind, "flex" if kind == "power" else "power"):
        if m in payouts.get(fam, {}):
            return payout_vector(f"{fam}{m}", payouts)
    return None


def slip_outcomes(ps, vs, slip_type: str, payouts: dict = PAYOUTS) -> dict:
    """Exact outcome table of a slip whose picks are independent.

    ps[i] = P(pick i hits | it is live); vs[i] = P(pick i is void: the player sits, or the stat lands on
    a whole-number line). ASSUMPTION (PrizePicks house rules as remembered, same as the grader): a void
    pick drops the slip to the next size down at that size's payout, and under two live picks the entry
    is refunded. Returns the probability and payout multiple of every (live picks, hits) cell, the
    marginal hit-count distribution and P(every pick is live and hits)."""
    kind, n = parse_slip_type(slip_type)
    table = np.zeros((n + 1, n + 1))            # [live picks, hits]
    table[0, 0] = 1.0
    for p, v in zip(ps, vs):
        p, v = float(p), float(v)
        nxt = table * v
        nxt[1:, :] += table[:-1, :] * (1 - v) * (1 - p)
        nxt[1:, 1:] += table[:-1, :-1] * (1 - v) * p
        table = nxt
    probs, mults = [], []
    for m in range(n + 1):
        vec = _step_down_vector(kind, m, payouts)
        for h in range(m + 1):
            if table[m, h] > 0:
                probs.append(table[m, h])
                mults.append(1.0 if vec is None else float(vec[h]))
    return {"probs": np.array(probs), "mults": np.array(mults), "hit_dist": table.sum(axis=0), "p_all": float(table[n, n])}


def enumerate_slips(legs: list[dict], hits: np.ndarray | None = None, payouts: dict = PAYOUTS,
                    min_entry: float = MIN_ENTRY, bankroll_ref: float = 100.0, max_legs: int = 6,
                    pool: int = 14, s: float = 0.5, top: int = 12, seed: int = 12345,
                    max_overlap: float = 0.8) -> list[dict]:
    """Every combination of 2..max_legs legs from the `pool` best legs (one leg per player, players from
    at least two teams), priced EXACTLY from the legs' own probabilities (slip_outcomes):
      p_dist     hit-count distribution at the raw probabilities (p_dist_shrunk at the shrunk ones)
      ev_mult/ev expected payout multiple and EV per $1 at raw probabilities, voids included
      ev_shrunk  the same with each leg shrunk toward this slip type's break-even
      growth_at_min_entry_per_100  E[ln(1 + (payout-1) * min_entry/bankroll_ref)] at shrunk probabilities
      b_min      smallest bankroll at which one minimum entry is growth-positive
      verdict    play / marginal / skip
    Picks of different players are treated as independent (measured |rho| < 0.02). The slips used to be
    priced from the joint simulation draws, but every one of several thousand combinations was scored
    on the same draws and the luckiest were reported: with every leg truly 0.57 that listed a dozen
    "play" slips at +12% to +14% when the exact answer is +9.3% and none grows a $100 bankroll.
    `hits` is only a fallback for a leg without "p_side" (its row mean). Returns the top `top` by
    growth, skipping near-duplicates of a listed slip of the same type (`max_overlap`, 1.0 disables),
    plus the best positive slip of any type not shown.
    """
    if not legs:
        return []
    H = None if hits is None or np.size(hits) == 0 else np.asarray(hits, dtype=np.uint8)
    n_all = len(legs) if H is None else min(len(legs), H.shape[0])

    def p_of(i: int) -> float:
        v = legs[i].get("p_side")
        return float(v) if v is not None else float(H[i].mean())

    if H is None and any(legs[i].get("p_side") is None for i in range(n_all)):
        return []
    order = sorted(range(n_all), key=lambda i: -float(legs[i].get("p_side_shrunk", p_of(i))))[:pool]
    if len(order) < 2:
        return []
    types = available_slip_types(payouts, max_legs)
    be = {t: breakeven_probability(t, payouts) for t in types}
    pay = {t: payout_vector(t, payouts) for t in types}
    p_raw = [p_of(i) for i in order]
    # a pick is void when the player sits or (whole-number lines only) the stat lands on the line
    void = [min(max(float(legs[i].get("p_void") or 0.0) + (1.0 - float(legs[i].get("p_void") or 0.0)) * float(legs[i].get("p_tie") or 0.0), 0.0), 1.0)
            for i in order]
    p_shr = {t: [float(shrink(x, be[t], s)) for x in p_raw] for t in types}
    who = [legs[i].get("player_id") for i in order]
    team = [legs[i].get("team") for i in order]
    found: list[dict] = []

    def rec(start: int, chosen: list[int]) -> None:
        d = len(chosen)
        # ASSUMPTION: PrizePicks rejects an entry whose players are all on one team
        if d >= 2 and not (all(team[c] is not None for c in chosen) and len({team[c] for c in chosen}) == 1):
            vs = [void[c] for c in chosen]
            for t in slip_types_for(d, payouts):
                if t not in types:
                    continue
                raw = slip_outcomes([p_raw[c] for c in chosen], vs, t, payouts)
                shr = slip_outcomes([p_shr[t][c] for c in chosen], vs, t, payouts)
                ev_mult = float(raw["probs"] @ raw["mults"])
                ev_shr = float(shr["probs"] @ shr["mults"]) - 1.0
                g = growth_of(shr["probs"], shr["mults"], min_entry, bankroll_ref)
                found.append({
                    "type": t, "legs": [legs[order[c]]["id"] for c in chosen], "n_legs": d,
                    "p_dist": raw["hit_dist"].tolist(), "p_dist_shrunk": shr["hit_dist"].tolist(),
                    "p_all": raw["p_all"], "p_all_shrunk": shr["p_all"],
                    "payout": pay[t].tolist(), "ev_mult": ev_mult, "ev": ev_mult - 1.0, "ev_shrunk": ev_shr,
                    "growth_at_min_entry_per_100": g,
                    "b_min": min_bankroll_for_growth(shr["probs"], shr["mults"], min_entry),
                    "verdict": "play" if (ev_shr > 0 and g > 0) else ("marginal" if ev_shr > 0 else "skip"),
                })
        if d >= max_legs:
            return
        used = {who[c] for c in chosen}
        for k in range(start, len(order)):
            if who[k] in used:
                continue
            rec(k + 1, chosen + [k])

    rec(0, [])
    found.sort(key=lambda x: (-x["growth_at_min_entry_per_100"], -x["ev_shrunk"]))
    chosen: list[dict] = []
    for x in found:
        if len(chosen) >= top:
            break
        if not _too_similar(x, chosen, max_overlap):
            chosen.append(x)
    for t in types:
        if any(x["type"] == t for x in chosen):
            continue
        best = next((x for x in found if x["type"] == t and x["ev_shrunk"] > 0), None)
        if best is not None:
            chosen.append(best)
    chosen.sort(key=lambda x: (-x["growth_at_min_entry_per_100"], -x["ev_shrunk"]))
    label = {l["id"]: l.get("label") or fmt_leg_label(l) for l in legs}
    for i, x in enumerate(chosen):
        x["id"] = f"pk-{i + 1}-{x['type']}"
        x["label"] = " + ".join(label.get(l, l) for l in x["legs"])
    return chosen


# ------------------------------------------------------------------ assembly
def _round(obj, nd: int = 4):
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else round(obj, nd)
    if isinstance(obj, (np.floating,)):
        return _round(float(obj), nd)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, dict):
        return {k: _round(v, nd) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round(v, nd) for v in obj]
    return obj


def empty_block(source: str = "dk_proxy", fetched_at: str | None = None, notes: list[str] | None = None,
                payouts: dict = PAYOUTS, min_entry: float = MIN_ENTRY) -> dict:
    return {"platform": "PrizePicks", "source": source, "fetched_at": fetched_at, "min_entry": float(min_entry),
            "payouts": payouts_json(payouts), "breakeven": _round(breakevens(payouts)),
            "legs": [], "sims": {"n": 0, "legs": [], "bits": ""}, "slips": [],
            "notes": list(PAYOUT_NOTES) + list(notes or [])}


def build_pickem_block(legs: list[dict], prop_sims: dict[str, dict], source: str = "dk_proxy",
                       fetched_at: str | None = None, payouts: dict = PAYOUTS, min_entry: float = MIN_ENTRY,
                       s: float = 0.5, notes: list[str] | None = None, max_sim_legs: int = 30,
                       pool: int = 14, max_legs: int = 6, bankroll_ref: float = 100.0, top: int = 12) -> dict:
    """The `pickem` object of the data contract. `legs` is the concatenation of build_legs over
    tonight's games (any order) and `prop_sims` the merged {player_id: sims} dict; every game's
    simulation must use the same number of draws."""
    out = empty_block(source, fetched_at, notes, payouts, min_entry)
    if not legs:
        out["notes"].append("No market-anchored legs tonight (no DraftKings props or no qualifying players).")
        return out
    legs = sorted(legs, key=lambda l: (-l["p_side_shrunk"], l["id"]))
    slips = enumerate_slips(legs[:max_sim_legs], None, payouts, min_entry, bankroll_ref, max_legs, pool, s, top)
    # anchoring parameters keep full precision so a leg's draws can be re-derived exactly (hit_matrix)
    out["legs"] = [_round(l) | {k: l.get(k) for k in ("anchor_m", "anchor_shift", "anchor_components")} for l in legs]
    # slips are priced exactly from the legs, so the packed joint draws are no longer published
    out["sims"] = {"n": 0, "legs": [], "bits": ""}
    proxy = source not in ("prizepicks", "oddsapi_prizepicks") and all(l.get("line_source") != "prizepicks" for l in legs)
    by_id = {l["id"]: l for l in legs}
    for x in slips:
        stand_in = proxy or any(by_id.get(i, {}).get("line_source") == "dk" for i in x.get("legs", []))
        if stand_in:
            x["verdict_if_lines_match"] = x["verdict"]
            # a leg with no take-it line can never be confirmed in the app, so the slip is not playable
            reachable = all((by_id.get(i, {}).get("more_if_at_most") if by_id.get(i, {}).get("side") == "more"
                             else by_id.get(i, {}).get("less_if_at_least")) is not None for i in x.get("legs", []))
            if x["verdict"] == "play":          # only would-be plays are worth a line check
                x["verdict"] = "check_lines" if reachable else "skip"
        out["notes"].append("PrizePicks lines were not available to the server, so these legs use DraftKings' main line "
                            "as a stand-in. A slip is only worth playing where PrizePicks posts a line at or better than each "
                            "leg's take-it threshold (More if the line is at most X, Less if at least Y). Type the app's "
                            "lines into the table to re-price.")
    out["notes"] = list(dict.fromkeys(out["notes"]))   # the stand-in note is added once per slip above
    out["mode"] = "check_lines" if proxy else "live_lines"
    out["slips"] = _round(slips)
    n_play = sum(1 for x in slips if x["verdict"] == "play")
    n_marg = sum(1 for x in slips if x["verdict"] == "marginal")
    if slips and n_play == 0 and not proxy:
        out["notes"].append(f"No slip grows a $100 bankroll at the $5 minimum tonight "
                            f"({n_marg} positive-EV but growth-negative: the entry is too large for the edge). "
                            "See b_min on each slip for the bankroll at which it turns growth-positive.")
    return out
