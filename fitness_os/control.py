"""Control plane: reconcile -> policy gate -> tier handling -> ledger. The only code path that writes state."""
from __future__ import annotations

from backend.agents.orchestrator import COOLDOWN_DAYS, REVERSAL_DAYS, STRONG_CHANGE
from backend.memory import db

from . import clock, executor, ledger, policy
from .schema import Proposal

# Authority when two agents disagree about the same domain: safety first.
AUTHORITY = {"watchdog": 5, "safety_critic": 5, "recovery": 4, "orchestrator": 3, "training": 2, "nutrition": 2, "progress": 1, "injury": 4, "sleep": 3, "adherence": 1, "periodization": 2, "logistics": 2, "research": 1, "intake": 1, "form": 1}


def reconcile(proposals: list[Proposal]) -> tuple[list[Proposal], list[str]]:
    """Deterministic conflict resolution (the Moderator's tiebreak rule). Returns (kept, notes)."""
    notes: list[str] = []
    kept = list(proposals)
    frozen = ledger.frozen_domains()
    for p in [p for p in kept if p.kind == "escalate" and (p.params.get("domain", "training") in frozen or "all" in frozen)]:
        kept.remove(p)
        notes.append(f"Dropped a repeat escalation: {p.params.get('domain', 'training')} is already frozen.")
    vols = [p for p in kept if p.kind == "volume_change"]
    if len(vols) > 1:
        # a cut from anyone beats an increase; among same-direction, higher authority then larger confidence wins
        cuts = [p for p in vols if p.params.get("fraction", 0) < 0]
        pool = cuts or vols
        win = max(pool, key=lambda p: (AUTHORITY.get(p.agent, 0), -abs(p.params.get("fraction", 0)) if cuts else p.confidence))
        for p in vols:
            if p is not win:
                kept.remove(p)
                notes.append(f"Dropped {p.agent}'s volume proposal ({p.summary()}) in favour of {win.agent}'s ({win.summary()}).")
    # Soft hysteresis from the existing adaptation policy: small non-safety nudges wait for the last change to settle.
    plan = db.get_plan() or {}
    for p in [p for p in kept if p.kind == "volume_change" and p.agent in ("training", "orchestrator")]:
        frac = p.params.get("fraction", 0)
        if frac == 0:
            kept.remove(p)
            notes.append("Dropped a zero-size volume proposal.")
            continue
        if abs(frac) >= STRONG_CHANGE:
            continue
        since = db.days_since_last_change(("training", "recovery"))
        last_dir, last_ts = plan.get("last_vol_dir", 0), plan.get("last_vol_ts")
        if since < COOLDOWN_DAYS:
            kept.remove(p)
            notes.append("Held a small volume nudge: the last change is still settling.")
        elif last_dir and last_ts and (frac > 0) != (last_dir > 0) and db.age_days(last_ts) < REVERSAL_DAYS:
            kept.remove(p)
            notes.append("Held a small volume nudge: not reversing the last change on weak evidence.")
    from . import experiments  # lazy: experiments imports control
    if experiments.held_domains():
        for p in [p for p in kept if p.agent in ("training", "nutrition", "orchestrator", "periodization") and p.kind in ("volume_change", "swap_exercise", "new_block")]:
            kept.remove(p)
            notes.append(f"Held {p.agent}'s {p.summary()}: a plateau experiment is running, and a second change would confound it.")
    return kept, notes


def _safe_apply(p: Proposal, params: dict):
    """executor.apply, but a failure becomes (None, None, message) so one bad proposal can never crash the control plane."""
    try:
        action, undo = executor.apply(p.kind, params, p.agent, p.rationale, p.evidence)
        return action, undo, None
    except (ValueError, KeyError, TypeError, IndexError) as e:
        return None, None, str(e)[:200]


def _finish(p: Proposal, verdict, trigger: str, *, force_tier: str | None = None) -> dict:
    tier = force_tier or verdict.tier
    base = dict(agent=p.agent, kind=p.kind, proposal=p.model_dump(), rationale=p.rationale, evidence=p.evidence,
                tier=tier, fired=verdict.fired, trigger=trigger)
    if verdict.verdict == "block":
        did = ledger.append(action=None, status="blocked", **{**base, "tier": verdict.tier})
        return {"id": did, "status": "blocked", "tier": verdict.tier, "fired": verdict.fired, "explain": verdict.explain}
    if tier == "approve":
        did = ledger.append(action=verdict.params, status="pending_approval", **base)
        return {"id": did, "status": "pending_approval", "tier": tier, "fired": verdict.fired,
                "explain": verdict.explain, "ask": f"{p.summary()}. {p.rationale}"}
    if tier == "notify_first":
        until = clock.plus(minutes=verdict.veto_window_minutes or policy.VETO_MINUTES)
        did = ledger.append(action=verdict.params, status="pending_veto", veto_until=until, **base)
        return {"id": did, "status": "pending_veto", "tier": tier, "veto_until": until, "fired": verdict.fired,
                "explain": verdict.explain}
    # auto and escalate both apply now (escalation freezes the domain)
    action, undo, failed = _safe_apply(p, verdict.params)
    if failed:
        did = ledger.append(action=None, status="blocked", **{**base, "tier": verdict.tier, "fired": [*verdict.fired, "apply_failed"]})
        return {"id": did, "status": "blocked", "tier": verdict.tier, "fired": [*verdict.fired, "apply_failed"], "explain": failed}
    status = "escalated" if tier == "escalate" else "applied"
    did = ledger.append(action=action, status=status, undo=undo, **base)
    return {"id": did, "status": status, "tier": tier, "fired": verdict.fired, "explain": verdict.explain, "action": action}


