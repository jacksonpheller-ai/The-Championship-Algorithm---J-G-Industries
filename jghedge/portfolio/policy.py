"""Sizing and verdict policy (design memo sections 4 and 5).

Everything here is bankroll-growth arithmetic on *shrunk* payoff vectors.

Conventions
- A payoff vector is per $1 staked, one entry per simulation draw: ``dec - 1`` on a win,
  ``-1`` on a loss, ``0`` on a push or void. A payoff matrix ``R`` is draws x bets, the same
  object ``jghedge.portfolio.kelly`` and ``discrete`` consume.
- Shrinkage ``s`` per bet kind (``SHRINK``) encodes "the realised edge is about s times the
  claimed edge" (memo s.1: predicted +9.7% vs realised +6.6% on 821 replay bets; s = 0.5 is
  the floor for totals and parlays, props have no measured out-of-sample edge, s = 0).
  It is applied to the simulation itself (``shrink_payoff``) so every downstream number
  (Kelly, growth gate, night score) is computed on the shrunk joint distribution.
- The Kelly schedule the user chose is ADAPTIVE: aggressive while the bankroll is tiny,
  steadier as it grows (``TIERS``). The memo (s.5) warns that a schedule which is more
  aggressive at lower bankrolls bets more after losses; what keeps bust probability at zero
  is the per-bet cap (5%), the night cap (10%) and the growth gate below, and the 'tiny' tier
  asks for a smaller unit rather than pretending a $0.50 unit on $10 can be sized.
- Nothing here claims that passing a night raises expected growth: a pass is variance
  reduction (memo s.1 point 5). The text helpers keep to that phrasing.

The integrator (pipeline.py) calls, in order: ``shrink_matrix`` on the candidate payoff
matrix, ``bet_metrics`` per column for the bets[]/parlays[] fields, ``night_block`` for the
"night" block, ``size_ticket`` for the user's integer-unit ticket, and ``POLICY`` verbatim
for the "policy" block.
"""
from __future__ import annotations

import math

import numpy as np

from .discrete import discrete_allocate

# ----------------------------------------------------------------------------- constants
SHRINK = {"total": 0.5, "parlay": 0.5, "prop": 0.0, "pickem": 0.5}
SHRINK_DEFAULT = 0.0          # kinds without a measured edge (spread, moneyline) are informational
PER_BET_CAP = 0.05            # fraction of bankroll per bet
NIGHT_CAP = 0.10              # fraction of bankroll per night
OPT_IN_K = 0.5                # raw Kelly multiple a 'standard' user may opt into on BET nights
TIERS = (                     # units = bankroll / unit; max_units is an exclusive upper bound
    {"max_units": 25, "k": 0.5, "label": "tiny"},
    {"max_units": 100, "k": 0.35, "label": "small"},
    {"max_units": None, "k": 0.25, "label": "standard"},
)
MAX_PARLAY_LEGS = {"tiny": 2, "small": 2, "standard": 4}   # memo s.5: 2-leg parlays only below 100 units
SCORE_THRESHOLDS = {"light": 0.0005, "bet": 0.0015}        # memo s.4, shrunk profit as fraction of bankroll
MIN_SHRUNK_EV = 0.015         # qualifying floor on shrunk EV (pipeline min_ev applied after shrinkage)
REF_BANKROLL = 100.0          # the night score is evaluated at $100 ...
REF_UNIT = 0.5                # ... with $0.50 units ...
REF_K = 0.25                  # ... at the standard Kelly fraction, so verdicts compare across users
# Memo s.3: a half point on an NBA total is worth about +2.1% ROI at DK prices (2.11% integer
# push rate, n=569). Used only to phrase "needs X or better" hints; an assumption, not a price.
HALF_POINT_EV = 0.021

POLICY = {
    "s": dict(SHRINK),
    "tiers": [dict(t) for t in TIERS],
    "per_bet_cap": PER_BET_CAP,
    "night_cap": NIGHT_CAP,
    "opt_in_k": OPT_IN_K,
}


# ----------------------------------------------------------------------------- shrinkage
def shrink_factor(kind: str | None) -> float:
    """Shrinkage multiple s for a bet kind (unknown kinds are informational: s = 0)."""
    return float(SHRINK.get(kind, SHRINK_DEFAULT))


def breakeven(dec: float, p_push: float = 0.0) -> float:
    """Unconditional win probability at which a bet at decimal odds ``dec`` has zero EV.

    EV = p_win (dec - 1) - (1 - p_win - p_push) = dec (p_win - be)  with  be = (1 - p_push) / dec.
    """
    return (1.0 - p_push) / dec


