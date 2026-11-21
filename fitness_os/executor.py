"""Applies an approved action to athlete state and returns the undo recipe. Only control.py calls this."""
from __future__ import annotations

import copy

from backend.memory import db
from backend.tools import plan_edit as pe
from backend.tools.planner import apply_volume

from . import clock, ledger


def _plan() -> dict:
    plan = db.get_plan()
    if not plan:
        raise ValueError("No plan yet")
    return plan


def apply(kind: str, params: dict, agent: str, rationale: str, evidence: str) -> tuple[dict, dict | None]:
    """-> (action actually applied, undo recipe)."""
    if kind == "volume_change":
        plan = _plan()
        cur = plan.get("volume_mult", 1.0)
        new = round(max(0.5, cur * (1 + params["fraction"])), 2)
        apply_volume(plan, new)
        plan["last_vol_dir"] = 1 if new > cur else -1
        plan["last_vol_ts"] = db.now()
        plan.pop("today_override", None)
        db.save_plan(plan)
        db.log_change(agent, f"Volume x{new}", rationale, evidence)
        return {"fraction": params["fraction"], "from": cur, "to": new}, {"op": "volume", "to": cur}

    if kind == "calorie_change":
        plan = _plan()
        before = {"calories": plan["nutrition"]["calories"], "calorie_mult": plan.get("calorie_mult", 1.0)}
        base = plan.get("base_calories", plan["nutrition"]["calories"])
        plan["calorie_mult"] = round(before["calorie_mult"] * (1 + params["fraction"]), 3)
        plan["nutrition"]["calories"] = round(base * plan["calorie_mult"])
        db.save_plan(plan)
        db.log_change(agent, f"Calories -> {plan['nutrition']['calories']}", rationale, evidence)
        return {"fraction": params["fraction"], "from": before["calories"], "to": plan["nutrition"]["calories"]}, \
            {"op": "calories", **before}

    if kind == "swap_exercise":
        pe.swap(int(params["day_index"]), params["exercise"], params["replacement"], scope="plan")
        return dict(params), {"op": "swap", "day_index": int(params["day_index"]),
                              "exercise": params["replacement"], "replacement": params["exercise"]}

    if kind in ("restriction", "note"):
        mem_kind = "injury" if kind == "restriction" else params.get("memory_kind", "semantic")
        db.add_memory(mem_kind, params["text"])
        with db.conn() as c:
            mid = c.execute("SELECT MAX(id) AS m FROM memory").fetchone()["m"]
        if kind == "restriction":
            db.log_change(agent, f"Restriction: {params['text']}", rationale, evidence)
        return dict(params), {"op": "memory", "id": mid}

    if kind == "nudge":
        from . import comms
        nid = comms.add(agent, params["text"])
        try:   # optional phone push (opt-in, quiet hours, cap and dial enforced inside); never blocks the apply
            from . import push
            push.notify_nudge(comms.clean(params["text"]), nid)
        except Exception:
            pass
        return {"text": comms.clean(params["text"]), "nudge_id": nid}, {"op": "nudge", "id": nid}

    if kind == "new_block":
        from backend.tools.profile_edit import training_plan
        plan = _plan()
        snapshot = copy.deepcopy(plan)
        new = training_plan(db.get_profile() or {})
        for k in ("nutrition", "base_calories", "calorie_mult"):
            if k in plan:
                new[k] = plan[k]
        db.save_plan(new)
        db.log_change(agent, "Started a new training block", rationale, evidence)
        return {"rebuilt": True}, {"op": "plan_snapshot", "plan": snapshot}

    if kind == "escalate":
        domain = params.get("domain", "training")
        ledger.freeze(domain)
        if domain in ("training", "all"):
            ledger.set_setting("pain_active", True)
        db.log_change(agent, f"Escalated: {domain} frozen", rationale, evidence)
        try:   # safety is the one category allowed through quiet hours; no health detail in the push text
            from . import push
            push.notify_safety(f"Your {domain} plan is paused. Open the app to see what to do next.")
        except Exception:
            pass
        return {"frozen": domain}, {"op": "unfreeze", "domain": domain}

    raise ValueError(f"unknown kind {kind}")


def apply_user(prep) -> dict:
    """The athlete's own change (prepared and validated by user_ops): perform the writes, return the undo recipe."""
    return prep.apply()


def undo(recipe: dict) -> str:
    op = recipe["op"]
    if op == "user_snapshot":
        from . import user_ops
        return user_ops.restore(recipe)
    if op == "volume":
        plan = _plan()
        apply_volume(plan, recipe["to"])
        plan["last_vol_dir"] = 0
        db.save_plan(plan)
        db.log_change("you", f"Reverted volume to x{recipe['to']}", "Decision reverted", "revert")
        return f"Volume restored to x{recipe['to']}"
    if op == "calories":
        plan = _plan()
        plan["nutrition"]["calories"] = recipe["calories"]
        plan["calorie_mult"] = recipe["calorie_mult"]
        db.save_plan(plan)
        db.log_change("you", f"Reverted calories to {recipe['calories']}", "Decision reverted", "revert")
        return f"Calories restored to {recipe['calories']}"
    if op == "swap":
        plan = _plan()
        if not pe._replace(plan["days"][recipe["day_index"]], recipe["exercise"], recipe["replacement"]):
            raise ValueError("Exercise to restore is no longer in the plan")
        db.save_plan(plan)
        return f"Swapped back to {recipe['replacement']}"
    if op == "memory":
        with db.conn() as c:
            c.execute("DELETE FROM memory WHERE id = ?", (recipe["id"],))
        return "Removed the stored note"
    if op == "plan_snapshot":
        db.save_plan(recipe["plan"])
        return "Previous plan restored"
    if op == "nudge":
        from . import comms
        comms.remove(recipe["id"])
        return "Message withdrawn"
    if op == "unfreeze":
        ledger.unfreeze(recipe["domain"])
        return f"{recipe['domain']} unfrozen"
    raise ValueError(f"unknown undo op {op}")
