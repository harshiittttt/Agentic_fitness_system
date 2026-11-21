"""Diet consultant endpoints: chat with the dietitian, build / fetch the day or week plan, swap a food, save preferences."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()


class DietChat(BaseModel):
    message: str = Field(min_length=1, max_length=1200)


class DietPrefs(BaseModel):
    diet_type: str | None = Field(None, pattern="^(veg|egg|nonveg)$")
    meals_per_day: int | None = Field(None, ge=3, le=5)
    dislikes: list[str] | None = None
    allergies: list[str] | None = None
    budget: str | None = Field(None, pattern="^(low|medium|high)$")
    notes: str | None = Field(None, max_length=300)


class DietSwap(BaseModel):
    slot: str
    food: str
    replacement: str | None = None


@router.post("/diet/chat")
def diet_chat(c: DietChat):
    from backend.diet import agent
    return agent.chat(c.message)


@router.get("/diet/state")
def diet_state():
    """What the Diet screen opens with: saved preferences, what is still unknown, the saved day plan and the recent conversation."""
    from backend.diet import agent
    return {"prefs": agent.get_prefs(), "needs": agent._missing(), "plan": agent.saved_plan(), "history": agent.history_for_app(), "targets": agent.targets()}


@router.post("/diet/prefs")
def diet_prefs(p: DietPrefs):
    from backend.diet import agent
    return agent.save_prefs(p.model_dump(exclude_none=True))


@router.get("/diet/plan")
def diet_plan(variation: int = 0):
    from backend.diet import agent
    d = agent.today_plan(max(0, min(int(variation), 27)))
    if "error" in d:
        raise HTTPException(409, d["error"])
    return d


@router.get("/diet/week")
def diet_week():
    from backend.diet import agent
    w = agent.week_plan()
    if "error" in w:
        raise HTTPException(409, w["error"])
    return w


@router.post("/diet/swap")
def diet_swap(s: DietSwap):
    from backend.diet import agent
    r = agent.swap(s.slot, s.food, s.replacement)
    if "error" in r:
        raise HTTPException(409, r["error"])
    return r["day"]
