"""HTTP face of the Agent OS (L7 experience <-> L6 control plane). Mounted by backend/main.py under /agents."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.agents.base import have_key
from backend.memory import db

from . import agenda, context, control, curator, experiments, intake, ledger, loop, memory, prompts, research, scheduler, watchdogs
from .schema import Proposal

router = APIRouter(prefix="/agents", tags=["agent-os"])

_PENDING = ("pending_approval", "pending_veto")


class EventIn(BaseModel):
    type: str
    payload: dict = {}


def _decision(d: dict) -> dict:
    p = ledger.proposal_of(d)
    act = d.get("action") or {}
    if p.kind in ("volume_change", "calorie_change") and isinstance(act.get("fraction"), (int, float)):
        # show what will actually happen: the policy gate may have trimmed the proposed size (e.g. weekly load cap)
        p = p.model_copy(update={"params": {**p.params, "fraction": act["fraction"]}})
    return {"id": d["id"], "ts": d["ts"], "agent": d["agent"], "kind": d["kind"], "summary": p.summary(), "why": d["rationale"],
            "evidence": d["evidence"], "tier": d["tier"], "status": d["status"], "fired": d["fired"], "veto_until": d["veto_until"],
            "trigger": d["trigger_event"], "can_revert": d["status"] == "applied" and bool(d.get("undo")),
            "pending": d["status"] in _PENDING}


@router.get("/status")
def status():
    plan = db.get_plan() or {}
    return {"llm": have_key(), "agents": sorted(prompts.ROLES), "offline_agents": sorted(loop.OFFLINE), "intake": intake.status(), "research": research.status(), "has_plan": bool(plan), "designed_by": plan.get("designed_by", "starter-template" if plan else None),
            "kill_switch": ledger.kill_switch(), "dial": ledger.dial(), "frozen": ledger.frozen_domains(),
            "pending": [_decision(d) for d in ledger.recent(50) if d["status"] in _PENDING],
            "watchdog_flags": [{k: v for k, v in w.items() if k != "proposal"} for w in watchdogs.run_all()]}


class IntakeIn(BaseModel):
    text: str
    slot: str | None = None


class AskIn(BaseModel):
    question: str


class ScreenIn(BaseModel):
    claim: str | None = None


@router.get("/intake")
def intake_status():
    return intake.status()


@router.post("/intake/answer")
def intake_answer(a: IntakeIn):
    return intake.answer(a.text, a.slot)


@router.post("/research/ask")
def research_ask(a: AskIn):
    return research.ask(a.question)


@router.post("/research/screen")
def research_screen(s: ScreenIn):
    return research.screen_claim(s.claim) if s.claim else research.screen_plan()


class FormIn(BaseModel):
    keypoints: dict | None = None
    frames_b64: list[str] | None = None
    exercise: str | None = None
    note: str = ""
    submit: bool = False


@router.get("/form")
def form_status():
    from . import form
    return form.status()


@router.post("/form/analyze")
def form_analyze(f: FormIn):
    """Technique cues from pose keypoints (always) or a few frames (needs a model key). Read-only unless `submit`, which sends the note/nudge
    Proposals through the usual critic and policy gate; pain language instead routes to the recovery path (pain.report)."""
    import json
    from . import form
    if f.keypoints is not None and len(json.dumps(f.keypoints)) > form.MAX_KEYPOINT_CHARS:
        raise HTTPException(413, "keypoint data is too large")
    try:
        res = form.analyze(f.keypoints, f.frames_b64, f.exercise, f.note)
    except form.FormInputError as ex:
        raise HTTPException(400, str(ex)[:200])
    if f.submit:
        if res["escalation"]:
            res["run"] = loop.run_event("pain.report", {"text": f.note[:500]})
        elif res["proposals"]:
            res["run"] = loop.run_event("form.review", {"keypoints": f.keypoints, "frames_b64": f.frames_b64, "exercise": f.exercise, "note": f.note})
    return res


@router.post("/event")
def event(e: EventIn):
    if e.type not in context.ROUTES:
        raise HTTPException(400, f"unknown event; choose from {sorted(context.ROUTES)}")
    try:
        return loop.run_event(e.type, e.payload)
    except (ValueError, TypeError) as ex:   # e.g. a malformed calendar event
        raise HTTPException(400, str(ex)[:200])


class AgendaIn(BaseModel):
    kind: str
    start: str
    end: str | None = None
    label: str = ""
    minutes: int | None = None
    equipment: list[str] | None = None


@router.get("/agenda")
def agenda_list():
    return {"events": agenda.upcoming(365)}


@router.post("/agenda")
def agenda_add(e: AgendaIn, run: bool = True):
    """Add a calendar event (competition, trip, no_gym, busy, equipment) and let Periodization / Logistics react through the usual gate."""
    try:
        ev = agenda.add(**e.model_dump())
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    return {"event": ev, "run": loop.run_event("calendar.changed", {}, record=False) if run else None}


@router.delete("/agenda/{event_id}")
def agenda_remove(event_id: int):
    if not agenda.remove(event_id):
        raise HTTPException(404, "no such event")
    return {"removed": event_id}


@router.get("/decisions")
def decisions(limit: int = 30, status: str | None = None):
    return [_decision(d) for d in ledger.recent(min(limit, 100), status)]


@router.get("/decisions/{decision_id}/why")
def why(decision_id: int):
    return {"id": decision_id, "text": control.why(decision_id)}


def _act(fn, decision_id: int):
    out = fn(decision_id)
    if "error" in out:
        raise HTTPException(400, out["error"])
    return out


@router.post("/decisions/{decision_id}/approve")
def approve(decision_id: int):
    return _act(control.approve, decision_id)


@router.post("/decisions/{decision_id}/reject")
def reject(decision_id: int):
    return _act(control.reject, decision_id)


@router.post("/decisions/{decision_id}/revert")
def revert(decision_id: int):
    return _act(control.revert, decision_id)


class Dial(BaseModel):
    mode: str


@router.post("/dial")
def set_dial(d: Dial):
    if d.mode not in ("hands_on", "balanced", "autopilot"):
        raise HTTPException(400, "mode must be hands_on, balanced or autopilot")
    ledger.set_setting("dial", d.mode)
    return {"dial": d.mode}


@router.post("/kill/{state}")
def kill(state: str):
    if state not in ("on", "off"):
        raise HTTPException(400, "state must be on or off")
    ledger.set_setting("kill_switch", state == "on")
    return {"kill_switch": state == "on"}


# ---- memory, curator and scheduler ----
@router.get("/memory")
def memory_list(status: str = "active", kind: str | None = None, limit: int = 60):
    return [{**m, "strength": round(memory.effective(m), 2)} for m in memory.items(kind=kind, status=status, limit=min(limit, 200))]


@router.post("/memory/curate")
def memory_curate():
    return curator.run_nightly()


@router.post("/memory/{item_id}/resolve")
def memory_resolve(item_id: int):
    """The athlete answered a flagged question (e.g. 'you said vegetarian but logged chicken')."""
    if not memory.set_status(item_id, "resolved"):
        raise HTTPException(404, "No such memory")
    return {"id": item_id, "status": "resolved"}


@router.get("/trace")
def trace_runs(run: str | None = None, limit: int = 20):
    """Local traces. No `run`: the most recent runs. `?run=<id>` (or /trace/<id>): that run's span tree. Hashes and lengths only, never raw text."""
    from . import trace
    if run:
        tree = trace.run_tree(run)
        if not tree:
            raise HTTPException(404, "no such run")
        return tree
    return {"enabled": trace.enabled(), "runs": trace.recent_runs(limit)}


