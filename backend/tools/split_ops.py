"""Pure, validated operations on a training plan. Each returns a Result with a NEW plan (the input is never mutated).

Nothing here touches the database except `make_ctx` (reads the profile + injuries to know what is allowed). Hard rules live in code:
exercise library, injury exclusions (never loaded through pain), equipment, set/rep/RPE bounds, 3-7 exercises per day for a built split
(an edit may not leave a day with fewer than 2), unique names, at most 6 training days (at least one rest day a week).

Errors are OpError. `kind` tells the caller what to do with it:
    "invalid"  fixable input (unknown exercise, bad number, missing equipment): the coach can retry with something else
    "refused"  a hard safety limit (injury/pain exclusion, no rest day): never negotiable, never retried
"""
from __future__ import annotations

import copy
from collections import defaultdict
from dataclasses import dataclass, field

from backend.tools.exercises import EXERCISES
from backend.tools.planner import apply_volume

MIN_DAYS, MAX_DAYS = 2, 6
MAX_EX_PER_DAY, MIN_EX_AFTER_EDIT = 7, 2
STYLES = ("full_body", "upper_lower", "push_pull_legs", "bro_split")
STYLE_DAYS = {"full_body": (2, 4), "upper_lower": (2, 6), "push_pull_legs": (3, 6), "bro_split": (4, 6)}
STYLE_LABEL = {"full_body": "full body", "upper_lower": "upper/lower", "push_pull_legs": "push/pull/legs", "bro_split": "bro split"}
STYLE_ALIASES = {"fullbody": "full_body", "full body": "full_body", "full-body": "full_body", "upper/lower": "upper_lower", "upper lower": "upper_lower",
                 "upperlower": "upper_lower", "ul": "upper_lower", "ppl": "push_pull_legs", "push pull legs": "push_pull_legs", "push/pull/legs": "push_pull_legs",
                 "push-pull-legs": "push_pull_legs", "pushpulllegs": "push_pull_legs", "bro": "bro_split", "bro split": "bro_split", "body part": "bro_split",
                 "bodypart": "bro_split", "body part split": "bro_split"}
LOWER_PATTERNS = {"squat", "hinge", "leg isolation", "calves"}


class OpError(ValueError):
    def __init__(self, message: str, kind: str = "invalid", rule: str = ""):
        super().__init__(message)
        self.kind, self.rule = kind, rule


@dataclass
class Result:
    plan: dict
    summary: str
    changes: list[str] = field(default_factory=list)
    structural: bool = False


@dataclass
class Ctx:
    profile: dict
    avoid: list[str]
    allowed: set[str]


def make_ctx(profile: dict | None = None) -> Ctx:
    from backend.memory import db
    from fitness_os import planning

    prof = profile if profile is not None else (db.get_profile() or {})
    avoid = planning._avoid(prof)
    return Ctx(prof, avoid, set(planning.allowed_exercises(prof, avoid)))


# ---- helpers ----
def norm_name(name) -> str:
    return " ".join(str(name or "").strip().lower().split())


def style_of(raw) -> str:
    s = norm_name(raw).replace("_", " ")
    key = STYLE_ALIASES.get(s) or STYLE_ALIASES.get(s.replace(" ", "")) or s.replace(" ", "_")
    if key not in STYLES:
        raise OpError(f"Unknown split style '{raw}'. Choose one of: full body, upper/lower, push/pull/legs, bro split, or give me your own days.")
    return key


def find_day(plan: dict, ref) -> int:
    """Day by 1-based number ('2', 2) or by name ('Pull'); -> 0-based index."""
    days = plan.get("days") or []
    if ref is None or str(ref).strip() == "":
        raise OpError("Which day? Give a day number or name.")
    text = norm_name(ref)
    if text.lstrip("#").isdigit():
        i = int(text.lstrip("#")) - 1
        if 0 <= i < len(days):
            return i
        raise OpError(f"There is no day {ref}; the split has {len(days)} days.")
    exact = [i for i, d in enumerate(days) if norm_name(d["name"]) == text]
    if exact:
        return exact[0]
    part = [i for i, d in enumerate(days) if text in norm_name(d["name"])]
    if len(part) == 1:
        return part[0]
    if len(part) > 1:
        raise OpError(f"'{ref}' matches several days ({', '.join(days[i]['name'] for i in part)}); be more specific or use the day number.")
    raise OpError(f"No day called '{ref}'. Days: {', '.join(d['name'] for d in days)}.")


