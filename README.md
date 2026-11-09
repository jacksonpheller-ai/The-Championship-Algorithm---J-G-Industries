# J&G Hedging Co. — NBA Desk

A quantitative NBA betting engine and dashboard for two people and a small bankroll. Every day it prices the slate (moneylines, spreads, totals, player props), builds cross-game parlays, and splits whatever you want to bet tonight across the bets that have an edge. It records the bets you say you placed and grades them from the box scores.

**Open it:** `https://jacksonpheller-ai.github.io/The-Championship-Algorithm---J-G-Industries/` once GitHub Pages is switched on (Settings → Pages → *Deploy from a branch* → `main`, folder `/docs`). `docs/index.html` is itself a single self-contained file (that run's data is built in), so it also opens straight from disk; `python -m jghedge site` writes a copy to `dist/` locally (not committed).

## Is it updated every day?

Yes, by GitHub Actions on this repository (`.github/workflows/nightly.yml`). Every pricing run records the lines it saw in `data/lines/<date>.json` under its label, so the desk can measure closing-line value and tell you when a number has moved past us.

| Schedule (UTC) | What runs |
|---|---|
| every two hours at :17 | prices the slate for the current Eastern time and grades any finished night. The snapshot label comes from the clock when the run starts: `opener` (8 pm to 6 am ET; from 6 pm it prices the **next** day's slate at the opening lines, where the totals edge lives: +1.75 pts of CLV at a 3-pt signal), `morning` (to 2 pm), `pretip` (to 5 pm), `afternoon` (to 8 pm) |
| Mondays 11:07 | refits every model parameter and refreshes the two-seed replay |

**GitHub starts scheduled jobs late.** In the repository's first days every scheduled run started 3 to 10 hours after its time (the 9:31 pm ET opener landed around 5 am, the 10:41 am run around 3 pm). The old schedule chose the task and label from *which* cron line fired, so late runs were mislabelled and the page could show lines many hours old. Now the run decides what to do from the clock, runs are scheduled every two hours so one lands in each part of the day, the page states how old its lines are (and warns past four hours), and games that have tipped drop off the page by the reader's clock. For on-time runs, trigger the workflow by hand (`workflow_dispatch`) or from an external scheduler.

GitHub runs scheduled jobs only on the repository's default branch and pauses them after 60 days without a commit; the bot's own commits keep it alive. Every run also mirrors its commit to `main`, so `main` stays current whichever branch is the default (set `main` as the default in Settings → General to run everything from it). If a run ever fails, the Actions tab shows why, and `workflow_dispatch` lets you run any task (and label) by hand. Set the repository secret `ODDS_API_KEY` to add multi-book prices (Fliff's own numbers, PrizePicks lines via the `us_dfs` region); without it the desk prices from ESPN's DraftKings feed.

## The platform split

The desk tells two different apps what to do:

- **Fliff — game bets.** Totals and cross-game parlays of totals, the only game markets with a measured edge. Spreads and moneylines are priced and shown but marked `info_only` (they lost in two of three replayed seasons). Each qualifying total comes with an instruction like *"Over 228.5 at -110 or better; skip above 229.5"*: the `max_line` is the worst number at which the shrunk edge still clears the 1.5% floor. Minimum wager assumed $0.25 (unverified).
- **PrizePicks — player pick'em.** PrizePicks pays fixed multiples, so a leg is worth playing only when its true hit rate clears the break-even (57.7% for a 2-pick Power, 55.0% for 3-pick, 54.3% per leg on a 5-pick Flex). Each player's simulated stat line is **anchored to DraftKings' line and price** (the sharp number) and then priced at PrizePicks' line. When the two lines differ, that gap is the edge. Validation: on 2,866 DraftKings prop lines that moved between open and close, a distribution anchored at the closing (sharp) line and priced at the opening (stale) line predicted 60.7% for the favoured side and realised 59.0%; in the 60-65% band it predicted 62% and realised 66% (n=589). PrizePicks blocks cloud servers (HTTP 403), so the nightly job usually cannot read the board. In that case the page lists near-even DraftKings legs with **take-it thresholds** ("More if the line is 24.5 or lower") and marks every suggested slip *check lines*: open the app, type PrizePicks' line into the table, and the leg is re-priced in the browser. Pick'em is provisional until the live ledger has graded enough slips, and at a $10 bankroll the $5 minimum entry is half the bank, which the page flags.
  - **Under lean.** Sportsbook overs hit less often than their no-vig price. On 8,009 pre-game DraftKings lines (133 games, Jan-Nov 2026) overs hit 45.4% against 48.8% priced: 3.4 points, 2.8 in 2025-26 and 4.9 in 2026-27 to date, the same sign in every month and every stat. In the band picks come from (priced side 57-62%), More sides hit 56.2% against 59.1% priced (n=521) and Less sides 61.7% against 59.2% (n=724). Fitted on either season and tested on the other, shifting the price by the lean lowered log loss (0.6836 → 0.6796; 0.6780 → 0.6774). So the anchor is the DraftKings price **minus the lean** on the over (`config.PICKEM_LEAN`: prior 3.0 points counted as 4,000 lines, blended by line count with the rolling live sample in `props_regime`, clipped to 0-5 points). A More pick priced 59% is treated as about 56%, a Less pick as about 62%; the half-edge shrink still applies on top. Caveats: one book's lines, 133 games, and whether PrizePicks offers the Less side at the sportsbook's line with the standard payout has to be checked in the app.

## The nightly verdict

The night is scored on a reference ticket of **$100 at $0.50 units, quarter Kelly on shrunk edge**, so verdicts compare across users: `score` = shrunk expected profit of that ticket as a fraction of bankroll, after the per-bet growth gate.

| Verdict | Rule | What the page does |
|---|---|---|
| **PASS** | score < 0.0005, or no bet survives the growth gate | Says so, names the best available bet and what line or news would change it. Passing cuts variance; it does not raise expected growth. |
| **LIGHT** | 0.0005 ≤ score < 0.0015, or lineups unconfirmed on most of the ticket | Ticket at the default Kelly fraction only, no opt-in. |
| **BET** | score ≥ 0.0015 | Full ticket; the 0.5-Kelly opt-in is offered at 100+ units. |

Three flags modify it: `props_regime` (rolling 30-night under lean of the logged DraftKings board: `lean_under` / `neutral` / `stand_down`), `lineup_uncertainty` (games with a GTD starter: stakes there are provisional), and `hours_to_first_tip`.

## Sizing: shrinkage and adaptive tiers

- **Shrinkage.** The realised edge on 821 replay bets was about half the claimed edge, so every payoff is shrunk before anything is sized: `s = 0.5` on totals, parlays and pick'em legs, `s = 0` on props and on spreads/moneylines. Shrinkage is applied to the simulation itself (winning draws thinned to losses with common random numbers), so Kelly, the growth gate, the night score and the parlay structure all use one shrunk joint distribution.
- **Growth gate.** A bet is placeable only if one unit has positive expected log growth at your bankroll: `b_min` on each bet is the smallest bankroll at which one unit grows it ($11.36 for a -110 total at 2% shrunk edge with $0.50 units).
- **Tiers by bankroll in units** (bankroll ÷ unit): below 25 units `tiny` (k 0.5, and the page asks for a smaller unit: a $0.50 unit on $10 freezes after one loss), 25-100 `small` (k 0.35, 2-leg parlays only), 100+ `standard` (k 0.25, opt-in 0.5 on BET nights only). Per-bet cap 5%, night cap 10%; bust probability was zero in every bootstrap configuration because of those caps.
- **Growth plan.** `growth.grid` bootstraps 160-night seasons from the replay's nightly returns for 7 bankrolls × 4 units × 3 Kelly multiples (median, p10, p90, P(bust), P(50% drawdown)); it is cached in `data/growth_grid.json` and recomputed when the replay changes.

## Using it

The page has five tabs: **Tonight**, **Games**, **Player props**, **Record** and **How it works** (the method in plain words). There is no bankroll box; the only input is what you want to bet tonight.

1. **Tonight's amount.** Type how much you want to bet tonight (it is remembered on your device). The desk splits it across the bets with an edge and you keep the rest:
   - **PrizePicks first:** if a slip has an edge and the $5 minimum entry is no more than half the amount, the slip gets the minimum entry (only the minimum: PrizePicks edges have no track record yet).
   - **Fliff with the rest:** all of it on a BET night, half on LIGHT, none on PASS, split in the proportions of the desk's reference ticket (quarter Kelly) in steps of Fliff's minimum bet.
2. **Read the plan.** Each bet is a card: amount, the bet, the worst line to take it at, and our chance against the chance the price needs. The PrizePicks card has a line box for every pick: type what the app shows and the slip is re-priced on the spot and marked enter or skip.
3. **Where the edge is.** Every game total and the top player picks, each with a bar (our chance after the safety cut) and a tick (the chance the price needs), tagged Edge / Close / None. The Games and Player props tabs have every row, the numbers behind it, PrizePicks line re-pricing and a slip builder.
4. **After you bet,** press *I placed these bets*. They are stored in your browser and graded automatically when the results files are published. *Log to shared record* opens a pre-filled GitHub issue; the nightly job grades it and closes it with the result, so both of us see one shared record. The desk's own reference ticket ($100 / $0.50, singles, parlays and up to two $5 pick'em entries) is recorded before tip and graded with closing-line value per leg.
5. **Assumptions** (Fliff's smallest bet, PrizePicks' minimum entry) are editable at the bottom of How it works.

## What it does each night

1. **Data.** ESPN: schedule, every box score of the last four seasons, injury report (play probabilities from fantasy status, return date and comment wording), DraftKings lines (open and current) and props. Basketball-Reference: BPM/OBPM/DBPM, individual offensive and defensive ratings, PER, TS%, USG%, WS/48, VORP, SRS, four factors. Optional: PrizePicks board (`jghedge/data/prizepicks.py`, blocked from most cloud IPs) and The Odds API (`jghedge/data/oddsapi.py`, with `ODDS_API_KEY`). NBA.com blocks cloud servers.
2. **Team ratings.** Offensive and defensive rating per 100 possessions plus pace from a recency-weighted ridge regression over the season, adjusted for opponent and venue, starting from a prior built from last season and the value of the players each team is actually using.
3. **Player value.** OBPM/DBPM blended with individual ORtg (usage-weighted) and DRtg, each mapped onto the BPM scale.
4. **Adjustments.** Injury statuses become play probabilities; the expected change in minutes times value above replacement moves team ratings. Rest, back-to-backs, 3-in-4, travel, time zones, altitude; an independent margin-of-victory Elo.
5. **Market blend and context.** 10% model on spreads and 30% on totals against the line, less near tip. Then `jghedge/model/context.py` adds the one totals feature that survived 48 candidates: late in the regular season (teams averaging 70+ games played) totals run over the close. In-sample (2023-26) the excess was +2.56 points; held out on 2021-22 and 2022-23 it was +1.97 and +0.57, so the desk adds +1.2 (+2.5 for games 79-82 or a tanking underdog, which ran +3.3 and +3.9 held out on 63 and 73 games). On its own, betting every late over at the close lost 1.3-1.7% in the held-out seasons: it tilts the projection, it is not a bet by itself.
6. **Simulation.** 20,000 draws per game from the empirical error distribution. Player stat lines simulated inside each game (minutes, usage from absent teammates, opponent allowance, pace, blowouts; negative binomial counts).
7. **Pricing.** Win probability, no-vig market probability and EV for every bet. Totals qualify at claimed EV ≥ 3% (1.5% after shrinkage) with an edge < 12 points; spreads, moneylines and props are informational. Each total carries `fair_line`, `max_line`, `shrunk_ev`, `p_shrunk`, `b_min` and `growth_1u_per_100`.
8. **Parlays.** Legs from different games only, so the hit probability is the exact product of leg probabilities; ranked by growth of one unit, not EV.
9. **Policy.** `jghedge/portfolio/policy.py`: shrink the joint simulation, growth gate, adaptive tier, whole-unit allocation with the 5% / 10% caps, the reference ticket, the night score and verdict, headline, reasons and "what would change it".
10. **Pick'em.** `jghedge/pickem.py`: market-anchored legs for every player with a DraftKings anchor, the hit matrix (packed bits for the page), slip enumeration with shrunk probabilities, growth at the minimum entry, play / marginal / skip.
11. **Lines and logs.** `jghedge/data/lines.py` appends this run's snapshot (opener, morning, pretip, afternoon) and reports which qualifying bets have moved past their max line; the DraftKings prop board is logged (`data/props_log/`) so the under lean can be measured.
12. **Record.** Reference ticket recorded before tip and graded nightly with CLV; your placed bets graded in the page; shared record via issues; growth grid refreshed from the replay.

## Evidence (walk-forward, no future information)

Game model vs the closing line, final-margin RMSE: 2024-25 14.01 vs 13.73, 2025-26 14.33 vs 14.06 (the market wins).

Full nightly pipeline replayed on every past night at that morning's opening lines, with only pre-tip information, under the current policy (totals and cross-game parlays of totals, shrinkage s = 0.5, growth gate, PASS nights skipped, adaptive tiers), props and pick'em excluded (no clean historical lines), averaged over two random seeds (`data/replay_open.json`; the pre-rebuild run is kept in `data/replay_open_prev.json`):

| Season | Nights bet / nights | Bets | ROI on money staked | $1,000 became (seed 1 / seed 2) | Worst drawdown | Before the rebuild | $10, $0.10 units | $10, $0.50 units |
|---|---|---|---|---|---|---|---|---|
| 2023-24 | 47.5 / 162 | 353 | +1.8% | $1,024 ($1,098 / $950) | 17% | $1,157, DD 27% | $10.20 | $9.50 |
| 2024-25 | 45 / 167 | 275 | +23.1% | $1,301 ($1,366 / $1,235) | 8% | $1,518, DD 17% | $12.96 | $15.41 |
| 2025-26 | 53 / 168 | 336 | +22.0% | $1,323 ($1,360 / $1,287) | 14% | $958, DD 41% | $13.01 | $9.90 |
| 2026-27 (to date) | 8.5 / 17 | 59.5 | +9.1% | $1,018 ($1,019 / $1,017) | 3% | $1,018, DD 7% | $10.20 | $9.72 |

**Read this table with care.** The late-season totals adjustment and the pass threshold were chosen by looking at these same seasons, so the improvement over the old policy is partly in-sample (the table uses the adjustment at its held-out size, +1.2 points). The honest out-of-sample evidence is 2026-27 to date (17 nights, +9.1% on staked, $1,018) and, from now on, the live ledger. The policy bets on roughly a third of nights; most of the drawdown reduction comes from passing and from the growth gate.

Player props: on 1,754 pre-tip DraftKings lines this season, overs hit 44.9% against a 49.0% implied rate; the calibrated blend returned +5.2% on 97 held-out bets. Small sample.

What this means: the repeatable edges are game totals (best at the opening line, which is why a run now prices the evening before), late-season overs, and PrizePicks lines that lag the sportsbook. Spreads and moneylines lost in two of three replayed seasons and are no longer bet. Edges are small, and single seasons swing by hundreds of dollars on a $1,000 bankroll from luck alone (re-running a season with different random draws moves the result by $50–150). A $10 bankroll compounds the same percentages with far lumpier stakes: in the replays, $10 with $0.10 units ended each season between $10.20 and $13.01, and with $0.50 units between $9.50 and $15.41, because a $0.50 unit is 5% of $10 and the desk could place it on only a handful of nights.

Reports: `data/backtest_report.json`, `data/prop_backtest.json`, `data/replay_open.json`.

## Running it yourself

```bash
pip install -r requirements.txt
python -m jghedge run                       # tonight (JG_RUN_LABEL or --label opener|morning|pretip|afternoon)
python -m jghedge backtest                  # walk-forward refit (includes the late-season total term)
python -m jghedge replay --seasons 2024,2025,2026,2027   # both seeds (0 and 1000) under the growth policy
python tests/run_all.py                     # every tests/test_*.py
```

## Layout

```
jghedge/
  data/        espn.py, bbref.py, history.py, http.py, lines.py (snapshots/CLV), prizepicks.py, oddsapi.py
  model/       ratings.py, elo.py, players.py, situational.py, context.py (late-season total), engine.py, simulate.py, props.py, odds.py
  portfolio/   kelly.py, discrete.py (whole-unit sizing), parlay.py, policy.py (shrinkage, tiers, gate, verdict)
  pickem.py    PrizePicks legs, hit matrix, slips      growth.py  bootstrap growth grid
  backtest.py  props_backtest.py  replay.py  pipeline.py  ledger.py  results.py  shared.py  site/
data/          4 seasons of games (gzipped), BBRef snapshots, fitted params, ledgers, reports,
               lines/<date>.json, props_log/<date>.json, growth_grid.json, replay_open.json (+ _prev)
docs/          GitHub Pages site, data/latest.json, data/results/<date>.json
dist/          local copy of the single-file dashboard (not committed)
tests/         test_core, test_policy, test_pickem, test_context, test_pipeline; run_all.py
```

## Assumptions (please check these in the apps)

- **PrizePicks payout table**: Power 2/3/4/5/6 picks 3x / 6x / 10x / 20x / 37.5x; Flex 3/4/5/6 picks 3x / 6x / 10x / 25x when all hit (PrizePicks help centre, via web search on 2026-11-07; the site is blocked from the build sandbox). Flex partial tiers (3-pick 1x with one miss, 4-pick 1.5x, 5- and 6-pick 2x / 0.4x) come from secondary sources. **Minimum entry $5.** PrizePicks says some lineups pay differently; the table is a default in `jghedge/config.py`.
- **PrizePicks void rules**: a pick that lands on the line or whose player does not play is voided and the entry drops to the next size down with that size's payout; below two picks it is refunded. Grading follows this.
- **Fliff minimum wager $0.25**; the Fliff numbers shown are DraftKings via ESPN unless an Odds API key supplies Fliff's own line.
- **Odds API** bookmaker keys (`fliff`, `prizepicks` under `us_dfs`) and credit costs were written from memory and are checked against the first live response.
- **Shrinkage s = 0.5** is a floor estimated from the published replay seed (seed-averaged ~0.45); **s = 0** on sportsbook props and **s = 0.5** on PrizePicks legs (one-sided: a leg below break-even is never pulled up) are provisional until the live ledger grades them; s is a fixed setting, not refit automatically. The pick'em market-anchored probabilities have no historical validation (no PrizePicks line archive).
- The late-season total adjustment was found on 2023-26 and checked on 2021-23 (direction held, size about half); it cannot be tested live until March.

## House rules

- Bet the number shown or better; if the line moved past it, skip.
- One book's lines (DraftKings via ESPN). Shopping other books is free edge.
- Only bet money you can afford to lose. Legal only where licensed and for adults of legal age. Help: 1-800-GAMBLER.
- Research software, not financial advice.
