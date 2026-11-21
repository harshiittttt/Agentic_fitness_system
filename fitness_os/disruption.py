"""Disruption mode and proactive check-ins (docs section 06: 'Disruption mode' and 'Proactive check-in').

    detect()   sick / travel / 3+ missed days, from the athlete's own words and the log (deterministic, outside the agents)
    tick()     enter degraded mode once, re-plan through the normal gate, schedule a gradual return ramp, leave when recovered
    proactive()  one well-timed message when the athlete went quiet (comms rules enforced by policy, not here)

State lives in the settings table (`disruption`), so a restart resumes where it stopped. Everything that changes the plan goes
through control.submit, so the critic-free deterministic fallback still obeys every hard limit.
"""
from __future__ import annotations

from backend.agents.base import have_key
from backend.memory import db

from . import clock, comms, control, ledger, signals
from .schema import Proposal

SICK = ("sick", "ill ", "fever", "flu", "cold and", "covid", "food poisoning", "vomit", "stomach bug", "बीमार", "बुखार", "तबीयत खराब")
TRAVEL = ("travel", "trip", "vacation", "holiday", "flight", "jet lag", "jetlag", "out of town", "no gym", "यात्रा", "बाहर जा रहा", "छुट्टी")
MISSED_DAYS = 3                 # days without a session (with a plan) that counts as a disruption
LOOKBACK_DAYS = 3               # how far back the athlete's words count
ENTRY_CUT = -0.20               # fallback degraded-mode volume when no model is available
RETURN_STEP = 0.05              # each return step stays in the auto tier (<=5%); the weekly load cap in policy still applies
RETURN_STEPS = 3                # one step a week for three weeks, then normal training takes over
MAX_RAMP_DAYS = 28
BACKOFF_NUDGES = 3              # this many unanswered nudges within BACKOFF_DAYS and proactive outreach pauses
BACKOFF_DAYS = 14


def _recent_words() -> str:
    parts = [c["note"] for c in db.get_checkins(10) if c.get("note") and clock.age_days(c["ts"]) <= LOOKBACK_DAYS]
    try:
        from . import conversation
        parts += [m["content"] for m in conversation.history(limit=30, since_days=LOOKBACK_DAYS) if m["role"] == "user"]
    except Exception:
        pass
    return " ".join(parts).lower()


def detect() -> dict | None:
    """-> {kind, evidence} or None. Sickness beats travel beats a plain gap (what the athlete says is the cause)."""
    words = _recent_words()
    for kind, vocab in (("sick", SICK), ("travel", TRAVEL)):
        hit = next((w for w in vocab if w in words), None)
        if hit:
            return {"kind": kind, "evidence": f"you mentioned '{hit.strip()}' in the last {LOOKBACK_DAYS} days"}
    adh = signals.adherence()
    gap = adh["days_since_last_session"]
    if db.get_plan() and gap is not None and gap >= MISSED_DAYS and adh["sessions_28d"] >= 2:
        return {"kind": "missed_days", "evidence": f"no session for {gap:g} days (you usually train {adh['planned_per_week']}x a week)"}
    return None


def state() -> dict | None:
    return ledger.get_setting("disruption")


def _sessions_since(ts: str) -> int:
    seen = {s["session"] for s in db.get_sets(limit=600) if s["ts"] > ts}
    return len(seen)


def _enter(found: dict) -> dict:
    ts = clock.iso()
    st = {"kind": found["kind"], "since": ts, "evidence": found["evidence"], "restored": 0, "cut": 0.0, "last_step": None}
    ledger.set_setting("disruption", st)
    out: dict = {"entered": found["kind"], "evidence": found["evidence"]}
    if have_key():
        from . import loop
        rep = loop.run_event("disruption.detected", {"kind": found["kind"], "evidence": found["evidence"]}, record=False)
        out["agents"] = list(rep["agents"])
        cuts = [r for r in rep["results"] if r["status"] == "applied" and (r.get("action") or {}).get("fraction", 0) < 0]
        st["cut"] = sum((r["action"] or {}).get("fraction", 0) for r in cuts)
    else:   # offline degraded mode: shrink sessions through the same gate
        r = control.submit(Proposal(agent="orchestrator", kind="volume_change", params={"fraction": ENTRY_CUT},
                                    rationale=f"Disruption mode ({found['kind'].replace('_', ' ')}): lighter sessions while life is in the way.",
                                    evidence=found["evidence"], confidence=0.8), trigger="disruption.detected")
        out["fallback"] = r["status"]
        if r["status"] == "applied":
            st["cut"] = (r.get("action") or {}).get("fraction", 0)
    ledger.set_setting("disruption", st)
    return out