def find_exercise(day: dict, name) -> int:
    n = norm_name(name)
    for i, e in enumerate(day["exercises"]):
        if e["name"] == n:
            return i
    raise OpError(f"{n} is not in {day['name']}. It has: {', '.join(e['name'] for e in day['exercises'])}.")


def weekly_sets(plan: dict) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for d in plan.get("days", []):
        for e in d["exercises"]:
            out[EXERCISES.get(e["name"], ("other",))[0]] += int(e["sets"])
    return dict(out)


def total_sets(plan: dict) -> int:
    return sum(weekly_sets(plan).values())


def has_lower_body(plan: dict) -> bool:
    return any(EXERCISES.get(e["name"], ("", "other"))[1] in LOWER_PATTERNS for d in plan.get("days", []) for e in d["exercises"])


def _check_exercise(name: str, ctx: Ctx) -> str:
    n = norm_name(name)
    if n not in EXERCISES:
        near = [k for k in EXERCISES if n and (n in k or k in n)][:4]
        raise OpError(f"'{name}' is not in the exercise library." + (f" Did you mean: {', '.join(near)}?" if near else ""))
    if any(a in n for a in ctx.avoid):
        raise OpError(f"{n} is off the table: it conflicts with your reported injury/restriction ({', '.join(ctx.avoid[:4])}). "
                      f"I will not load you through pain. See a physio if it persists.", "refused", "injury_load")
    if n not in ctx.allowed:
        raise OpError(f"{n} needs equipment you do not have ({', '.join(sorted(_eq_needed(n)))}). Pick something you can do with your equipment.")
    return n


def _eq_needed(n: str) -> set[str]:
    from fitness_os.planning import EQUIPMENT
    return EQUIPMENT.get(n, set())


def _defaults(name: str) -> tuple[int, int]:
    if name == "plank":
        return 3, 45
    return (3, 8) if EXERCISES[name][2] else (3, 12)


def _numbers(name: str, sets, reps, rpe) -> dict:
    from fitness_os.planning import HOLD_RANGE, REPS_RANGE, RPE_RANGE, SETS_RANGE

    try:
        s, r = int(sets), int(reps)
        e = round(float(rpe), 1)
    except (TypeError, ValueError):
        raise OpError(f"{name}: sets, reps and RPE must be numbers.")
    lo, hi = HOLD_RANGE if name == "plank" else REPS_RANGE
    if not SETS_RANGE[0] <= s <= SETS_RANGE[1]:
        raise OpError(f"{name}: sets must be {SETS_RANGE[0]}-{SETS_RANGE[1]} (got {s}).")
    if not lo <= r <= hi:
        raise OpError(f"{name}: {'seconds' if name == 'plank' else 'reps'} must be {lo}-{hi} (got {r}).")
    if not RPE_RANGE[0] <= e <= RPE_RANGE[1]:
        raise OpError(f"{name}: target RPE must be {RPE_RANGE[0]}-{RPE_RANGE[1]} (got {e}).")
    return {"name": name, "sets": s, "reps": r, "target_rpe": e}


def _check_weekly_cap(plan: dict, muscles: set[str]) -> None:
    from fitness_os.planning import MAX_WEEKLY_SETS_PER_MUSCLE

    w = weekly_sets(plan)
    for m in muscles:
        if w.get(m, 0) > MAX_WEEKLY_SETS_PER_MUSCLE:
            raise OpError(f"That would put {m} at {w[m]} sets a week; the limit is {MAX_WEEKLY_SETS_PER_MUSCLE}.")


def _fresh(plan: dict) -> dict:
    return copy.deepcopy(plan)


def _fmt(e: dict) -> str:
    return f"{e['name']} {e['sets']}x{e['reps']}" + ("s" if e["name"] == "plank" else "")


