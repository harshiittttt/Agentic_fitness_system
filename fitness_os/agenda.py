"""The athlete's calendar as the coaches see it: competitions, trips, no-gym stretches, busy days, equipment limits.

Stored in the settings table (`agenda`), so it is just data: nothing here changes the plan. The Periodization agent reads
competitions (taper / peak), the Logistics agent reads everything else (travel, schedule, equipment). Event text is DATA.
"""
from __future__ import annotations

from datetime import date, timedelta

from . import clock, ledger

KINDS = ("competition", "trip", "no_gym", "busy", "equipment")
MAX_EVENTS = 40


def today() -> date:
    return clock.now().date()


def _d(s: str) -> date:
    return date.fromisoformat(str(s)[:10])


def _all() -> list[dict]:
    return ledger.get_setting("agenda", []) or []


def add(kind: str, start: str, end: str | None = None, label: str = "", minutes: int | None = None, equipment: list[str] | None = None) -> dict:
    """Validate and store one event. Raises ValueError on anything malformed (callers surface it)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    try:
        s, e = _d(start), _d(end or start)
    except (TypeError, ValueError):
        raise ValueError("dates must look like 2026-12-01")
    if e < s or (e - s).days > 120:
        raise ValueError("end must be on or after start, within 120 days")
    ev = {"id": 1 + max((x["id"] for x in _all()), default=0), "kind": kind, "start": s.isoformat(), "end": e.isoformat(),
          "label": str(label or kind).strip()[:80]}
    if minutes:
        ev["minutes"] = max(10, min(180, int(minutes)))
    if equipment:
        ev["equipment"] = [str(x).strip().lower()[:30] for x in equipment][:8]
    ledger.set_setting("agenda", (_all() + [ev])[-MAX_EVENTS:])
    return ev


def remove(event_id: int) -> bool:
    ev = _all()
    keep = [e for e in ev if e["id"] != event_id]
    ledger.set_setting("agenda", keep)
    return len(keep) != len(ev)


def upcoming(days: int = 120, kinds: tuple[str, ...] | None = None) -> list[dict]:
    """Events that are active today or start within `days`, soonest first, each with `days_until` (negative = already running)."""
    t, out = today(), []
    for e in _all():
        if kinds and e["kind"] not in kinds:
            continue
        s, en = _d(e["start"]), _d(e["end"])
        if en < t or s > t + timedelta(days=days):
            continue
        out.append({**e, "days_until": (s - t).days, "active": s <= t <= en, "length_days": (en - s).days + 1})
    return sorted(out, key=lambda e: (e["start"], e["id"]))


def next_competition() -> dict | None:
    return next((e for e in upcoming(365, ("competition",)) if e["days_until"] >= 0), None)
