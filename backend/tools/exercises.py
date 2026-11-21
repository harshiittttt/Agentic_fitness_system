"""Exercise metadata + the two plan-editing operations: swap and time-fit. All deterministic."""
from __future__ import annotations

import copy
from datetime import date

from backend.tools.planner import derive_avoid

# name -> (muscle, movement pattern, compound?)
EXERCISES: dict[str, tuple[str, str, bool]] = {
    "squat": ("quads", "squat", True),
    "front squat": ("quads", "squat", True),
    "leg press": ("quads", "squat", True),
    "goblet squat": ("quads", "squat", True),
    "lunges": ("quads", "squat", True),
    "split squat": ("quads", "squat", True),
    "deadlift": ("posterior chain", "hinge", True),
    "romanian deadlift": ("posterior chain", "hinge", True),
    "hip thrust": ("glutes", "hinge", True),
    "back extension": ("posterior chain", "hinge", False),
    "bench press": ("chest", "horizontal push", True),
    "incline press": ("chest", "horizontal push", True),
    "dumbbell press": ("chest", "horizontal push", True),
    "push up": ("chest", "horizontal push", True),
    "dips": ("chest", "horizontal push", True),
    "overhead press": ("shoulders", "vertical push", True),
    "dumbbell shoulder press": ("shoulders", "vertical push", True),
    "landmine press": ("shoulders", "vertical push", True),
    "barbell row": ("back", "horizontal pull", True),
    "cable row": ("back", "horizontal pull", True),
    "dumbbell row": ("back", "horizontal pull", True),
    "chest supported row": ("back", "horizontal pull", True),
    "pull up": ("back", "vertical pull", True),
    "lat pulldown": ("back", "vertical pull", True),
    "assisted pull up": ("back", "vertical pull", True),
    "curls": ("biceps", "biceps", False),
    "hammer curls": ("biceps", "biceps", False),
    "cable curl": ("biceps", "biceps", False),
    "triceps pushdown": ("triceps", "triceps", False),
    "skullcrushers": ("triceps", "triceps", False),
    "overhead triceps extension": ("triceps", "triceps", False),
    "lateral raise": ("shoulders", "side/rear delts", False),
    "face pull": ("rear delts", "side/rear delts", False),
    "rear delt fly": ("rear delts", "side/rear delts", False),
    "leg curl": ("hamstrings", "leg isolation", False),
    "leg extension": ("quads", "leg isolation", False),
    "calf raise": ("calves", "calves", False),
    "seated calf raise": ("calves", "calves", False),
    "plank": ("core", "core", False),
    "cable crunch": ("core", "core", False),
    "hanging leg raise": ("core", "core", False),
}

SECONDS_PER_COMPOUND_SET = 3.0  # minutes incl. rest
MINUTES_PER_ISO_SET = 2.0
WARMUP_MIN = 5


def info(name: str) -> dict:
    muscle, pattern, compound = EXERCISES.get(name.lower(), ("other", "other", False))
    return {"muscle": muscle, "pattern": pattern, "compound": compound}


def alternatives(name: str, injuries: list[str] | None = None, avoid: list[str] | None = None, exclude: list[str] | None = None) -> list[dict]:
    """Same-pattern swaps, never violating the user's injury-derived avoid list."""
    pattern = info(name)["pattern"]
    blocked = derive_avoid(injuries, avoid)
    exclude = {e.lower() for e in (exclude or [])} | {name.lower()}
    out = []
    for n, (muscle, pat, compound) in EXERCISES.items():
        if pat != pattern or n in exclude or any(a in n for a in blocked):
            continue
        out.append({"name": n, "muscle": muscle, "pattern": pat, "compound": compound})
    # prefer same compound-ness first, then alphabetical for stable ordering
    ref = info(name)["compound"]
    return sorted(out, key=lambda a: (a["compound"] != ref, a["name"]))


def estimate_minutes(day: dict) -> int:
    t = WARMUP_MIN
    for e in day["exercises"]:
        t += e["sets"] * (SECONDS_PER_COMPOUND_SET if info(e["name"])["compound"] else MINUTES_PER_ISO_SET)
    return round(t)


def fit_to_time(day: dict, minutes: int) -> dict:
    """Trim a session to fit. Order of sacrifice: accessory sets -> accessories -> compound sets -> compounds.
    Keeps the first (main) lift no matter what."""
    d = copy.deepcopy(day)
    ex = d["exercises"]
    guard = 200
    while estimate_minutes(d) > minutes and guard:
        guard -= 1
        iso = [i for i, e in enumerate(ex) if not info(e["name"])["compound"]]
        comp = [i for i, e in enumerate(ex) if info(e["name"])["compound"]]
        trim = next((i for i in reversed(iso) if ex[i]["sets"] > 2), None)
        if trim is not None:
            ex[trim]["sets"] -= 1
        elif iso:
            ex.pop(iso[-1])
        else:
            trim = next((i for i in reversed(comp) if ex[i]["sets"] > 2 and i != 0), None)
            if trim is not None:
                ex[trim]["sets"] -= 1
            elif len(comp) > 1 and ex[0]["sets"] > 3:
                ex[0]["sets"] -= 1          # shave the main lift to 3 sets before dropping a whole movement
            elif len(ex) > 1 and len(comp) > 1:
                ex.pop(comp[-1])
            elif ex[0]["sets"] > 2:
                ex[0]["sets"] -= 1
            else:
                break
    d["est_minutes"] = estimate_minutes(d)
    return d


# Spoken shorthand -> canonical movement. People say "bench", "ohp", "rdl", not the full name.
ALIASES = {
    "bench": "bench press", "flat bench": "bench press", "chest press": "bench press", "ohp": "overhead press",
    "shoulder press": "overhead press", "military press": "overhead press", "rdl": "romanian deadlift",
    "romanian": "romanian deadlift", "dead lift": "deadlift", "squats": "squat", "back squat": "squat",
    "pull ups": "pull up", "pullup": "pull up", "pullups": "pull up", "chin up": "pull up", "chin ups": "pull up",
    "lat pull down": "lat pulldown", "pulldown": "lat pulldown", "pull down": "lat pulldown", "rows": "barbell row",
    "row": "barbell row", "bent over row": "barbell row", "tricep pushdown": "triceps pushdown", "pushdown": "triceps pushdown",
    "bicep curl": "curls", "bicep curls": "curls", "curl": "curls", "lunge": "lunges", "dip": "dips", "calves": "calf raise",
    "lateral raises": "lateral raise", "side raise": "lateral raise", "leg curls": "leg curl", "push ups": "push up", "pushups": "push up",
}


def resolve_exercise(spoken: str, candidates: list[str]) -> str:
    """Map what the lifter said to a plan exercise: exact, alias, substring, then best word overlap.
    Falls back to the spoken name (still logged, just not counted toward the plan)."""
    t = spoken.lower().strip()
    pool = [c.lower() for c in candidates] or []
    if t in pool:
        return t
    t = ALIASES.get(t, t)
    if t in pool:
        return t
    for c in pool:
        if t in c or c in t:
            return c
    if t in EXERCISES:                 # a real, different exercise ("bench press" on an incline-press day) is not the one that shares the word "press"
        return t
    words = {w.rstrip("s") for w in t.split() if len(w) > 3}
    best, score = None, 0
    for c in pool:
        sc = len(words & {w.rstrip("s") for w in c.split() if len(w) > 3})
        if sc > score:
            best, score = c, sc
    return best or t


def today() -> str:
    return date.today().isoformat()