# ---- style templates (slot = candidates in order of preference, sets, reps) ----
_S = lambda *n: tuple(n)  # noqa: E731
_PUSH_H = _S("bench press", "dumbbell press", "push up")
_PUSH_INC = _S("incline press", "dumbbell press", "dips", "push up")
_PUSH_V = _S("overhead press", "dumbbell shoulder press", "landmine press")
_PULL_H = _S("barbell row", "dumbbell row", "cable row", "chest supported row")
_PULL_H2 = _S("cable row", "chest supported row", "dumbbell row", "barbell row")
_PULL_V = _S("pull up", "lat pulldown", "assisted pull up")
_PULL_V2 = _S("lat pulldown", "pull up", "assisted pull up")
_SQUAT = _S("squat", "leg press", "goblet squat", "split squat", "lunges")
_SQUAT2 = _S("front squat", "leg press", "goblet squat", "lunges", "split squat")
_HINGE = _S("romanian deadlift", "hip thrust", "deadlift", "back extension")
_HINGE2 = _S("deadlift", "romanian deadlift", "hip thrust", "back extension")
_CURL_LEG = _S("leg curl", "romanian deadlift", "back extension")
_CALF = _S("calf raise", "seated calf raise")
_CORE = _S("plank", "cable crunch", "hanging leg raise")
_LAT = _S("lateral raise", "rear delt fly")
_REAR = _S("face pull", "rear delt fly", "lateral raise")
_TRI = _S("triceps pushdown", "overhead triceps extension", "skullcrushers", "dips")
_BIC = _S("curls", "hammer curls", "cable curl")
_BIC2 = _S("hammer curls", "cable curl", "curls")

STYLE_DAYS_DEF: dict[str, list[tuple[str, list]]] = {
    "full_body": [
        ("Full Body A", [(_SQUAT, 3, 6), (_PUSH_H, 3, 8), (_PULL_H, 3, 8), (_HINGE, 2, 10), (_CORE, 2, 12)]),
        ("Full Body B", [(_HINGE2, 3, 5), (_PUSH_V, 3, 8), (_PULL_V2, 3, 10), (_SQUAT2, 2, 10), (_CALF, 2, 12)]),
        ("Full Body C", [(_SQUAT2, 3, 6), (_PUSH_INC, 3, 8), (_PULL_H2, 3, 10), (_CURL_LEG, 3, 10), (_BIC, 2, 12)]),
        ("Full Body D", [(_SQUAT, 3, 8), (_PUSH_V, 3, 8), (_PULL_V, 3, 8), (_HINGE, 3, 8), (_TRI, 2, 12)]),
    ],
    "upper_lower": [
        ("Upper A", [(_PUSH_H, 4, 6), (_PULL_H, 4, 8), (_PUSH_V, 3, 8), (_PULL_V2, 3, 10), (_BIC, 2, 12)]),
        ("Lower A", [(_SQUAT, 4, 6), (_HINGE, 3, 8), (_CURL_LEG, 3, 12), (_CALF, 3, 12)]),
        ("Upper B", [(_PUSH_INC, 4, 8), (_PULL_V, 4, 8), (_LAT, 3, 12), (_TRI, 3, 12), (_REAR, 2, 15)]),
        ("Lower B", [(_HINGE2, 3, 5), (_SQUAT2, 3, 10), (_S("lunges", "split squat", "leg press"), 3, 10), (_CORE, 3, 12)]),
        ("Upper C", [(_PUSH_H, 3, 8), (_PULL_H2, 3, 10), (_PUSH_V, 3, 10), (_BIC2, 3, 12), (_TRI, 3, 12)]),
        ("Lower C", [(_SQUAT, 3, 8), (_HINGE, 3, 10), (_S("leg extension", "leg press", "lunges"), 3, 12), (_CALF, 3, 12)]),
    ],
    "push_pull_legs": [
        ("Push A", [(_PUSH_H, 4, 6), (_PUSH_V, 3, 8), (_PUSH_INC, 3, 10), (_LAT, 3, 12), (_TRI, 3, 12)]),
        ("Pull A", [(_PULL_H, 4, 8), (_PULL_V, 4, 8), (_REAR, 3, 15), (_BIC, 3, 12)]),
        ("Legs A", [(_SQUAT, 4, 6), (_HINGE, 3, 8), (_S("leg press", "lunges", "split squat"), 3, 10), (_CURL_LEG, 3, 12), (_CALF, 3, 12)]),
        ("Push B", [(_PUSH_V, 4, 8), (_PUSH_INC, 3, 8), (_PUSH_H, 3, 10), (_LAT, 3, 15), (_S("overhead triceps extension", "triceps pushdown", "skullcrushers"), 3, 12)]),
        ("Pull B", [(_PULL_V2, 4, 10), (_PULL_H2, 3, 10), (_S("rear delt fly", "face pull", "lateral raise"), 3, 15), (_BIC2, 3, 12)]),
        ("Legs B", [(_HINGE2, 3, 5), (_SQUAT2, 3, 8), (_S("hip thrust", "lunges", "split squat"), 3, 10), (_CURL_LEG, 3, 12), (_S("seated calf raise", "calf raise"), 3, 15)]),
    ],
    "bro_split": [
        ("Chest", [(_PUSH_H, 4, 6), (_PUSH_INC, 4, 8), (_S("dumbbell press", "dips", "push up"), 3, 10), (_S("triceps pushdown", "overhead triceps extension", "skullcrushers"), 3, 12)]),
        ("Back", [(_PULL_H, 4, 8), (_PULL_V, 4, 8), (_PULL_H2, 3, 10), (_REAR, 3, 15)]),
        ("Shoulders", [(_PUSH_V, 4, 8), (_LAT, 4, 12), (_S("rear delt fly", "face pull"), 3, 15), (_S("dumbbell shoulder press", "landmine press", "overhead press"), 3, 10)]),
        ("Legs", [(_SQUAT, 4, 6), (_HINGE, 3, 8), (_S("leg press", "lunges", "split squat"), 3, 10), (_CURL_LEG, 3, 12), (_CALF, 3, 12)]),
        ("Arms", [(_BIC, 3, 10), (_TRI, 3, 10), (_BIC2, 3, 12), (_S("overhead triceps extension", "skullcrushers", "triceps pushdown"), 3, 12), (_CORE, 3, 12)]),
        ("Legs B", [(_HINGE2, 3, 5), (_SQUAT2, 3, 8), (_S("hip thrust", "lunges", "split squat"), 3, 10), (_CURL_LEG, 3, 12), (_S("seated calf raise", "calf raise"), 3, 15)]),
    ],
}
# 4-day bro split drops the Arms day into Shoulders: keep Chest, Back, Shoulders, Legs
_BRO_ORDER = {4: [0, 1, 2, 3], 5: [0, 1, 2, 3, 4], 6: [0, 1, 3, 2, 4, 5]}