def shrink_payoff(R, p_win_est: float | None = None, kind: str = "total", s: float | None = None,
                  dec: float | None = None, rng: np.random.Generator | None = None,
                  u: np.ndarray | None = None) -> np.ndarray:
    """Thin winning draws into losses so the payoff's mean EV becomes ``s x EV``.

    Target win rate  p' = be + s (p - be)  with be the break-even probability for the bet's
    decimal odds (push/void draws untouched). Because EV = dec (p - be), this is exactly
    EV' = s EV. For a payoff whose winning draws are not all equal (a parlay with a pushed
    leg pays the shorter price) the same target is reached by flipping the fraction
    q = (1 - s) (W - L) / (W + n_w) of winners in every payoff-value group, where W is the
    sum of winning payoffs, L the sum of loss magnitudes and n_w the number of winners.

    ``p_win_est`` (optional) is a strict win probability P(R > 0) the caller trusts more than
    the empirical rate, e.g. a closed-form parlay hit probability; with it the number of
    flipped draws is (1 - s)(p_win_est - be) n. ``dec`` is inferred from the winning payoffs
    when not given. Winners are flipped in order of a uniform ``u`` per draw (common random
    numbers): pass the same ``u`` for every column of a matrix so a draw thinned in a leg is
    the draw thinned in its parlay. A bet whose EV is already <= 0 is returned unchanged:
    shrinkage never improves a bet.
    """
    R = np.asarray(R, dtype=float)
    out = R.copy()
    s = shrink_factor(kind) if s is None else float(s)
    n = R.size
    if n == 0 or s >= 1.0:
        return out
    win = R > 0
    n_w = int(win.sum())
    if n_w == 0:
        return out
    W = float(R[win].sum())
    L = float(-R[R < 0].sum())
    ev_sum = W - L
    if ev_sum <= 0:
        return out
    if p_win_est is not None:
        d = float(dec) if dec is not None else 1.0 + W / n_w
        be = breakeven(d, float(np.mean(R == 0)))
        k = int(round((1.0 - s) * max(float(p_win_est) - be, 0.0) * n))
    else:
        q = (1.0 - s) * ev_sum / (W + n_w)
        k = int(round(q * n_w))
    k = min(max(k, 0), n_w)
    if k == 0:
        return out
    if u is None:
        u = (np.random.default_rng(0) if rng is None else rng).random(n)
    out[_pick_winners(R, win, np.asarray(u, dtype=float), k)] = -1.0
    return out


def _pick_winners(R: np.ndarray, win: np.ndarray, u: np.ndarray, k: int) -> np.ndarray:
    """Indices of the k winning draws to flip: smallest u within each payoff-value group,
    k apportioned across groups in proportion to their size (largest remainder)."""
    idx = np.flatnonzero(win)
    vals = np.round(R[idx], 9)
    uniq, inv, counts = np.unique(vals, return_inverse=True, return_counts=True)
    inv = np.asarray(inv).ravel()
    if uniq.size == 1:
        return idx[np.argsort(u[idx], kind="stable")][:k]
    raw = k * counts / idx.size
    kv = np.floor(raw).astype(int)
    rem = int(k - kv.sum())
    if rem > 0:
        kv[np.argsort(-(raw - kv), kind="stable")[:rem]] += 1
    chosen = []
    for gi in range(uniq.size):
        if kv[gi] <= 0:
            continue
        members = idx[inv == gi]
        chosen.append(members[np.argsort(u[members], kind="stable")][: kv[gi]])
    return np.concatenate(chosen) if chosen else np.zeros(0, dtype=int)


def shrink_matrix(R, kinds, decs=None, seed: int = 0, p_win=None) -> np.ndarray:
    """Shrink every column of a draws x bets payoff matrix by its kind's ``s``.

    One uniform per draw is shared by all columns (common random numbers), so the joint
    structure between a parlay and its legs survives the thinning as far as possible.
    Deterministic for a given ``seed``.
    """
    R = np.asarray(R, dtype=float)
    if R.ndim != 2 or R.shape[1] == 0:
        return R.copy()
    n, m = R.shape
    kinds = list(kinds)
    if len(kinds) != m:
        raise ValueError(f"kinds has {len(kinds)} entries for {m} columns")
    decs = list(decs) if decs is not None else [None] * m
    p_win = list(p_win) if p_win is not None else [None] * m
    u = np.random.default_rng(seed).random(n)
    out = np.empty_like(R)
    for j in range(m):
        out[:, j] = shrink_payoff(R[:, j], p_win[j], kinds[j], dec=decs[j], u=u)
    return out


