"""Parlay construction from simulated joint outcomes.

A parlay's payoff per draw is  prod(leg multipliers) - 1  where a leg multiplier is its
decimal price on a win, 1.0 on a push/void and 0 on a loss — exactly how books settle.
Only legs from *different* games are combined for auto-allocation: books price same-game
parlays with their own correlation haircut, so for those we only publish fair odds.
"""
from __future__ import annotations

from itertools import combinations

import numpy as np


def leg_multiplier(payoff: np.ndarray, dec: float) -> np.ndarray:
    return np.where(payoff > 0, dec, np.where(payoff == 0, 1.0, 0.0))


def build_parlays(legs: list[dict], payoffs: dict[str, np.ndarray], max_legs: int = 4,
                  pool: int = 14, min_leg_prob: float = 0.45) -> list[dict]:
    cand = [l for l in legs if l["ev"] > 0 and l["p_final"] >= min_leg_prob]
    cand.sort(key=lambda l: l["ev"] * np.sqrt(l["p_final"]), reverse=True)
    cand = cand[:pool]
    out = []
    for k in range(2, max_legs + 1):
        for combo in combinations(cand, k):
            games = {l["game_id"] for l in combo}
            if len(games) < k:
                continue
            mult = np.ones_like(payoffs[combo[0]["id"]], dtype=float)
            dec = 1.0
            for l in combo:
                mult *= leg_multiplier(payoffs[l["id"]], l["dec"])
                dec *= l["dec"]
            pay = mult - 1
            ev = float(pay.mean())
            if ev <= 0:
                continue
            out.append({"legs": [l["id"] for l in combo], "n_legs": k, "dec": dec,
                        "p_hit": float(np.mean(mult > 1.0)), "ev": ev, "payoff": pay})
    return out


def select_parlays(parlays: list[dict], n: int = 6) -> list[dict]:
    """Keep a diverse set: best EV, best hit probability per leg count, avoid near-duplicates."""
    chosen: list[dict] = []

    def ok(p):
        return all(len(set(p["legs"]) & set(c["legs"])) < max(2, p["n_legs"] - 1) for c in chosen)

    by_ev = sorted(parlays, key=lambda p: p["ev"], reverse=True)
    by_hit = sorted(parlays, key=lambda p: (p["p_hit"], p["ev"]), reverse=True)
    for k in (2, 3, 4):
        for src in (by_hit, by_ev):
            for p in src:
                if p["n_legs"] == k and ok(p):
                    chosen.append(p)
                    break
    for p in by_ev:
        if len(chosen) >= n:
            break
        if p not in chosen and ok(p):
            chosen.append(p)
    return chosen[:n]


def same_game_fair(legs: list[dict], payoffs: dict[str, np.ndarray]) -> dict:
    mult = np.ones_like(payoffs[legs[0]["id"]], dtype=float)
    for l in legs:
        mult *= leg_multiplier(payoffs[l["id"]], l["dec"])
    p = float(np.mean(mult > 1.0))
    return {"p_hit": p, "fair_dec": (1 / p) if p > 0 else None}