def _fill(slots, ctx: Ctx) -> list[dict]:
    from backend.tools import exercises as ex

    out: list[dict] = []
    for cands, sets, reps in slots:
        have = {e["name"] for e in out}
        pick = next((c for c in cands if c in ctx.allowed and c not in have), None)
        if pick is None:  # nothing preferred is usable: a same-pattern alternative the athlete can actually do
            for a in ex.alternatives(cands[0], ctx.profile.get("injuries", []), ctx.avoid, exclude=list(have)):
                if a["name"] in ctx.allowed:
                    pick = a["name"]
                    break
        if pick is None:
            continue
        out.append({"name": pick, "sets": sets, "reps": 45 if pick == "plank" else reps, "target_rpe": 8.0})
    return out


def build_style(old_plan: dict, style, days: int, ctx: Ctx) -> Result:
    """A whole new split in a named style with `days` training days, from the exercise library, honouring injuries and equipment."""
    from fitness_os import planning

    st = style_of(style)
    try:
        n = int(days)
    except (TypeError, ValueError):
        raise OpError("How many days a week? Give a number.")
    if n > MAX_DAYS:
        raise OpError(f"{n} training days leaves no rest day. Your body adapts while it rests; {MAX_DAYS} days is the most I will program.", "refused", "no_rest_day")
    lo, hi = STYLE_DAYS[st]
    if n < MIN_DAYS or not lo <= n <= hi:
        raise OpError(f"A {STYLE_LABEL[st]} split works with {lo}-{hi} days a week, not {n}. Pick a different count or style.")
    defs = STYLE_DAYS_DEF[st]
    order = _BRO_ORDER[n] if st == "bro_split" else list(range(n))
    new_days = []
    for i in order:
        name, slots = defs[i % len(defs)]
        new_days.append({"name": name, "exercises": _fill(slots, ctx)})
    plan = _fresh(old_plan)
    plan["days"] = new_days
    mult = old_plan.get("volume_mult", 1.0)
    plan["volume_mult"] = 1.0
    apply_volume(plan, mult)
    for d in plan["days"]:
        if len(d["exercises"]) < 3:
            raise OpError(f"With your equipment and restrictions I cannot build a decent '{d['name']}' day for a {STYLE_LABEL[st]} split "
                          f"(only {len(d['exercises'])} usable exercises). Try another style or tell me what equipment you have.")
    errs = planning.validate({"days": plan["days"]}, {**ctx.profile, "days_per_week": n}, ctx.avoid)
    if errs:
        raise OpError("Could not build that split: " + "; ".join(errs[:4]))
    plan["style"] = st
    plan["designed_by"] = "athlete-request"
    plan.pop("today_override", None)
    names = ", ".join(d["name"] for d in new_days)
    return Result(plan, f"Changed your split to a {n}-day {STYLE_LABEL[st]} ({names})",
                  [f"{d['name']}: " + ", ".join(_fmt(e) for e in d["exercises"]) for d in new_days], structural=True)