# ----------------------------------------------------------------------------- schedule
def tiers(bankroll: float, unit: float) -> dict:
    """Kelly tier for a bankroll measured in units (bankroll / unit).

    < 25 units: k 0.5 'tiny' and ``ask_for_smaller_unit`` (a $0.50 unit on $10 is 5% of the
    bankroll, the per-bet cap, and freezes after one loss); 25-100: k 0.35 'small';
    >= 100: k 0.25 'standard' with ``opt_in_k`` 0.5 available on BET nights.
    """
    units = (bankroll / unit) if (unit and unit > 0 and bankroll and bankroll > 0) else 0.0
    tier = TIERS[-1]
    for t in TIERS:
        if t["max_units"] is None or units < t["max_units"]:
            tier = t
            break
    return {
        "k": tier["k"], "label": tier["label"], "units": float(units),
        "ask_for_smaller_unit": tier["label"] == "tiny",
        "opt_in_k": OPT_IN_K if units >= 100 else None,
        "per_bet_cap": PER_BET_CAP, "night_cap": NIGHT_CAP,
        "max_parlay_legs": MAX_PARLAY_LEGS[tier["label"]],
    }


# ----------------------------------------------------------------------------- growth gate
def growth_gate(R_col, bankroll: float, unit: float) -> float:
    """Expected log growth E[ln(1 + unit/bankroll x R)] of ONE unit on a (shrunk) payoff.

    -inf when a draw would wipe the bankroll (unit >= bankroll on a full loss)."""
    R = np.asarray(R_col, dtype=float)
    if R.size == 0 or bankroll <= 0 or unit <= 0:
        return -math.inf
    w = 1.0 + (unit / bankroll) * R
    if np.any(w <= 0):
        return -math.inf
    return float(np.mean(np.log(w)))


def growth_gate_matrix(R, bankroll: float, unit: float) -> np.ndarray:
    """``growth_gate`` for every column of a payoff matrix (vectorised)."""
    R = np.asarray(R, dtype=float)
    if R.ndim != 2 or R.shape[1] == 0:
        return np.zeros(0)
    if bankroll <= 0 or unit <= 0:
        return np.full(R.shape[1], -np.inf)
    w = 1.0 + (unit / bankroll) * R
    bad = (w <= 0).any(axis=0)
    g = np.mean(np.log(np.where(w > 0, w, 1.0)), axis=0)
    return np.where(bad, -np.inf, g)


def b_min(dec: float, ev_shrunk: float, unit: float) -> float:
    """Smallest bankroll at which one unit is growth-positive (memo s.2 B1 closed form):

        B_min = unit (dec - 1) / (2 EV_shrunk)

    from E[ln(1 + fR)] ~ f EV - f^2 E[R^2]/2 with E[R^2] = dec - 1 exactly at break-even.
    $11.36 for a -110 total at 4% claimed (2% shrunk) EV with $0.50 units. inf when
    ``ev_shrunk <= 0``."""
    if ev_shrunk is None or ev_shrunk <= 0 or dec is None or dec <= 1 or unit <= 0:
        return math.inf
    return float(unit * (dec - 1.0) / (2.0 * ev_shrunk))


def b_min_exact(R_col, unit: float, iters: int = 80) -> float:
    """Exact counterpart of ``b_min`` from the payoff draws: unit / f*, where f* is the
    positive root of g(f) = E[ln(1 + fR)] (bisection; g is concave with g(0) = 0 and
    g'(0) = EV). inf when EV <= 0; never below one unit."""
    R = np.asarray(R_col, dtype=float)
    if R.size == 0 or R.mean() <= 0 or unit <= 0:
        return math.inf
    worst = float(R.min())
    f_hi = (1.0 - 1e-9) / (-worst) if worst < 0 else 1.0
    if float(np.mean(np.log1p(f_hi * R))) > 0:
        return float(max(unit / f_hi, unit))
    lo, hi = 0.0, f_hi
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if float(np.mean(np.log1p(mid * R))) > 0:
            lo = mid
        else:
            hi = mid
    return float(max(unit / lo, unit)) if lo > 0 else math.inf


