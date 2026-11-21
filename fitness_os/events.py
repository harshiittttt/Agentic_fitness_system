"""L2 event plane: record an event's data, run watchdogs (outside the agents), close veto windows.

Used by both the CLI (`python -m fitness_os event`) and the in-process agent loop, so the two can never drift apart.
"""
from __future__ import annotations

from backend.memory import db

from . import context, control, watchdogs


def ingest(event_type: str, payload: dict | None = None, *, record: bool = True) -> dict:
    """`record=False` when the caller already stored the data (e.g. the app's own /checkin endpoint)."""
    payload = payload or {}
    if event_type not in context.ROUTES:
        raise ValueError(f"unknown event {event_type}; choose from {sorted(context.ROUTES)}")
    if not record:
        pass
    elif event_type == "session.logged":
        for s in payload.get("sets", []):
            db.add_set(payload.get("session", db.now()[:10]), s["exercise"], s.get("weight"), s.get("reps"), s.get("rpe"), s.get("note"))
    elif event_type == "calendar.changed" and payload.get("event"):   # data only: the agenda stores it, agents react
        from . import agenda
        agenda.add(**{k: payload["event"].get(k) for k in ("kind", "start", "end", "label", "minutes", "equipment") if payload["event"].get(k) is not None})
    elif event_type in ("checkin.logged", "pain.report"):
        db.add_checkin(payload.get("sleep_h"), payload.get("soreness"), payload.get("energy"), payload.get("bodyweight"),
                       payload.get("note") or payload.get("text"))
    fired = []
    for w in watchdogs.run_all():  # watchdogs run on every event, before any agent
        if not watchdogs.already_open(w):
            fired.append({"watchdog": w["watchdog"], "evidence": w["evidence"],
                          "result": control.submit(w["proposal"], trigger=f"watchdog:{event_type}")})
    applied = control.tick()
    return {"event": event_type, "wake": context.ROUTES[event_type], "then": ["reconcile", "safety_critic", "policy gate"],
            "watchdogs_fired": fired, "veto_windows_closed": applied}
