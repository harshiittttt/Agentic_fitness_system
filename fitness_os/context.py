"""Recall: build the context pack an agent receives (not the whole database), plus read-only analysis tools."""
from __future__ import annotations

from backend.agents import specialists as sp
from backend.memory import db
from backend.tools import calculators as calc

from . import clock, ledger, memory, watchdogs

# Which agents wake for which event (L2 -> L6 routing). Deterministic so it is testable.
ROUTES: dict[str, list[str]] = {
    "session.logged": ["progress", "training", "nutrition"],
    "checkin.logged": ["recovery", "training", "nutrition", "sleep"],
    "pain.report": ["recovery", "training", "injury"],
    "meal.logged": ["nutrition"],
    "cron.daily": ["recovery", "training", "nutrition", "logistics"],
    "cron.weekly": ["progress", "training", "nutrition", "recovery", "adherence", "sleep", "injury", "periodization", "logistics"],
    "proactive.checkin": ["adherence"],                       # silence / streak at risk: one well-timed message
    "disruption.detected": ["adherence", "recovery", "training", "nutrition", "logistics"],   # sick / travel / missed days: re-plan together
    "chat.message": ["training", "nutrition", "recovery"],
    "intake.start": ["intake"], "intake.answer": ["intake"],      # onboarding / re-profiling interview
    "research.question": ["research"], "research.screen": ["research"],   # evidence answers and plan / claim screening
    "form.review": ["form"],                                   # a lift video / pose keypoints to look at (technique cues)
    "calendar.changed": ["periodization", "logistics"],      # a competition, trip, busy day or equipment limit was added
}
CRITIC_EVENTS = set(ROUTES)  # every plan-changing run goes through the Safety Critic


def _slim_plan(plan: dict | None) -> dict | None:
    if not plan:
        return None
    return {
        "volume_mult": plan.get("volume_mult"), "base_volume": plan.get("base_volume", 1.0), "goal": plan.get("goal"),
        "calories": plan.get("nutrition", {}).get("calories"), "macros": {k: v for k, v in plan.get("nutrition", {}).items() if k != "calories"},
        "excluded": plan.get("excluded", []),
        "days": [{"i": i, "name": d["name"], "exercises": [f"{e['name']} {e['sets']}x{e['reps']}" for e in d["exercises"]]}
                 for i, d in enumerate(plan["days"])],
    }


WITHHOLD_HEALTH = ("adherence", "nudge", "comms")


def _athlete_model(agent: str) -> dict | None:
    """The dossier digest (what the system knows about this athlete, scoped to the agent). Never raises."""
    try:
        from .mem.dossier import digest
        d = digest(agent)
        return d if d.get("known") or d.get("not_yet_known") else None
    except Exception:
        return None


def _memories(agent: str, query: str) -> list[dict]:
    """Lessons from past outcomes (legacy memory) plus the athlete memory layer's budgeted pack; falls back to the legacy recall alone if mem fails."""
    legacy = memory.recall(f"{agent} {query}", k=6)
    try:
        from .mem.pack import build as pack_build   # the module directly: the facade's mem.pack() is shadowed by the submodule once it is imported
        pk = pack_build(agent, query, 500)
        extra: list[dict] = [{"kind": "athlete", "body": t, "src": "mem"} for t in pk.core] + [{"kind": h.kind or "semantic", "body": h.text, "src": "mem"} for h in pk.retrieved]
        legacy_part = legacy[:4]
        seen = {str(m.get("body", "")).strip().lower() for m in legacy_part}
        for e in extra:
            k = e["body"].strip().lower()
            if k and k not in seen and len(legacy_part) < 6:
                seen.add(k)
                legacy_part.append(e)
        return legacy_part
    except Exception:
        return legacy