def _finite_or_none(x) -> float | None:
    """JSON-safe float: ``inf``/``nan`` become None (``json.dumps`` would write ``Infinity``,
    which the dashboard's ``JSON.parse`` rejects)."""
    if x is None:
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def bet_metrics(R_col, dec: float | None, unit: float = REF_UNIT, bankroll: float | None = None,
                bankroll_ref: float = REF_BANKROLL, unit_ref: float = REF_UNIT) -> dict:
    """Per-bet fields the contract adds to bets[] / parlays[] from a SHRUNK payoff column:
    shrunk_ev, p_shrunk (pushes count half, as ``simulate.win_prob``), b_min at ``unit``
    (None when no bankroll makes one unit growth-positive, i.e. shrunk EV <= 0),
    growth_1u_per_100 (one $0.50 unit on $100) and, with ``bankroll``, growth_1u at the
    user's bankroll and unit (None when a unit is at least the bankroll). All values are
    JSON-safe floats."""
    R = np.asarray(R_col, dtype=float)
    ev = float(R.mean()) if R.size else 0.0
    p = float(np.mean(R > 0) + 0.5 * np.mean(R == 0)) if R.size else 0.0
    out = {"shrunk_ev": ev, "p_shrunk": p,
           "b_min": _finite_or_none(b_min(dec, ev, unit) if dec else b_min_exact(R, unit)),
           "growth_1u_per_100": _finite_or_none(growth_gate(R, bankroll_ref, unit_ref))}
    if bankroll is not None:
        out["growth_1u"] = _finite_or_none(growth_gate(R, bankroll, unit))
    return out


# ----------------------------------------------------------------------------- lines
def fair_line(total_draws) -> float:
    """Our blended total: the median of the simulated totals."""
    return float(np.median(np.asarray(total_draws, dtype=float)))


def total_ev_at_line(total_draws, side: str, line: float, dec: float = 1.0 + 100.0 / 110.0) -> float:
    """Claimed EV per $1 of Over/Under ``line`` at decimal odds ``dec`` on the simulated totals."""
    t = np.asarray(total_draws, dtype=float)
    win = (t > line) if side == "over" else (t < line)
    push = t == line
    p_w, p_p = float(win.mean()), float(push.mean())
    return p_w * (dec - 1.0) - (1.0 - p_w - p_p)


def max_line(total_draws, side: str, line: float, dec: float = 1.0 + 100.0 / 110.0, s: float | None = None,
             floor: float = MIN_SHRUNK_EV, step: float = 0.5, span: float = 8.0) -> float | None:
    """Worst line at ``dec`` (default -110) at which the shrunk EV still clears ``floor``:
    for Over the highest total you may take, for Under the lowest. Scans ``line +- span`` in
    ``step`` increments; None when no line in the window qualifies."""
    s = shrink_factor("total") if s is None else float(s)
    n_steps = int(round(span / step))
    cands = [line + step * j for j in range(-n_steps, n_steps + 1)]
    ok = [c for c in cands if s * total_ev_at_line(total_draws, side, c, dec) >= floor]
    if not ok:
        return None
    return float(max(ok) if side == "over" else min(ok))


def still_ok(side: str, line_now: float | None, max_line_: float | None) -> bool:
    """Is the current line still at or better than the worst acceptable line?"""
    if line_now is None or max_line_ is None:
        return False
    return line_now <= max_line_ if side == "over" else line_now >= max_line_


# ----------------------------------------------------------------------------- allocation
def allocate(R_shrunk, bankroll: float, unit: float, k: float, per_bet_cap: float = PER_BET_CAP,
             night_cap: float = NIGHT_CAP) -> np.ndarray:
    """Integer units per bet: ``discrete_allocate`` on the shrunk joint draws with the policy
    caps, after dropping every bet whose single-unit growth at this bankroll is <= 0."""
    R = np.asarray(R_shrunk, dtype=float)
    n_bets = R.shape[1] if R.ndim == 2 else 0
    units = np.zeros(n_bets, dtype=int)
    if n_bets == 0 or bankroll <= 0 or unit <= 0:
        return units
    keep = np.flatnonzero(growth_gate_matrix(R, bankroll, unit) > 0)
    if keep.size == 0:
        return units
    cap_total = int(np.floor(night_cap * bankroll / unit + 1e-9))      # the greedy may need one step per unit
    units[keep] = discrete_allocate(R[:, keep], bankroll, unit, k, per_bet_cap, night_cap,
                                    max_steps=max(600, cap_total))
    return units


def reference_units(R_shrunk, k: float = REF_K, bankroll_ref: float = REF_BANKROLL,
                    unit_ref: float = REF_UNIT) -> np.ndarray:
    """The ticket at the reference configuration ($100, $0.50 units, k 0.25) that the night
    score is evaluated on."""
    return allocate(R_shrunk, bankroll_ref, unit_ref, k)


