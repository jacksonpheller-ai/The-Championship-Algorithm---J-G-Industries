# Improvement loop: five passes

Goal: price PrizePicks' real lines (primary) and Fliff totals (secondary) for two people with a $20 bankroll, show honestly whether it is winning, and be effortless to read on a phone.

| Pass | What | Status |
|---|---|---|
| 1 | Import and share a pasted PrizePicks board | done 2026-11-16 |
| 2 | Price the real board (standard, demon, goblin) | next |
| 3 | $20 bankroll: bank, slip type and size, bank needed | |
| 4 | Redesign Tonight, navigation, shared styles; Fliff collapsed | |
| 5 | Grade archived PrizePicks lines; Record by side and type; other tabs | |

**No real PrizePicks board is in the repository yet.** Everything below is built to the shape in `jghedge/data/prizepicks.py` (JSON:API `data[]` projections + `included[]` new_player). The demon/goblin multiplier field name is a guess (`payout_multiplier`, `multiplier`, `odds_multiplier`); if the real board carries none, the app's number must be typed. A real sample is needed as a test fixture.

## Pass 1: import and share (2026-11-16)

How it works now:
- Paste (or drop a file) into the board box on Tonight or Player props. It is read on paste, with no button. Standard, demon and goblin lines are parsed and kept apart; a second page of the board merges with the first if pasted within 20 minutes.
- **Save for both** is a link to a pre-filled GitHub issue "Board <date> ..." whose body holds the board packed into one line (`JGB1.` + base64url of deflated JSON; format in `jghedge/boards.py`). Pressing *Submit new issue* on GitHub saves it.
- `.github/workflows/board.yml` runs on that issue (issues: opened; it does not start a model run or touch the schedule): archives `data/boards/<slate date>/<UTC time>Z.json`, writes `docs/data/board.json`, pushes to the default branch (the live branch) and `main`, comments and closes the issue. The nightly `ingest` catches any board issue the workflow missed.
- The other person's page polls GitHub's public issues API every 2 minutes (20 seconds while a save is waiting) and `docs/data/board.json`, and applies the newest board for its slate if it is under 3 hours old. Older: "The last board is N h old. Paste a fresh one."
- Bar states, each with one action: no board (paste), board read (Save for both / Undo), saving (Open GitHub again / Undo), saved, save failed (Try saving again), from partner, stale, not a board.

Assumptions (unverified): GitHub accepts a pre-filled issue address up to 7,000 characters (longer boards fall back to "copy, then paste into the issue": 3 taps); anyone who can open an issue on this public repository can save a board (the page and the archive show who saved it).

Design review, three worst problems found and fixed: the board bar sat above the headline (moved below the slip); "Save for both" was a small button under 44px (all `.btn` now min 44px, the save link full size); "698 of 700 priced (100%)" rounded up (now rounds down: 99.7%).

### Scorecard after pass 1

| Line | Score | Evidence |
|---|---|---|
| Full board parses < 2 s on a phone | met (emulated) | 1,500-line board read and priced in 569 ms with the CPU slowed 4x (test_site.js). Not tested on a real phone. |
| ≥95% of supported NBA lines match; unmatched listed by name | not tested | Synthetic board: 698 of 700 standard lines (99.7%), the 2 missing listed by name. A real board will include bench players with no DraftKings line, so the real rate is unknown until a sample arrives. |
| Every board archived; every archived line graded by next run | partly met | Archived on save (test_boards.py). Grading is pass 5. |
| Demons/goblins use board or typed multiplier; no assumed payout without a label | not met | Parsed and archived with any multiplier found; not priced yet (pass 2). |
| Both test suites pass; new code has tests | not met | Python 6/6 files pass (new test_boards.py). test_site.js 144/145: the one failure ("page formula matches the engine", gap 0.0131 vs 0.013 on today's slate) also failed on the untouched live branch before this pass. |
| Five-second test from the first phone screen | not met | First screen is the amount box; the headline is below it ("Tonight: a PrizePicks slip") while the night's call is pass. |
| Friend ≤3 taps after paste; partner 0; logging a slip 2 taps | partly met | Scripted: friend 2 taps (Save for both, Submit on GitHub); partner 0 (page picks the board up from GitHub). Slip logging not measured yet (pass 3). |
| Verdict, slip, bank needed, record in first two screens; Tonight ≤ 3 screens | not met | Tonight is 5,799 px tall at 390 px (6.9 screens of 844). |
| Each pick names player, stat, More/Less, line, line type; headline agrees with verdict | not met | Line type not shown; headline offers a slip on a pass night (pass 4). |
| One banner and one primary card per screen; no sideways scroll; 44px taps; 14px text; AA; focus; reduced motion | partly met | No sideways scroll on any tab (test_site.js). Buttons now ≥44px. Rest is pass 4. |
| Every state screenshotted at 390x844 and 1280x900 | partly met | `node tests/shots.js` shoots no board, board read, saving, save failed, not a board, Player props with a board, every tab, all games tipped, no games. Pass night: today's real slate is a pass night (no-board shots). Not yet: board stale, from partner. |
| PrizePicks results labelled unproven until 300 graded picks | not tested | Pass 5. |
| EV floor, shrink, lean, pass thresholds unchanged | met | No change to `jghedge/config.py` or the policy. |
