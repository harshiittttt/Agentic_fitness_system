"""The four specialist agents. Each computes an evidence-backed proposal deterministically;
the LLM (via the orchestrator) only explains and converses."""
from __future__ import annotations

from collections import defaultdict

from backend.memory import db
from backend.policy.adaptation import (
    Signal, has_safety_flag, readiness_score, recommended_change, trend_signal,
)
from backend.tools.calculators import linear_slope, rpe_adjusted_e1rm

MAIN_LIFTS = ("squat", "bench press", "deadlift", "overhead press", "barbell row")
# Trends judge the RECENT past. Over a whole history, early gains drown out a stall that began later
# (found by the 12-week simulation: plateau and overreach were invisible to full-history trends).
WINDOW = 8
PERSISTENT_LOW_AVG = 55   # mean readiness of the last 4 check-ins below this = a real pattern, not one bad night


def _session_series(sets: list[dict], exercise: str, window: int | None = None) -> tuple[list[float], list[float]]:
    """Per-session best e1RM and mean RPE, oldest first. `window` keeps only the most recent N sessions."""
    by_session: dict[str, list[dict]] = defaultdict(list)
    for s in reversed(sets):  # sets are newest-first
        if s["exercise"] == exercise and s["weight"] and s["reps"]:
            by_session[s["session"]].append(s)
    e1, rpe = [], []
    for ss in by_session.values():
        e1.append(max(rpe_adjusted_e1rm(s["weight"], s["reps"], s["rpe"]) for s in ss))
        r = [s["rpe"] for s in ss if s["rpe"] is not None]
        if r:
            rpe.append(sum(r) / len(r))
    if window:
        e1, rpe = e1[-window:], rpe[-window:]
    return e1, rpe


# ---------------- Progress Analyst ----------------
def analyst_report() -> dict:
    sets = db.get_sets(limit=500)
    lifts = {}
    for lift in MAIN_LIFTS:
        full, _ = _session_series(sets, lift)
        e1, _ = _session_series(sets, lift, WINDOW)
        if len(e1) >= 3:
            sig = trend_signal(lift, e1, scale=max(e1) * 0.01)  # 1%/session counts as strong
            lifts[lift] = {
                "latest_e1rm": full[-1], "sessions": len(full),
                "trend": "up" if sig.strength > 0.15 else "down" if sig.strength < -0.15 else "flat",
                "strength": round(sig.strength, 2), "confidence": round(sig.confidence, 2),
            }
    plateaus = [k for k, v in lifts.items() if v["trend"] == "flat" and v["confidence"] >= 0.5]
    return {"lifts": lifts, "plateaus": plateaus}


def _session_keys(sets: list[dict], exercise: str) -> list[str]:
    """Session labels (dates, in real use) for the points _session_series returns, oldest first."""
    keys: dict[str, None] = {}
    for s in reversed(sets):  # newest-first in, oldest-first out
        if s["exercise"] == exercise and s["weight"] and s["reps"]:
            keys.setdefault(s["session"])
    return list(keys)


def series_report() -> dict:
    """Chart data: per-lift e1RM per session WITH dates, bodyweight and readiness with dates,
    and per-session activity (sets + tonnage) for the weekly-volume bars and the consistency heatmap."""
    sets = db.get_sets(limit=2000)
    lifts, lift_dates = {}, {}
    for lift in MAIN_LIFTS:
        e1, _ = _session_series(sets, lift)
        if e1:
            lifts[lift] = e1
            lift_dates[lift] = _session_keys(sets, lift)
    checks = list(reversed(db.get_checkins(60)))
    with_bw = [c for c in checks if c["bodyweight"]]
    with_sleep = [c for c in checks if c["sleep_h"] is not None]
    act: dict[str, dict] = {}
    for st in reversed(sets):
        if not (st["weight"] and st["reps"]):
            continue
        a = act.setdefault(st["session"], {"date": st["session"], "sets": 0, "tonnage": 0.0})
        a["sets"] += 1
        a["tonnage"] += st["weight"] * st["reps"]
    return {
        "lifts": lifts,
        "lift_dates": lift_dates,
        "bodyweight": [c["bodyweight"] for c in with_bw],
        "bodyweight_dates": [c["ts"][:10] for c in with_bw],
        "readiness": [readiness_score(c["sleep_h"] or 7.5, c["soreness"] or 3, c["energy"] or 3)["score"] for c in with_sleep],
        "readiness_dates": [c["ts"][:10] for c in with_sleep],
        "activity": [{**a, "tonnage": round(a["tonnage"])} for a in act.values()],
        "sessions": len(db.session_names()),
        "total_sets": len(sets),
    }


