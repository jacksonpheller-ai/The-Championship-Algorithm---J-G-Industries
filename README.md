# J&G Hedging Co. — NBA Desk

A quantitative NBA betting engine and dashboard. Every day it prices the slate (moneylines, spreads, totals and player props), builds cross-game parlays, and splits whatever bankroll you enter across the bets with positive expected value using a Kelly optimiser.

**Open the dashboard:** `docs/index.html` (GitHub Pages) or the single-file copy `dist/jg-hedging-co.html`. That file works offline and can be sent to anyone.

## What it does each night

1. **Data.** ESPN supplies the schedule, every box score of the last three seasons, the injury report, DraftKings lines (open and current) and DraftKings player props. Basketball-Reference supplies BPM/OBPM/DBPM, individual offensive and defensive ratings, PER, TS%, USG%, WS/48, VORP, SRS and the four factors. NBA.com blocks cloud servers, so it is not used.
2. **Team ratings.** Offensive and defensive rating per 100 possessions plus pace, from a recency-weighted ridge regression over every game this season, adjusted for opponent and home court. Ratings start from a prior built from last season and the BPM of the players each team is actually using.
3. **Player value.** OBPM/DBPM blended with individual ORtg (usage-weighted) and individual DRtg, each mapped onto the BPM scale by a league-wide regression.
4. **Adjustments.** Injury statuses become play probabilities, and the expected change in minutes times each player's BPM above replacement moves the team's ratings. Other inputs: rest, back-to-backs, 3-in-4 nights, travel miles, time-zone shifts, altitude, and an independent margin-of-victory Elo.
5. **Market blend.** The model is blended with the market line. The weights were fit out of sample: about 25–30% model against a morning line, falling to about 5–8% near tip.
6. **Simulation.** Each game is played 20,000 times, with errors drawn from the empirical distribution of past misses. Player stat lines are simulated inside each game (minutes, usage from absent teammates, opponent allowance, pace, blowout minutes; negative binomial counts), so props and same-game bets stay correlated.
7. **Pricing.** Every bet gets a win probability, a no-vig market probability and an expected value. Bets qualify with EV ≥ 1.5%, an edge under 12 points (bigger usually means a stale line or late news) and, for props, agreement between the raw projection and the calibrated probability.
8. **Sizing.** A simultaneous Kelly optimiser runs over all qualifying singles and the best cross-game parlays using the joint simulated outcomes. The result is then scaled by risk profile: Conservative 0.15×, Balanced ¼×, Aggressive ½× Kelly, with per-bet and per-night caps.
9. **Ledger.** The balanced-profile ticket is recorded before tip and graded from final box scores. The live record is on the dashboard.

**Tonight's calls:**
- **The Lock** is the highest-probability bet that still has positive EV. The highest-probability bet overall is usually a heavy favourite at a price that loses money long-term.
- The other calls are **Best value**, **Best game bet**, **Best prop**, **Best parlay** (highest EV with at least a 20% hit chance) and **Safest parlay**.

## How good is it? (walk-forward backtest, no future data)

| Test | Result |
|---|---|
| Final-margin error, model alone vs closing line | 14.01 vs 13.73 pts (2024-25), 14.33 vs 14.06 (2025-26). The closing line is sharper. |
| Model vs **opening** line (no injury look-ahead), bet when ≥3 pts apart | 2024-25: 57.9% ATS, +10.6% ROI at -110, line moved our way 59% of the time. 2025-26: 51.8% ATS, -1.1% ROI. 2026-27 to date: 59.3% ATS on 59 bets. |
| **Full nightly pipeline replayed on past nights** (morning lines, no injury report, balanced profile, $1,000 start) | 2024-25: $2,351 (+11.7% ROI on staked, worst drawdown 30%). 2025-26: $730 (-2.6%, worst drawdown 44%). 2026-27 to date: $1,028 (+2.7%). Totals were profitable in both full seasons; spreads were roughly flat. The model's settings were fit on these seasons, so the replay is optimistic. |
| Moneylines at the close | No bet clears 2% EV once anchored to the moneyline market. |
| Player props, pre-game DraftKings lines (1,754 scored) | Overs hit 44.9% vs 49.0% implied. The raw projection loses to the market (log loss 0.710 vs 0.684). The calibrated blend gained +5.2% ROI on 97 held-out bets, a small sample. |

What this means in practice:
- Closing lines can't be beaten with public data.
- The repeatable edges are betting **early** before lines absorb the ratings, **injury news**, and the props market's **lean toward overs**.
- Expect small edges and real variance.

Full reports: `data/backtest_report.json`, `data/prop_backtest.json`, `data/replay_open.json`. Rerun the replay with `python -m jghedge replay`.

One data finding: ESPN's stored "closing" prop prices for past games were mostly updated *during* the game (77% after tip), and the historical milestone ladders were corrupt. The prop backtest therefore uses only lines last updated before tip, and milestone ("N+") props are excluded from recommendations.

## Sharing with a friend

The repository is public, so any of these work:

- **Permanent link (recommended).** Settings → Pages → *Deploy from a branch* → `claude/wonderful-bell-w1m2r1`, folder `/docs` → Save. The site appears at `https://jacksonpheller-ai.github.io/The-Championship-Algorithm---J-G-Industries/`. The scheduled workflow refreshes it several times a day.
- **No-setup link.** `https://raw.githack.com/jacksonpheller-ai/The-Championship-Algorithm---J-G-Industries/claude/wonderful-bell-w1m2r1/docs/index.html` renders the latest committed page through a third-party service. This hasn't been tested from this environment.
- **A file.** Send `dist/jg-hedging-co.html`. It opens in any browser with that day's data built in.

## Running it

```bash
pip install -r requirements.txt
```

```bash
python -m jghedge run                 # price tonight's slate, update ledger, rebuild the site
```

```bash
python -m jghedge run --date 2026-11-08 --sims 30000
```

```bash
python -m jghedge backtest            # refit all parameters (≈5 min)
```

```bash
python -m jghedge grade               # grade last night's bets
```

```bash
python tests/test_core.py             # math tests
```

The workflow in `.github/workflows/nightly.yml` runs on GitHub's schedule:
- about 10:40 am ET (morning lines)
- about 5:15 pm ET (injury refresh)
- about 4:20 am ET (grading)
- a weekly refit on Mondays

Scheduled workflows only run on the repository's default branch.

## Layout

```
jghedge/
  data/        espn.py, bbref.py, history.py, http.py (cached, rate-limited)
  model/       ratings.py (ridge efficiency), elo.py, players.py (BPM, availability),
               situational.py, engine.py, simulate.py, props.py, odds.py
  portfolio/   kelly.py (simultaneous Kelly), parlay.py
  backtest.py  props_backtest.py  pipeline.py  ledger.py  site/ (dashboard)
data/          history (3 seasons, gzipped), BBRef snapshots, fitted params, ledger, reports
docs/          GitHub Pages site + data/latest.json
dist/          single-file dashboard
```

## House rules

- Bet the number shown or better. If the line has moved past it, rerun or skip.
- Lines come from one book (DraftKings via ESPN). Shopping other books is free edge.
- Only bet what you can afford to lose. Betting is legal only where licensed and for adults of legal age. Problem gambling help: 1-800-GAMBLER.
- This is research software, not financial advice.
