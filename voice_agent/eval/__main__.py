"""python -m voice_agent.eval <command>     (run from the project root; see voice_agent/eval/README.md)
   run [files...]     replay scenarios, print the scorecard   (--brain legacy=URL [--brain converse=URL] --mode text|audio --judge N --noise 5 ...)
   report RUN_DIR     re-render a saved run's report.md
   judge RUN_DIR -n N  naturalness judge on a saved run
   generate           (re)write voice_agent/scenarios/generated_*.yaml
   list               list scenarios with their tags
   checklist          print the 10-question human checklist"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def cmd_run(a) -> int:
    from . import brains, judge as J, report, runner
    from .world import World

    scen = runner.select(runner.load(a.files), a.tags, a.exclude, a.name, a.limit, a.mode)
    if not scen:
        print("no scenarios selected")
        return 2
    turns = sum(len(s["turns"]) for s in scen)
    specs = a.brain or ["legacy=http://127.0.0.1:8011"]
    execs, warnings = [], []
    for spec in specs:
        try:
            b = brains.make(spec)
        except brains.BrainUnavailable as e:
            print(f"SKIP {spec}: {e}")
            continue
        w = World(b.url, db=a.db)
        if not w.health():
            print(f"{b.url} is not answering. Start the throwaway backend first:  python voice_agent/throwaway_backend.py")
            return 2
        if a.mode == "audio":
            from .audio import AudioExecutor
            ex = AudioExecutor(b, a.ws, noise_db=a.noise, lowpass=a.lowpass, speed=a.speed, voice=a.voice)
        else:
            ex = runner.TextExecutor(b)
        st = w.reset()                                     # the scenarios assume the demo plan: squat is the current exercise
        cur = ((st or {}).get("current") or {}).get("name")
        if cur != "squat":
            warnings.append(f"{b.url}: the current exercise is {cur!r}, the scenarios assume 'squat' (a different demo plan). Exercise-specific results are NOT comparable; wrong-action counts for negatives still are.")
            print("WARNING:", warnings[-1])
        if hasattr(ex, "prerender"):
            ex.prerender(scen)
        execs.append((ex, w, 1.0 if b.kind == "converse" else 0.5))
    if not execs:
        return 2
    est = int(sum(turns * f for _, _, f in execs)) + a.judge
    if not J.budget_ok(est) and not a.force:
        print(f"REFUSING: this run needs about {est} model requests, today's budget is {J.DAILY_BUDGET} and {J.budget_used()} are used. "
              f"Run a smaller set (--limit/--tags), raise EVAL_DAILY_REQUEST_BUDGET, or pass --force.")
        return 3
    print(f"{len(scen)} scenarios / {turns} turns, mode={a.mode}, configs={[e.label for e, _, _ in execs]}")
    all_recs = {}
    for ex, w, _ in execs:
        all_recs[ex.label] = runner.run(ex, w, scen)
    J.budget_add(int(sum(turns * f for _, _, f in execs)))
    jr = J.judge(next(iter(all_recs.values())), a.judge) if a.judge else None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(a.out) if a.out else HERE / "runs" / stamp
    text = report.write_run(out, all_recs, jr, {"stamp": stamp, "mode": a.mode, "scenario_count": len(scen), "files": sorted({s["file"] for s in scen}), "noise": a.noise, "warnings": warnings})
    print()
    print(text)
    print(f"\nsaved: {out}")
    if a.save_baseline:
        Path(a.save_baseline).write_text(text, encoding="utf-8")
        print(f"baseline written to {a.save_baseline}")
    bad = sum(report.summarise(r)["wrong_actions_all"] for r in all_recs.values())
    return 1 if bad else 0                                     # any wrong action fails the build (plan 6.2)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="python -m voice_agent.eval", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("files", nargs="*", help="scenario files or globs (default: all of voice_agent/scenarios/*.yaml)")
    r.add_argument("--brain", action="append", help="NAME=URL, e.g. legacy=http://127.0.0.1:8011 or converse=http://127.0.0.1:8011 (repeat to compare)")
    r.add_argument("--db", help="throwaway database file of the backend under test; only needed for OLD backends without /live/unskip (to clear skips between scenarios)")
    r.add_argument("--mode", choices=["text", "audio"], default="text")
    r.add_argument("--ws", default="ws://127.0.0.1:8766/ws?token=testtoken", help="audio mode: the voice agent websocket (a THROWAWAY agent, not :8765)")
    r.add_argument("--noise", type=float, help="audio mode: mix gym noise at this SNR in dB (0, 5, 10)")
    r.add_argument("--lowpass", action="store_true", help="audio mode: 'phone far away' (muffled, quiet) variant")
    r.add_argument("--speed", type=float, default=1.0, help="audio mode: caller speaking speed")
    r.add_argument("--voice", default="am_adam", help="audio mode: Kokoro caller voice")
    r.add_argument("--judge", type=int, default=0, metavar="N", help="LLM-judge N sampled replies for naturalness (uses free-tier requests)")
    r.add_argument("--tags", nargs="*")
    r.add_argument("--exclude", nargs="*")
    r.add_argument("--name")
    r.add_argument("--limit", type=int)
    r.add_argument("--out")
    r.add_argument("--force", action="store_true", help="ignore the daily request budget")
    r.add_argument("--save-baseline", metavar="FILE", help="also write the report to this file")
    sub.add_parser("report").add_argument("run_dir", help="a folder under voice_agent/eval/runs/ (re-renders report.md from its turns.jsonl)")
    j = sub.add_parser("judge", help="run the naturalness LLM judge on a SAVED run (uses free-tier requests, cached)")
    j.add_argument("run_dir")
    j.add_argument("-n", type=int, default=12)
    sub.add_parser("generate")
    sub.add_parser("list").add_argument("files", nargs="*")
    sub.add_parser("checklist")
    a = p.parse_args(argv)
    if a.cmd == "run":
        return cmd_run(a)
    if a.cmd == "report":
        from . import report
        d = Path(a.run_dir)
        d = d if d.exists() else HERE / "runs" / a.run_dir
        recs = report.load_run(d)
        print(report.write_run(d, recs, None, {"stamp": d.name, "mode": "re-rendered", "scenario_count": len({r["scenario"] for v in recs.values() for r in v}), "files": ["(saved run)"]}))
        return 0
    if a.cmd == "judge":
        from . import judge as J, report
        d = Path(a.run_dir)
        d = d if d.exists() else HERE / "runs" / a.run_dir
        recs = next(iter(report.load_run(d).values()))
        jr = J.judge(recs, a.n)
        text = (jr or {}).get("markdown", "no judge result")
        (d / "judge.md").write_text(text, encoding="utf-8")
        print(text)
        return 0
    if a.cmd == "generate":
        from . import generate
        generate.main()
        return 0
    if a.cmd == "list":
        from . import runner
        for s in runner.load(a.files):
            print(f"{s['name']:42} {len(s['turns'])} turn(s)  [{', '.join(s['tags'])}]  {s['file']}")
        return 0
    print((HERE / "human_checklist.md").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
