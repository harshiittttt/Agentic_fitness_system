"""The control loop from the docs (section 05), in-process:

    event -> watchdogs -> agents propose (parallel) -> reconcile -> safety critic -> policy gate -> tier -> ledger

Same shape for every event, so it is testable. The LLM only proposes; control.py is the only code path that writes state.
"""
from __future__ import annotations

from backend.agents.base import have_key

from . import control, critic, events, form, intake, logistics, periodization, research, runtime, trace

# Agents with a deterministic half: they still work with no model (and stand in if the model call fails).
OFFLINE = {"periodization": periodization.propose, "logistics": logistics.propose,
           "intake": intake.propose, "research": research.propose, "form": form.propose}


def _traced_offline(fn):
    def wrapper(agent, event, payload, why=None):
        with trace.span(agent, kind="agent", event=event, mode="offline") as sp:
            r = fn(agent, event, payload, why)
            sp.set(outcome="error" if r.get("error") else "ok", proposals=len(r.get("proposals") or []))
            return r
    return wrapper


def _submit(p, event: str) -> dict:
    """The policy gate + ledger write for one proposal, traced with the tier / status it ended in."""
    with trace.span("policy.gate", kind="policy", agent=p.agent, op=p.kind, trigger=event) as sp:
        r = control.submit(p, trigger=event)
        sp.set(outcome=str(r.get("status") or "ok"), status=r.get("status"), tier=r.get("tier"))
        return r


@_traced_offline
def _offline(agent: str, event: str, payload: dict, why: str | None = None) -> dict:
    """Run an agent's deterministic fallback; never raises (a failure is a first-class path, like a model error)."""
    try:
        r = OFFLINE[agent](event, payload)
    except Exception as e:
        return {"agent": agent, "reasoning": "", "proposals": [], "dropped": [], "error": f"offline {agent}: {type(e).__name__}: {str(e)[:120]}"}
    r["offline"] = True
    if why:
        r["reasoning"] = (r.get("reasoning") or "").strip()   # the model failed; the rule-based answer stands in (the error stays in `errors` via the report)
        r["error"] = f"model failed, used the rule-based {agent} agent: {why}"[:300]
    return r


def run_event(event_type: str, payload: dict | None = None, *, use_critic: bool = True, record: bool = True) -> dict:
    with trace.span(event_type, kind="event", event=event_type, payload=payload or {}, llm=have_key()) as sp:
        report = _run_event(event_type, payload, use_critic=use_critic, record=record)
        sp.set(outcome="ok" if not report.get("errors") else "partial", results=len(report.get("results") or []), errors=len(report.get("errors") or []))
        return report


def _run_event(event_type: str, payload: dict | None = None, *, use_critic: bool = True, record: bool = True) -> dict:
    payload = payload or {}
    ing = events.ingest(event_type, payload, record=record)  # records data, runs watchdogs, closes veto windows
    report = {**ing, "agents": {}, "reconcile_notes": [], "critic": None, "results": [], "errors": [], "llm": have_key()}
    offline_only = not have_key()
    if offline_only:
        report["errors"].append("No LLM key: model agents did not run. Watchdogs, the deterministic agents and the policy gate still ran.")
        runs = [_offline(a, event_type, payload) for a in ing["wake"] if a in OFFLINE]
        if not runs:
            return report
    else:
        runs = runtime.run_agents(ing["wake"], event_type, payload)
        runs = [_offline(r["agent"], event_type, payload, why=r["error"]) if r.get("error") and r["agent"] in OFFLINE else r for r in runs]
        runs += [_offline(a, event_type, payload) for a in ing["wake"] if a in OFFLINE and a not in runtime.prompts.ROLES]  # code-first agents (intake, research) run with a key too
    merged = []
    for r in runs:
        report["agents"][r["agent"]] = {"reasoning": r["reasoning"], "proposals": [p.summary() for p in r["proposals"]],
                                        "dropped": r["dropped"]}
        if r.get("error"):
            report["errors"].append(f"{r['agent']}: {r['error']}")  # failure is a first-class path: others still count
        merged += r["proposals"]

    kept, notes = control.reconcile(merged)
    report["reconcile_notes"] = notes
    if use_critic:
        with trace.span("critic", kind="critic", mode="offline" if offline_only else "llm", proposals=len(kept)) as csp:
            rv = (critic.review_offline if offline_only else critic.review)(kept, event_type, notes)
            csp.set(outcome="ok" if rv.get("critic_ok", True) else "degraded", held=len(rv.get("held") or []))
        report["critic"] = {k: rv[k] for k in ("assessment", "verdicts", "missed_flags", "critic_ok", "held")}
        kept = rv["proposals"]
    report["results"] = [{"proposal": p.summary(), "agent": p.agent, **_submit(p, event_type)} for p in kept]
    return report
