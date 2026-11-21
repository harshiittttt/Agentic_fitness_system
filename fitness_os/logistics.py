"""Logistics agent, deterministic half: travel, busy days, equipment limits, session moves and time-boxed sessions.

Docs: "Grocery, meal prep, travel mode, gym access. Reads calendar, meal plan, location. Returns lists, bookings, schedules."
Scope here is the training-schedule side (the grocery / ordering tools stay out: `order` needs approval and there is no
logistics-mcp yet). The agent only ever returns existing typed Proposals:
    nudge            the concrete suggestion to the athlete (move a session, a 25-minute version, a hotel-room session)
    volume_change    a modest CUT for a long stretch with no gym (through the same gate as everything else)
    note             a durable fact worth remembering (an equipment limit, a trip plan)
The one-day "fit to N minutes" edit itself is the athlete's tap in the app (/session/fit); the message previews exactly that trim.
"""
from __future__ import annotations

from backend.memory import db
from backend.tools import exercises as ex
from backend.tools import plan_edit as pe

from . import agenda, clock, comms, ledger, signals as sig
from .schema import Proposal

TRIP_DAYS_AHEAD = 3           # start talking about a trip this close to it
LONG_NO_GYM_DAYS = 4          # a stretch with no gym this long justifies a small volume cut
NO_GYM_CUT = -0.10
DEFAULT_BUSY_MINUTES = 30


def time_box(minutes: int) -> dict | None:
    """Preview of today's session trimmed to `minutes` using the app's own rule (compounds first). None without a plan."""
    view = pe.plan_view()
    if not view:
        return None
    day = view["days"][view["today_index"]]
    fitted = ex.fit_to_time(day, int(minutes))
    return {"day": day["name"], "day_index": view["today_index"], "minutes": int(minutes), "full_minutes": ex.estimate_minutes(day),
            "fitted_minutes": ex.estimate_minutes(fitted), "exercises": [f"{e['name']} {e['sets']}x{e['reps']}" for e in fitted["exercises"]]}


def signals() -> dict:
    ev = agenda.upcoming(14, ("trip", "no_gym", "busy", "equipment"))
    active = [e for e in ev if e["active"]]
    soon = [e for e in ev if not e["active"] and e["days_until"] <= TRIP_DAYS_AHEAD]
    busy_today = next((e for e in active if e["kind"] == "busy"), None)
    box = time_box(busy_today.get("minutes") or DEFAULT_BUSY_MINUTES) if busy_today else None
    view = pe.plan_view()
    return {"events_14d": ev, "active_today": active, "starting_soon": soon,
            "time_box_today": box,
            "next_session": (view["days"][view["today_index"]]["name"] if view else None),
            "equipment_limits": sorted({q for e in active if e["kind"] == "equipment" for q in e.get("equipment", [])}),
            "quiet_hours_now": comms.in_quiet_hours(),
            "training_frozen": bool({"training", "all"} & set(ledger.frozen_domains())),
            "disruption_active": bool(ledger.get_setting("disruption"))}


def _recent_logistics(days: float, part: str) -> bool:
    return any(d["agent"] == "logistics" and clock.age_days(d["ts"]) <= days and d["status"] in ("applied", "pending_veto", "pending_approval")
               and part in (d["rationale"] + " " + d["evidence"]).lower() for d in ledger.recent(100))


def propose(event: str = "cron.daily", payload: dict | None = None) -> dict:
    """Offline fallback -> {agent, reasoning, proposals, dropped}. One short message per situation, never repeated for the same event."""
    s = signals()
    props: list[Proposal] = []

    def mk(kind, params, why, evd, conf=0.75):
        return Proposal(agent="logistics", kind=kind, params=params, rationale=why, evidence=evd, confidence=conf)

    for e in s["active_today"] + s["starting_soon"]:
        tag = f"event {e['id']}"
        if e["kind"] == "busy" and e["active"] and s["time_box_today"] and not _recent_logistics(1, tag + ";"):
            b = s["time_box_today"]
            text = (f"Short on time today? Here is {b['day']} in about {b['fitted_minutes']} min: " + "; ".join(b["exercises"]) + ". Tap 'fit to time' to use it.")
            props.append(mk("nudge", {"text": text[:240]}, f"You are busy today ({e['label']}), so a shorter session keeps the habit alive.",
                            f"{tag}; {b['minutes']} min available; full session {b['full_minutes']} min"))
        elif e["kind"] in ("trip", "no_gym"):
            gone = e["length_days"]
            if not _recent_logistics(e["length_days"] + 3, tag + " message") and not s["training_frozen"]:
                where = "hotel-room or bodyweight sessions" if e["kind"] == "trip" else "sessions without gym equipment"
                when = "is on now" if e["active"] else f"starts in {e['days_until']} day{'s' if e['days_until'] != 1 else ''}"
                text = (f"{e['label']} {when} ({gone} days). Plan 2 short {where} of 20-30 min, "
                        f"and move any session you would miss to the first free day after you are back.")
                props.append(mk("nudge", {"text": text[:240]}, f"{e['label']} will get in the way of your usual sessions.",
                                f"{tag} message; {gone} days, {e['kind']}"))
            if gone >= LONG_NO_GYM_DAYS and e["days_until"] <= 1 and not s["disruption_active"] and not _recent_logistics(gone + 3, tag + " cut"):
                props.append(mk("volume_change", {"fraction": NO_GYM_CUT}, f"A {gone}-day stretch without your gym: slightly lighter sessions while you are away.",
                                f"{tag} cut; {gone} days with no gym", 0.7))
        elif e["kind"] == "equipment" and not _recent_logistics(30, tag + ";"):
            props.append(mk("note", {"text": f"Equipment limit: {', '.join(e.get('equipment') or ['limited equipment'])} ({e['start']} to {e['end']})"},
                            f"Remembered your equipment limit: {e['label']}.", f"{tag}; equipment limit", 0.7))
    names = [e["label"] for e in s["active_today"] + s["starting_soon"]]
    reasoning = ("Your calendar has " + ", ".join(names) + ": " + props[0].rationale) if props else "Nothing in your calendar needs a change to your sessions right now."
    return {"agent": "logistics", "reasoning": reasoning, "proposals": props, "dropped": []}