def custom_split(old_plan: dict, days: list, ctx: Ctx) -> Result:
    """Your own days and exercises, validated against the library, injuries, equipment and set/rep bounds."""
    from fitness_os import planning

    if not isinstance(days, list) or not days:
        raise OpError("Give me the days as a list: each with a name and exercises (name, sets, reps).")
    if len(days) > MAX_DAYS:
        raise OpError(f"{len(days)} training days leaves no rest day; {MAX_DAYS} is the most I will program.", "refused", "no_rest_day")
    if len(days) < MIN_DAYS:
        raise OpError(f"A split needs at least {MIN_DAYS} training days.")
    new_days, names = [], set()
    for d in days:
        nm = " ".join(str((d or {}).get("name", "")).split())[:30]
        if not nm:
            raise OpError("Every day needs a name.")
        if nm.lower() in names:
            raise OpError(f"Two days are both called '{nm}'; give them different names.")
        names.add(nm.lower())
        exs = []
        for e in (d.get("exercises") or []):
            n = _check_exercise((e or {}).get("name"), ctx)
            if any(x["name"] == n for x in exs):
                raise OpError(f"{n} appears twice on {nm}.")
            ds, dr = _defaults(n)
            exs.append(_numbers(n, e.get("sets", ds), e.get("reps", dr), e.get("target_rpe") or 8.0))
        new_days.append({"name": nm, "exercises": exs})
    errs = planning.validate({"days": new_days}, {**ctx.profile, "days_per_week": len(new_days)}, ctx.avoid)
    if errs:
        raise OpError("That split does not pass validation: " + "; ".join(errs[:4]))
    plan = _fresh(old_plan)
    plan["days"] = new_days
    plan["style"] = "custom"
    plan["designed_by"] = "athlete-request"
    plan.pop("today_override", None)
    mult = old_plan.get("volume_mult", 1.0)
    plan["volume_mult"] = 1.0
    apply_volume(plan, mult)
    return Result(plan, f"Set your custom {len(new_days)}-day split ({', '.join(d['name'] for d in new_days)})",
                  [f"{d['name']}: " + ", ".join(_fmt(e) for e in d['exercises']) for d in plan["days"]], structural=True)


def infer_style(plan: dict) -> str:
    if plan.get("style") in STYLES:
        return plan["style"]
    names = " ".join(norm_name(d["name"]) for d in plan.get("days", []))
    if "push" in names or "pull" in names:
        return "push_pull_legs"
    if "upper" in names or "lower" in names:
        return "upper_lower"
    if "chest" in names or "back" in names or "arms" in names:
        return "bro_split"
    return "full_body" if len(plan.get("days", [])) <= 3 else "upper_lower"


def set_days(old_plan: dict, days, ctx: Ctx, style=None) -> Result:
    """Change days per week, keeping the current style when it supports that many days (else the closest one)."""
    try:
        n = int(days)
    except (TypeError, ValueError):
        raise OpError("How many days a week? Give a number.")
    if n > MAX_DAYS:
        raise OpError(f"{n} days a week leaves no rest day; {MAX_DAYS} is the most I will program.", "refused", "no_rest_day")
    if n < MIN_DAYS:
        raise OpError(f"Training {n} day a week is below the {MIN_DAYS}-day minimum I program. Pick {MIN_DAYS} or more.")
    st = style_of(style) if style else infer_style(old_plan)
    lo, hi = STYLE_DAYS[st]
    if not lo <= n <= hi:
        st = "full_body" if n <= 3 else "upper_lower" if n == 4 else "push_pull_legs"
    r = build_style(old_plan, st, n, ctx)
    r.summary = f"Moved you to {n} training days a week ({STYLE_LABEL[st]}: {', '.join(d['name'] for d in r.plan['days'])})"
    return r


