"""Bootstrap of bankroll growth from the replay's nightly returns (memo s.1 point 5, s.7).

``data/replay_open.json`` stores, per season and per night, ``ret`` (return on bankroll of
the balanced profile, k = 0.25 raw Kelly, 5% per-bet / 20% night caps, priced at the opener)
and ``staked`` (fraction of bankroll staked). A season of ``nights`` nights is resampled
with replacement from the pooled nights of every season (and every seed the file stores),
and replayed through the sizing policy crudely:

- the night's stake fraction is ``staked x k / 0.25`` capped at the policy night cap (10%),
  so other Kelly multiples are a linear rescaling of the balanced path (first-order exact,
  caps aside; the output ``basis`` says so);
- the night's stake is split equally across its ``bets`` (the replay stores the count) and
  each is rounded to whole units, ``round_to_unit(frac x bankroll)``, capped at 5% of the
  bankroll per bet and 10% per night in whole units (as ``discrete_allocate`` caps); the
  night is skipped when a bet rounds below one unit, which is what freezes a $10 bankroll
  at $0.50 units (the balanced replay's median stake is ~1% of bankroll per bet);
- the night's return per dollar staked, ``ret / staked``, is applied to the rounded stake.

Reported per configuration: median, p10 and p90 of the ending bankroll (dollars), p_bust
(bankroll fell below one unit, which also freezes the path), p_dd50 (max drawdown >= 50%).
The 2000 x 160 grid over 7 bankrolls x 4 units x 3 Kelly multiples runs in a few seconds.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np

from .config import DATA_DIR

REPLAY_FILE = DATA_DIR / "replay_open.json"
REPLAY_K = 0.25                         # the replay's balanced profile
NIGHT_CAP = 0.10                        # policy night cap (jghedge.portfolio.policy.NIGHT_CAP)
PER_BET_CAP = 0.05                      # policy per-bet cap (jghedge.portfolio.policy.PER_BET_CAP)
BANKROLLS = (10, 25, 50, 100, 250, 500, 1000)
UNITS = (0.1, 0.5, 1, 5)
KS = (0.25, 0.35, 0.5)
DEFAULT_SEASONS = ("2024", "2025", "2026", "2027")


# ----------------------------------------------------------------------------- data
def _night_rows(x) -> bool:
    """A list of per-night replay rows (``date/games/bets/staked/ret/bank``)."""
    return (isinstance(x, list) and bool(x) and isinstance(x[0], dict)
            and "ret" in x[0] and "staked" in x[0])


def _season_paths(v: dict) -> list[list]:
    """Every nightly path stored in one season entry: ``nightly`` plus any further seed the
    replay may store as another list of night rows (e.g. ``nightly_seed2``), a list of runs
    (``seeds``/``runs``/``paths``: dicts with their own ``nightly``) or a dict of runs keyed
    by seed. Per-bet rows, ``small`` tracks and calibration tables are not night rows and
    are ignored."""
    paths, seen = [], set()

    def add(p):
        if _night_rows(p) and id(p) not in seen:
            seen.add(id(p))
            paths.append(p)

    add(v.get("nightly"))
    for key, val in v.items():
        if key == "nightly":
            continue
        if _night_rows(val):
            add(val)
        elif isinstance(val, list):
            for run in val:
                if isinstance(run, dict):
                    add(run.get("nightly"))
        elif isinstance(val, dict):
            for run in val.values():
                if isinstance(run, dict):
                    add(run.get("nightly"))
    return paths


def load_replay_nightly(path: str | Path | dict = REPLAY_FILE, seasons=None) -> dict:
    """Nightly ``ret``, ``staked`` and ``bets`` per season from a replay file (or an already
    parsed replay dict).

    Returns {"ret": {season: [..]}, "staked": {season: [..]}, "bets": {season: [..]},
    "seeds": {season: n_paths}, "seasons": [..], "nights": pooled night count}. Every path
    found for a season is pooled: its ``nightly`` list, any extra seed stored inside the
    season entry (see ``_season_paths``) and any further top-level entry whose key starts
    with the same four-digit season (``"2025_seed2"``, ``"2025b"``); the per-season path
    count is reported in ``seeds`` so the ``basis`` text can say "both seeds"."""
    raw = path if isinstance(path, dict) else json.loads(Path(path).read_text())
    by_season: dict[str, list] = {}
    for key, v in raw.items():
        m = re.match(r"^(\d{4})", str(key))
        if m and isinstance(v, dict):
            by_season.setdefault(m.group(1), []).append(v)
    want = [str(s) for s in (seasons or sorted(by_season.keys()))]
    rets, staked, nbets, seeds = {}, {}, {}, {}
    for s in want:
        paths = [p for v in by_season.get(s, []) for p in _season_paths(v)]
        if not paths:
            continue
        r, st, nb = [], [], []
        for nightly in paths:
            for night in nightly:
                r.append(float(night.get("ret") or 0.0))
                st.append(float(night.get("staked") or 0.0))
                nb.append(int(night.get("bets") or 0))
        rets[s], staked[s], nbets[s], seeds[s] = r, st, nb, len(paths)
    return {"ret": rets, "staked": staked, "bets": nbets, "seeds": seeds, "seasons": sorted(rets.keys()),
            "nights": int(sum(len(x) for x in rets.values()))}


def historical_band_roi(nightly_rows, lo: float, hi: float, key: str = "score", min_n: int = 10) -> dict | None:
    """Realised return per dollar staked on past nights whose stored per-night ``key`` (a
    night score the replay may record once it emits one) lies in ``[lo, hi)``:
    {"roi", "n", "se", "lo", "hi"}. None when fewer than ``min_n`` staked nights carry the
    key. Feeds the PASS headline's "historically nights like this returned about x per
    dollar (not distinguishable from zero)" clause; it is never a claim that passing raises
    growth."""
    roi = []
    for night in nightly_rows or []:
        sc, st = night.get(key), float(night.get("staked") or 0.0)
        if sc is None or st <= 0:
            continue
        if lo <= float(sc) < hi:
            roi.append(float(night.get("ret") or 0.0) / st)
    if len(roi) < min_n:
        return None
    a = np.asarray(roi)
    return {"roi": float(a.mean()), "n": int(a.size), "se": float(a.std(ddof=1) / np.sqrt(a.size)),
            "lo": float(lo), "hi": float(hi)}


def _pool(by_season: dict) -> np.ndarray:
    parts = [np.asarray(v, dtype=float) for _, v in sorted(by_season.items())]
    return np.concatenate(parts) if parts else np.zeros(0)


# ----------------------------------------------------------------------------- engine
def _simulate(rets: np.ndarray, staked: np.ndarray | None, idx: np.ndarray, bankroll: float, unit: float,
              k: float, replay_k: float = REPLAY_K, night_cap: float = NIGHT_CAP, nbets: np.ndarray | None = None,
              per_bet_cap: float = PER_BET_CAP) -> dict:
    """Vectorised over paths: ``idx`` is a (nights, paths) array of pooled-night indices."""
    nights, paths = idx.shape
    scale = k / replay_k
    bank = np.full(paths, float(bankroll))
    peak = bank.copy()
    low = bank.copy()
    max_dd = np.zeros(paths)
    skipped = 0.0
    bet_nights = 0.0
    r_all = rets[idx]
    if staked is not None:
        st_all = staked[idx]
        with np.errstate(divide="ignore", invalid="ignore"):
            roi_all = np.where(st_all > 0, r_all / np.where(st_all > 0, st_all, 1.0), 0.0)
        nb_all = np.maximum(nbets[idx], 1) if nbets is not None else np.ones_like(idx)
    for t in range(nights):
        if staked is None:
            # continuous fallback (no stake data): the night's return scales linearly, loss capped at 100%
            bank = bank * (1.0 + np.maximum(r_all[t] * scale, -1.0))
        else:
            n_b = nb_all[t]
            frac_each = np.minimum(st_all[t] * scale, night_cap) / n_b
            each = np.floor(frac_each * bank / unit + 0.5) * unit        # round each bet to whole units (half up)
            each = np.minimum(each, np.floor(per_bet_cap * bank / unit) * unit)
            stake = np.minimum(n_b * each, np.floor(night_cap * bank / unit) * unit)
            can = (each >= unit) & (stake <= bank)
            stake = np.where(can, stake, 0.0)
            active = st_all[t] > 0
            bet_nights += float(active.sum())
            skipped += float((active & ~can).sum())
            bank = bank + stake * roi_all[t]
        peak = np.maximum(peak, bank)
        low = np.minimum(low, bank)
        max_dd = np.maximum(max_dd, 1.0 - bank / peak)
    return {
        "bankroll": float(bankroll), "unit": float(unit), "k": float(k),
        "median": float(np.median(bank)), "p10": float(np.percentile(bank, 10)),
        "p90": float(np.percentile(bank, 90)),
        "p_bust": float(np.mean(low < unit)), "p_dd50": float(np.mean(max_dd >= 0.5)),
        "p_loss": float(np.mean(bank < bankroll)),
        "skip_share": float(skipped / bet_nights) if bet_nights else 0.0,
    }


def _inputs(nightly_returns_by_season, nightly_staked_by_season, nightly_bets_by_season):
    rets = _pool(nightly_returns_by_season)
    staked = _pool(nightly_staked_by_season) if nightly_staked_by_season is not None else None
    if staked is not None and staked.shape != rets.shape:
        raise ValueError("staked and returns differ in length")
    nbets = None
    if nightly_bets_by_season is not None and staked is not None:
        nbets = _pool(nightly_bets_by_season).astype(int)
        if nbets.shape != rets.shape:
            raise ValueError("bets and returns differ in length")
    return rets, staked, nbets


def bootstrap(nightly_returns_by_season: dict, bankroll: float, unit: float, k: float, nights: int = 160,
              paths: int = 2000, seed: int = 1, nightly_staked_by_season: dict | None = None,
              nightly_bets_by_season: dict | None = None, replay_k: float = REPLAY_K,
              night_cap: float = NIGHT_CAP, per_bet_cap: float = PER_BET_CAP) -> dict:
    """Resample ``nights`` nights (with replacement, pooled across seasons) ``paths`` times
    and replay them at bankroll/unit/k with integer-unit stakes. Deterministic in ``seed``.

    ``nightly_returns_by_season`` holds the replay's ``ret`` (balanced, k = 0.25) per night;
    other ``k`` scale the stake by k / 0.25. ``nightly_staked_by_season`` (same keys and
    lengths) enables the whole-unit rounding; without it the path is continuous.
    ``nightly_bets_by_season`` (the night's bet count) splits the stake per bet before
    rounding; without it the night is treated as one bet."""
    rets, staked, nbets = _inputs(nightly_returns_by_season, nightly_staked_by_season, nightly_bets_by_season)
    if rets.size == 0:
        raise ValueError("no nightly returns")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, rets.size, size=(int(nights), int(paths)))
    out = _simulate(rets, staked, idx, bankroll, unit, k, replay_k, night_cap, nbets, per_bet_cap)
    out.update({"nights": int(nights), "paths": int(paths), "seed": int(seed), "pooled_nights": int(rets.size),
                "integer_units": staked is not None, "per_bet_split": nbets is not None,
                "basis": (f"{int(paths)} paths of {int(nights)} nights resampled with replacement from {int(rets.size)} "
                          f"pooled replay nights (balanced profile, k={replay_k:g}); stakes scaled by k/{replay_k:g} "
                          f"= {k / replay_k:.2f} for k={k:g}, capped at {per_bet_cap:.0%} per bet and "
                          f"{night_cap:.0%} per night; "
                          + ("whole-unit stakes, night skipped when a bet rounds below one unit"
                             if staked is not None else "continuous stakes (no stake data)"))})
    return out


def grid(nightly_returns_by_season: dict, nightly_staked_by_season: dict | None = None, nights: int = 160,
         paths: int = 2000, seed: int = 1, bankrolls=BANKROLLS, units=UNITS, ks=KS,
         nightly_bets_by_season: dict | None = None, replay_k: float = REPLAY_K,
         night_cap: float = NIGHT_CAP, per_bet_cap: float = PER_BET_CAP) -> list[dict]:
    """The contract's ``growth.grid``: one row per bankroll x unit x k, floats at 4 dp. The
    same resampled nights (one draw per ``seed``) are used for every cell so cells compare
    like for like."""
    rets, staked, nbets = _inputs(nightly_returns_by_season, nightly_staked_by_season, nightly_bets_by_season)
    if rets.size == 0:
        return []
    idx = np.random.default_rng(seed).integers(0, rets.size, size=(int(nights), int(paths)))
    rows = []
    for b in bankrolls:
        for u in units:
            for k in ks:
                r = _simulate(rets, staked, idx, b, u, k, replay_k, night_cap, nbets, per_bet_cap)
                rows.append({key: (round(v, 4) if isinstance(v, float) else v) for key, v in r.items()})
    return rows


def growth_block(path: str | Path | dict = REPLAY_FILE, seasons=None, nights: int = 160, paths: int = 2000,
                 seed: int = 1, **kw) -> dict:
    """The contract's "growth" block: {"basis", "nights", "grid"} built from a replay file
    (or a parsed replay dict). Returns an empty grid (and says why in ``basis``) when the
    file is missing or holds no nightly returns."""
    if isinstance(path, dict):
        p, data = Path("replay_open.json"), load_replay_nightly(path, seasons)
    else:
        p = Path(path)
        if not p.exists():
            return {"basis": f"no replay file at {p.name}; growth grid unavailable", "nights": int(nights), "grid": []}
        data = load_replay_nightly(p, seasons)
    if not data["seasons"]:
        return {"basis": f"{p.name} holds no nightly returns", "nights": int(nights), "grid": []}
    per_season = sorted(set(data["seeds"].values()))
    if per_season == [1]:
        seeds_txt = "one seed path per season; a second seed is pooled where the file stores one"
    elif all(v >= 2 for v in per_season):
        seeds_txt = "both seeds"
    else:
        seeds_txt = "seeds per season " + ", ".join(f"{s}: {n}" for s, n in sorted(data["seeds"].items()))
    try:
        rel = p.resolve().relative_to(DATA_DIR.parent) if p.exists() else Path("data") / p.name
    except ValueError:
        rel = p
    basis = (f"bootstrap of nightly returns from {rel.as_posix()} seasons {data['seasons'][0]}-{data['seasons'][-1]} "
             f"({data['nights']} nights, {seeds_txt}); returns are the balanced profile at k=0.25 scaled by k/0.25 for "
             f"other k; each night's stake is split across its bets, rounded to whole units, capped at "
             f"{PER_BET_CAP:.0%} per bet and {NIGHT_CAP:.0%} per night, and the night is skipped when a bet rounds "
             f"below one unit; {paths} paths of {nights} nights, seed {seed}")
    return {"basis": basis, "nights": int(nights),
            "grid": grid(data["ret"], data["staked"], nights=nights, paths=paths, seed=seed,
                         nightly_bets_by_season=data["bets"], **kw)}
