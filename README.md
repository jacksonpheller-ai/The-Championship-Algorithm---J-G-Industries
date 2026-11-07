# J&G Hedging Co. — NBA Desk

A quantitative NBA betting engine and dashboard for two people and a small bankroll. Every day it prices the slate (moneylines, spreads, totals, player props), builds cross-game parlays, and turns whatever bankroll you type in into a ticket of whole-unit stakes. It records the bets you say you placed and grades them from the box scores.

**Open it:** `https://jacksonpheller-ai.github.io/The-Championship-Algorithm---J-G-Industries/` once GitHub Pages is switched on (Settings → Pages → *Deploy from a branch* → `main`, folder `/docs`). Until then, `dist/jg-hedging-co.html` is a single file that opens in any browser with that day's data built in.

## Is it updated every day?

Yes, by GitHub Actions on this repository (`.github/workflows/nightly.yml`). Every pricing run records the lines it saw in `data/lines/<date>.json` under its label, so the desk can measure closing-line value and tell you when a number has moved past us.

| Time (Eastern) | Label | What runs |
|---|---|---|
| ~9:31 pm (evening before) | `opener` | prices the next day's slate at the **opening** lines (where the totals edge lives: +1.75 pts of CLV at a 3-pt signal) |
| ~10:41 am | `morning` | re-prices against the morning lines and injury report |
| ~3:41 pm | `pretip` | pre-tip run for the 4-5 pm tips |
| ~5:17 pm | `afternoon` | afternoon injury news before the main slate |
| ~4:23 am | grade | grades last night's ledger (CLV per leg), publishes results files, ingests the shared record |
| Mondays ~6:00 am | refit | refits every model parameter and refreshes the two-seed replay |

GitHub runs scheduled jobs only on the repository's default branch and pauses them after 60 days without a commit; the bot's own commits keep it alive. Every run also mirrors its commit to `main`, so `main` stays current whichever branch is the default (set `main` as the default in Settings → General to run everything from it). If a run ever fails, the Actions tab shows why, and `workflow_dispatch` lets you run any task (and label) by hand. Set the repository secret `ODDS_API_KEY` to add multi-book prices (Fliff's own numbers, PrizePicks lines via the `us_dfs` region); without it the desk prices from ESPN's DraftKings feed.

## The platform split

The desk tells two different apps what to do:

- **Fliff — game bets.** Totals and cross-game parlays of totals, the only game markets with a measured edge. Spreads and moneylines are priced and shown but marked `info_only` (they lost in two of three replayed seasons). Each qualifying total comes with an instruction like *"Over 228.5 at -110 or better; skip above 229.5"*: the `max_line` is the worst number at which the shrunk edge still clears the 1.5% floor. Minimum wager assumed $0.25 (unverified).
- **PrizePicks — player pick'em.** PrizePicks pays fixed multiples, so a leg is worth playing only when its true hit rate clears the break-even (57.7% for a 2-pick Power, 55.0% for 3-pick, 54.3% per leg on a 5-pick Flex). Each player's simulated stat line is **anchored to DraftKings' line and price** (the sharp number) and then priced at PrizePicks' line. When the two lines differ, that gap is the edge. Validation: on 2,866 DraftKings prop lines that moved between open and close, a distribution anchored at the closing (sharp) line and priced at the opening (stale) line predicted 60.7% for the favoured side and realised 59.0%; in the 60-65% band it predicted 62% and realised 66% (n=589). PrizePicks blocks cloud servers (HTTP 403), so the nightly job usually cannot read the board. In that case the page lists near-even DraftKings legs with **take-it thresholds** ("More if the line is 24.5 or lower") and marks every suggested slip *check lines*: open the app, type PrizePicks' line into the table, and the leg is re-priced in the browser. Pick'em is provisional until the live ledger has graded enough slips, and at a $10 bankroll the $5 minimum entry is half the bank, which the page flags.

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

1. **Bankroll and smallest bet.** Type the bankroll ($10 works) and the smallest stake your book allows. The page tells you your tier and whether the unit is too large for the bankroll.
2. **Read the verdict.** PASS / LIGHT / BET with the reasons and what would change it. On a PASS there is nothing to place; the best available bet is shown with the line that would make it playable.
3. **Fliff ticket.** Whole-unit stakes across the qualifying totals and cross-game parlays under your tier. Bet each number at the stated price or better; skip anything past its max line.
4. **PrizePicks slips.** Check each leg's take-it threshold against the line in the app (or type the app's line into the table). Suggested slips show payout multiple, shrunk EV and growth at the minimum entry; build your own from the leg list and the page prices it.
5. **After you bet,** press *I placed these bets*. They are stored in your browser and graded automatically when the results files are published. *Log to shared record* opens a pre-filled GitHub issue; the nightly job grades it and closes it with the result, so both of us see one shared record. The desk's own reference ticket ($100 / $0.50, singles, parlays and up to two $5 pick'em entries) is recorded before tip and graded with closing-line value per leg.

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
| 2023-24 | 47.5 / 162 | 361 | +0.8% | $1,013 ($1,078 / $948) | 18% | $1,157, DD 27% | $10.45 | $9.50 |
| 2024-25 | 45.5 / 167 | 311.5 | +23.0% | $1,336 ($1,387 / $1,285) | 7% | $1,518, DD 17% | $13.70 | $14.98 |
| 2025-26 | 56 / 168 | 382.5 | +19.4% | $1,316 ($1,330 / $1,301) | 15% | $958, DD 41% | $12.57 | $9.90 |
| 2026-27 (to date) | 8.5 / 17 | 58.5 | +7.3% | $1,015 ($1,019 / $1,010) | 3% | $1,018, DD 7% | $10.20 | $9.72 |

