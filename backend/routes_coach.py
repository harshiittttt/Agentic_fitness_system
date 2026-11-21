"""Coach chat with actions: the reply plus what the coach changed (with Undo), and the undo endpoint itself."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from backend.agents import orchestrator as orch

router = APIRouter()


class CoachChat(BaseModel):
    message: str


class CoachUndo(BaseModel):
    decision_id: int | None = None   # omitted = undo the latest change of the athlete's own


@router.post("/coach/chat")
def coach_chat(c: CoachChat, bg: BackgroundTasks):
    """{"reply": str, "actions": [{kind: changed|pushback|refused|undone, summary, decision_id, can_undo, ...}]}"""
    from backend.main import _run_agents_bg
    from backend.policy.adaptation import has_safety_flag
    if has_safety_flag(c.message):   # pain mentioned in chat is an event, not just a reply
        bg.add_task(_run_agents_bg, "pain.report", {"text": c.message})
    return orch.chat_with_actions(c.message)


@router.post("/coach/undo")
def coach_undo(u: CoachUndo):
    from fitness_os import coach_tools
    res = coach_tools.undo_decision(u.decision_id) if u.decision_id is not None else coach_tools.undo_last_change()
    if res.get("status") != "reverted":
        raise HTTPException(409, res.get("error") or "Nothing to undo")
    return res
