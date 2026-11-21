"""CLI for the Fitness Agent OS. Agents (Claude Code subagents) read through `context`/`tool`; only `propose` writes."""
from __future__ import annotations

import argparse
import json
import sys

from backend.memory import db

from . import context, control, curator, experiments, ledger, memory, policy, reflection, scheduler, watchdogs
from .schema import Proposal


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _read_json(src: str):
    raw = sys.stdin.read() if src == "-" else (open(src, encoding="utf-8").read() if not src.lstrip().startswith(("{", "[")) else src)
    return json.loads(raw)


def _proposals(data) -> list[Proposal]:
    items = data if isinstance(data, list) else data.get("proposals", [data])
    return [Proposal(**i) for i in items]


def cmd_intake(a):
    from . import intake
    _out(intake.status() if a.action == "status" else intake.preview(a.text, a.slot))


def cmd_research(a):
    from . import research
    _out(research.ask(a.text) if a.action == "ask" else research.screen_claim(a.text) if a.action == "claim" else research.screen_plan())


def cmd_form(a):
    from . import form
    if a.action == "status":
        return _out(form.status())
    try:
        _out(form.analyze(_read_json(a.file) if a.file else None, None, a.exercise, a.note))   # read-only: never submits anything
    except (form.FormInputError, ValueError) as e:
        _out({"error": str(e)})


def cmd_status(a):
    plan = db.get_plan() or {}
    _out({"has_plan": bool(plan), "volume_mult": plan.get("volume_mult"), "calories": plan.get("nutrition", {}).get("calories"),
          "kill_switch": ledger.kill_switch(), "dial": ledger.dial(), "frozen": ledger.frozen_domains(),
          "pending": [{"id": d["id"], "status": d["status"], "agent": d["agent"], "what": Proposal(**d["proposal"]).summary(),
                       "veto_until": d["veto_until"]} for d in ledger.recent(50) if d["status"] in ("pending_approval", "pending_veto")],
          "watchdog_flags": [{k: v for k, v in w.items() if k != "proposal"} for w in watchdogs.run_all()]})


def cmd_context(a):
    _out(context.pack(a.agent))


def cmd_tool(a):
    kw = dict(kv.split("=", 1) for kv in a.arg)
    _out(context.tool(a.name, **kw))


def cmd_event(a):
    """Record an event's data, run watchdogs, and print which agents should wake. The orchestrator takes it from here."""
    from . import events
    try:
        _out(events.ingest(a.type, _read_json(a.json) if a.json else {}))
    except ValueError as e:
        sys.exit(str(e))


def cmd_propose(a):
    props = _proposals(_read_json(a.json))
    if a.dry_run:
        kept, notes = control.reconcile(props)
        _out({"reconcile_notes": notes, "preview": [{"proposal": p.summary(), "agent": p.agent,
                                                      "verdict": policy.check_action(p).model_dump()} for p in kept]})
    else:
        _out(control.submit_batch(props, trigger=a.trigger))


def cmd_ledger(a):
    rows = ledger.recent(a.limit, a.status)
    for d in rows:
        p = ledger.proposal_of(d)
        print(f"#{d['id']:<3} {d['ts'][:16]}  {d['status']:<17} {d['tier']:<12} {d['agent']:<12} {p.summary()}")
    if not rows:
        print("(empty)")


def cmd_watchdogs(a):
    ws = watchdogs.run_all()
    _out([{k: v for k, v in w.items() if k != "proposal"} for w in ws])
    if a.apply:
        for w in ws:
            if not watchdogs.already_open(w):
                _out(control.submit(w["proposal"], trigger="watchdog:manual"))


def cmd_kill(a):
    ledger.set_setting("kill_switch", a.state == "on")
    _out({"kill_switch": a.state == "on"})


def cmd_dial(a):
    ledger.set_setting("dial", a.mode)
    _out({"dial": a.mode})


def cmd_unfreeze(a):
    ledger.unfreeze(a.domain)
    if a.domain in ("training", "all"):
        ledger.set_setting("pain_active", False)
    _out({"frozen": ledger.frozen_domains()})


def cmd_scheduler(a):
    _out(scheduler.status() if a.action == "status" else scheduler.run_due())


def cmd_trace(a):
    from . import trace
    if not a.run_id:
        runs = trace.recent_runs(a.limit)
        if a.json:
            return _out(runs)
        for r in runs:
            print(f"{r['run_id']}  {r['started']}  {r['kind']}:{r['name']:<18} {r['dur_ms']:8.1f}ms  {r['outcome']:<8} spans={r['spans']} errors={r['errors']} tok={r['tokens_in']}/{r['tokens_out']}")
        return None
    tree = trace.run_tree(a.run_id)
    if not tree:
        print(f"no such run: {a.run_id}")
        sys.exit(1)
    _out(tree) if a.json else print(trace.render(tree))


def cmd_memory(a):
    if a.action == "curate":
        return _out(curator.run_nightly(use_llm=not a.offline))
    for m in memory.items(kind=a.kind, status=a.status, limit=a.limit):
        print(f"#{m['id']:<3} {m['kind']:<10} {memory.effective(m):.2f}  x{m['hits']}  {'[pinned] ' if m['pinned'] else ''}{m['body']}")


def cmd_experiments(a):
    if a.action == "list":
        return _out(experiments.listing(20))
    if a.action == "start":
        return _out(experiments.start(a.lift))
    if a.action == "evaluate":
        return _out({"activated": experiments.activate_pending(), "concluded": experiments.evaluate_due()})
    _out({"plateaus": __import__("backend.agents.specialists", fromlist=["x"]).analyst_report()["plateaus"], "confounders": experiments.confounders()})


