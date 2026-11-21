"""Profile editing that does NOT rebuild your life.

Changing body stats or goal recomputes nutrition targets only. Changing training inputs (days, experience, injuries)
only rebuilds the training plan if you explicitly ask; your history, memory and adaptations are never touched.
"""
from __future__ import annotations

from backend.memory import db
from backend.tools import calculators as calc
from backend.tools.planner import EXPERIENCE_VOLUME, apply_volume, build_plan, derive_avoid

NUTRITION_KEYS = {"age", "sex", "height_cm", "weight_kg", "goal", "activity"}
TRAINING_KEYS = {"days_per_week", "experience", "injuries", "avoid"}


def nutrition_targets(profile: dict) -> dict:
    bmr = calc.mifflin_st_jeor(profile["weight_kg"], profile["height_cm"], profile["age"], profile.get("sex", "m"))
    kcal = calc.goal_calories(calc.tdee(bmr, profile.get("activity", "moderate")), profile.get("goal", "maintain"))
    return calc.macro_targets(kcal, profile["weight_kg"], profile.get("goal", "maintain"))


def _fit_equipment(plan: dict, profile: dict, avoid: list[str]) -> None:
    """Swap any exercise the athlete cannot do with equipment they have, using a like-for-like movement; drop it if none exists.
    Unspecified equipment means a full gym, so nothing changes."""
    from backend.tools.exercises import alternatives
    from fitness_os.planning import EQUIPMENT, _equipment

    eq = _equipment(profile)
    ok = lambda n: bool(EQUIPMENT.get(n, eq) & eq)
    for day in plan["days"]:
        have = {e["name"] for e in day["exercises"]}
        keep = []
        for e in day["exercises"]:
            if ok(e["name"]):
                keep.append(e)
                continue
            sub = next((a["name"] for a in alternatives(e["name"], profile.get("injuries", []), avoid, exclude=list(have)) if ok(a["name"])), None)
            if sub:
                have.add(sub)
                keep.append({**e, "name": sub})
        day["exercises"] = keep


def training_plan(profile: dict) -> dict:
    """A fresh training plan (days, volume) from the profile. Does not touch nutrition."""
    avoid = derive_avoid(profile.get("injuries", []), profile.get("avoid", []))
    plan = None
    try:  # adaptive path: the Training agent designs the block, code validates it (fitness_os/planning.py)
        from fitness_os.planning import design_block
        plan = design_block(profile)
    except Exception:  # any failure falls back to the offline starter so the app never breaks
        plan = None
    if plan is None:
        plan = build_plan(profile.get("days_per_week", 4), profile.get("goal", "maintain"), avoid=avoid)
        _fit_equipment(plan, profile, avoid)   # the offline starter must not hand a dumbbell-only athlete barbell lifts
    start_vol = EXPERIENCE_VOLUME.get(profile.get("experience", "intermediate"), 1.0)
    if start_vol != 1.0:
        apply_volume(plan, start_vol)
    plan["base_volume"] = start_vol   # what 'recovered' means: Recovery caps and rebounds are relative to this
    return plan


def update_profile(patch: dict, rebuild: bool = False) -> dict:
    prof = db.get_profile()
    if not prof:
        raise ValueError("No profile yet")
    patch = {k: v for k, v in patch.items() if v is not None}
    before = dict(prof)
    prof.update(patch)
    changed = {k for k in patch if before.get(k) != prof[k]}
    out = {"changed": sorted(changed), "nutrition_updated": False, "needs_rebuild": False, "rebuilt": False}
    if not changed:
        return out
    db.save_profile(prof)

    if "weight_kg" in changed:
        db.add_checkin(bodyweight=prof["weight_kg"])           # keeps the bodyweight chart honest
    for inj in set(prof.get("injuries", [])) - set(before.get("injuries", [])):
        db.add_memory("injury", inj)

    plan = db.get_plan()
    if plan and changed & NUTRITION_KEYS:
        plan["nutrition"] = nutrition_targets(prof)
        plan["base_calories"] = plan["nutrition"]["calories"]
        plan["calorie_mult"] = 1.0                              # the base moved, so past calorie nudges no longer apply
        plan["goal"] = prof.get("goal", plan.get("goal"))
        db.save_plan(plan)
        db.log_change("you", "Updated nutrition targets", ", ".join(sorted(changed & NUTRITION_KEYS)) + " changed",
                      f"{plan['nutrition']['calories']} kcal/day")
        out["nutrition_updated"] = True

    if changed & TRAINING_KEYS:
        if rebuild and plan:
            new = training_plan(prof)
            for k in ("nutrition", "base_calories", "calorie_mult"):
                if k in plan:
                    new[k] = plan[k]
            db.save_plan(new)
            db.log_change("you", "Rebuilt training plan", ", ".join(sorted(changed & TRAINING_KEYS)) + " changed",
                          f"{prof.get('days_per_week')} days/week, {prof.get('experience')}")
            out["rebuilt"] = True
        else:
            out["needs_rebuild"] = True
    return out