def _ramp(st: dict) -> dict | None:
    """The athlete trained again: restore volume gradually (RETURN_STEP per step, one step a week)."""
    if st["cut"] >= -0.001 or st["restored"] >= RETURN_STEPS:
        return None
    if st["last_step"] and clock.age_days(st["last_step"]) < 7:
        return None
    r = control.submit(Proposal(agent="orchestrator", kind="volume_change", params={"fraction": RETURN_STEP},
                                rationale="Back to training: bringing your volume up gradually, not all at once.",
                                evidence=f"{st['kind'].replace('_', ' ')} ended; return step {st['restored'] + 1} of {RETURN_STEPS}", confidence=0.8),
                       trigger="disruption.return")
    if r["status"] in ("applied", "pending_veto", "pending_approval"):
        st["restored"] += 1
        st["last_step"] = clock.iso()
        ledger.set_setting("disruption", st)
    return {"step": st["restored"], "result": r["status"]}


def _close(st: dict, how: str) -> None:
    log = ledger.get_setting("disruption_log", []) or []
    log.append({"kind": st["kind"], "since": st["since"][:10], "ended": clock.iso()[:10], "days": round(clock.age_days(st["since"]), 1), "how": how})
    ledger.set_setting("disruption_log", log[-20:])
    ledger.set_setting("disruption", None)


def tick() -> dict:
    st = state()
    found = detect()
    if not st:
        if found and not ledger.kill_switch():
            return _enter(found)
        return {"active": False}
    back = _sessions_since(st["since"])
    still = found is not None and found["kind"] in ("sick", "travel")
    out = {"active": True, "kind": st["kind"], "sessions_since": back, "restored": st["restored"]}
    if back >= 1 and not still:
        step = _ramp(st)
        if step:
            out["ramp"] = step
        st = state() or st
        if st["restored"] >= RETURN_STEPS or st["cut"] >= -0.001 or clock.age_days(st["since"]) > MAX_RAMP_DAYS:
            _close(st, "ramped back")
            out.update(active=False, ended=True)
    elif clock.age_days(st["since"]) > MAX_RAMP_DAYS:
        _close(st, "timed out")
        out.update(active=False, ended=True)
    return out


def _fallback_nudge(adh: dict) -> str:
    gap = adh["days_since_last_session"]
    return (f"It's been {gap:g} days since your last session. No pressure: want a lighter 20-minute one today?" if gap
            else "Ready to start? A short first session is a perfectly good start.")


def proactive() -> dict:
    """Reach out when the athlete went quiet. A pattern, not one missed log; comms rules are enforced by the gate."""
    adh = signals.adherence()
    if not adh["silent"]:
        return {"sent": False, "why": "not silent"}
    if any(n["age_hours"] < 24 for n in adh["last_nudges"]):
        return {"sent": False, "why": "already messaged in the last day"}
    if len(comms.recent(30, hours=BACKOFF_DAYS * 24)) >= BACKOFF_NUDGES:   # the silence after our messages IS the answer: stop, do not nag
        return {"sent": False, "why": f"{BACKOFF_NUDGES} messages in {BACKOFF_DAYS} days went unanswered; backing off"}
    if ledger.kill_switch():
        return {"sent": False, "why": "kill switch"}
    if have_key():
        from . import loop
        rep = loop.run_event("proactive.checkin", {"days_silent": adh["days_since_last_session"]}, record=False)
        sent = any(r["status"] == "applied" and r["proposal"].startswith("Message") for r in rep["results"])
        return {"sent": sent, "via": "adherence agent", "results": [r["status"] for r in rep["results"]]}
    r = control.submit(Proposal(agent="adherence", kind="nudge", params={"text": _fallback_nudge(adh)},
                                rationale="You have been quiet for a few days.", evidence=f"{adh['days_since_last_session']} days since last session",
                                confidence=0.6), trigger="proactive.checkin")
    return {"sent": r["status"] == "applied", "via": "template", "result": r["status"], "fired": r.get("fired", [])}
