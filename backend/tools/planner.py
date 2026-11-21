"""Deterministic starter-plan templates. The Training agent then adapts them continuously."""
from __future__ import annotations

TEMPLATES = {
    3: [
        ("Full Body A", [("squat", 3, 6), ("bench press", 3, 8), ("barbell row", 3, 8), ("plank", 2, 45)]),
        ("Full Body B", [("deadlift", 3, 5), ("overhead press", 3, 8), ("lat pulldown", 3, 10), ("lunges", 2, 10)]),
        ("Full Body C", [("front squat", 3, 6), ("incline press", 3, 8), ("cable row", 3, 10), ("curls", 2, 12)]),
    ],
    4: [
        ("Upper A", [("bench press", 4, 6), ("barbell row", 4, 8), ("overhead press", 3, 8), ("curls", 2, 12)]),
        ("Lower A", [("squat", 4, 6), ("romanian deadlift", 3, 8), ("leg curl", 3, 12), ("calf raise", 3, 12)]),
        ("Upper B", [("incline press", 4, 8), ("pull up", 4, 8), ("lateral raise", 3, 12), ("triceps pushdown", 3, 12)]),
        ("Lower B", [("deadlift", 3, 5), ("leg press", 3, 10), ("lunges", 3, 10), ("plank", 3, 45)]),
    ],
    5: [
        ("Push", [("bench press", 4, 6), ("overhead press", 3, 8), ("lateral raise", 3, 12), ("triceps pushdown", 3, 12)]),
        ("Pull", [("barbell row", 4, 8), ("pull up", 4, 8), ("face pull", 3, 15), ("curls", 3, 12)]),
        ("Legs", [("squat", 4, 6), ("romanian deadlift", 3, 8), ("leg press", 3, 10), ("calf raise", 3, 12)]),
        ("Upper", [("incline press", 3, 8), ("lat pulldown", 3, 10), ("dips", 3, 10), ("hammer curls", 3, 12)]),
        ("Lower", [("deadlift", 3, 5), ("front squat", 3, 6), ("leg curl", 3, 12), ("plank", 3, 45)]),
    ],
}


# Movements to skip for a reported problem area. Conservative: skip rather than risk it.
INJURY_AVOID = {
    "shoulder": ["overhead press", "dips", "lateral raise", "incline press", "face pull"],
    "lower back": ["deadlift", "barbell row", "romanian deadlift", "good morning"],
    "back": ["deadlift", "barbell row", "romanian deadlift"],
    "knee": ["lunges", "leg press", "front squat", "squat"],
    "wrist": ["curls", "front squat", "pull up"],
    "elbow": ["triceps pushdown", "curls", "dips", "hammer curls"],
    "neck": ["overhead press", "pull up"],
    "hip": ["lunges", "squat", "deadlift"],
}

# Starting volume by training experience (the Training agent adapts from here).
EXPERIENCE_VOLUME = {"beginner": 0.8, "intermediate": 1.0, "advanced": 1.15}


def derive_avoid(injuries: list[str] | None, avoid: list[str] | None) -> list[str]:
    out = [a.lower() for a in (avoid or [])]
    for inj in injuries or []:
        text = inj.lower()
        for key, moves in INJURY_AVOID.items():
            if key in text:
                out += moves
    return sorted(set(out))


def build_plan(days_per_week: int, goal: str, equipment: list[str] | None = None, avoid: list[str] | None = None) -> dict:
    days = TEMPLATES[min(TEMPLATES, key=lambda k: abs(k - days_per_week))]
    avoid = [a.lower() for a in (avoid or [])]
    out_days, excluded = [], []
    for name, exercises in days:
        ex = []
        for n, s, r in exercises:
            if any(a in n for a in avoid):
                if n not in excluded:
                    excluded.append(n)
                continue
            ex.append({"name": n, "sets": s, "reps": r, "target_rpe": 8.0})
        out_days.append({"name": name, "exercises": ex})
    return {"goal": goal, "days": out_days, "volume_mult": 1.0, "calorie_mult": 1.0, "excluded": excluded}


def apply_volume(plan: dict, multiplier: float) -> dict:
    """Scale working sets by multiplier relative to the template (min 1 set)."""
    base = plan.get("volume_mult", 1.0)
    ratio = multiplier / base if base else multiplier
    for d in plan["days"]:
        for e in d["exercises"]:
            e["sets"] = max(1, round(e["sets"] * ratio))
    plan["volume_mult"] = multiplier
    return plan