# ---------------- Recovery ----------------
def recovery_assess() -> dict:
    checks = db.get_checkins(14)
    notes = " ".join((c.get("note") or "") for c in checks[:3])
    mem = " ".join(m["text"] for m in db.get_memory(10) if m["kind"] == "injury")
    flag = has_safety_flag(notes)
    if not checks:
        return {"readiness": None, "scale": 1.0, "safety": flag, "injuries": mem, "persistent_low": False, "recent_avg": None, "reason": "No check-ins yet."}
    latest = checks[0]
    r = readiness_score(latest["sleep_h"] or 7.5, latest["soreness"] or 3, latest["energy"] or 3)
    hist = [readiness_score(c["sleep_h"] or 7.5, c["soreness"] or 3, c["energy"] or 3)["score"] for c in reversed(checks)]
    sig = trend_signal("readiness", hist, scale=3.0)
    recent = hist[-4:]
    recent_avg = sum(recent) / len(recent)
    persistent_low = len(recent) >= 4 and recent_avg < PERSISTENT_LOW_AVG
    scale = 0.6 if flag else 0.85 if persistent_low else 1.0
    reason = f"Readiness {r['score']} ({r['label']})"
    if flag:
        reason += f"; safety flag: {flag}"
    elif persistent_low:
        reason += f"; averaging {recent_avg:.0f} over the last 4 check-ins, so volume is capped"
    return {
        "readiness": r, "trend": sig.strength, "scale": scale, "safety": flag, "injuries": mem,
        "persistent_low": persistent_low, "recent_avg": round(recent_avg), "reason": reason,
    }


# ---------------- Training ----------------
def training_propose(plan: dict) -> dict:
    sets = db.get_sets(limit=500)
    e1_signals, rpe_signals = [], []
    for lift in MAIN_LIFTS:
        e1, rpe = _session_series(sets, lift, WINDOW)
        if len(e1) >= 3:
            e1_signals.append(trend_signal(lift, e1, scale=max(e1) * 0.01))
        if len(rpe) >= 3:
            rpe_signals.append(trend_signal(lift + " rpe", rpe, scale=0.3))
    if not e1_signals and not rpe_signals:
        return {"agent": "training", "volume_change": 0.0, "reason": "Not enough sessions yet; keep the plan.", "evidence": ""}
    avg = lambda xs, f: sum(f(x) for x in xs) / len(xs) if xs else 0.0
    rpe_s = Signal("rpe", avg(rpe_signals, lambda s: s.strength), avg(rpe_signals, lambda s: s.confidence))
    e1_s = Signal("e1rm", avg(e1_signals, lambda s: s.strength), avg(e1_signals, lambda s: s.confidence))
    recent = db.recent_change_count()
    if rpe_s.strength > 0.2 and e1_s.strength <= 0.15:   # work feels harder, not getting stronger
        chg = -abs(recommended_change(rpe_s, 0.15, recent))
        reason = "RPE climbing without strength gains: back off volume."
    elif e1_s.strength < 0.1 and rpe_s.strength <= 0.1 and e1_s.confidence >= 0.5:  # stalled but not fatigued
        chg = abs(recommended_change(Signal("stall", 0.6, e1_s.confidence), 0.15, recent))
        reason = "Strength stalled while effort is stable: add a little volume."
    else:
        chg, reason = 0.0, "Progressing well; no change needed."
    return {
        "agent": "training", "volume_change": chg, "reason": reason,
        "evidence": f"e1RM strength={e1_s.strength:.2f}, RPE strength={rpe_s.strength:.2f}, recent changes={recent}",
    }


# ---------------- Nutrition ----------------
def nutrition_propose(plan: dict, volume_mult: float) -> dict:
    prof = db.get_profile() or {}
    goal = prof.get("goal", "maintain")
    bw = [c["bodyweight"] for c in reversed(db.get_checkins(30)) if c["bodyweight"]]
    if len(bw) < 4 or goal == "maintain":
        return {"agent": "nutrition", "calorie_change": 0.0, "reason": "Holding calories steady.", "evidence": f"{len(bw)} weigh-ins"}
    slope = linear_slope(bw)  # kg per check-in
    want = {"cut": -0.15, "bulk": 0.1}[goal] if goal in ("cut", "bulk") else 0.0
    gap = want - slope
    sig = Signal("bw", max(-1, min(1, gap / 0.3)), min(1.0, len(bw) / 10))
    chg = recommended_change(sig, 0.08, db.recent_change_count())
    reason = "On track; no calorie change." if chg == 0 else (
        "Weight moving slower than goal: raise deficit/surplus slightly." if chg > 0 else "Moving faster than goal: ease off.")
    if goal == "cut":
        chg = -chg  # cutting = fewer calories when weight isn't dropping fast enough
    return {"agent": "nutrition", "calorie_change": chg, "reason": reason, "evidence": f"bw slope={slope:.2f}kg/check-in vs goal {want}"}
