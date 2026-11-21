"""Periodization agent, deterministic half: block position, deload / taper / rebound timing, and an offline fallback.

Docs: "Macro and meso cycles, peaking, deloads. Reads goals, events, history. Returns block structure. approve."
Everything is a normal typed Proposal (volume_change, new_block, note), so the policy gate, the Safety Critic and the ledger
apply unchanged. State is DERIVED from the ledger and the log (no extra writable state), so it cannot drift.

    signals()          evidence the agent (and the model, if any) receives
    propose(event)     the offline fallback: at most one load move (taper > deload > rebound), plus block-plan notes
"""
from __future__ import annotations

from backend.agents import specialists as sp
from backend.memory import db
from backend.policy.adaptation import readiness_score

from . import agenda, clock, ledger, signals as sig
from .schema import Proposal

MESO_WEEKS = 4                  # three loading weeks, then a deload
DELOAD_CUT = -0.30              # the largest cut that stays in the auto tier
REBOUND_STEP = 0.10             # the weekly load-increase cap in policy; larger steps would only be trimmed
TAPER_FAR, TAPER_NEAR = -0.15, -0.30
TAPER_WINDOW_DAYS = 14
NEW_BLOCK_WEEKS = 12            # three mesocycles
LOW_READINESS = sp.PERSISTENT_LOW_AVG


def _decisions(days: float = 60) -> list[dict]:
    return [d for d in ledger.recent(300) if clock.age_days(d["ts"]) <= days]


def _frac(d: dict) -> float:
    act = (d.get("action") or {}).get("fraction")
    if act is None:
        act = (d.get("proposal") or {}).get("params", {}).get("fraction", 0)
    return float(act or 0)


def _live(d: dict) -> bool:
    return d["status"] in ("applied", "pending_veto", "pending_approval")


def block_anchor() -> str | None:
    """When the current block started: the last applied new_block, else the first logged session (None if the athlete has not trained)."""
    nb = [d for d in _decisions(400) if d["kind"] == "new_block" and d["status"] == "applied"]
    if nb:
        return nb[0]["ts"]
    dates = sig._session_dates(365)
    return (dates[0] + "T00:00:00+00:00") if dates else None


def _last_deload() -> dict | None:
    cuts = [d for d in _decisions(120) if d["kind"] == "volume_change" and d["status"] == "applied" and _frac(d) <= -0.25 and d["agent"] != "watchdog"]
    return cuts[0] if cuts else None   # ledger.recent is newest first


def _readiness_avg() -> float | None:
    cs = db.get_checkins(4)
    if len(cs) < 3:
        return None
    return sum(readiness_score(c["sleep_h"] or 7.5, c["soreness"] or 3, c["energy"] or 3)["score"] for c in cs) / len(cs)


