"""Extra read routes for the Trends and Today screens (PRs, recent sessions, today's state) plus meal undo.
Kept in its own router so it never collides with main.py."""
from __future__ import annotations

import re
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.agents.specialists import MAIN_LIFTS
from backend.memory import db
from backend.tools import exercises as ex
from backend.tools.calculators import rpe_adjusted_e1rm

router = APIRouter()


def _logged_sets() -> list[dict]:
    """Every set with a weight and reps, oldest first."""
    return [s for s in reversed(db.get_sets(limit=5000)) if s["weight"] and s["reps"]]


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _sessions(sets: list[dict]) -> list[dict]:
    by: dict[str, dict] = {}
    for s in sets:
        ses = by.setdefault(s["session"], {"date": s["session"], "sets": 0, "tonnage": 0, "ex": {}})
        ses["sets"] += 1
        ses["tonnage"] += s["weight"] * s["reps"]
        e = ses["ex"].setdefault(s["exercise"], {"name": s["exercise"], "sets": 0, "top": None})
        e["sets"] += 1
        if e["top"] is None or s["weight"] * s["reps"] > e["top"]["weight"] * e["top"]["reps"]:
            e["top"] = {"weight": s["weight"], "reps": s["reps"]}
    out = []
    for ses in by.values():
        out.append({"date": ses["date"], "sets": ses["sets"], "tonnage": round(ses["tonnage"]), "exercises": list(ses["ex"].values())})
    return out


@router.get("/insights/summary")
def insights_summary():
    """Personal bests per main lift, the last few sessions, and this week's consistency."""
    sets = _logged_sets()
    prs = []
    for lift in MAIN_LIFTS:
        mine = [s for s in sets if s["exercise"] == lift]
        if not mine:
            continue
        scored = [(rpe_adjusted_e1rm(s["weight"], s["reps"], s["rpe"]), s) for s in mine]
        best_e1, best = max(scored, key=lambda t: t[0])
        heaviest = max(mine, key=lambda s: (s["weight"], s["reps"]))
        latest_session = mine[-1]["session"]
        latest_e1 = max(e for e, s in scored if s["session"] == latest_session)
        prs.append({
            "lift": lift, "best_e1rm": round(best_e1, 1), "best_date": best["session"],
            "heaviest": {"weight": heaviest["weight"], "reps": heaviest["reps"], "date": heaviest["session"]},
            "latest_e1rm": round(latest_e1, 1), "at_best": latest_e1 >= best_e1 - 0.05, "sessions": len({s["session"] for s in mine}),
        })
    sessions = _sessions(sets)
    recent = list(reversed(sessions[-8:]))
    prof = db.get_profile() or {}
    target = int(prof.get("days_per_week") or 3)
    mon = _monday(date.today()).isoformat()
    this_week = sum(1 for s in sessions if s["date"] >= mon)
    return {"prs": prs, "recent": recent, "week": {"sessions": this_week, "target": target, "start": mon},
            "total_sessions": len(sessions), "first_session": sessions[0]["date"] if sessions else None}


@router.get("/insights/today")
def insights_today():
    """What the Today screen needs to say after (or instead of) a session: did you train, what is next, how is the week."""
    plan = db.get_plan()
    if not plan:
        return {"has_plan": False}
    from backend.tools.plan_edit import plan_view
    view = plan_view()
    names = db.session_names()
    trained_today = bool(names) and names[-1] == ex.today()
    sets = _logged_sets()
    today_sets = [s for s in sets if s["session"] == ex.today()]
    days = view["days"]
    nxt = days[(view["today_index"] + 1) % len(days)] if trained_today else days[view["today_index"]]
    prof = db.get_profile() or {}
    target = int(prof.get("days_per_week") or 3)
    mon = _monday(date.today()).isoformat()
    this_week = sum(1 for n in names if n >= mon)
    last = names[-1] if names else None
    days_since = (date.today() - date.fromisoformat(last)).days if last and re.fullmatch(r"\d{4}-\d{2}-\d{2}", last) else None
    return {
        "has_plan": True, "trained_today": trained_today, "sets_today": len(today_sets),
        "tonnage_today": round(sum(s["weight"] * s["reps"] for s in today_sets)),
        "next": {"name": nxt["name"], "lifts": len(nxt["exercises"]), "sets": sum(e["sets"] for e in nxt["exercises"]),
                 "minutes": nxt.get("est_minutes"), "exercises": [e["name"] for e in nxt["exercises"]]},
        "week": {"sessions": this_week, "target": target},
        "days_since_last": days_since,
        # a gentle rest nudge: at or past this week's target, or trained yesterday and today is not planned as a push
        "week_done": this_week >= target,
    }


class MealRestore(BaseModel):
    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    name: str = Field(min_length=1, max_length=120)
    grams: float | None = None
    kcal: float = Field(ge=0, le=10000)
    protein: float = Field(0, ge=0, le=1000)
    carbs: float = Field(0, ge=0, le=1000)
    fat: float = Field(0, ge=0, le=1000)
    estimated: bool = False


@router.post("/meal/restore")
def meal_restore(m: MealRestore):
    """Undo for a deleted meal entry: put it back exactly as it was."""
    try:
        date.fromisoformat(m.day)
    except ValueError:
        raise HTTPException(422, "Bad day")
    return {"id": db.add_meal(m.day, m.name, m.grams, m.kcal, m.protein, m.carbs, m.fat, m.estimated)}