# ---- single edits ----
def add_exercise(old_plan: dict, ctx: Ctx, day, name, sets=None, reps=None, target_rpe=None, position=None) -> Result:
    plan = _fresh(old_plan)
    i = find_day(plan, day)
    d = plan["days"][i]
    n = _check_exercise(name, ctx)
    if any(e["name"] == n for e in d["exercises"]):
        raise OpError(f"{n} is already in {d['name']}.")
    if len(d["exercises"]) >= MAX_EX_PER_DAY:
        raise OpError(f"{d['name']} already has {MAX_EX_PER_DAY} exercises, the most one session can hold. Remove one first.")
    ds, dr = _defaults(n)
    e = _numbers(n, sets if sets is not None else ds, reps if reps is not None else dr, target_rpe if target_rpe is not None else 8.0)
    pos = len(d["exercises"]) if position is None else max(0, min(len(d["exercises"]), int(position) - 1))
    d["exercises"].insert(pos, e)
    _check_weekly_cap(plan, {EXERCISES[n][0]})
    return Result(plan, f"Added {_fmt(e)} to {d['name']}", [f"{d['name']}: + {_fmt(e)}"])


def remove_exercise(old_plan: dict, ctx: Ctx, day, name) -> Result:
    plan = _fresh(old_plan)
    d = plan["days"][find_day(plan, day)]
    j = find_exercise(d, name)
    if len(d["exercises"]) <= MIN_EX_AFTER_EDIT:
        raise OpError(f"{d['name']} would be left with {len(d['exercises']) - 1} exercise(s); a day needs at least {MIN_EX_AFTER_EDIT}. Replace it instead.")
    gone = d["exercises"].pop(j)
    return Result(plan, f"Removed {gone['name']} from {d['name']}", [f"{d['name']}: - {_fmt(gone)}"])


def replace_exercise(old_plan: dict, ctx: Ctx, day, name, replacement, scope="plan", today_index: int | None = None, today: str = "") -> Result:
    """scope 'plan' edits the split; scope 'today' only changes today's session (expires on its own, like a swap)."""
    plan = _fresh(old_plan)
    scope = norm_name(scope or "plan")
    if scope not in ("plan", "today"):
        raise OpError("scope must be 'today' or 'plan'.")
    rep = _check_exercise(replacement, ctx)
    if scope == "today":
        idx = today_index if (day is None or str(day).strip() == "") else find_day(plan, day)
        if idx is None:
            raise OpError("Which day is today?")
        ov = plan.get("today_override")
        live = ov and ov.get("date") == today and ov.get("day_index") == idx
        if not live:
            ov = plan["today_override"] = {"date": today, "day_index": idx, "minutes": None, "day": copy.deepcopy(plan["days"][idx]), "swaps": [], "reason": "swap"}
        d = ov["day"]
        j = find_exercise(d, name)
        if any(e["name"] == rep for e in d["exercises"]):
            raise OpError(f"{rep} is already in today's session.")
        old = d["exercises"][j]["name"]
        d["exercises"][j]["name"] = rep
        _fix_plank(d["exercises"][j], old)
        ov.setdefault("swaps", []).append([old, rep])
        return Result(plan, f"Today only: {old} -> {rep} ({plan['days'][idx]['name']})", [f"today: {old} -> {rep}"])
    di = find_day(plan, day)
    d = plan["days"][di]
    j = find_exercise(d, name)
    if any(e["name"] == rep for e in d["exercises"]):
        raise OpError(f"{rep} is already in {d['name']}.")
    old = d["exercises"][j]["name"]
    d["exercises"][j]["name"] = rep
    _fix_plank(d["exercises"][j], old)
    ov = plan.get("today_override")
    if ov and ov.get("day_index") == di and ov.get("date") == today:
        for e in ov["day"]["exercises"]:
            if e["name"] == old:
                e["name"] = rep
                _fix_plank(e, old)
    _check_weekly_cap(plan, {EXERCISES[rep][0]})
    return Result(plan, f"Replaced {old} with {rep} in {d['name']} (permanent)", [f"{d['name']}: {old} -> {rep}"])


