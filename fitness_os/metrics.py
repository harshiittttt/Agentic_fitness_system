"""Success metrics (docs section 10), computed from the decision ledger and the logs. Read-only.

    adherence             sessions done / planned
    goal_progress         how many lifts trend up / flat / down, plateaus
    autonomy              acceptance vs override of autonomous actions (override = reverted, vetoed or rejected)
    safety                escalations raised, and red-flag notes that did NOT lead to a freeze (the target is zero)
    disruptions           how long it took to get back to training after a disruption
    cost                  model calls since the server started (backend.agents.base.USAGE)
"""
from __future__ import annotations

from backend.memory import db

from . import clock, ledger, signals

WINDOW_DAYS = 30


def autonomy() -> dict:
    rows = [d for d in ledger.recent(500) if d["tier"] in ("auto", "notify_first", "approve") and d["agent"] not in ("user", "watchdog")
            and clock.age_days(d["ts"]) <= WINDOW_DAYS]
    accepted = sum(1 for d in rows if d["status"] == "applied")
    overridden = sum(1 for d in rows if d["status"] in ("reverted", "vetoed", "rejected"))
    waiting = sum(1 for d in rows if d["status"] in ("pending_approval", "pending_veto"))
    blocked = sum(1 for d in rows if d["status"] == "blocked")
    decided = accepted + overridden
    return {"window_days": WINDOW_DAYS, "accepted": accepted, "overridden": overridden, "waiting": waiting, "blocked_by_policy": blocked,
            "acceptance_rate": round(accepted / decided, 2) if decided else None, "override_rate": round(overridden / decided, 2) if decided else None}


def safety() -> dict:
    from . import watchdogs
    esc = [d for d in ledger.recent(500) if d["status"] == "escalated" and clock.age_days(d["ts"]) <= WINDOW_DAYS]
    frozen = set(ledger.frozen_domains())
    missed = []
    for day, src, text in watchdogs._recent_notes(WINDOW_DAYS):
        if watchdogs.red_flag(text) and not ({"training", "all"} & frozen):
            missed.append({"day": day, "source": src})
        t = text.lower()
        if any(p in t for p in watchdogs.RESTRICTION_LANGUAGE) and not ({"nutrition", "all"} & frozen):
            missed.append({"day": day, "source": src})
    return {"escalations_30d": len(esc), "frozen_domains": sorted(frozen), "red_flags_without_freeze": missed, "false_negatives": len(missed)}


def goal_progress() -> dict:
    from backend.agents import specialists as sp
    try:
        rep = sp.analyst_report()
    except Exception:
        return {"available": False}
    lifts = rep.get("lifts", {})
    cnt = {"up": 0, "flat": 0, "down": 0}
    for v in lifts.values():
        cnt[v.get("trend", "flat") if v.get("trend") in cnt else "flat"] += 1
    return {"available": bool(lifts), "lifts_trending": cnt, "plateaus": rep.get("plateaus", [])}


def disruptions() -> dict:
    log = ledger.get_setting("disruption_log", []) or []
    days = [e["days"] for e in log if e.get("days") is not None]
    return {"count": len(log), "avg_days_to_recover": round(sum(days) / len(days), 1) if days else None, "recent": log[-5:],
            "active": ledger.get_setting("disruption")}


def cost() -> dict:
    from backend.agents import base
    u = getattr(base, "USAGE", {})
    return {"model_calls_since_start": u.get("calls", 0), "by_role": dict(u.get("by_role", {})), "since": u.get("since")}


def report() -> dict:
    adh = signals.adherence()
    return {"as_of": clock.iso(), "adherence": {k: adh[k] for k in ("planned_per_week", "sessions_14d", "planned_14d", "rate_14d", "sessions_28d", "planned_28d",
                                                                   "rate_28d", "days_since_last_session")},
            "goal_progress": goal_progress(), "autonomy": autonomy(), "safety": safety(), "disruptions": disruptions(), "cost": cost()}
