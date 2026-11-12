"""J&G Hedging Co. command line.

    python -m jghedge run            # tonight's slate -> docs/data/latest.json (+ ledger)
    python -m jghedge run --date 2026-11-08 --sims 30000
    python -m jghedge backtest       # walk-forward game + prop backtests, refits model params
    python -m jghedge update         # pull newly completed games into data/history
    python -m jghedge grade          # grade the ledger from final box scores
    python -m jghedge site           # build standalone dashboard HTML (dist/)
    python -m jghedge replay --seasons 2024,2025,2026,2027   # both seeds under the growth policy
"""
from __future__ import annotations

import argparse
import json
from datetime import date

from .config import CURRENT_SEASON, DATA_DIR, PARAMS_FILE, SITE_DATA


def cmd_update(args):
    from .data import history
    from .data.http import Fetcher
    history.update_season(Fetcher(), args.season)


def cmd_backtest(args):
    from . import backtest
    from . import props_backtest as pb
    from .data.http import Fetcher
    from .loader import build_engine
    f = Fetcher()
    seasons = [int(x) for x in args.seasons.split(",")] if getattr(args, "seasons", None) else \
        [args.season - 2, args.season - 1, args.season]
    eng = build_engine(f, seasons)
    print("Walk-forward game backtest ...")
    rep, preds = backtest.run(eng, seasons, verbose=True)
    rep = backtest._clean(rep)
    print("Prop backtest (pre-tip prop lines, last two seasons) ...")
    eng.params.update(rep["final_params"])
    prep = pb.fit_and_save(f, eng, [args.season - 1, args.season], PARAMS_FILE)
    rep["props"] = backtest._clean(prep)
    # open-line (bet-early) diagnostics without lineup look-ahead
    rep["open_line_study"] = backtest.open_line_study(preds, rep["final_params"])
    (DATA_DIR / "backtest_report.json").write_text(json.dumps(rep, indent=1))
    SITE_DATA.mkdir(parents=True, exist_ok=True)
    slim = {k: v for k, v in rep.items() if k != "final_params"}
    slim["params"] = {k: v for k, v in rep["final_params"].items() if not k.endswith("_q")}
    (SITE_DATA / "backtest.json").write_text(json.dumps(slim, separators=(",", ":")))
    print("Wrote", PARAMS_FILE)


def cmd_run(args):
    import os

    from . import ledger, pipeline, results
    from .data.http import Fetcher
    from .site import build_standalone
    runs = []
    if args.date or args.label or os.environ.get("JG_RUN_LABEL", "").strip():
        # one named night (a date or a label typed in, or the weekly refit): priced on its own, as before
        d = date.fromisoformat(args.date) if args.date else None
        runs.append(pipeline.run(day=d, n_sims=args.sims, update=not args.no_update, with_props=not args.no_props, label=args.label))
        main, nxt = runs[0], None
    else:
        # the usual run prices tonight and tomorrow: the page offers both (before tomorrow's lines are posted it
        # shows each game's own number and the opening line that would be a bet)
        f = Fetcher()
        (d0, lab0), (d1, lab1) = pipeline.run_plan()
        s0, s1 = pipeline.fetch_slate(f, d0), pipeline.fetch_slate(f, d1)
        today = tomorrow = None
        if s0 or not s1:
            today = pipeline.run(day=d0, n_sims=args.sims, update=not args.no_update, with_props=not args.no_props, label=lab0, slate=s0)
            runs.append(today)
        if s1:
            tomorrow = pipeline.run(day=d1, n_sims=args.sims, update=not args.no_update and not runs, with_props=not args.no_props,
                                    label=lab1, slate=s1)
            runs.append(tomorrow)
        main, nxt = pipeline.pick_slates(today, tomorrow)
    if not args.no_update:
        ledger.grade(CURRENT_SEASON)
    summary = {k: v for k, v in ledger.summary().items() if k != "recent"}
    for res in runs:
        res["ledger"] = summary
    pipeline.write_outputs(main)
    pipeline.write_outputs(nxt, name="next")
    results.publish(CURRENT_SEASON)
    for res in runs:
        if res["games"] and not args.no_ledger:
            n = ledger.record(res)
            print(f"Ledger: recorded {n} reference-ticket entries for {res['date']}")
    ledger.publish()
    out = build_standalone()
    for res in runs:
        nt, pk = res["night"], res["pickem"]
        top = pk["slips"][0] if pk["slips"] else None
        print(f"Night {res['date']} ({res['run_label']}{', main' if res is main else ', next' if res is nxt else ''}): {nt['verdict']} "
              f"score {nt['score']:.6f} | {res['counts']['placeable_ref']} placeable at $100/$0.50 | pick'em {pk['source']}: "
              f"{len(pk['legs'])} legs, top slip {top['label'] if top else 'none'}" + (f" ({top['type']}, {top['verdict']})" if top else "")
              + (" [stand-in lines: check the app]" if pk.get("mode") == "check_lines" else ""))
        print(nt["headline"])
    print("Dashboard data:", SITE_DATA / "latest.json", "| standalone:", out)