def size_ticket(R_shrunk, bankroll: float, unit: float, k: float | None = None, opt_in: bool = False,
                verdict: str | None = None) -> dict:
    """The user's integer-unit ticket under the adaptive schedule. ``k`` overrides the tier;
    ``opt_in`` uses the tier's opt_in_k when it offers one (>= 100 units) and the night's
    ``verdict`` is BET (or not given): LIGHT nights are sized at the default fraction only
    (memo s.4)."""
    t = tiers(bankroll, unit)
    allow_opt_in = bool(opt_in and t["opt_in_k"] and verdict in (None, "BET"))
    k_used = float(k) if k is not None else (t["opt_in_k"] if allow_opt_in else t["k"])
    units = allocate(R_shrunk, bankroll, unit, k_used, t["per_bet_cap"], t["night_cap"])
    R = np.asarray(R_shrunk, dtype=float)
    stakes = units * unit
    return {"tier": t, "k": k_used, "opt_in": allow_opt_in, "units": units, "stakes": stakes,
            "staked": float(stakes.sum()), "staked_frac": float(stakes.sum() / bankroll) if bankroll > 0 else 0.0,
            "growth_1u": growth_gate_matrix(R, bankroll, unit),
            "exp_profit": float(np.sum(stakes * R.mean(axis=0))) if units.size else 0.0}


# ----------------------------------------------------------------------------- night verdict
def night_score(R_shrunk, units, unit: float, bankroll_ref: float = REF_BANKROLL) -> float:
    """Shrunk expected profit of a ticket as a fraction of the reference bankroll:
    sum_i frac_i x mean(R_shrunk[:, i]) with frac_i = units_i x unit / bankroll_ref."""
    R = np.asarray(R_shrunk, dtype=float)
    units = np.asarray(units, dtype=float)
    if R.ndim != 2 or R.shape[1] == 0 or units.size == 0:
        return 0.0
    frac = units * unit / bankroll_ref
    return float(np.sum(frac * R.mean(axis=0)))


def verdict(score: float, n_bets: int, flags: dict | None = None, thresholds: dict = SCORE_THRESHOLDS) -> str:
    """PASS below the light threshold (or no bet survives the gate), LIGHT below the bet
    threshold, BET otherwise. ``flags['provisional']`` (lineups unconfirmed on most of the
    ticket) caps the verdict at LIGHT: the opt-in ceiling is only offered on clean information."""
    if n_bets <= 0 or score < thresholds["light"]:
        return "PASS"
    if score < thresholds["bet"]:
        return "LIGHT"
    if flags and flags.get("provisional"):
        return "LIGHT"
    return "BET"


night_verdict = verdict   # alias for use where a local variable is named `verdict`


def _money(x) -> str:
    x = float(x)
    return f"${x:,.0f}" if abs(x - round(x)) < 1e-9 else f"${x:,.2f}"


def _pct(x, dp: int = 1) -> str:
    return f"{100.0 * float(x):.{dp}f}%"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def headline(verdict: str, best_bets: list[dict], flags: dict | None = None,
             thresholds: dict = SCORE_THRESHOLDS) -> str:
    """One sentence in the memo's register. ``best_bets`` are dicts (best first) with
    label/kind/shrunk_ev and optionally growth_1u (at the user's bankroll) and b_min;
    ``flags`` may carry bankroll, unit, score, n_bets, provisional and, for PASS nights,
    history_roi / history_n / history_se (realised return per dollar on comparable past
    nights, from ``jghedge.growth.historical_band_roi``) for the memo's "historically nights
    like this returned about x per dollar" clause."""
    flags = flags or {}
    bankroll, unit = flags.get("bankroll"), flags.get("unit")
    score, n_bets = float(flags.get("score") or 0.0), int(flags.get("n_bets") or 0)
    top = best_bets[0] if best_bets else None
    where = f" at your {_money(unit)} unit on {_money(bankroll)}" if bankroll and unit else ""
    close = " Skipping costs no expected growth and cuts variance."
    if flags.get("history_roi") is not None and (flags.get("history_n") or 0) >= 10:
        roi, se = float(flags["history_roi"]), flags.get("history_se")
        nd = " (not distinguishable from zero)" if (se is None or abs(roi) <= 2.0 * float(se)) else ""
        close = (f" Historically nights like this returned about {_pct(roi, 0)} per dollar{nd}; "
                 "skipping costs no expected growth and cuts variance.")
    if verdict == "PASS":
        if top is None:
            return "Pass tonight: no bet still has an edge once the model's edge is halved." + close
        label = top.get("label") or top.get("id") or "best bet"
        ev = float(top.get("shrunk_ev") or 0.0)
        g = top.get("growth_1u")
        if g is not None and g <= 0:
            bm = top.get("b_min")
            need = (f" (about {_money(bm)} is needed to place one unit)"
                    if bm is not None and math.isfinite(bm) else "")
            tail = f"{where} no stake grows the bankroll{need}"
        else:
            tail = (f" the reference ticket's shrunk expected profit ({_pct(score, 3)} of bankroll) "
                    f"is below the {_pct(thresholds['light'], 3)} floor")
        return (f"Pass tonight. The best available {top.get('kind', 'bet')} ({label}) has {_pct(ev)} shrunk edge;"
                f"{tail}." + close)
    best_txt = ""
    if top is not None:
        best_txt = f" Best: {top.get('label') or top.get('id')} ({_pct(float(top.get('shrunk_ev') or 0.0))} shrunk edge)."
    if verdict == "LIGHT":
        prov = (" Provisional: GTD starters on the priced games; confirm lineups before staking."
                if flags.get("provisional") else "")
        return (f"Light night. {_plural(n_bets, 'bet')} clear the gate for about {_pct(score, 2)} of bankroll in "
                f"shrunk expected profit (a full ticket needs {_pct(thresholds['bet'], 2)}); stake at the default "
                f"Kelly fraction only, no opt-in.{best_txt}{prov}")
    return (f"Bet tonight. {_plural(n_bets, 'bet')} clear the gate for about {_pct(score, 2)} of bankroll in "
            f"shrunk expected profit; full ticket, opt-in ceiling available at 100+ units.{best_txt}")