def macro_plan(days_out: int) -> list[dict]:
    """Weeks between now and an event as phases: build (with a deload every 4th week), peak, taper (last 2 weeks)."""
    weeks = max(1, -(-days_out // 7))
    out = []
    for w in range(1, weeks + 1):
        left = weeks - w
        phase = "taper" if left <= 1 and weeks > 2 else "peak" if left == 2 and weeks > 4 else "deload" if w % MESO_WEEKS == 0 and left > 2 else "build"
        out.append({"week": w, "phase": phase})
    return out


def _plan_text(ev: dict) -> str:
    runs, cur = [], None
    for w in macro_plan(ev["days_until"]):
        if cur and cur[0] == w["phase"]:
            cur[2] = w["week"]
        else:
            cur = [w["phase"], w["week"], w["week"]]
            runs.append(cur)
    parts = [f"{p} wk {a}" if a == b else f"{p} wk {a}-{b}" for p, a, b in runs]
    return f"Block plan to {ev['label']} ({ev['start']}): " + ", ".join(parts)


def signals() -> dict:
    anchor = block_anchor()
    ld = _last_deload()
    since_reset_ts = max([t for t in (anchor, ld["ts"] if ld else None) if t], default=None)
    plan = db.get_plan() or {}
    ev = agenda.next_competition()
    avg = _readiness_avg()
    falling = [k for k, v in sp.analyst_report()["lifts"].items() if v["trend"] == "down"]
    cur, base = plan.get("volume_mult", 1.0), plan.get("base_volume", 1.0)
    in_deload = bool(ld and clock.age_days(ld["ts"]) < 7)
    ramped = bool(ld and any(d["kind"] == "volume_change" and _frac(d) > 0 and _live(d) and d["ts"] > ld["ts"] for d in _decisions(60)))
    return {
        "weeks_in_block": round(clock.age_days(anchor) / 7, 1) if anchor else None,
        "weeks_since_deload_or_block_start": round(clock.age_days(since_reset_ts) / 7, 1) if since_reset_ts else None,
        "meso_weeks": MESO_WEEKS, "sessions_14d": len(sig._session_dates(14)),
        "last_deload_age_days": round(clock.age_days(ld["ts"]), 1) if ld else None, "in_deload": in_deload,
        "needs_rebound": bool(ld and 7 <= clock.age_days(ld["ts"]) <= 35 and not ramped and cur < base * 0.95),
        "volume_mult": cur, "base_volume": base,
        "readiness_avg_last_4": round(avg) if avg is not None else None, "falling_lifts": falling,
        "next_competition": ({"label": ev["label"], "date": ev["start"], "days_until": ev["days_until"], "id": ev["id"]} if ev else None),
        "macro_plan": macro_plan(ev["days_until"]) if ev and ev["days_until"] > 0 else [],
        "pain_active": bool(ledger.get_setting("pain_active", False)),
        "training_frozen": bool({"training", "all"} & set(ledger.frozen_domains())),
        "disruption_active": bool(ledger.get_setting("disruption")),
    }


def _already(kind: str, text_part: str, days: float) -> bool:
    return any(d["agent"] == "periodization" and d["kind"] == kind and _live(d) and clock.age_days(d["ts"]) <= days
               and text_part in (d["rationale"] + " " + d["evidence"]).lower() for d in _decisions(days + 1))


def propose(event: str = "cron.weekly", payload: dict | None = None) -> dict:
    """Offline fallback -> {agent, reasoning, proposals, dropped}. Same shape as runtime.run_agent."""
    s = signals()

    def mk(kind, params, why, ev, conf):
        return Proposal(agent="periodization", kind=kind, params=params, rationale=why, evidence=ev, confidence=conf)

    limited = s["training_frozen"] or s["pain_active"] or s["disruption_active"]
    if limited or s["sessions_14d"] < 2 or s["weeks_in_block"] is None:
        why = "Training is paused or limited right now" if limited else "There is not enough training history yet to place you in a block"
        return {"agent": "periodization", "reasoning": f"{why}, so I am not changing your block structure.", "proposals": [], "dropped": []}

    props: list[Proposal] = []
    comp, load_move = s["next_competition"], False
    wk = s["weeks_since_deload_or_block_start"] or 0
    strained = (s["readiness_avg_last_4"] is not None and s["readiness_avg_last_4"] < LOW_READINESS) or len(s["falling_lifts"]) >= 2
    if comp and 1 <= comp["days_until"] <= TAPER_WINDOW_DAYS:
        if not _already("volume_change", "taper", TAPER_WINDOW_DAYS + 7):
            frac = TAPER_NEAR if comp["days_until"] <= 7 else TAPER_FAR
            props.append(mk("volume_change", {"fraction": frac}, f"Taper for {comp['label']} in {comp['days_until']} days: less volume, same intensity, so you arrive fresh.",
                            f"taper; {comp['label']} on {comp['date']} ({comp['days_until']} days)", 0.85))
            load_move = True
    elif s["in_deload"]:
        pass
    elif wk >= MESO_WEEKS or (wk >= MESO_WEEKS - 1 and strained):
        if not _already("volume_change", "deload", 14):
            props.append(mk("volume_change", {"fraction": DELOAD_CUT}, "Deload week: about a third less volume so the last few weeks of work can turn into progress.",
                            f"deload; {wk:g} weeks since the last reset; readiness avg {s['readiness_avg_last_4']}; falling lifts {s['falling_lifts']}", 0.8))
            load_move = True
    elif s["needs_rebound"] and (s["readiness_avg_last_4"] is None or s["readiness_avg_last_4"] >= LOW_READINESS):
        if not _already("volume_change", "rebound", 6):
            props.append(mk("volume_change", {"fraction": REBOUND_STEP}, "Deload done: stepping your volume back up gradually.",
                            f"rebound; volume x{s['volume_mult']} vs baseline x{s['base_volume']}; last deload {s['last_deload_age_days']} days ago", 0.75))
            load_move = True

    if not load_move and s["weeks_in_block"] >= NEW_BLOCK_WEEKS and len(s["falling_lifts"]) < 2 and not _already("new_block", "block", 28):
        props.append(mk("new_block", {}, f"You have been in this block for {s['weeks_in_block']:g} weeks: time for fresh exercises and a new progression.",
                        f"block age {s['weeks_in_block']} weeks (meso = {MESO_WEEKS})", 0.7))
    if comp and 28 <= comp["days_until"] <= 112 and not any(
            d["agent"] == "periodization" and d["kind"] == "note" and f"event {comp['id']}" in d["evidence"] for d in _decisions(120)):
        ev = next(e for e in agenda.upcoming(365, ("competition",)) if e["id"] == comp["id"])
        props.append(mk("note", {"text": _plan_text(ev)}, f"Mapped out your build to {comp['label']}.", f"event {comp['id']}", 0.7))
    reasoning = (f"You are {s['weeks_in_block']:g} weeks into this block"
                 + (f" and {comp['days_until']} days from {comp['label']}" if comp else "")
                 + (": " + props[0].rationale if props else ", and nothing needs to change in your block structure right now."))
    return {"agent": "periodization", "reasoning": reasoning, "proposals": props, "dropped": []}
