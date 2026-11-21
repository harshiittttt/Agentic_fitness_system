"""Deterministic evidence for the Adherence, Sleep & Stress and Injury & Rehab agents (agents judge, code computes)."""
from __future__ import annotations

import re
from collections import defaultdict

from backend.memory import db

from . import clock, comms, ledger

SLEEP_TARGET_H = 7.5
SILENCE_DAYS = 3


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _session_dates(days: int) -> list[str]:
    """Distinct training days in the last `days`. A session's LABEL is its date (back-dated logging exists), so it wins over the row's insert time."""
    seen = set()
    for s in db.get_sets(limit=1500):
        day = s["session"] if _DATE.match(s["session"] or "") else s["ts"][:10]
        if clock.age_days(day + "T00:00:00") <= days:
            seen.add(day)
    return sorted(seen)


def adherence() -> dict:
    """Sessions done vs planned, the gap since the last session / check-in, and when we last reached out."""
    prof = db.get_profile() or {}
    per_week = int(prof.get("days_per_week") or 3)
    done14, done28 = _session_dates(14), _session_dates(28)
    last_session = max(_session_dates(365), default=None)   # look far back: someone away for 5 weeks is silent, not 'never trained'
    checks = db.get_checkins(40)
    last_check = checks[0]["ts"] if checks else None
    nudges = comms.recent(5)
    days_silent = round(clock.age_days(last_session + "T00:00:00"), 1) if last_session else None
    planned14, planned28 = per_week * 2, per_week * 4
    return {
        "planned_per_week": per_week,
        "sessions_14d": len(done14), "planned_14d": planned14, "rate_14d": round(min(1.0, len(done14) / planned14), 2) if planned14 else None,
        "sessions_28d": len(done28), "planned_28d": planned28, "rate_28d": round(min(1.0, len(done28) / planned28), 2) if planned28 else None,
        "days_since_last_session": days_silent,
        "days_since_last_checkin": round(clock.age_days(last_check), 1) if last_check else None,
        "silent": bool(last_session and days_silent is not None and days_silent >= SILENCE_DAYS),   # never-trained newcomers are not "silent"
        "last_nudges": [{"age_hours": round(clock.age_days(n["ts"]) * 24, 1), "text": n["text"][:80], "status": n["status"]} for n in nudges],
        "quiet_hours_now": comms.in_quiet_hours(),
    }


def sleep_debt() -> dict:
    cs = [c for c in db.get_checkins(14) if c.get("sleep_h") is not None]
    if not cs:
        return {"n": 0, "note": "No sleep data yet."}
    hrs = [c["sleep_h"] for c in cs]
    avg = sum(hrs) / len(hrs)
    recent, older = hrs[:3], hrs[3:7]
    stress = [c for c in cs if (c.get("energy") or 3) <= 2 and (c.get("soreness") or 3) >= 4]
    return {"n": len(hrs), "avg_h": round(avg, 1), "target_h": SLEEP_TARGET_H, "debt_h_per_night": round(max(0.0, SLEEP_TARGET_H - avg), 1),
            "nights_under_6h": sum(1 for h in hrs if h < 6),
            "trend": ("worsening" if older and sum(recent) / len(recent) < sum(older) / len(older) - 0.5 else
                      "improving" if older and sum(recent) / len(recent) > sum(older) / len(older) + 0.5 else "flat"),
            "low_energy_high_soreness_days": len(stress)}


def injury_history() -> dict:
    from . import watchdogs
    from backend.policy.adaptation import has_safety_flag
    by_day: dict[str, list[str]] = defaultdict(list)
    for day, _src, text in watchdogs._recent_notes(60):
        if has_safety_flag(text):
            by_day[day].append(text[:100])
    restrictions = [m["text"] for m in db.get_memory(40) if m["kind"] == "injury"]
    plan = db.get_plan() or {}
    return {"pain_mention_days_60d": sorted(by_day), "samples": {d: v[0] for d, v in sorted(by_day.items())[-4:]},
            "active_restrictions": restrictions, "excluded_exercises": plan.get("excluded", []),
            "training_frozen": "training" in ledger.frozen_domains() or "all" in ledger.frozen_domains(),
            "pain_active": bool(ledger.get_setting("pain_active", False))}