def would_change(verdict: str, bets: list[dict], score: float, flags: dict | None = None,
                 thresholds: dict = SCORE_THRESHOLDS, floor: float = MIN_SHRUNK_EV) -> list[str]:
    """What line or news would flip the verdict. ``bets`` are dicts (one per priced bet) with
    id/label/kind/side/line and optionally max_line, shrunk_ev, frac_ref (reference stake
    fraction; > 0 means on the ticket), game_id."""
    flags = flags or {}
    out: list[str] = []
    ticket = sorted([b for b in bets if (b.get("frac_ref") or 0) > 0], key=lambda b: -(b.get("frac_ref") or 0))
    for b in ticket:
        label = b.get("label") or b.get("id") or "bet"
        side, line, mx = b.get("side"), b.get("line"), b.get("max_line")
        if b.get("kind") == "total" and side in ("over", "under") and line is not None:
            if mx is None:
                text = f"{label}: take {side.capitalize()} {line:g} at -110 or better."
            else:
                text = (f"{label}: {side.capitalize()} {line:g} at -110 or better; "
                        f"skip {'above' if side == 'over' else 'below'} {mx:g}.")
        else:
            text = f"{label}: take the number shown or better."
        v2 = night_verdict(score - (b.get("frac_ref") or 0) * (b.get("shrunk_ev") or 0), len(ticket) - 1,
                           flags, thresholds)
        if v2 != verdict:
            text += f" Losing it to a line move drops the night to {v2}."
        out.append(text)
    if verdict in ("PASS", "LIGHT"):
        nxt = "LIGHT" if verdict == "PASS" else "BET"
        gap = (thresholds["light"] if verdict == "PASS" else thresholds["bet"]) - score
        misses = [b for b in bets if (b.get("frac_ref") or 0) <= 0 and b.get("kind") == "total"
                  and b.get("shrunk_ev") is not None and b.get("line") is not None
                  and b.get("side") in ("over", "under") and b.get("shrunk_ev") < floor]
        misses.sort(key=lambda b: -b["shrunk_ev"])
        for b in misses[:2]:
            if b.get("max_line") is not None:                 # model-based worst acceptable line
                need, how = float(b["max_line"]), ""
            else:                                             # fall back to the half-point rule of thumb
                s_k = shrink_factor(b.get("kind", "total")) or 1.0
                per_half = HALF_POINT_EV * s_k
                n_half = max(1, math.ceil((floor - b["shrunk_ev"]) / per_half))
                need = b["line"] - 0.5 * n_half if b["side"] == "over" else b["line"] + 0.5 * n_half
                how = f" (assuming about {_pct(per_half)} shrunk edge per half point)"
            out.append(f"{b.get('label') or b.get('id')} is {_pct(b['shrunk_ev'])} shrunk edge, below the "
                       f"{_pct(floor)} floor; {b['side'].capitalize()} {need:g} or better would clear it{how}.")
        if bets and gap > 0:
            need_edge = gap / 0.02                          # edge a 2%-of-bankroll -110 total would need
            one = "one more" if ticket else "one"
            if need_edge < floor:
                out.append(f"{nxt} is within {_pct(gap, 3)} of bankroll in shrunk expected profit: {one} -110 total "
                           f"at the {_pct(floor)} floor sized at 2% of bankroll would clear it.")
            elif need_edge <= 0.08:
                out.append(f"{nxt} needs another {_pct(gap, 3)} of bankroll in shrunk expected profit, e.g. "
                           f"{one} -110 total at {_pct(need_edge)} shrunk edge sized at 2% of bankroll.")
    unc = list(flags.get("lineup_uncertainty") or [])
    if unc:
        lab = flags.get("game_labels") or {}
        out.append(f"GTD starters in {_plural(len(unc), 'priced game')} ({', '.join(str(lab.get(g, g)) for g in unc[:4])}): "
                   "a confirmed scratch re-prices those totals; stakes there are provisional until lineups post.")
    regime = flags.get("props_regime")
    if regime == "stand_down":
        out.append("Props are informational tonight (regime: stand down); no prop can change the verdict.")
    elif regime == "lean_under":
        out.append("Props regime: lean under, still informational (s = 0) until the rolling lean beats the vig "
                   "for 30 nights.")
    h = flags.get("hours_to_first_tip")
    if h is not None and h < 1.5:
        out.append(f"First tip in {float(h):.1f} h: lines may already have moved; re-check each bet against its "
                   "worst line before placing.")
    if verdict == "BET":
        out.append(f"BET holds while the reference ticket's shrunk expected profit stays at or above "
                   f"{_pct(thresholds['bet'], 2)} of bankroll; it has {_pct(score - thresholds['bet'], 2)} of room.")
    return out