def cmd_grade(args):
    from . import ledger
    from .data import history
    from .data.http import Fetcher
    history.update_season(Fetcher(), args.season)
    print("graded", ledger.grade(args.season))
    ledger.publish()
    from . import results
    results.publish(args.season)


def cmd_replay(args):
    from .replay import run_and_save
    from .site import build_standalone
    seasons = [int(x) for x in args.seasons.split(",")]
    seeds = tuple(int(x) for x in args.seeds.split(","))
    res = run_and_save(seasons, lines=args.lines, seeds=seeds)
    for k, r in res.items():
        print(k, f"${r['start_bankroll']:,.0f} -> ${r['end_bankroll']:,.0f} (seeds {r.get('per_seed_end')})",
              "ROI", r.get("stake_weighted_roi"), "bets", r.get("bets"), "max DD", r.get("max_drawdown"),
              "verdicts", r.get("verdicts"), "small", {k_: v["end"] for k_, v in r["small"].items()})
    for line in res[next(iter(res))]["not_replayed"]:
        print("note:", line)
    build_standalone()


def cmd_props(args):
    from . import props_backtest as pb
    from .data.http import Fetcher
    from .loader import build_engine
    f = Fetcher()
    eng = build_engine(f, [args.season - 2, args.season - 1, args.season])
    rep = pb.fit_and_save(f, eng, [args.season - 1, args.season], PARAMS_FILE)
    ou = rep.get("by_kind", {}).get("ou", {})
    print("rows", rep.get("rows_by_season"), "stack", ou.get("stack_coef_all"), "bias", rep.get("prop_bias"))
    print("held-out", ou.get("oos_betting"))


def cmd_ingest(args):
    from . import shared
    print("shared ledger:", shared.ingest(args.season))


def cmd_site(args):
    from .site import build_standalone
    print(build_standalone())


def main():
    ap = argparse.ArgumentParser(prog="jghedge", description="J&G Hedging Co. NBA engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--date")
    p.add_argument("--sims", type=int)
    p.add_argument("--no-update", action="store_true")
    p.add_argument("--no-props", action="store_true")
    p.add_argument("--no-ledger", action="store_true")
    p.add_argument("--label", help="snapshot label: opener | morning | pretip | afternoon (default: JG_RUN_LABEL or by hour)")
    p.set_defaults(fn=cmd_run)
    for name, fn in (("update", cmd_update), ("backtest", cmd_backtest), ("grade", cmd_grade), ("ingest", cmd_ingest),
                     ("props", cmd_props)):
        q = sub.add_parser(name)
        q.add_argument("--season", type=int, default=CURRENT_SEASON)
        if name == "backtest":
            q.add_argument("--seasons", default=f"{CURRENT_SEASON - 3},{CURRENT_SEASON - 2},{CURRENT_SEASON - 1},{CURRENT_SEASON}")
        q.set_defaults(fn=fn)
    sub.add_parser("site").set_defaults(fn=cmd_site)
    q = sub.add_parser("replay")
    q.add_argument("--seasons", default=f"{CURRENT_SEASON - 1},{CURRENT_SEASON}")
    q.add_argument("--lines", default="open", choices=["open", "close"])
    q.add_argument("--seeds", default="0,1000", help="seed offsets to run and average")
    q.set_defaults(fn=cmd_replay)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
