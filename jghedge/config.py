"""Paths and model constants.

Values marked "fit" are overwritten by `data/model_params.json`, which the
backtest (`python -m jghedge backtest`) writes after fitting on past seasons.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = Path(os.environ.get("JG_CACHE_DIR", ROOT / ".cache"))
DATA_DIR = ROOT / "data"            # committed: compact history, params, ledger
SITE_DIR = ROOT / "docs"            # GitHub Pages root
SITE_DATA = SITE_DIR / "data"
PARAMS_FILE = DATA_DIR / "model_params.json"

# ESPN "season" = year the season ends (2026-27 -> 2027). Same as Basketball-Reference.
CURRENT_SEASON = int(os.environ.get("JG_SEASON", "2027"))

DEFAULT_PARAMS: dict = {
    # --- efficiency ratings ---------------------------------------------
    "rating_half_life_days": 45.0,     # recency weighting of game results
    "rating_ridge": 6.0,               # shrinkage (in games) toward the talent prior
    "prior_season_carry": 0.55,        # weight on last season's player value in priors
    "hca_points": 2.3,                 # home-court advantage, points (fit)
    "altitude_points": 0.9,            # extra for DEN/UTA/Mexico City home teams (fit)
    # --- situational (points of margin) ------------------------------------
    "b2b_points": 1.6,                 # penalty for 2nd night of back-to-back (fit)
    "rest_advantage_points": 0.35,     # per extra rest day, capped at 3 (fit)
    "travel_points_per_1000mi": 0.45,  # (fit)
    "tz_points_per_hour": 0.25,        # eastward/westward body-clock shift (fit)
    "three_in_four_points": 0.5,       # 3 games in 4 nights (fit)
    "total_b2b_points": -0.8,          # total points change per team on a back-to-back
    # --- player value -------------------------------------------------------
    "replacement_bpm": -2.0,
    "bpm_prior_minutes": 600.0,        # minutes of shrinkage toward prior BPM
    "injury_scale": 0.85,              # share of BPM*min impact that hits team rating
    # --- Elo -------------------------------------------------------------
    "elo_k": 20.0,
    "elo_hca": 70.0,
    "elo_season_revert": 0.25,
    "elo_points_per_100": 3.6,         # Elo points per point of margin (100/28)
    # --- blending -----------------------------------------------------------
    "w_ratings": 0.70,                 # ratings vs Elo inside our model (fit)
    "w_model_vs_market_margin": 0.30,  # weight of our model vs closing spread (fit)
    "w_model_vs_market_total": 0.30,   # (fit)
    "margin_sd": 12.2,                 # (fit)
    "total_sd": 18.5,                  # (fit)
    "margin_total_corr": 0.0,
    # --- live line timing (fit on opening lines, model without injury look-ahead) ---
    "w_margin_early": 0.0,             # spreads/ML: 0 = information only, never bet (fit)
    "w_total_early": 0.30,
    # --- props -------------------------------------------------------------
    "w_model_vs_market_props": 0.15,
    "prop_stack": [0.15, 0.85, -0.10],  # logit p = a*logit(model) + c*logit(market) + b (fit)
    "prop_bias": {},                   # per-stat multiplicative projection correction (fit)
    "margin_resid_q": None,            # empirical residual quantiles (fit)
    "total_resid_q": None,
    "prop_minutes_cv": 0.14,
    # --- portfolio -------------------------------------------------------
    "min_ev": 0.03,                    # game totals / parlay legs: min EV per $ (fit: 3% beat 1.5% and 4.5%)
    "prop_min_ev": 0.02,               # props: held-out ROI is positive only in the 2-6% EV band
    "prop_max_ev": 0.06,               # larger prop edges were stale lines / overfit tails out of sample
    "max_edge_prob": 0.12,             # edges above this are treated as stale data
    "n_sims": 20000,
}


# --- growth-policy defaults (research memo s.4-5). The modules own the live values
# (jghedge.portfolio.policy, jghedge.model.context, jghedge.pickem); these are the documented
# defaults so a reader of this file sees every knob in one place. ------------------------
CONTEXT_PARAMS: dict = {            # late-season total adjustment, applied AFTER the market blend
    "late_gp": 70, "late_adj": 1.8, "very_late_gp": 79, "very_late_adj": 2.5,
    "tank_spread": 8.0, "tank_winpct": 0.35, "regular_season_type": 2,
    "kill_switch_window": 100, "kill_switch_floor": 0.50,
}
SHRINK: dict = {"total": 0.5, "parlay": 0.5, "prop": 0.0, "pickem": 0.5}   # realised edge ~ s x claimed
TIERS: tuple = (                    # Kelly multiple by bankroll in units (max_units exclusive)
    {"max_units": 25, "k": 0.5, "label": "tiny"},
    {"max_units": 100, "k": 0.35, "label": "small"},
    {"max_units": None, "k": 0.25, "label": "standard"},
)
PER_BET_CAP, NIGHT_CAP, OPT_IN_K = 0.05, 0.10, 0.5
SCORE_THRESHOLDS: dict = {"light": 0.0005, "bet": 0.0015}
REF_BANKROLL, REF_UNIT = 100.0, 0.5   # the ledger's reference ticket and the night score basis
# PrizePicks payout table and minimum entry: ASSUMPTIONS (the site could not be reached from the
# build sandbox and web summaries disagreed); override via data/model_params.json "pickem".
PICKEM_PAYOUTS: dict = {"power": {2: 3.0, 3: 5.0, 4: 10.0},
                        "flex": {3: [2.25, 1.25], 4: [5.0, 1.5], 5: [10.0, 2.0, 0.4], 6: [25.0, 2.0, 0.4]}}
PICKEM_MIN_ENTRY = 5.0
FLIFF_MIN_BET = 0.25                # ASSUMPTION: Fliff Cash minimum wager, unverified
PROPS_REGIME: dict = {              # rolling under-lean regime flag (memo B6); assumptions, not fits
    "window_nights": 30, "min_lines": 300, "lean_under_at": 0.035, "stand_down_at": -0.02,
}
RUN_LABEL_HOURS_ET: dict = {"opener": 6, "morning": 14, "pretip": 17, "evening": 20}   # ET-hour label fallback


def load_params() -> dict:
    params = dict(DEFAULT_PARAMS)
    if PARAMS_FILE.exists():
        try:
            fitted = json.loads(PARAMS_FILE.read_text())
            params.update({k: v for k, v in fitted.get("params", {}).items() if k in params})
        except (OSError, ValueError):
            pass
    return params