def submit(p: Proposal, trigger: str = "manual") -> dict:
    return _finish(p, policy.check_action(p), trigger)


def submit_batch(proposals: list[Proposal], trigger: str = "manual") -> dict:
    kept, notes = reconcile(proposals)
    return {"reconcile_notes": notes, "results": [submit(p, trigger) for p in kept]}


def tick() -> list[dict]:
    """Apply notify-first actions whose veto window has passed, re-checking policy against current state."""
    out = []
    for d in ledger.recent(200, "pending_veto"):
        if d["veto_until"] and clock.now() >= clock.parse(d["veto_until"]):
            p = Proposal(**d["proposal"])
            v = policy.check_action(p)
            if v.verdict == "block":
                ledger.update(d["id"], status="vetoed", outcome={"reason": "policy blocked at apply time", "fired": v.fired},
                              resolved_ts=clock.iso())
                out.append({"id": d["id"], "status": "vetoed", "fired": v.fired})
                continue
            action, undo, failed = _safe_apply(p, v.params)
            if failed:
                ledger.update(d["id"], status="vetoed", outcome={"reason": f"could not apply: {failed}"}, resolved_ts=clock.iso())
                out.append({"id": d["id"], "status": "vetoed", "fired": ["apply_failed"]})
                continue
            ledger.update(d["id"], status="applied", action=action, undo=undo, resolved_ts=clock.iso())
            out.append({"id": d["id"], "status": "applied", "action": action})
    return out


def approve(decision_id: int) -> dict:
    d = ledger.get(decision_id)
    if not d or d["status"] not in ("pending_approval", "pending_veto"):
        return {"error": f"Decision {decision_id} is not waiting for approval."}
    p = Proposal(**d["proposal"])
    v = policy.check_action(p)
    if v.verdict == "block":
        ledger.update(decision_id, status="blocked", resolved_ts=clock.iso(), outcome={"fired": v.fired, "why": v.explain})
        return {"id": decision_id, "status": "blocked", "fired": v.fired, "explain": v.explain}
    action, undo, failed = _safe_apply(p, v.params)
    if failed:
        ledger.update(decision_id, status="blocked", resolved_ts=clock.iso(), outcome={"fired": ["apply_failed"], "why": failed})
        return {"id": decision_id, "status": "blocked", "fired": ["apply_failed"], "explain": failed}
    ledger.update(decision_id, status="applied", action=action, undo=undo, resolved_ts=clock.iso())
    return {"id": decision_id, "status": "applied", "action": action}


def reject(decision_id: int) -> dict:
    d = ledger.get(decision_id)
    if not d or d["status"] not in ("pending_approval", "pending_veto"):
        return {"error": f"Decision {decision_id} is not pending."}
    status = "vetoed" if d["status"] == "pending_veto" else "rejected"
    ledger.update(decision_id, status=status, resolved_ts=clock.iso())
    return {"id": decision_id, "status": status}


def revert(decision_id: int) -> dict:
    d = ledger.get(decision_id)
    if not d:
        return {"error": "No such decision."}
    if d["status"] == "escalated":
        return {"error": "Escalations are not reverted automatically; a professional must clear them (use `unfreeze`)."}
    if d["status"] != "applied" or not d.get("undo"):
        return {"error": f"Decision {decision_id} is {d['status']}; nothing to revert."}
    before = db.get_plan() if d["undo"].get("structural") else None
    msg = executor.undo(d["undo"])
    ledger.update(decision_id, status="reverted", resolved_ts=clock.iso())
    if d["undo"].get("structural"):
        _split_changed(before, db.get_plan())
    ledger.append(agent="you", kind="note", proposal={"agent": "you", "kind": "note", "params": {"text": f"Undid decision {decision_id}: {msg}"[:200], "revert_of": decision_id}, "rationale": f"Reverted decision {decision_id}", "evidence": msg},
                  action={"reverted": decision_id},
                  rationale=f"Reverted decision {decision_id}", evidence=msg, tier="auto", fired=[], status="applied")
    return {"id": decision_id, "status": "reverted", "result": msg}


