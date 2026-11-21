"""Make a correction of a profile-backed belief real: the athlete fixes "goal" or "4 days a week" on the memory screen and the profile, nutrition
targets and plan follow, through the same validated path as POST /profile (backend.tools.profile_edit.update_profile).

A belief is profile-backed only if the profile currently holds the value the belief states, so facts learned from chat that never reached the
profile are never written into it. Forget/deny never touch the profile: they only stop the coach using the belief.
"""
from __future__ import annotations

import re

from backend.memory import db

from . import store
from .ingest import GOAL_TEXT, safe_phrase

PROFILE_PREDICATES = {"goal", "trains_days_per_week", "experience_level", "injury_site", "dislikes_exercise"}
LISTS = {"injury_site": "injuries", "dislikes_exercise": "avoid"}


class ProfileRefused(ValueError):
    """The corrected value cannot go into the profile; the message is plain language for the athlete."""


def _goal(text: str) -> str | None:
    t = (text or "").lower()
    if re.search(r"\b(cut|lose|losing|fat|lean|slim|shred)", t):
        return "cut"
    if re.search(r"\b(bulk|gain|bigger|size|muscle|mass)", t):
        return "bulk"
    if re.search(r"\b(maintain|maintenance|stay|keep)", t):
        return "maintain"
    return None


def _experience(text: str) -> str | None:
    t = (text or "").lower()
    for k, pat in (("beginner", r"beginner|novice|new to|starter"), ("advanced", r"advanced|expert|veteran"), ("intermediate", r"intermediate|medium")):
        if re.search(pat, t):
            return k
    return None


def _same(a, b) -> bool:
    return store.norm(str(a)) == store.norm(str(b))


def backed(fact: dict, prof: dict | None = None) -> bool:
    """Does the profile currently hold what this belief says?"""
    prof = prof if prof is not None else (db.get_profile() or {})
    p, old = fact["predicate"], fact.get("object_text") or ""
    if p not in PROFILE_PREDICATES or not prof:
        return False
    if p == "goal":
        return _goal(old) is not None and _goal(old) == prof.get("goal")
    if p == "trains_days_per_week":
        m = re.search(r"\d+", old)
        return bool(m) and prof.get("days_per_week") == int(m.group())
    if p == "experience_level":
        return _experience(old) == prof.get("experience") and prof.get("experience") is not None
    return any(_same(x, old) for x in prof.get(LISTS[p], []) or [])


def plan_correction(fact: dict, value: str) -> tuple[dict, str] | None:
    """(profile patch, canonical belief value) for a profile-backed belief, None if the correction is memory-only. Raises ProfileRefused."""
    prof = db.get_profile() or {}
    if not backed(fact, prof):
        return None
    p = fact["predicate"]
    if p == "goal":
        g = _goal(value)
        if not g:
            raise ProfileRefused("I can only set your goal to cut (lose fat), bulk (gain size) or maintain. Which one is it?")
        return {"goal": g}, GOAL_TEXT[g]
    if p == "trains_days_per_week":
        m = re.search(r"\d+", value)
        n = int(m.group()) if m else 0
        if not 3 <= n <= 5:
            raise ProfileRefused("Your plan is built for 3 to 5 training days a week, so that number can't go in. Pick 3, 4 or 5.")
        return {"days_per_week": n}, str(n)
    if p == "experience_level":
        e = _experience(value)
        if not e:
            raise ProfileRefused("Your experience level needs to be beginner, intermediate or advanced.")
        return {"experience": e}, e
    v = safe_phrase(value, 50)
    if not v:
        raise ProfileRefused("Say it in a few plain words, for example a body part or an exercise name.")
    key = LISTS[p]
    old = fact.get("object_text") or ""
    items = [x for x in prof.get(key, []) or [] if not _same(x, old)]
    if not any(_same(x, v) for x in items):
        items.append(v)
    return {key: items}, v


def apply(patch: dict) -> dict:
    """Run the patch through the validated profile edit; training inputs rebuild the plan, body/goal inputs recompute targets."""
    from backend.main import ProfilePatch
    from backend.tools.profile_edit import update_profile
    try:
        clean = ProfilePatch(**patch).model_dump(exclude={"rebuild"}, exclude_none=True)
        return update_profile(clean, rebuild=True)
    except ValueError as e:   # pydantic's ValidationError is a ValueError
        raise ProfileRefused("That value isn't one your profile can hold, so nothing was changed.") from e