def cmd_reflect(a):
    _out(reflection.run())


def cmd_demo(a):
    from backend.tools import demo
    if a.action == "reset":
        db.reset_all()
        ledger.set_setting("kill_switch", False); ledger.set_setting("frozen", []); ledger.set_setting("pain_active", False)
        with db.conn() as c:
            c.execute("DELETE FROM decision")
        _out({"reset": True})
    else:
        _out(demo.seed(a.weeks))


def cmd_eval(a):
    from . import evals
    rep = evals.run_all(a.model, a.only)
    for s in rep["scenarios"]:
        print(f"{'PASS' if s['passed'] else 'FAIL'}  {s['name']:<22} applied={s['applied']} blocked={s['blocked']} pending={s['pending']} escalated={s['escalated']}")
        for f in s["failures"]:
            print(f"        x {f}")
        for w in s["warnings"]:
            print(f"        ! {w}")
    print(f"\n{'ALL PASSED' if rep['passed'] else 'FAILED'} ({rep['model']} model)")
    sys.exit(0 if rep["passed"] else 1)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="fitness_os", description="Fitness Agent OS control plane")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    it = sub.add_parser("intake", help="Intake agent (read-only): what is missing, or a dry-run read of an answer"); it.add_argument("action", choices=["status", "preview"])
    it.add_argument("--text", default=""); it.add_argument("--slot"); it.set_defaults(fn=cmd_intake)
    rs = sub.add_parser("research", help="Research agent (read-only): cited answers from the local evidence library"); rs.add_argument("action", choices=["ask", "claim", "plan"])
    rs.add_argument("--text", default=""); rs.set_defaults(fn=cmd_research)
    fm = sub.add_parser("form", help="Form agent (read-only): technique cues from pose-keypoint JSON; says so when there is no video analysis")
    fm.add_argument("action", choices=["status", "analyze"]); fm.add_argument("--file", default=""); fm.add_argument("--exercise", default=None)
    fm.add_argument("--note", default=""); fm.set_defaults(fn=cmd_form)
    c = sub.add_parser("context"); c.add_argument("agent"); c.set_defaults(fn=cmd_context)
    t = sub.add_parser("tool"); t.add_argument("name", choices=context.TOOLS); t.add_argument("--arg", action="append", default=[]); t.set_defaults(fn=cmd_tool)
    e = sub.add_parser("event"); e.add_argument("type"); e.add_argument("--json"); e.set_defaults(fn=cmd_event)
    p = sub.add_parser("propose"); p.add_argument("json", help="JSON, a file path, or - for stdin")
    p.add_argument("--dry-run", action="store_true"); p.add_argument("--trigger", default="manual"); p.set_defaults(fn=cmd_propose)
    l = sub.add_parser("ledger"); l.add_argument("--limit", type=int, default=20); l.add_argument("--status"); l.set_defaults(fn=cmd_ledger)
    for name, fn in (("approve", control.approve), ("reject", control.reject), ("revert", control.revert)):
        x = sub.add_parser(name); x.add_argument("id", type=int); x.set_defaults(fn=lambda a, fn=fn: _out(fn(a.id)))
    w = sub.add_parser("why"); w.add_argument("id", type=int); w.set_defaults(fn=lambda a: print(control.why(a.id)))
    sub.add_parser("tick").set_defaults(fn=lambda a: _out(control.tick()))
    wd = sub.add_parser("watchdogs"); wd.add_argument("--apply", action="store_true"); wd.set_defaults(fn=cmd_watchdogs)
    k = sub.add_parser("kill"); k.add_argument("state", choices=["on", "off"]); k.set_defaults(fn=cmd_kill)
    d = sub.add_parser("dial"); d.add_argument("mode", choices=["hands_on", "balanced", "autopilot"]); d.set_defaults(fn=cmd_dial)
    u = sub.add_parser("unfreeze"); u.add_argument("domain"); u.set_defaults(fn=cmd_unfreeze)
    sc = sub.add_parser("scheduler"); sc.add_argument("action", choices=["status", "tick"]); sc.set_defaults(fn=cmd_scheduler)
    tr = sub.add_parser("trace", help="local run traces: recent runs, or one run's span tree"); tr.add_argument("run_id", nargs="?")
    tr.add_argument("--limit", type=int, default=20); tr.add_argument("--json", action="store_true"); tr.set_defaults(fn=cmd_trace)
    me = sub.add_parser("memory"); me.add_argument("action", choices=["list", "curate"]); me.add_argument("--kind"); me.add_argument("--status", default="active")
    me.add_argument("--limit", type=int, default=40); me.add_argument("--offline", action="store_true", help="curate without the LLM polish"); me.set_defaults(fn=cmd_memory)
    ex = sub.add_parser("experiments"); ex.add_argument("action", choices=["list", "detect", "start", "evaluate"]); ex.add_argument("--lift"); ex.set_defaults(fn=cmd_experiments)
    sub.add_parser("reflect").set_defaults(fn=cmd_reflect)
    ev = sub.add_parser("eval", help="replay golden scenarios + red-team against the loop"); ev.add_argument("--model", choices=["adversarial", "passive", "live"], default="adversarial")
    ev.add_argument("--only"); ev.set_defaults(fn=cmd_eval)
    dm = sub.add_parser("demo"); dm.add_argument("action", choices=["seed", "reset"]); dm.add_argument("--weeks", type=int, default=8); dm.set_defaults(fn=cmd_demo)
    return ap


def main(argv=None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    a = build_parser().parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