def why(decision_id: int) -> str:
    d = ledger.get(decision_id)
    if not d:
        return "No such decision."
    p = ledger.proposal_of(d)
    lines = [f"#{d['id']} {d['ts']} by {d['agent']} ({d['trigger_event']}): {p.summary()}",
             f"Why: {d['rationale']}", f"Evidence: {d['evidence'] or 'n/a'}",
             f"Gate: tier {d['tier']}, status {d['status']}" + (f", limits fired: {', '.join(d['fired'])}" if d["fired"] else "")]
    if d.get("outcome"):
        lines.append(f"Outcome: {d['outcome']}")
    return "\n".join(lines)


# ---- the athlete's own changes (chat/live coach tools and anything else acting on their behalf) ----
def _split_changed(old_plan, new_plan) -> None:
    """Contract with session_flow: ANY structural split change must call this so 'which day is next' stays sensible."""
    try:
        from backend.coach import session_flow
        session_flow.on_split_changed(old_plan, new_plan)
    except Exception:
        pass   # keeping the rotation tidy must never undo a change the athlete asked for


def _blocked_row(op: str, params: dict, agent: str, words: str, why: str, fired: list[str]) -> int:
    return ledger.append(agent=agent, kind="user_change", proposal={"agent": agent, "kind": "user_change", "params": {"op": op, **params}, "rationale": (words or "Your request")[:300]},
                         action=None, rationale=(words or "Your request")[:300], evidence=why[:300], tier="user", fired=fired, status="blocked", trigger="user.request")


def submit_user_change(op: str, params: dict | None = None, *, words: str = "", agent: str = "coach", turn: int | None = None, debate: bool = True) -> dict:
    """The ONE path for a change the athlete asked for. Hard limits are enforced here and in user_ops/split_ops, the one-round debate
    (fitness_os/debate.py) runs here, and every applied change gets a ledger row (tier 'user', their words as the rationale, an undo recipe).

    -> {"status": "applied" | "pushback" | "wait" | "refused" | "blocked" | "invalid", ...}
    """
    from backend.tools.split_ops import OpError
    from . import debate as dbt, user_ops

    params = dict(params or {})
    if op not in user_ops.OPS:
        return {"status": "invalid", "error": f"Unknown change '{op}'."}
    if agent == "coach" and ledger.kill_switch():
        _blocked_row(op, params, agent, words, "kill switch on", ["kill_switch"])
        return {"status": "refused", "rule": "kill_switch", "reasons": ["The coach's ability to change things is switched off (kill switch). Turn it off in the Team screen, or make the change yourself."]}
    try:
        prep = user_ops.prepare(op, params)
    except OpError as e:
        if e.kind == "refused":
            did = _blocked_row(op, params, agent, words, str(e), [e.rule or "hard_limit"])
            return {"status": "refused", "rule": e.rule, "reasons": [str(e)], "decision_id": did}
        return {"status": "invalid", "error": str(e)}
    frozen = ledger.frozen_domains()
    if prep.domain and (prep.domain in frozen or "all" in frozen):
        why = f"The {prep.domain} side is frozen pending professional review (a safety escalation). I cannot change it; please see a qualified professional."
        did = _blocked_row(op, params, agent, words, why, ["domain_frozen"])
        return {"status": "refused", "rule": "domain_frozen", "reasons": [why], "professional": True, "decision_id": did}

    override = False
    key = dbt.change_key(op, params, prep)
    if debate:
        found = dbt.concerns(prep, params)
        g = dbt.gate(key, op, params, found, turn)
        if g.status in ("pushback", "wait"):
            first = g.concerns[0]
            return {"status": g.status, "change_key": key, "rules": [c.rule for c in g.concerns], "reasons": [c.reason for c in g.concerns],
                    "evidence": [c.evidence for c in g.concerns], "alternative": first.alternative, "would_have": prep.summary}
        override = g.override
    try:
        undo = executor.apply_user(prep)
    except OpError as e:
        return {"status": "refused" if e.kind == "refused" else "invalid", "reasons": [str(e)], "error": str(e), "rule": e.rule}
    except (ValueError, KeyError, TypeError, IndexError) as e:
        return {"status": "invalid", "error": str(e)[:200]}
    evidence = "; ".join(prep.changes)[:600] or prep.summary
    fired = ["user_override"] if override else []
    did = ledger.append(
        agent=agent, kind="user_change",
        proposal={"agent": agent, "kind": "user_change", "params": {"op": op, "summary": prep.summary, "user_override": override, **{k: v for k, v in params.items() if k != "days"}},
                  "rationale": (words or "Your request")[:300], "evidence": evidence},
        action={"op": op, "changes": prep.changes, "user_override": override}, rationale=(words or "Your request")[:300], evidence=evidence,
        tier="user", fired=fired, status="applied", trigger="user.request", undo=undo)
    if prep.structural:
        _split_changed(prep.old_plan, db.get_plan())
    plan = db.get_plan() or {}
    return {"status": "applied", "decision_id": did, "summary": prep.summary, "changes": prep.changes, "user_override": override,
            "structural": prep.structural, "plan_version": plan.get("version"), "facts": {k: v for k, v in prep.facts.items() if k in ("conflicts", "unknown")},
            "undo": f"The athlete can undo this (decision {did}): tap Undo or ask you to undo it."}
