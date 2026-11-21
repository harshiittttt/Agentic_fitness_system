"""What the coach remembers about you, and where you are in the split. Read-mostly; lets the app (or you) see and delete learned facts."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend.coach import session_flow
from backend.memory import db
from fitness_os import conversation, learn

router = APIRouter()


@router.get("/coach/memory")
def coach_memory():
    plan = db.get_plan() or {}
    return {"facts": learn.facts(40), "split": session_flow.status(), "next_targets": plan.get("next_targets") or {},
            "last_session": (session_flow.last_session() or {}).get("result"),
            "recent_turns": len(conversation.history(limit=50))}


@router.delete("/coach/memory/{item_id}")
def forget_fact(item_id: int):
    if not learn.forget(item_id):
        raise HTTPException(404, "No such learned fact")
    return {"ok": True}
