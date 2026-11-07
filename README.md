# J&G Hedging Co. — NBA Desk

A quantitative NBA betting engine and dashboard for two people and a small bankroll. Every day it prices the slate (moneylines, spreads, totals, player props), builds cross-game parlays, and turns whatever bankroll you type in into a ticket of whole-unit stakes. It records the bets you say you placed and grades them from the box scores.

**Open it:** `https://jacksonpheller-ai.github.io/The-Championship-Algorithm---J-G-Industries/` once GitHub Pages is switched on (Settings → Pages → *Deploy from a branch* → `claude/wonderful-bell-w1m2r1`, folder `/docs`). Until then, `dist/jg-hedging-co.html` is a single file that opens in any browser with that day's data built in.

## Is it updated every day?

Yes, by GitHub Actions on this repository (`.github/workflows/nightly.yml`), confirmed working on 2026-11-07 (run #1 priced the slate and pushed the results in 38 seconds):

| Time (Eastern) | What runs |
|---|---|
| ~10:40 am | prices the day's slate against the morning lines |
| ~5:15 pm | re-prices with the afternoon injury report |
| ~4:20 am | grades last night, publishes results files, ingests the shared record |
| Mondays ~6:00 am | refits every model parameter and refreshes the replay |

GitHub runs scheduled jobs only on the default branch (this branch is the default) and pauses them after 60 days without a commit; the bot's own commits keep it alive. If a run ever fails, the Actions tab shows why, and `workflow_dispatch` lets you run any task by hand.

## Using it

1. **Bankroll and smallest bet.** Type the bankroll ($10 works) and the smallest stake your book allows ($0.50 by default).
2. **Risk profile.** Conservative, Balanced, Aggressive or Max growth. A profile is a fraction of the Kelly stake. With $10 and $0.50 units each unit is 5% of the bankroll, so Balanced rarely bets; choose $0.10 units if your book allows them. Max growth is full Kelly: fastest compounding and the most likely to lose most of the money.
3. **Ticket.** The optimiser fills in whole-unit stakes across singles and cross-game parlays at once. Untick a bet or type your own stake and the rest re-balances. Qualifying props can be added from the Props tab.
4. **Parlays.** Three cards: the safest, the best expected value, and a longshot. All legs come from different games.
5. **After you bet,** press *I placed these bets*. They are stored in your browser and graded automatically when the results files are published. *Log to shared record* opens a pre-filled GitHub issue; the nightly job grades it and closes it with the result, so both of us see one shared record.

## What it does each night

1. **Data.** ESPN: schedule, every box score of the last four seasons, injury report, DraftKings lines (open and current) and props. Basketball-Reference: BPM/OBPM/DBPM, individual offensive and defensive ratings, PER, TS%, USG%, WS/48, VORP, SRS, four factors. NBA.com blocks cloud servers.
2. **Team ratings.** Offensive and defensive rating per 100 possessions plus pace from a recency-weighted ridge regression over the season, adjusted for opponent and venue, starting from a prior built from last season and the value of the players each team is actually using.
3. **Player value.** OBPM/DBPM blended with individual ORtg (usage-weighted) and DRtg, each mapped onto the BPM scale.
4. **Adjustments.** Injury statuses become play probabilities; the expected change in minutes times value above replacement moves team ratings. Rest, back-to-backs, 3-in-4, travel, time zones, altitude; an independent margin-of-victory Elo.
5. **Market blend.** 10% model on spreads and 30% on totals against a morning line, less near tip. Fit out of sample; closing lines are sharper than the model.
6. **Simulation.** 20,000 draws per game from the empirical error distribution. Player stat lines simulated inside each game (minutes, usage from absent teammates, opponent allowance, pace, blowouts; negative binomial counts).
7. **Pricing.** Win probability, no-vig market probability and EV for every bet. Qualify at EV ≥ 1.5%, edge < 12 points, raw and calibrated prop signals agreeing.
8. **Parlays.** Legs from different games only, so the hit probability is the exact product of leg probabilities. (Reading it off the simulation flattered lucky combos; the fix changed one season's replay from +$97 to −$78.)
9. **Sizing.** Simultaneous Kelly over all candidates on the joint simulation, then whole-unit greedy allocation at the chosen profile.
10. **Record.** Desk ticket recorded before tip and graded nightly; your placed bets graded in the page; shared record via issues.

## Evidence (walk-forward, no future information)

Game model vs the closing line, final-margin RMSE: 2024-25 14.01 vs 13.73, 2025-26 14.33 vs 14.06 (the market wins).

Full nightly pipeline replayed at the morning lines, props excluded, balanced profile from $1,000, closed-form parlays, averaged over two random seeds (see `data/replay_open.json` for the published run):

| Season | Nights | Bets | ROI on money staked | $1,000 became | Worst drawdown | $10 at ¼ Kelly, $0.50 units | $10 full Kelly, $0.50 | $10 full Kelly, $0.10 |
|---|---|---|---|---|---|---|---|---|
| 2023-24 | 162 | 322 | +5.0% | $1,157 | 27% | $9.50 | $3.24 | $10.06 |
| 2024-25 | 167 | 227 | +16.8% | $1,518 | 17% | $13.65 | $44.31 | $43.14 |
| 2025-26 | 168 | 272 | +0.0% | $958 | 41% | $9.23 | $3.09 | $4.45 |
| 2026-27 (to date) | 17 | 60 | +3.8% | $1,018 | 7% | $9.95 | $8.18 | $9.73 |

Player props: on 1,754 pre-tip DraftKings lines this season, overs hit 44.9% against a 49.0% implied rate; the calibrated blend returned +5.2% on 97 held-out bets. Small sample.

What this means: the repeatable edges are game totals and parlays of the strongest totals against morning lines, and the props market's lean to overs. Spreads and moneylines lost in two of three replayed seasons and are no longer bet. Edges are small, and single seasons swing by hundreds of dollars on a $1,000 bankroll from luck alone (re-running a season with different random draws moves the result by $50–150). A $10 bankroll compounds the same percentages with far lumpier stakes: in the replays, $10 at Balanced with $0.10 units ended most seasons between $9 and $16, and full Kelly with $0.50 units anywhere from $3 to $44.

Reports: `data/backtest_report.json`, `data/prop_backtest.json`, `data/replay_open.json`.

## Running it yourself

```bash
pip install -r requirements.txt
```

```bash
python -m jghedge run
```

```bash
python -m jghedge backtest
```

```bash
python -m jghedge replay --seasons 2025,2026,2027
```

```bash
python tests/test_core.py
```

## Layout

```
jghedge/
  data/        espn.py, bbref.py, history.py, http.py
  model/       ratings.py, elo.py, players.py, situational.py, engine.py, simulate.py, props.py, odds.py
  portfolio/   kelly.py, discrete.py (whole-unit sizing), parlay.py
  backtest.py  props_backtest.py  replay.py  pipeline.py  ledger.py  results.py  shared.py  site/
data/          4 seasons of games (gzipped), BBRef snapshots, fitted params, ledgers, reports
docs/          GitHub Pages site, data/latest.json, data/results/<date>.json
dist/          single-file dashboard
```

## House rules

- Bet the number shown or better; if the line moved past it, skip.
- One book's lines (DraftKings via ESPN). Shopping other books is free edge.
- Only bet money you can afford to lose. Legal only where licensed and for adults of legal age. Help: 1-800-GAMBLER.
- Research software, not financial advice.
