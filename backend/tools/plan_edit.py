"""Plan editing: swap an exercise, fit today's session to the time you have. Plan stays the source of truth;
'today' changes live in plan['today_override'] and expire on their own."""
from __future__ import annotations

from backend.memory import db
from backend.tools import exercises as ex


def _rotation_index(plan: dict) -> int:
    """Which day of the split is today's / next. Backed by the explicit session-to-split record (backend/coach/session_flow.py):
    the day AFTER the one last trained, matched to the CURRENT split by name, so stray labels, skipped days, split edits and
    after-midnight sessions cannot move it. Falls back to day 1 if anything is wrong with the record."""
    try:
        from backend.coach import session_flow
        return session_flow.rotation_index(plan) % len(plan["days"])
    except Exception:
        names = db.session_names()
        return len(names) % len(plan["days"])


def plan_view() -> dict | None:
    """The plan as the app should see it: rotation index, time estimates, and any live override."""
    plan = db.get_plan()
    if not plan:
        return None
    plan["today_index"] = _rotation_index(plan)
    try:
        from backend.coach import session_flow
        plan["sessions_done"] = session_flow.count()
    except Exception:
        plan["sessions_done"] = len(db.session_names())
    for d in plan["days"]:
        d["est_minutes"] = ex.estimate_minutes(d)
    ov = plan.get("today_override")
    if ov and (ov["date"] != ex.today() or ov["day_index"] != plan["today_index"]):
        plan.pop("today_override")  # stale: different day or rotation moved on
    elif ov:
        ov["day"]["est_minutes"] = ex.estimate_minutes(ov["day"])
    return plan


def _profile_filters() -> tuple[list[str], list[str]]:
    prof = db.get_profile() or {}
    return prof.get("injuries", []), prof.get("avoid", [])


def get_alternatives(name: str) -> list[dict]:
    inj, avoid = _profile_filters()
    return ex.alternatives(name, inj, avoid)


def _replace(day: dict, old: str, new: str) -> bool:
    for e in day["exercises"]:
        if e["name"] == old:
            e["name"] = new
            return True
    return False


def swap(day_index: int, exercise: str, replacement: str, scope: str = "today") -> dict:
    plan = db.get_plan()
    if not plan:
        raise ValueError("No plan yet")
    if not 0 <= day_index < len(plan["days"]):
        raise ValueError("Unknown day")
    if replacement not in {a["name"] for a in get_alternatives(exercise)}:
        raise ValueError(f"{replacement} is not a safe like-for-like swap for {exercise}")

    day = plan["days"][day_index]
    ov = plan.get("today_override")
    ov_live = ov and ov["date"] == ex.today() and ov["day_index"] == day_index

    if scope == "plan":
        if not _replace(day, exercise, replacement):
            raise ValueError(f"{exercise} is not in {day['name']}")
        if ov_live:
            _replace(ov["day"], exercise, replacement)
        db.log_change("you", f"Swapped {exercise} -> {replacement}", f"Your choice ({day['name']})", "manual swap")
        db.add_memory("preference", f"Prefers {replacement} over {exercise}")
    else:
        if not ov_live:
            import copy
            ov = plan["today_override"] = {"date": ex.today(), "day_index": day_index, "minutes": None,
                                           "day": copy.deepcopy(day), "swaps": [], "reason": "swap"}
        if not _replace(ov["day"], exercise, replacement):
            raise ValueError(f"{exercise} is not in today's session")
        ov.setdefault("swaps", []).append([exercise, replacement])
    db.save_plan(plan)
    return plan_view()


def fit_today(minutes: int) -> dict:
    plan = db.get_plan()
    if not plan:
        raise ValueError("No plan yet")
    import copy
    idx = _rotation_index(plan)
    ov = plan.get("today_override")
    live = ov and ov["date"] == ex.today() and ov["day_index"] == idx
    swaps = ov.get("swaps", []) if live else []
    base = copy.deepcopy(plan["days"][idx])  # always fit from the full session, so 30 -> 45 min restores moves
    for old, new in swaps:
        _replace(base, old, new)
    plan["today_override"] = {"date": ex.today(), "day_index": idx, "minutes": minutes,
                              "day": ex.fit_to_time(base, minutes), "swaps": swaps,
                              "reason": f"short on time ({minutes} min)"}
    db.save_plan(plan)
    return plan_view()


def clear_override() -> dict:
    plan = db.get_plan()
    if plan and plan.pop("today_override", None):
        db.save_plan(plan)
    return plan_view()
