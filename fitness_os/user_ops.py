"""The athlete's own changes, prepared as validated operations. control.submit_user_change is the only caller.

prepare(op, params) validates everything (hard limits live HERE, in code) and returns a Prep: what will change, in words, plus an
`apply()` that performs the writes and returns the undo recipe. Nothing is written until apply() runs, so the debate gate can look
at the proposed change first. Hard limits raise OpError(kind="refused") and can never be argued past.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Callable

from backend.memory import db
from backend.tools import exercises as ex
from backend.tools import plan_edit as pe
from backend.tools import split_ops as so
from backend.tools.exercises import EXERCISES
from backend.tools.planner import apply_volume, derive_avoid
from backend.tools.split_ops import OpError

CALORIE_FLOOR = {"m": 1500, "f": 1200}
CALORIE_CEILING = 6000
VOLUME_BOUNDS = (0.4, 1.6)        # x baseline, absolute for the athlete too (agents are held tighter by policy.py)
MAX_WEIGHT_KG, MAX_REPS = 700.0, 100

TRAINING_OPS = {"restructure_split", "set_days_per_week", "add_exercise", "remove_exercise", "replace_exercise", "change_sets_reps_rpe", "move_exercise",
                "reorder_days", "rename_day", "change_volume", "rebuild_plan", "fit_session", "skip_session", "restore_session", "clear_restriction"}
NUTRITION_OPS = {"set_goal", "set_calories", "set_macros"}


@dataclass
class Prep:
    op: str
    params: dict
    summary: str
    changes: list[str] = field(default_factory=list)
    structural: bool = False
    domain: str = ""                      # 'training' | 'nutrition' | '' (logging and notes are never frozen)
    old_plan: dict | None = None
    new_plan: dict | None = None
    facts: dict = field(default_factory=dict)
    apply: Callable[[], dict] = lambda: {}


# ---- shared state helpers ----
def _plan() -> dict:
    p = db.get_plan()
    if not p:
        raise OpError("There is no plan yet; set up your profile first.")
    return p


def _profile() -> dict:
    return db.get_profile() or {}


def _floor(prof: dict) -> int:
    return CALORIE_FLOOR.get(str(prof.get("sex", "m")).lower()[:1], 1500)


def _save_plan_undo(old: dict, new: dict, summary: str, structural: bool, agent_note: str = "Your request") -> dict:
    cur = db.get_plan() or {}
    new["version"] = int(cur.get("version", 0)) + 1
    db.save_plan(new)
    db.log_change("you", summary[:120], agent_note, "")
    return {"op": "user_snapshot", "plan": old, "version": new["version"], "structural": structural}


def _plan_prep(op: str, params: dict, r: so.Result, old: dict) -> Prep:
    p = Prep(op, params, r.summary, r.changes, r.structural, "training", old, r.plan)
    p.facts = {"sets_before": so.total_sets(old), "sets_after": so.total_sets(r.plan),
               "lower_before": so.has_lower_body(old), "lower_after": so.has_lower_body(r.plan), "days_after": len(r.plan["days"])}
    p.apply = lambda: _save_plan_undo(old, r.plan, r.summary, r.structural)
    return p


# ---- training ops ----
def _restructure(params: dict) -> Prep:
    old, ctx = _plan(), so.make_ctx()
    if params.get("days") and isinstance(params["days"], list) and params["days"] and isinstance(params["days"][0], dict):
        r = so.custom_split(old, params["days"], ctx)
    else:
        if not params.get("style"):
            raise OpError("Which style? full body, upper/lower, push/pull/legs, bro split, or give me your own days.")
        n = params.get("days_per_week") or params.get("days") or len(old["days"])
        r = so.build_style(old, params["style"], n, ctx)
    prep = _plan_prep("restructure_split", params, r, old)
    prof = _profile()
    n = len(r.plan["days"])

    def apply():
        if prof:
            db.save_profile({**prof, "days_per_week": n})
        undo = _save_plan_undo(old, r.plan, r.summary, True)
        undo["profile"] = prof
        return undo
    prep.apply = apply
    return prep


def _set_days(params: dict) -> Prep:
    old, ctx = _plan(), so.make_ctx()
    r = so.set_days(old, params.get("days"), ctx, params.get("style"))
    prep = _plan_prep("set_days_per_week", params, r, old)
    prof, n = _profile(), len(r.plan["days"])

    def apply():
        if prof:
            db.save_profile({**prof, "days_per_week": n})
        undo = _save_plan_undo(old, r.plan, r.summary, True)
        undo["profile"] = prof
        return undo
    prep.apply = apply
    return prep


def _edit(fn_name: str, op: str, params: dict, *keys: str) -> Prep:
    old, ctx = _plan(), so.make_ctx()
    kw = {k: params.get(k) for k in keys if k in params}
    r = getattr(so, fn_name)(old, ctx, **kw)
    return _plan_prep(op, params, r, old)


def _replace(params: dict) -> Prep:
    old, ctx = _plan(), so.make_ctx()
    scope = params.get("scope") or "plan"
    r = so.replace_exercise(old, ctx, params.get("day"), params.get("exercise"), params.get("replacement"), scope,
                            today_index=pe._rotation_index(old), today=ex.today())
    return _plan_prep("replace_exercise", params, r, old)


def _change_volume(params: dict) -> Prep:
    old = _plan()
    try:
        frac = float(params.get("fraction"))
    except (TypeError, ValueError):
        raise OpError("Give the volume change as a fraction, e.g. 0.1 for +10% or -0.2 for -20%.")
    if abs(frac) > 1.0:
        raise OpError("That is not a believable volume change (more than 100%).", "refused", "absurd_value")
    cur, base = float(old.get("volume_mult", 1.0)), float(old.get("base_volume", 1.0))
    new = round(cur * (1 + frac), 2)
    lo, hi = round(base * VOLUME_BOUNDS[0], 2), round(base * VOLUME_BOUNDS[1], 2)
    if not lo <= new <= hi:
        raise OpError(f"Volume x{new} is outside the safe range x{lo}-x{hi} (0.4x-1.6x your baseline). I will not go beyond that; "
                      f"the biggest change from x{cur} is to x{hi if frac > 0 else lo}.", "refused", "volume_bounds")
    if new == cur:
        raise OpError(f"Volume is already x{cur}; that change is too small to move any set counts.")
    plan = copy.deepcopy(old)
    apply_volume(plan, new)
    plan["last_vol_dir"] = 1 if new > cur else -1
    plan["last_vol_ts"] = db.now()
    plan.pop("today_override", None)
    summary = f"Training volume x{cur} -> x{new} ({'+' if new > cur else ''}{round((new / cur - 1) * 100)}% sets)"
    r = so.Result(plan, summary, [f"volume x{cur} -> x{new}: weekly sets {so.total_sets(old)} -> {so.total_sets(plan)}"])
    prep = _plan_prep("change_volume", params, r, old)
    prep.facts.update(volume_from=cur, volume_to=new, fraction=round(new / cur - 1, 3))
    return prep


def _rebuild(params: dict) -> Prep:
    old, prof = _plan(), _profile()
    prefs = " ".join(str(params.get("preferences") or "").split())[:300]
    n = params.get("days_per_week")
    if n is not None:
        try:
            n = int(n)
        except (TypeError, ValueError):
            raise OpError("days_per_week must be a number.")
        if n > so.MAX_DAYS:
            raise OpError(f"{n} days a week leaves no rest day; {so.MAX_DAYS} is the most I will program.", "refused", "no_rest_day")
        if n < so.MIN_DAYS:
            raise OpError(f"{so.MIN_DAYS} training days is the minimum I program.")
    prep = Prep("rebuild_plan", params, "Rebuilt your training plan" + (f" around: {prefs}" if prefs else ""), [], True, "training", old, None,
                {"days_after": n or len(old["days"])})

    def apply():
        from backend.tools.profile_edit import training_plan
        prof2 = {**prof, "days_per_week": n or prof.get("days_per_week", len(old["days"]))}
        mem_ids = []
        if prefs:
            db.add_memory("preference", prefs)
            with db.conn() as c:
                mem_ids.append(c.execute("SELECT MAX(id) AS m FROM memory").fetchone()["m"])
        new = training_plan(prof2)
        for k in ("nutrition", "base_calories", "calorie_mult", "goal"):
            if k in old:
                new[k] = old[k]
        if new.get("designed_by") != "training-agent":
            new.setdefault("designed_by", "starter-template")
        if prof:
            db.save_profile(prof2)
        undo = _save_plan_undo(old, new, prep.summary, True)
        undo.update(profile=prof, delete_memory=mem_ids)
        prep.new_plan = new
        prep.changes = [f"{d['name']}: " + ", ".join(f"{e['name']} {e['sets']}x{e['reps']}" for e in d["exercises"]) for d in new["days"]]
        prep.summary = f"Rebuilt your plan: {len(new['days'])} days ({', '.join(d['name'] for d in new['days'])}), designed by {new.get('designed_by')}"
        return undo
    prep.apply = apply
    return prep


def _fit(params: dict) -> Prep:
    old = _plan()
    try:
        m = int(params.get("minutes"))
    except (TypeError, ValueError):
        raise OpError("How many minutes do you have?")
    if not 10 <= m <= 240:
        raise OpError("Minutes must be between 10 and 240.")
    prep = Prep("fit_session", params, f"Fitted today's session to {m} minutes", [], False, "training", old)

    def apply():
        v = pe.fit_today(m)
        day = (v.get("today_override") or {}).get("day") or {}
        prep.changes = [f"today ({day.get('name', '')}): " + ", ".join(f"{e['name']} {e['sets']}x{e['reps']}" for e in day.get("exercises", []))]
        prep.summary = f"Fitted today's {day.get('name', 'session')} to {m} minutes (about {day.get('est_minutes', m)} min)"
        return {"op": "user_snapshot", "plan": old}
    prep.apply = apply
    return prep


def _restore(params: dict) -> Prep:
    old = _plan()
    if not old.get("today_override"):
        raise OpError("Today's session is already the full planned one; nothing to restore.")
    prep = Prep("restore_session", params, "Restored today's full planned session", ["today's override cleared"], False, "training", old)

    def apply():
        pe.clear_override()
        return {"op": "user_snapshot", "plan": old}
    prep.apply = apply
    return prep


def _skip(params: dict) -> Prep:
    old = _plan()
    plan = copy.deepcopy(old)
    today = ex.today()
    if any(s.get("date") == today for s in plan.get("skipped", [])):
        raise OpError("Today's session is already skipped.")
    skipped = list(plan.get("skipped", []))[-9:]
    idx = pe._rotation_index(old)
    skipped.append({"date": today, "day": old["days"][idx]["name"], "reason": str(params.get("reason") or "")[:120]})
    plan["skipped"] = skipped
    name = old["days"][idx]["name"]
    nxt = old["days"][(idx + 1) % len(old["days"])]["name"]
    r = so.Result(plan, f"Skipped today's session ({name}). The rotation moves on: {nxt} is next", [f"skipped {name} on {today}", f"next: {nxt}"])
    return _plan_prep("skip_session", params, r, old)


# ---- nutrition ----
def _with_nutrition(plan: dict, cal: int, prof: dict, protein=None, fat=None, carbs=None) -> dict:
    n = plan.setdefault("nutrition", {})
    p = protein if protein is not None else n.get("protein_g", round(prof.get("weight_kg", 75) * 1.8))
    f = fat if fat is not None else n.get("fat_g", round(prof.get("weight_kg", 75) * 0.8))
    c = carbs if carbs is not None else max(0, round((cal - p * 4 - f * 9) / 4))
    n.update(calories=int(cal), protein_g=int(p), fat_g=int(f), carbs_g=int(c))
    plan["base_calories"], plan["calorie_mult"] = int(cal), 1.0   # agent nudges are relative to the athlete's own baseline from now on
    return plan


def _tdee(prof: dict) -> int | None:
    from backend.tools import calculators as calc
    try:
        return round(calc.tdee(calc.mifflin_st_jeor(prof["weight_kg"], prof["height_cm"], prof["age"], prof.get("sex", "m")), prof.get("activity", "moderate")))
    except (KeyError, TypeError):
        return None


def _nutrition_prep(op: str, params: dict, old: dict, new: dict, prof: dict, summary: str, changes: list[str]) -> Prep:
    prep = Prep(op, params, summary, changes, False, "nutrition", old, new)
    before = old.get("nutrition", {})
    prep.facts = {"cal_from": before.get("calories"), "cal_to": new["nutrition"]["calories"], "goal": prof.get("goal", "maintain"), "tdee": _tdee(prof)}
    prep.apply = lambda: _save_plan_undo(old, new, summary, False)
    return prep


def _set_calories(params: dict) -> Prep:
    old, prof = _plan(), _profile()
    try:
        cal = round(float(params.get("calories")))
    except (TypeError, ValueError):
        raise OpError("Give the daily calories as a number, e.g. 2400.")
    floor = _floor(prof)
    if cal < floor:
        raise OpError(f"{cal} kcal is below the {floor} kcal safety floor. I will not set calories that low; if you want to lose weight faster, "
                      f"talk to a doctor or dietitian.", "refused", "calorie_floor")
    if cal > CALORIE_CEILING:
        raise OpError(f"{cal} kcal is not a believable daily target (max {CALORIE_CEILING}).", "refused", "absurd_value")
    cur = (old.get("nutrition") or {}).get("calories")
    if not cur:
        raise OpError("There is no nutrition target yet.")
    if cal == cur:
        raise OpError(f"Your target is already {cur} kcal.")
    new = _with_nutrition(copy.deepcopy(old), cal, prof)
    n = new["nutrition"]
    return _nutrition_prep("set_calories", params, old, new, prof, f"Calories {cur} -> {cal} kcal/day (protein {n['protein_g']}g, carbs {n['carbs_g']}g, fat {n['fat_g']}g)",
                           [f"calories {cur} -> {cal}", f"carbs adjusted to {n['carbs_g']}g to fit"])


def _set_macros(params: dict) -> Prep:
    old, prof = _plan(), _profile()
    n0 = old.get("nutrition") or {}
    if not n0:
        raise OpError("There is no nutrition target yet.")
    vals = {}
    for k, lo, hi in (("protein_g", 40, 400), ("fat_g", 20, 250), ("carbs_g", 0, 800)):
        if params.get(k) is not None:
            try:
                v = round(float(params[k]))
            except (TypeError, ValueError):
                raise OpError(f"{k} must be a number.")
            if not lo <= v <= hi:
                raise OpError(f"{k} {v} is outside {lo}-{hi}.", "refused", "absurd_value")
            vals[k] = v
    if not vals:
        raise OpError("Tell me which macro(s): protein_g, fat_g and/or carbs_g.")
    p, f = vals.get("protein_g", n0.get("protein_g")), vals.get("fat_g", n0.get("fat_g"))
    if "carbs_g" in vals:
        c = vals["carbs_g"]
        cal = round(p * 4 + c * 4 + f * 9)          # carbs given explicitly: calories follow the macros
    else:
        cal = n0["calories"]                         # otherwise calories stay and carbs fill what is left
        c = max(0, round((cal - p * 4 - f * 9) / 4))
        if p * 4 + f * 9 > cal:
            raise OpError(f"{p}g protein + {f}g fat already exceed {cal} kcal. Lower them or raise calories.")
    floor = _floor(prof)
    if cal < floor:
        raise OpError(f"Those macros add up to {cal} kcal, below the {floor} kcal safety floor.", "refused", "calorie_floor")
    if cal > CALORIE_CEILING:
        raise OpError(f"Those macros add up to {cal} kcal, which is not believable.", "refused", "absurd_value")
    new = _with_nutrition(copy.deepcopy(old), cal, prof, p, f, c)
    return _nutrition_prep("set_macros", params, old, new, prof, f"Macros set to {p}g protein, {c}g carbs, {f}g fat ({cal} kcal/day)",
                           [f"protein {n0.get('protein_g')} -> {p}g", f"carbs {n0.get('carbs_g')} -> {c}g", f"fat {n0.get('fat_g')} -> {f}g", f"calories {n0.get('calories')} -> {cal}"])


def _set_goal(params: dict) -> Prep:
    from backend.tools.profile_edit import nutrition_targets

    old, prof = _plan(), _profile()
    g = str(params.get("goal", "")).strip().lower()
    g = {"lose": "cut", "lose fat": "cut", "fat loss": "cut", "cutting": "cut", "gain": "bulk", "muscle": "bulk", "bulking": "bulk", "build muscle": "bulk",
         "maintenance": "maintain", "recomp": "maintain"}.get(g, g)
    if g not in ("cut", "bulk", "maintain"):
        raise OpError("Goal must be cut, bulk or maintain.")
    if prof.get("goal") == g:
        raise OpError(f"Your goal is already {g}.")
    new_prof = {**prof, "goal": g}
    t = nutrition_targets(new_prof)
    floor = _floor(prof)
    if t["calories"] < floor:
        t = {**t, "calories": floor}
    new = copy.deepcopy(old)
    new["nutrition"] = {k: t[k] for k in t}
    new["base_calories"], new["calorie_mult"], new["goal"] = t["calories"], 1.0, g
    prep = Prep("set_goal", params, f"Goal changed from {prof.get('goal')} to {g}; calories reset to {t['calories']} kcal (protein {t['protein_g']}g)", [], False, "nutrition", old, new,
                {"cal_from": (old.get("nutrition") or {}).get("calories"), "cal_to": t["calories"], "goal": g, "tdee": _tdee(prof)})
    prep.changes = [f"goal {prof.get('goal')} -> {g}", f"calories {prep.facts['cal_from']} -> {t['calories']}"]

    def apply():
        db.save_profile(new_prof)
        undo = _save_plan_undo(old, new, prep.summary, False)
        undo["profile"] = prof
        return undo
    prep.apply = apply
    return prep


# ---- memory / restrictions ----
def _last_memory_id() -> int:
    with db.conn() as c:
        return c.execute("SELECT COALESCE(MAX(id), 0) AS m FROM memory").fetchone()["m"]


def _add_restriction(params: dict) -> Prep:
    prof, plan = _profile(), db.get_plan() or {}
    text = " ".join(str(params.get("text", "")).split())[:200]
    if len(text) < 3:
        raise OpError("What is the injury or restriction? A few words, e.g. 'left knee pain on deep squats'.")
    avoid = derive_avoid([text], [])
    conflicts = []
    for i, d in enumerate(plan.get("days", [])):
        for e in d["exercises"]:
            if any(a in e["name"] for a in avoid):
                alts = [a["name"] for a in ex.alternatives(e["name"], [*prof.get("injuries", []), text], prof.get("avoid", []))][:3]
                conflicts.append({"day": d["name"], "day_number": i + 1, "exercise": e["name"], "safe_alternatives": alts})
    prep = Prep("add_restriction", params, f"Noted restriction: {text}" + (f" ({len(conflicts)} planned exercises conflict; swap them)" if conflicts else ""),
                [f"restriction: {text}"] + [f"conflict: {c['exercise']} on {c['day']}" for c in conflicts], False, "", None, None, {"conflicts": conflicts})

    def apply():
        before = _last_memory_id()
        inj = list(prof.get("injuries", []))
        if text not in inj:
            inj.append(text)
        if prof:
            db.save_profile({**prof, "injuries": inj})
        db.add_memory("injury", text)
        db.log_change("you", f"Restriction: {text}", "Your request", "")
        return {"op": "user_snapshot", "profile": prof, "delete_memory": list(range(before + 1, _last_memory_id() + 1))}
    prep.apply = apply
    return prep


def _add_preference(params: dict) -> Prep:
    text = " ".join(str(params.get("text", "")).split())[:200]
    if len(text) < 3:
        raise OpError("What is the preference? A few words.")
    prep = Prep("add_preference", params, f"Noted your preference: {text}", [f"preference: {text}"])

    def apply():
        from fitness_os import memory
        before = _last_memory_id()
        db.add_memory("preference", text)
        m = memory.add("preference", text, 0.7, pinned=True)
        db.log_change("you", f"Preference: {text}", "Your request", "")
        return {"op": "user_snapshot", "delete_memory": list(range(before + 1, _last_memory_id() + 1)), "archive_items": [] if m.get("merged") else [m["id"]]}
    prep.apply = apply
    return prep


def _clear_restriction(params: dict) -> Prep:
    from fitness_os import ledger

    prof = _profile()
    q = " ".join(str(params.get("text", "")).lower().split())
    if not q:
        raise OpError("Which restriction should I clear? Say a few words from it.")
    if ledger.get_setting("pain_active", False):
        raise OpError("A pain escalation is active, so training is frozen until a qualified professional clears it. I cannot lift a restriction on my own.", "refused", "pain_active")
    rows = [m for m in db.get_memory(100) if m["kind"] == "injury" and (q in m["text"].lower() or m["text"].lower() in q)]
    inj = [i for i in prof.get("injuries", []) if q in i.lower() or i.lower() in q]
    if not rows and not inj:
        have = [m["text"] for m in db.get_memory(100) if m["kind"] == "injury"][:5]
        raise OpError(f"I have no restriction matching '{q}'." + (f" Current ones: {'; '.join(have)}." if have else ""))
    prep = Prep("clear_restriction", params, f"Cleared restriction: {(rows[0]['text'] if rows else inj[0])}", [f"cleared: {r['text']}" for r in rows] or [f"cleared: {i}" for i in inj],
                False, "training", None, None, {"age_days": min([db.age_days(r["ts"]) for r in rows] or [999]), "texts": [r["text"] for r in rows] or inj})

    def apply():
        keep = [i for i in prof.get("injuries", []) if i not in inj]
        if prof:
            db.save_profile({**prof, "injuries": keep})
        with db.conn() as c:
            for r in rows:
                c.execute("DELETE FROM memory WHERE id = ?", (r["id"],))
        db.log_change("you", prep.summary[:120], "Your request", "")
        return {"op": "user_snapshot", "profile": prof, "add_memory": [{"kind": r["kind"], "text": r["text"]} for r in rows]}
    prep.apply = apply
    return prep


# ---- logging ----
def _log_set(params: dict) -> Prep:
    plan = db.get_plan() or {}
    name = so.norm_name(params.get("exercise"))
    in_plan = any(e["name"] == name for d in plan.get("days", []) for e in d["exercises"])
    if not name or (name not in EXERCISES and not in_plan):
        raise OpError(f"'{name}' is not an exercise I know. Use a library name.")
    w = params.get("weight")
    try:
        w = None if w is None else float(w)
        reps = int(params.get("reps"))
        rpe = None if params.get("rpe") is None else float(params.get("rpe"))
    except (TypeError, ValueError):
        raise OpError("A set needs numbers: weight (kg), reps and optionally RPE.")
    if (w is not None and not 0 <= w <= MAX_WEIGHT_KG) or not 1 <= reps <= MAX_REPS or (rpe is not None and not 1 <= rpe <= 10):
        raise OpError(f"That set is not believable (weight 0-{MAX_WEIGHT_KG:g} kg, reps 1-{MAX_REPS}, RPE 1-10).", "refused", "absurd_value")
    prep = Prep("log_set", params, f"Logged {name}: {'bodyweight' if not w else f'{w:g} kg'} x {reps}" + (f" @RPE {rpe:g}" if rpe else ""), [])

    def apply():
        sid = db.add_set(ex.today(), name, w, reps, rpe, params.get("note"))
        return {"op": "user_snapshot", "delete_sets": [sid]}
    prep.apply = apply
    prep.changes = [prep.summary]
    return prep


def _log_meal(params: dict) -> Prep:
    from backend.tools import meal_log

    text = " ".join(str(params.get("description", "")).split())[:300]
    if len(text) < 2:
        raise OpError("What did you eat? e.g. '2 eggs and toast'.")
    prep = Prep("log_meal", params, f"Logged meal: {text}", [])

    def apply():
        with db.conn() as c:
            before = c.execute("SELECT COALESCE(MAX(id), 0) AS m FROM meals").fetchone()["m"]
        out = meal_log.log_meal(text, params.get("day"))
        with db.conn() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM meals WHERE id > ?", (before,)).fetchall()]
        if not ids:
            raise OpError("I could not recognise any food in that. Try naming the foods and amounts.")
        kcal = round(sum(i.get("kcal", 0) for i in out["items"]))
        prep.summary = f"Logged meal ({kcal} kcal): " + ", ".join(i["name"] for i in out["items"])
        prep.changes = [f"{i['name']}: {round(i.get('kcal', 0))} kcal, {round(i.get('protein', 0))}g protein" for i in out["items"]]
        prep.facts["unknown"] = out["unknown"]
        return {"op": "user_snapshot", "delete_meals": ids}
    prep.apply = apply
    return prep


def _log_bodyweight(params: dict) -> Prep:
    try:
        kg = round(float(params.get("kg")), 1)
    except (TypeError, ValueError):
        raise OpError("Give your body weight in kg.")
    if not 30 <= kg <= 300:
        raise OpError("Body weight must be between 30 and 300 kg.", "refused", "absurd_value")
    prep = Prep("log_bodyweight", params, f"Logged body weight {kg:g} kg", [f"bodyweight {kg:g} kg"])

    def apply():
        from backend.routes_session import WeighIn, weigh_in
        with db.conn() as c:
            last = c.execute("SELECT id, bodyweight FROM checkins ORDER BY id DESC LIMIT 1").fetchone()
        weigh_in(WeighIn(bodyweight=kg))
        with db.conn() as c:
            now = c.execute("SELECT id FROM checkins ORDER BY id DESC LIMIT 1").fetchone()
        if last and now["id"] == last["id"]:
            return {"op": "user_snapshot", "checkin_restore": {"id": last["id"], "bodyweight": last["bodyweight"]}}
        return {"op": "user_snapshot", "delete_checkins": [now["id"]]}
    prep.apply = apply
    return prep


OPS: dict[str, Callable[[dict], Prep]] = {
    "restructure_split": _restructure,
    "set_days_per_week": _set_days,
    "add_exercise": lambda p: _edit("add_exercise", "add_exercise", p, "day", "name", "sets", "reps", "target_rpe", "position"),
    "remove_exercise": lambda p: _edit("remove_exercise", "remove_exercise", p, "day", "name"),
    "replace_exercise": _replace,
    "change_sets_reps_rpe": lambda p: _edit("change_numbers", "change_sets_reps_rpe", p, "day", "name", "sets", "reps", "target_rpe"),
    "move_exercise": lambda p: _edit("move_exercise", "move_exercise", p, "from_day", "name", "to_day", "position"),
    "reorder_days": lambda p: _reorder(p),
    "rename_day": lambda p: _rename(p),
    "change_volume": _change_volume,
    "rebuild_plan": _rebuild,
    "fit_session": _fit,
    "skip_session": _skip,
    "restore_session": _restore,
    "set_goal": _set_goal,
    "set_calories": _set_calories,
    "set_macros": _set_macros,
    "add_restriction": _add_restriction,
    "add_preference": _add_preference,
    "clear_restriction": _clear_restriction,
    "log_set": _log_set,
    "log_meal": _log_meal,
    "log_bodyweight": _log_bodyweight,
}


def _reorder(p: dict) -> Prep:
    old = _plan()
    return _plan_prep("reorder_days", p, so.reorder_days(old, p.get("order")), old)


def _rename(p: dict) -> Prep:
    old = _plan()
    return _plan_prep("rename_day", p, so.rename_day(old, p.get("day"), p.get("new_name")), old)


def domain_of(op: str) -> str:
    return "training" if op in TRAINING_OPS else "nutrition" if op in NUTRITION_OPS else ""


def prepare(op: str, params: dict) -> Prep:
    fn = OPS.get(op)
    if not fn:
        raise OpError(f"Unknown change '{op}'.")
    prep = fn(dict(params or {}))
    prep.domain = domain_of(op)
    return prep


# ---- undo ----
def restore(recipe: dict) -> str:
    """Undo for every user change: put back what the change replaced (plan, profile) and remove what it added."""
    msgs = []
    with db.conn() as c:
        for table, key in (("sets", "delete_sets"), ("meals", "delete_meals"), ("memory", "delete_memory"), ("checkins", "delete_checkins")):
            for i in recipe.get(key) or []:
                c.execute(f"DELETE FROM {table} WHERE id = ?", (i,))
        r = recipe.get("checkin_restore")
        if r:
            c.execute("UPDATE checkins SET bodyweight = ? WHERE id = ?", (r["bodyweight"], r["id"]))
    for m in recipe.get("add_memory") or []:
        db.add_memory(m["kind"], m["text"])
    if recipe.get("archive_items"):
        from fitness_os import memory
        for i in recipe["archive_items"]:
            memory.set_status(i, "archived")
    if recipe.get("profile"):
        db.save_profile(recipe["profile"])
        msgs.append("profile restored")
    if recipe.get("plan"):
        cur = db.get_plan() or {}
        plan = copy.deepcopy(recipe["plan"])
        later = int(cur.get("version", 0)) - int(recipe.get("version", cur.get("version", 0)))
        plan["version"] = int(cur.get("version", 0)) + 1
        db.save_plan(plan)
        msgs.append("plan restored" + (f" (this also rolled back {later} later plan change(s))" if later > 0 else ""))
    db.log_change("you", "Reverted a change", "Your request", "undo")
    return "Undone: " + (", ".join(msgs) or "removed what that change added")
