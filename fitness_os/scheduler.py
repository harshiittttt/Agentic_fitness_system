"""Durable-style scheduler (docs: 'Each runs as a durable workflow, so a restart in the middle resumes where it stopped').

No external service: every job's state lives in the ledger settings table and a job is due when the MOST RECENT scheduled
moment (e.g. today 05:30) has not been run yet. So a laptop that was off at 05:30 catches up at the next tick, a job never
runs twice for the same moment, and a failing job backs off instead of hammering the model.

    cron.daily   05:30 local      Recovery / Training / Nutrition look at today (readiness-driven adjustment)
    cron.weekly  Sunday 18:00     weekly review across all four agents
    curator      02:00 local      nightly memory maintenance for yesterday
    reflect      every tick       score changes whose horizon has passed (cheap, no model)
    sessions     every tick       close workouts that have sets but were never ended (session_flow.close_stale)
    veto windows every tick       apply notify-first actions the athlete did not stop
    disruption   every tick       sick / travel / missed days -> degraded mode, then a gradual return ramp
    proactive    17:00 local      one check-in message if the athlete went quiet (quiet hours + frequency cap enforced in policy)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.agents.base import have_key

from . import clock, control, curator, experiments, ledger, loop, reflection

BACKOFF_MINUTES = 20
AGENT_JOBS = ("cron.daily", "cron.weekly")   # run the agent loop; with no LLM key they still run in OFFLINE mode (deterministic agents + offline critic + policy gate)
SCHEDULE = {  # name -> (hour, minute, weekday or None)   weekday: Monday=0 .. Sunday=6
    "cron.daily": (5, 30, None),
    "cron.weekly": (18, 0, 6),
    "curator": (2, 0, None),
    "memory.nightly": (3, 30, None),   # consolidate, re-embed, mine patterns, refresh the athlete model (fitness_os/mem/jobs.py)
    "memory.weekly": (4, 0, 6),        # Sunday: full re-test of every pattern, the weekly "what I learned about you"
    "proactive": (17, 0, None),     # early evening, outside quiet hours: one check-in if the athlete went quiet
}


def athlete_now() -> datetime:
    """The athlete's wall clock. `tz_offset_min` setting overrides; tests (FITNESS_NOW) use UTC; otherwise the machine's zone."""
    off = ledger.get_setting("tz_offset_min")
    if off is None:
        import os
        off = 0 if os.environ.get("FITNESS_NOW") else int((datetime.now().astimezone().utcoffset() or timedelta(0)).total_seconds() // 60)
    return clock.now().astimezone(timezone(timedelta(minutes=int(off))))


def last_occurrence(now: datetime, hour: int, minute: int, weekday: int | None) -> datetime:
    t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if weekday is None:
        return t if t <= now else t - timedelta(days=1)
    t -= timedelta(days=(t.weekday() - weekday) % 7)
    return t if t <= now else t - timedelta(days=7)


def _state(name: str) -> dict:
    return ledger.get_setting(f"job:{name}", {}) or {}


def _save(name: str, **kw) -> None:
    ledger.set_setting(f"job:{name}", {**_state(name), **kw})


def job_key(name: str) -> str:
    h, m, wd = SCHEDULE[name]
    return last_occurrence(athlete_now(), h, m, wd).date().isoformat()


def is_due(name: str) -> bool:
    st = _state(name)
    if st.get("last_key") == job_key(name):
        # ran offline (no key) for this moment: once a key exists the full run is owed ONCE (it is marked mode=llm afterwards)
        return bool(st.get("mode") == "offline" and name in AGENT_JOBS and have_key())
    la = st.get("last_attempt")
    if st.get("failures", 0) and la and (clock.now() - clock.parse(la)) < timedelta(minutes=BACKOFF_MINUTES):
        return False  # backing off after a failure
    return True


def status() -> dict:
    now = athlete_now()
    jobs = {}
    for name, (h, m, wd) in SCHEDULE.items():
        st = _state(name)
        jobs[name] = {"due": is_due(name), "last_run": st.get("last_run"), "last_result": st.get("last_result"), "failures": st.get("failures", 0),
                      **({"mode": st.get("mode")} if name in AGENT_JOBS else {}),
                      "schedule": f"{h:02d}:{m:02d}" + (f" on {['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][wd]}" if wd is not None else " daily")}
    from . import context
    key = have_key()
    return {"athlete_time": now.isoformat(timespec="minutes"), "llm": key, "mode": "llm" if key else "offline", "jobs": jobs,
            "offline": {"jobs": list(AGENT_JOBS), "runs_offline_agents": sorted(loop.OFFLINE),
                        "skipped_llm_agents": {} if key else {j: [a for a in context.ROUTES[j] if a not in loop.OFFLINE] for j in AGENT_JOBS}},
            "reflections_waiting": len(reflection.due()), "kill_switch": ledger.kill_switch()}


def _run(name: str) -> dict:
    if name in ("cron.daily", "cron.weekly"):
        from . import context
        key = have_key()   # no key: the loop's OFFLINE path runs (periodization / logistics / ... + offline critic + policy gate); LLM agents are skipped
        r = loop.run_event(name, {})
        exp = experiments.maybe_start() if name == "cron.weekly" and key else None   # experiments are chosen by the LLM-backed review
        skipped = [] if key else [a for a in context.ROUTES.get(name, []) if a not in loop.OFFLINE]
        return {"mode": "llm" if key else "offline", **({"skipped_llm_agents": skipped} if skipped else {}),
                **({"experiment": exp} if exp else {}), "applied": sum(1 for x in r["results"] if x["status"] == "applied"), "proposals": len(r["results"]),
                "agent_errors": len([e for e in r["errors"] if not e.startswith("No LLM key")])}
    if name.startswith("memory."):
        from .mem import jobs
        rep = jobs.nightly() if name == "memory.nightly" else jobs.weekly()
        return {"steps": len(rep), "errors": [k for k, v in rep.items() if isinstance(v, dict) and v.get("error")], "report": rep}
    if name == "proactive":
        from . import disruption
        return disruption.proactive()
    if name == "curator":
        r = curator.run_nightly()
        return {"summarised": bool(r["summary"]), "deduped": r["deduped"], "decayed": r["decayed"], "flagged": len(r["flagged"])}
    raise ValueError(name)


def run_due(only: str | None = None) -> dict:
    """One scheduler tick: veto windows, reflection, then any due job. Safe to call as often as you like."""
    report: dict = {"at": clock.iso(), "veto_windows_closed": control.tick(), "reflected": reflection.run(), "jobs": {},
                    "experiments": {"activated": experiments.activate_pending(), "concluded": experiments.evaluate_due()}}
    try:   # safety net: a workout with sets that nobody ended is closed (memory, next targets, agent loop) on a later tick
        from backend.coach import session_flow
        report["closed_sessions"] = session_flow.close_stale()
    except Exception as e:
        report["closed_sessions"] = {"error": str(e)[:120]}
    try:   # disruption mode (sick / travel / missed days): cheap deterministic detection every tick, enters once, ramps back gradually
        from . import disruption
        report["disruption"] = disruption.tick()
    except Exception as e:
        report["disruption"] = {"error": str(e)[:120]}
    try:   # push receipts: no network unless a ticket is >= 15 min old; prunes a token the OS says is gone
        from . import push
        rc = push.check_receipts()
        if rc.get("checked") or rc.get("pruned"):
            report["push_receipts"] = rc
    except Exception as e:
        report["push_receipts"] = {"error": str(e)[:120]}
    try:   # decisions waiting for the athlete's OK -> one push per tick, each once (no token / opt-out: no-op, no network)
        from . import push
        pp = push.notify_pending_decisions()
        if pp.get("sent") or pp.get("error"):
            report["push_pending"] = pp
    except Exception as e:
        report["push_pending"] = {"error": str(e)[:120]}
    for name in SCHEDULE:
        if only and name != only:
            continue
        if not is_due(name):
            continue
        try:
            res = _run(name)
        except Exception as e:  # one broken job must never stop the others
            _save(name, last_attempt=clock.iso(), failures=_state(name).get("failures", 0) + 1, last_result={"error": str(e)[:160]})
            report["jobs"][name] = {"error": str(e)[:160]}
            continue
        if res.get("skipped"):
            report["jobs"][name] = res
            continue
        _save(name, last_run=clock.iso(), last_attempt=clock.iso(), last_key=job_key(name), failures=0, last_result=res, mode=res.get("mode", "llm"))
        report["jobs"][name] = res
    return report