def pack(agent: str, query: str = "") -> dict:
    """Context pack for one agent: plan slice, recent metrics, memories, restrictions, settings, flags."""
    prof = db.get_profile() or {}
    base = {
        "as_of": clock.iso(), "agent": agent,
        "profile": {k: prof.get(k) for k in ("age", "sex", "height_cm", "weight_kg", "goal", "days_per_week", "experience", "injuries", "avoid")},
        "plan": _slim_plan(db.get_plan()),
        "restrictions": [m["text"] for m in db.get_memory(30) if m["kind"] == "injury"],
        "preferences": [m["text"] for m in db.get_memory(30) if m["kind"] == "preference"],
        "autonomy": {"kill_switch": ledger.kill_switch(), "dial": ledger.dial(), "frozen": ledger.frozen_domains()},
        "recent_decisions": [{"id": d["id"], "agent": d["agent"], "kind": d["kind"], "status": d["status"],
                              "age_days": round(clock.age_days(d["ts"]), 1), "rationale": d["rationale"][:100]}
                             for d in ledger.recent(8)],
        "watchdog_flags": [{k: v for k, v in w.items() if k != "proposal"} for w in watchdogs.run_all()],
        # what the system has learned about THIS person (lessons from past outcomes, recent days): small, ranked, relevant
        "memories": _memories(agent, query),
    }
    am = _athlete_model(agent)
    if am:
        base["athlete_model"] = am
    if agent in WITHHOLD_HEALTH:   # outreach copy must never carry health facts: the agent still knows THAT something is going on, not what
        base["restrictions"] = []
        base["profile"] = {**base["profile"], "injuries": None}
    if agent in ("recovery", "training", "orchestrator", "safety_critic", "sleep", "adherence", "injury", "periodization", "logistics"):
        base["checkins_14d"] = [{"date": c["ts"][:10], "sleep_h": c["sleep_h"], "soreness": c["soreness"], "energy": c["energy"],
                                 "bodyweight": c["bodyweight"], "note": c["note"]} for c in db.get_checkins(14)]
    if agent in ("recovery", "sleep", "orchestrator", "training"):
        try:
            from backend.wearables import recent_summary
            ws = recent_summary(7)
            if ws:
                base["wearables_7d"] = ws
        except Exception:
            pass
    if agent in ("periodization", "logistics", "orchestrator", "safety_critic"):
        from . import agenda
        base["calendar"] = agenda.upcoming(120)
    if agent in ("nutrition", "orchestrator"):
        base["bodyweight_series"] = [c["bodyweight"] for c in reversed(db.get_checkins(30)) if c["bodyweight"]]
    return base


# ---- read-only analysis tools (deterministic; agents judge, code computes) ----
def tool(name: str, **kw) -> dict:
    if name == "trends":
        return sp.analyst_report()
    if name == "readiness":
        return sp.recovery_assess()
    if name == "training_signal":
        plan = db.get_plan()
        return sp.training_propose(plan) if plan else {"error": "no plan"}
    if name == "nutrition_signal":
        plan = db.get_plan()
        return sp.nutrition_propose(plan, plan.get("volume_mult", 1.0)) if plan else {"error": "no plan"}
    if name == "e1rm":
        return {"e1rm": round(calc.epley_e1rm(float(kw["weight"]), int(kw["reps"])), 1)}
    if name == "tdee":
        p = db.get_profile() or {}
        bmr = calc.mifflin_st_jeor(p["weight_kg"], p["height_cm"], p["age"], p["sex"])
        t = calc.tdee(bmr, p.get("activity", "moderate"))
        return {"bmr": round(bmr), "tdee": round(t), "goal_calories": round(calc.goal_calories(t, p.get("goal", "maintain")))}
    if name == "adherence":
        from . import signals
        return signals.adherence()
    if name == "sleep_debt":
        from . import signals
        return signals.sleep_debt()
    if name == "injury_history":
        from . import signals
        return signals.injury_history()
    if name == "periodization_signal":
        from . import periodization
        return periodization.signals()
    if name == "logistics_signal":
        from . import logistics
        return logistics.signals()
    if name == "alternatives":
        from backend.tools import plan_edit as pe
        return {"alternatives": [a["name"] for a in pe.get_alternatives(kw["exercise"])]}
    raise ValueError(f"unknown tool {name}")


TOOLS = ("trends", "readiness", "training_signal", "nutrition_signal", "e1rm", "tdee", "alternatives", "adherence", "sleep_debt", "injury_history", "periodization_signal", "logistics_signal")
