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
    "min_ev": 0.015,                   # min expected value per $ to consider a bet
    "prop_min_ev": 0.02,               # props: held-out ROI is positive only in the 2-6% EV band
    "prop_max_ev": 0.06,               # larger prop edges were stale lines / overfit tails out of sample
    "max_edge_prob": 0.12,             # edges above this are treated as stale data
    "n_sims": 20000,
}


def load_params() -> dict:
    params = dict(DEFAULT_PARAMS)
    if PARAMS_FILE.exists():
        try:
            fitted = json.loads(PARAMS_FILE.read_text())
            params.update({k: v for k, v in fitted.get("params", {}).items() if k in params})
        except (OSError, ValueError):
            pass
    return params