def _fix_plank(e: dict, old: str) -> None:
    if e["name"] == "plank" and old != "plank":
        e["reps"] = 45
    elif old == "plank" and e["name"] != "plank":
        e["reps"] = _defaults(e["name"])[1]


def change_numbers(old_plan: dict, ctx: Ctx, day, name, sets=None, reps=None, target_rpe=None) -> Result:
    if sets is None and reps is None and target_rpe is None:
        raise OpError("Tell me what to change: sets, reps and/or target RPE.")
    plan = _fresh(old_plan)
    d = plan["days"][find_day(plan, day)]
    e = d["exercises"][find_exercise(d, name)]
    before = _fmt(e) + f" @RPE {e.get('target_rpe', 8.0)}"
    e.update(_numbers(e["name"], sets if sets is not None else e["sets"], reps if reps is not None else e["reps"],
                      target_rpe if target_rpe is not None else e.get("target_rpe", 8.0)))
    _check_weekly_cap(plan, {EXERCISES[e["name"]][0]})
    after = _fmt(e) + f" @RPE {e['target_rpe']}"
    return Result(plan, f"{d['name']}: {e['name']} is now {after} (was {before})", [f"{d['name']}: {e['name']} {before} -> {after}"])


def move_exercise(old_plan: dict, ctx: Ctx, from_day, name, to_day, position=None) -> Result:
    plan = _fresh(old_plan)
    a, b = find_day(plan, from_day), find_day(plan, to_day)
    if a == b:
        raise OpError("That exercise is already on that day.")
    src, dst = plan["days"][a], plan["days"][b]
    j = find_exercise(src, name)
    if len(src["exercises"]) <= MIN_EX_AFTER_EDIT:
        raise OpError(f"{src['name']} would be left with fewer than {MIN_EX_AFTER_EDIT} exercises.")
    if len(dst["exercises"]) >= MAX_EX_PER_DAY:
        raise OpError(f"{dst['name']} is full ({MAX_EX_PER_DAY} exercises).")
    if any(e["name"] == src["exercises"][j]["name"] for e in dst["exercises"]):
        raise OpError(f"{src['exercises'][j]['name']} is already on {dst['name']}.")
    e = src["exercises"].pop(j)
    pos = len(dst["exercises"]) if position is None else max(0, min(len(dst["exercises"]), int(position) - 1))
    dst["exercises"].insert(pos, e)
    return Result(plan, f"Moved {e['name']} from {src['name']} to {dst['name']}", [f"{e['name']}: {src['name']} -> {dst['name']}"])


def reorder_days(old_plan: dict, order: list) -> Result:
    plan = _fresh(old_plan)
    if not isinstance(order, list) or len(order) != len(plan["days"]):
        raise OpError(f"Give all {len(plan['days'])} days in the new order (numbers or names).")
    idx = [find_day(plan, o) for o in order]
    if sorted(idx) != list(range(len(plan["days"]))):
        raise OpError("Each day must appear exactly once in the new order.")
    plan["days"] = [plan["days"][i] for i in idx]
    plan.pop("today_override", None)
    return Result(plan, "New day order: " + ", ".join(d["name"] for d in plan["days"]), ["order: " + " > ".join(d["name"] for d in plan["days"])], structural=True)


def rename_day(old_plan: dict, day, new_name) -> Result:
    plan = _fresh(old_plan)
    i = find_day(plan, day)
    nm = " ".join(str(new_name or "").split())[:30]
    if not nm:
        raise OpError("The new name cannot be empty.")
    if any(norm_name(d["name"]) == nm.lower() for k, d in enumerate(plan["days"]) if k != i):
        raise OpError(f"Another day is already called '{nm}'.")
    old = plan["days"][i]["name"]
    plan["days"][i]["name"] = nm
    if plan.get("today_override") and plan["today_override"].get("day_index") == i:
        plan["today_override"]["day"]["name"] = nm
    return Result(plan, f"Renamed day {i + 1} from {old} to {nm}", [f"{old} -> {nm}"], structural=True)