**Read this table with care.** The late-season totals adjustment and the pass threshold were chosen by looking at these same seasons, so the improvement over the old policy is partly in-sample (the adjustment has since been cut to its held-out size, +1.2 points, which the table above predates). The honest out-of-sample evidence is 2026-27 to date (17 nights, +7.3% on staked, $1,015) and, from now on, the live ledger. The policy bets on roughly a third of nights; most of the drawdown reduction comes from passing and from the growth gate.

Player props: on 1,754 pre-tip DraftKings lines this season, overs hit 44.9% against a 49.0% implied rate; the calibrated blend returned +5.2% on 97 held-out bets. Small sample.

What this means: the repeatable edges are game totals (best at the opening line, which is why a run now prices the evening before), late-season overs, and PrizePicks lines that lag the sportsbook. Spreads and moneylines lost in two of three replayed seasons and are no longer bet. Edges are small, and single seasons swing by hundreds of dollars on a $1,000 bankroll from luck alone (re-running a season with different random draws moves the result by $50–150). A $10 bankroll compounds the same percentages with far lumpier stakes: in the replays, $10 at Balanced with $0.10 units ended most seasons between $9 and $16, and full Kelly with $0.50 units anywhere from $3 to $44.

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
dist/          single-file dashboard
tests/         test_core, test_policy, test_pickem, test_context, test_pipeline; run_all.py
```

## Assumptions (please check these in the apps)

- **PrizePicks payout table**: Power 2/3/4/5/6 picks 3x / 6x / 10x / 20x / 37.5x; Flex 3/4/5/6 picks 3x / 6x / 10x / 25x when all hit (PrizePicks help centre, via web search on 2026-11-07; the site is blocked from the build sandbox). Flex partial tiers (3-pick 1x with one miss, 4-pick 1.5x, 5- and 6-pick 2x / 0.4x) come from secondary sources. **Minimum entry $5.** PrizePicks says some lineups pay differently; the table is a default in `jghedge/config.py`.
- **PrizePicks void rules**: a pick that lands on the line or whose player does not play is voided and the entry drops to the next size down with that size's payout; below two picks it is refunded. Grading follows this.
- **Fliff minimum wager $0.25**; the Fliff numbers shown are DraftKings via ESPN unless an Odds API key supplies Fliff's own line.
- **Odds API** bookmaker keys (`fliff`, `prizepicks` under `us_dfs`) and credit costs were written from memory and are checked against the first live response.
- **Shrinkage s = 0.5** is a floor estimated from the published replay seed (seed-averaged ~0.45); **s = 0** on props and pick'em edges are provisional until the live ledger grades them. The pick'em market-anchored probabilities have no historical validation (no PrizePicks line archive).
- The late-season total adjustment was found on 2023-26 and checked on 2021-23 (direction held, size about half); it cannot be tested live until March.

## House rules

- Bet the number shown or better; if the line moved past it, skip.
- One book's lines (DraftKings via ESPN). Shopping other books is free edge.
- Only bet money you can afford to lose. Legal only where licensed and for adults of legal age. Help: 1-800-GAMBLER.
- Research software, not financial advice.