@router.get("/trace/{run_id}")
def trace_run(run_id: str):
    return trace_runs(run=run_id)


@router.get("/scheduler")
def scheduler_status():
    from . import runner
    return {**scheduler.status(), "runner": runner.status()}


@router.post("/scheduler/tick")
def scheduler_tick():
    """One scheduler pass: veto windows, reflection on past changes, and any job that is due. Idempotent."""
    return scheduler.run_due()


@router.get("/experiments")
def experiments_list(limit: int = 20):
    return experiments.listing(min(limit, 100))


class RoundtableIn(BaseModel):
    question: str
    addressed: list[str] = []


@router.post("/roundtable")
def roundtable_call(r: RoundtableIn):
    """Ask the whole team: the Moderator invites 2-3 specialists, they answer, disagree at most twice, and one verdict comes back."""
    from . import roundtable
    try:
        return roundtable.run(r.question, r.addressed or None)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/metrics")
def metrics_report():
    """Success metrics: adherence, goal progress, autonomy acceptance vs override, safety false negatives (target zero), disruptions, cost."""
    from . import metrics
    return metrics.report()


@router.get("/export")
def export_data():
    """Everything stored about you, as JSON (every table, nothing summarised). The export is recorded in the access log."""
    from . import privacy
    return privacy.export_all()


class DeleteIn(BaseModel):
    confirm: str


@router.post("/data/delete")
def delete_data(d: DeleteIn):
    """Delete everything. Requires the exact phrase DELETE EVERYTHING; irreversible."""
    from . import privacy
    try:
        return privacy.delete_all(d.confirm)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/access-log")
def access_log_view(limit: int = 50):
    from . import privacy
    return privacy.access_log(min(limit, 200))


@router.get("/trust")
def trust_report():
    """How much autonomy each kind of change has earned (accepted vs pushed back), and the new-types-stricter switch."""
    from . import trust
    return {"types": trust.report(), "strict_new_types": bool(ledger.get_setting("strict_new_types", False))}


class StrictIn(BaseModel):
    on: bool


@router.post("/trust/strict-new-types")
def trust_strict(s: StrictIn):
    ledger.set_setting("strict_new_types", s.on)
    return {"strict_new_types": s.on}


@router.get("/nudges")
def nudges(unread_only: bool = True):
    """Messages the team chose to send you (quiet hours and the frequency cap are enforced in code)."""
    from . import comms
    return comms.unread() if unread_only else comms.recent(30)


@router.post("/nudges/{nudge_id}/read")
def nudge_read(nudge_id: int):
    from . import comms
    if not comms.mark_read(nudge_id):
        raise HTTPException(404, "No such message")
    return {"id": nudge_id, "status": "read"}