def _why_nothing_placeable(rows: list[dict], bankroll: float, unit: float, t: dict) -> str:
    """Which of the three sizing hurdles stops every bet at the user's unit: the per-bet cap
    (one unit is more than 5% of the bankroll), the growth gate (no bet grows the bankroll
    with a one-unit stake) or the Kelly fraction (a unit is growth-positive but larger than
    the k-Kelly stake the tier allows, the memo's 'frozen' small bankroll)."""
    share = unit / bankroll if bankroll > 0 else math.inf
    head = f"At the reference unit no bet is placeable: one {_money(unit)} unit is {_pct(share)} of bankroll"
    if unit > t["per_bet_cap"] * bankroll + 1e-12:
        return f"{head}, above the {_pct(t['per_bet_cap'], 0)} per-bet cap ({_money(t['per_bet_cap'] * bankroll)})."
    pos = [r for r in rows if (r.get("growth_1u") or 0) > 0]
    if not pos:
        bms = [r["b_min"] for r in rows if r.get("b_min") is not None and math.isfinite(r["b_min"])]
        need = f"; about {_money(min(bms))} is the smallest bankroll at which one unit of the best bet grows it" if bms else ""
        return f"{head} and no stake of that size grows the bankroll on any bet{need}."
    return (f"{head}: growth-positive on {_plural(len(pos), 'bet')} but larger than the {t['k']:g}-Kelly stake on any "
            f"of them (the tier bets as if the bankroll were {t['k']:g} x {_money(bankroll)}); a smaller unit would "
            "place them.")


def reasons(verdict: str, rows: list[dict], score: float, n_bets: int, bankroll: float, unit: float,
            flags: dict | None = None, thresholds: dict = SCORE_THRESHOLDS, user_units=None) -> list[str]:
    """Plain-language reasons for the verdict (the dashboard's bullet list)."""
    flags = flags or {}
    t = tiers(bankroll, unit)
    positive = sum(1 for r in rows if (r.get("shrunk_ev") or 0) > 0)
    gate = (f"{n_bets} of the {len(rows)} totals and parlays that passed the edge filter ({positive} with a positive "
            f"conservative edge) clear the growth gate at the $100 / $0.50 reference." if rows else
            "No total or parlay passed the edge filter tonight (each needs a conservative edge of at least the "
            "floor before it is sized), so nothing reaches the growth gate.")
    out = [gate,
           f"Reference ticket shrunk expected profit: {_pct(score, 3)} of bankroll (PASS below "
           f"{_pct(thresholds['light'], 3)}, BET from {_pct(thresholds['bet'], 3)}).",
           f"Reference tier: {t['label']} ({t['units']:.0f} units of {_money(unit)} on {_money(bankroll)}); "
           f"Kelly fraction {t['k']:g} on shrunk edge"
           + (f", opt-in {t['opt_in_k']:g} available" if t["opt_in_k"] and verdict == "BET" else "") + "."]
    if user_units is not None:
        uu = np.asarray(user_units)
        n_user = int((uu > 0).sum())
        staked = float(uu.sum() * unit)
        if n_user:
            out.append(f"At the reference unit, {_plural(n_user, 'bet')} placeable for {_money(staked)} "
                       f"({_pct(staked / bankroll)} of bankroll).")
        else:
            out.append(_why_nothing_placeable(rows, bankroll, unit, t))
    if t["ask_for_smaller_unit"]:
        out.append(f"Below 25 units a {_money(unit)} unit freezes the bankroll after one loss; "
                   "ask your book for $0.10 units.")
    if flags.get("provisional"):
        out.append("Lineups unconfirmed on most of the ticket: verdict capped at LIGHT until they post.")
    out.append("Shrinkage: totals and parlays at s = 0.5 (realised edge about half of claimed on 821 replay bets); "
               "props at s = 0, informational.")
    return out


