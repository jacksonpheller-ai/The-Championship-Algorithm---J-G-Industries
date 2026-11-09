# J&G Hedging Co. — NBA Desk

A quantitative NBA betting engine and dashboard for two people and a small bankroll. Every day it prices the slate (moneylines, spreads, totals, player props), builds cross-game parlays, and splits whatever you want to bet tonight across the bets that have an edge. It records the bets you say you placed and grades them from the box scores.

**Open it:** `https://jacksonpheller-ai.github.io/The-Championship-Algorithm---J-G-Industries/` once GitHub Pages is switched on (Settings → Pages → *Deploy from a branch* → `main`, folder `/docs`). `docs/index.html` is itself a single self-contained file (that run's data is built in), so it also opens straight from disk; `python -m jghedge site` writes a copy to `dist/` locally (not committed).

## Is it updated every day?

Yes, by GitHub Actions on this repository (`.github/workflows/nightly.yml`). Every pricing run records the lines it saw in `data/lines/<date>.json` under its label, so the desk can measure closing-line value and tell you when a number has moved past us.

| Schedule (UTC) | What runs |
|---|---|
| every two hours at :17 | prices the slate for the current Eastern time and grades any finished night. The snapshot label comes from the clock when the run starts: `opener` (8 pm to 6 am ET; from 6 pm it prices the **next** day's slate at the opening lines, where the totals edge is thought to live; the "+1.75 pts of CLV at a 3-pt signal" study behind that used lineups known only after the fact, see Evidence), `morning` (to 2 pm), `pretip` (to 5 pm), `afternoon` (to 8 pm) |
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
5. **Market blend and context.** 30% model on totals against the line six or more hours out, falling to none at tip (the fitted weight on spreads is zero: the spread shown is the market's). The simulation is centred so that with no model weight it returns the market's own no-vig probability. Then `jghedge/model/context.py` adds the one totals feature that survived 48 candidates: late in the regular season (teams averaging 70+ games played) totals run over the close. In-sample (2023-26) the excess was +2.56 points; held out on 2021-22 and 2022-23 it was +1.97 and +0.57, so the desk adds +1.2 (+2.5 for games 79-82 or a tanking underdog, which ran +3.3 and +3.9 held out on 63 and 73 games). On its own, betting every late over at the close lost 1.3-1.7% in the held-out seasons: it tilts the projection, it is not a bet by itself.
6. **Simulation.** 20,000 draws per game from the empirical error distribution, centred on its median (the centres are 50/50 points; totals are right-skewed) with tied draws settled by one point. Player stat lines simulated inside each game (minutes, usage from absent teammates, opponent allowance, pace, blowouts; negative binomial counts).
7. **Pricing.** Win probability, no-vig market probability and EV for every bet. Totals qualify at claimed EV ≥ 3% (1.5% after shrinkage) with an edge < 12 points; spreads, moneylines and props are informational. Each total carries `fair_line`, `max_line`, `shrunk_ev`, `p_shrunk`, `b_min` and `growth_1u_per_100`.
8. **Parlays.** Legs from different games only, so the hit probability is the exact product of leg probabilities; ranked by growth of one unit, not EV.
9. **Policy.** `jghedge/portfolio/policy.py`: shrink the joint simulation, growth gate, adaptive tier, whole-unit allocation with the 5% / 10% caps, the reference ticket, the night score and verdict, headline, reasons and "what would change it".
10. **Pick'em.** `jghedge/pickem.py`: market-anchored legs for every player with a DraftKings anchor (price minus the under lean), then every 2- to 6-pick slip from the 14 best legs priced **exactly** from the legs' probabilities (`slip_outcomes`: picks of different players independent, a void pick drops the slip a size, players from at least two teams), with shrunk probabilities, growth at the minimum entry, play / marginal / skip. The page computes the same value in the browser for typed lines and custom slips.
11. **Lines and logs.** `jghedge/data/lines.py` appends this run's snapshot (opener, morning, pretip, afternoon) and reports which qualifying bets have moved past their max line; the DraftKings prop board is logged (`data/props_log/`) so the under lean can be measured.
12. **Record.** Reference ticket recorded before tip and graded nightly with CLV; your placed bets graded in the page; shared record via issues; growth grid refreshed from the replay.

## Evidence (what it shows and what it does not)

Game model vs the closing line, final-margin RMSE: 2024-25 14.01 vs 13.73, 2025-26 14.33 vs 14.06 (the market wins).

Full nightly pipeline replayed on every past night at the stored opening lines, with the lines, results and rosters known before tip, under the current policy (totals and cross-game parlays of totals, shrinkage s = 0.5, growth gate, PASS nights skipped, adaptive tiers), props and pick'em excluded (no clean historical lines), averaged over two random seeds (`data/replay_open.json`). Re-run on 2026-11-09 with the refit parameters after the code-review fixes; the last column is the same replay (same parameters and seeds) on the code before them:

| Season | Nights bet / nights | Bets | ROI on money staked | $1,000 became (seed 1 / seed 2) | Worst drawdown | Before the 11-09 fixes | $10, $0.10 units | $10, $0.50 units |
|---|---|---|---|---|---|---|---|---|
| 2023-24 | 51 / 162 | 375 | +4.3% | $1,065 ($1,118 / $1,013) | 19% | $1,035, 355 bets, DD 16% | $10.29 | $9.72 |
| 2024-25 | 45 / 167 | 293.5 | +20.0% | $1,279 ($1,317 / $1,241) | 8% | $1,328, 277 bets, DD 7% | $12.95 | $16.02 |
| 2025-26 | 64.5 / 168 | 405.5 | +23.4% | $1,450 ($1,556 / $1,344) | 15% | $1,353, 328.5 bets, DD 14% | $13.80 | $9.82 |
| 2026-27 (to date) | 7.5 / 19 | 53 | +5.2% | $1,009 ($1,020 / $999) | 4% | $1,018, 63 bets, DD 4% | $10.19 | $9.72 |

The simulator fix removed a 0.4-point tilt toward Unders, so the replay now bets more Overs and fewer Unders (1,135 Overs and 129 Unders across both seeds, against 970 and 184). The three full seasons end at $3,795 in total against $3,716 before: no difference beyond seed noise (two seeds of one season differ by up to $210).

**Read this table with care. None of it is out-of-sample.**

- **The parameters saw these nights.** `backtest` fits the final parameters (blend weights, error tables, late-season term) on every season at once, including the 2026-27 nights, and the replay then prices those same nights with them. The EV floor, the shrink and the pass threshold were chosen on this same four-season replay. Earlier versions of this file called the 2026-27 row "the honest out-of-sample evidence"; it is not.
- **Every bet is assumed filled at the stored opening line.** On the desk's first live opener night (2026-11-08) its own first look was already 2 to 3 points off that line in 4 of 7 games. An opener the desk cannot bet is not an edge. (That run started seven hours late; on-time runs should be closer.)
- **The opening-line study behind "+1.75 pts of CLV at a 3-pt signal" had lineup look-ahead.** `backtest.open_line_study` strips the who-actually-played adjustment from the spread signal but not from the totals signal, so it overstates what a pre-game model can see.
- **Thin nights have not paid.** In this replay, totals on BET nights returned +10.5% per $1 (841 bets), on LIGHT nights -0.7% (423 bets), and on the thinnest betting nights (score 0.05-0.10%) -2.1% (260 bets). That supports passing; it also says the LIGHT tier has been roughly break-even.

The only test the method has not already seen is the live ledger (Record tab).

Player props: on 8,009 pre-game DraftKings lines (Jan-Nov 2026), overs hit 45.4% against a 48.8% no-vig price (see "Under lean" above). The stacked prop model returned +5.2% on 97 held-out bets in the earlier, smaller sample; props at a sportsbook price are not bet.

What this means: the candidate edges are game totals near the opening line, late-season overs, and PrizePicks picks on the Less side where PrizePicks posts the sportsbook's line. Spreads and moneylines lost in two of three replayed seasons and are not bet. Edges are small and unproven live, and single seasons swing by hundreds of dollars on a $1,000 bankroll from luck alone. A $10 bankroll compounds the same percentages with far lumpier stakes: in the replays, $10 with $0.10 units ended each season between $10.19 and $13.80, and with $0.50 units between $9.72 and $16.02.

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
- **PrizePicks void rules**: a pick that lands on the line or whose player does not play is voided and the entry drops to the next size down with that size's payout; below two picks it is refunded. Grading and slip pricing both follow this.
- **PrizePicks entry rules**: an entry needs players from at least two teams (from memory, not checked against the app), and "Less" may not be offered on every pick; the page asks you to confirm each pick's side and line in the app.
- **Fliff minimum wager $0.25**; the Fliff numbers shown are DraftKings via ESPN unless an Odds API key supplies Fliff's own line.
- **Odds API** bookmaker keys (`fliff`, `prizepicks` under `us_dfs`) and credit costs were written from memory and are checked against the first live response.
- **Shrinkage s = 0.5** is a floor estimated from the published replay seed (seed-averaged ~0.45); **s = 0** on sportsbook props and **s = 0.5** on PrizePicks legs (one-sided: a leg below break-even is never pulled up) are provisional until the live ledger grades them; s is a fixed setting, not refit automatically. The pick'em market-anchored probabilities have no historical validation (no PrizePicks line archive).
- The late-season total adjustment was found on 2023-26 and checked on 2021-23 (direction held, size about half); it cannot be tested live until March.

## Code review of 2026-11-09

Three independent reviews of the engine. **Fixed** (each reproduced first, each with a test): the simulator's 0.4-point tilt toward Unders; back-to-backs missed by evening runs; "not expected to play" read as "expected to play"; pick'em slips priced from the luckiest of ~6,500 combinations on shared draws (now exact); a did-not-play valued three different ways (now one void rule everywhere); single-team slips; the under lean double-counted on combos anchored through components; reference-ticket caps applied per run instead of per night; closing-line value graded against the desk's last snapshot instead of the close; a 0.4x Flex payout graded as a push; the prop log overwritten by each run; a "line moved past our number" flag that could never fire; the lineup-uncertainty cap ignoring parlay stake.

**Found and not fixed yet:**

- Player minutes are not reconciled to 240 per team: on one slate expected minutes ran 227 to 299 per team, which inflates the information-only prop projections and feeds the injury adjustment to team ratings (small on totals: the model carries at most 30% of the total).
- A questionable player's own chance of sitting raises their projection for the games they do play (information-only props; pick'em needs 90% to play and is anchored to the sportsbook).
- The "2-leg parlays only below 100 units" limit is computed but not enforced when a ticket is sized; it only affects the $10 replay columns.
- The dated slate archive (`docs/data/slates/<date>.json`) holds only the last run's un-tipped games.
- The late-season +2.5 starts at a team's 80th game, not its 79th.
- The workflow's `git pull --rebase -X theirs` would drop a ledger row pushed by someone else while a job is running.

## House rules

- Bet the number shown or better; if the line moved past it, skip.
- One book's lines (DraftKings via ESPN). Shopping other books is free edge.
- Only bet money you can afford to lose. Legal only where licensed and for adults of legal age. Help: 1-800-GAMBLER.
- Research software, not financial advice.