def night_block(R_shrunk, bets_meta: list[dict], bankroll: float, unit: float, flags: dict | None = None,
                k: float | None = None, thresholds: dict = SCORE_THRESHOLDS, bankroll_ref: float = REF_BANKROLL,
                unit_ref: float = REF_UNIT, k_ref: float = REF_K, history: dict | None = None) -> dict:
    """Assemble the contract's "night" block.

    ``bets_meta[i]`` describes column i of ``R_shrunk``: id, kind, label, game_id, dec and,
    for totals, side, line, max_line, fair_line (optional). ``flags`` may carry props_regime,
    lineup_uncertainty (game ids with GTD starters), hours_to_first_tip. The score is computed
    on the reference ticket ($100, $0.50, k 0.25); 'provisional' is set when at least half of
    the reference stake sits on games with unconfirmed lineups. ``history`` (optional, from
    ``jghedge.growth.historical_band_roi`` on nights with a comparable stored score) adds the
    realised-return clause to a PASS headline. The score is reported at 6 dp because its
    thresholds are 0.0005 and 0.0015.
    """
    R = np.asarray(R_shrunk, dtype=float)
    n_cols = R.shape[1] if R.ndim == 2 else 0
    flags = dict(flags or {})
    meta = list(bets_meta or [])
    if len(meta) != n_cols:
        raise ValueError(f"bets_meta has {len(meta)} entries for {n_cols} columns")
    ref_units = reference_units(R, k_ref, bankroll_ref, unit_ref) if n_cols else np.zeros(0, dtype=int)
    score = night_score(R, ref_units, unit_ref, bankroll_ref) if n_cols else 0.0
    n_bets = int((ref_units > 0).sum())
    unc = {str(g) for g in (flags.get("lineup_uncertainty") or [])}
    if unc and n_bets:
        # a parlay is exposed to a late scratch in any of its legs' games (meta "game_ids")
        stake_unc = sum(int(u) for u, m in zip(ref_units, meta)
                        if str(m.get("game_id")) in unc or any(str(g) in unc for g in (m.get("game_ids") or [])))
        flags["provisional"] = stake_unc >= 0.5 * int(ref_units.sum())
    v = verdict(score, n_bets, flags, thresholds)
    ev = R.mean(axis=0) if n_cols else np.zeros(0)
    g_user = growth_gate_matrix(R, bankroll, unit) if n_cols else np.zeros(0)
    g_ref = growth_gate_matrix(R, bankroll_ref, unit_ref) if n_cols else np.zeros(0)
    rows = []
    for j, m in enumerate(meta):
        rows.append({**m, "shrunk_ev": float(ev[j]), "growth_1u": _finite_or_none(g_user[j]),
                     "growth_1u_per_100": _finite_or_none(g_ref[j]),
                     "frac_ref": float(ref_units[j] * unit_ref / bankroll_ref),
                     "b_min": _finite_or_none(b_min(m.get("dec"), float(ev[j]), unit) if m.get("dec")
                                              else b_min_exact(R[:, j], unit))})
    best = sorted([r for r in rows if r["shrunk_ev"] > 0], key=lambda r: -(r["growth_1u_per_100"] or -math.inf))[:3]
    user_units = size_ticket(R, bankroll, unit, k=k, verdict=v)["units"] if n_cols else np.zeros(0, dtype=int)
    hflags = {**flags, "bankroll": bankroll, "unit": unit, "score": score, "n_bets": n_bets}
    if history:
        hflags.update({"history_roi": history.get("roi"), "history_n": history.get("n"), "history_se": history.get("se")})
    return {
        "verdict": v,
        "score": round(score, 6),
        "score_thresholds": dict(thresholds),
        "headline": headline(v, best, hflags, thresholds),
        "reasons": reasons(v, rows, score, n_bets, bankroll, unit, flags, thresholds, user_units),
        "would_change": would_change(v, rows, score, flags, thresholds),
        "flags": {"props_regime": flags.get("props_regime", "neutral"),
                  "props_regime_n": flags.get("props_regime_n"), "props_regime_min": flags.get("props_regime_min"),
                  "lineup_uncertainty": sorted(unc),
                  "hours_to_first_tip": (round(float(flags["hours_to_first_tip"]), 4)
                                         if flags.get("hours_to_first_tip") is not None else None)},
        "best_available": [str(r.get("id")) for r in best],
    }
