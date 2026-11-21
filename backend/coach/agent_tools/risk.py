"""Risk tiers, enforced in code (never by the model). Every OpenAPI operation gets exactly one tier.

READ       pure queries; run immediately
WRITE_SAFE reversible, user-visible, small; run immediately and are reported
CONFIRM    destructive or big; NEVER run on the model's say-so: stored, asked aloud, run only after a spoken yes
BLOCKED    never callable by the agent (binary/audio, uploads, exports, wipes, auth/server, push, autonomy controls, recursion)

Resolution: explicit RULES[(METHOD, path)] first, then the default by method (GET -> READ, everything else -> CONFIRM).
Unknown/new operations therefore land in CONFIRM (or READ for a plain GET). A GET that is not a pure query must be listed.
"""
from __future__ import annotations

READ, WRITE_SAFE, CONFIRM, BLOCKED = "READ", "WRITE_SAFE", "CONFIRM", "BLOCKED"
TIERS = (READ, WRITE_SAFE, CONFIRM, BLOCKED)

RULES: dict[tuple[str, str], str] = {
    # ---- BLOCKED
    ("GET", "/health"): BLOCKED,
    ("POST", "/voice/command"): BLOCKED,
    ("GET", "/tts"): BLOCKED, ("GET", "/tts/status"): BLOCKED, ("GET", "/tts/test"): BLOCKED,
    ("GET", "/agents/export"): BLOCKED,                       # data export
    ("POST", "/agents/data/delete"): BLOCKED, ("DELETE", "/wearables/data"): BLOCKED,
    ("POST", "/reset-all"): BLOCKED, ("POST", "/demo/reset"): BLOCKED, ("POST", "/demo/seed"): BLOCKED,
    ("POST", "/wearables/import"): BLOCKED, ("POST", "/wearables/ingest"): BLOCKED,   # file / bulk upload
    ("POST", "/agents/form/analyze"): BLOCKED,                # keypoint upload
    ("POST", "/push/register"): BLOCKED, ("DELETE", "/push/register"): BLOCKED, ("PUT", "/push/settings"): BLOCKED,
    ("POST", "/push/test"): BLOCKED, ("POST", "/push/receipts/check"): BLOCKED,
    ("POST", "/agents/kill/{state}"): BLOCKED,                # kill switch: the agent may read /agents/status, never flip it
    ("POST", "/agents/dial"): BLOCKED, ("POST", "/agents/trust/strict-new-types"): BLOCKED,   # autonomy controls
    ("POST", "/chat"): BLOCKED, ("POST", "/coach/chat"): BLOCKED,   # a chat model inside the voice model: recursion
    # ---- READ (POST that only queries, plus GET by default)
    ("POST", "/meal/preview"): READ, ("POST", "/memory/search"): READ,
    ("POST", "/agents/research/ask"): READ, ("POST", "/agents/research/screen"): READ,
    # ---- WRITE_SAFE
    ("POST", "/checkin"): WRITE_SAFE, ("POST", "/log"): WRITE_SAFE, ("POST", "/log/set"): WRITE_SAFE,
    ("POST", "/meal"): WRITE_SAFE, ("POST", "/meal/portion"): WRITE_SAFE, ("POST", "/meal/repeat"): WRITE_SAFE,
    ("POST", "/meal/restore"): WRITE_SAFE, ("PATCH", "/meal/{meal_id}/portion"): WRITE_SAFE,
    ("POST", "/session/restore"): WRITE_SAFE, ("POST", "/session/weighin"): WRITE_SAFE, ("POST", "/session/fit"): WRITE_SAFE,
    ("POST", "/plan/swap"): WRITE_SAFE, ("POST", "/agents/agenda"): WRITE_SAFE, ("POST", "/coach"): WRITE_SAFE,
    ("POST", "/live/start"): WRITE_SAFE, ("POST", "/live/say"): WRITE_SAFE, ("POST", "/live/cue"): WRITE_SAFE,
    ("POST", "/memory/remember"): WRITE_SAFE,
    ("POST", "/memory/facts/{fact_id}/confirm"): WRITE_SAFE, ("POST", "/memory/facts/{fact_id}/correct"): WRITE_SAFE,
    ("POST", "/memory/discoveries/{discovery_id}/confirm"): WRITE_SAFE, ("POST", "/memory/discoveries/{discovery_id}/dismiss"): WRITE_SAFE,
    ("POST", "/memory/questions/{question_id}/answer"): WRITE_SAFE, ("POST", "/memory/questions/{question_id}/dismiss"): WRITE_SAFE,
    ("POST", "/agents/nudges/{nudge_id}/read"): WRITE_SAFE, ("POST", "/agents/decisions/{decision_id}/reject"): WRITE_SAFE,
    # agent runs (also refused while the kill switch is ON, see AGENT_RUNS)
    ("POST", "/agents/event"): WRITE_SAFE, ("POST", "/agents/roundtable"): WRITE_SAFE, ("POST", "/agents/scheduler/tick"): WRITE_SAFE,
    ("POST", "/agents/memory/curate"): WRITE_SAFE, ("POST", "/review"): WRITE_SAFE,
    # ---- CONFIRM (explicit, so they are visible in the table; everything unknown is CONFIRM anyway)
    ("POST", "/profile"): CONFIRM, ("POST", "/onboard"): CONFIRM, ("POST", "/onboard/split/confirm"): CONFIRM,
    ("POST", "/onboard/split/consult"): CONFIRM, ("POST", "/coach/undo"): CONFIRM, ("POST", "/live/end"): CONFIRM,
    ("POST", "/agents/intake/answer"): CONFIRM,
    ("POST", "/agents/decisions/{decision_id}/approve"): CONFIRM, ("POST", "/agents/decisions/{decision_id}/revert"): CONFIRM,
    ("POST", "/agents/memory/{item_id}/resolve"): CONFIRM,
    ("POST", "/memory/facts/{fact_id}/forget"): CONFIRM, ("POST", "/memory/facts/{fact_id}/deny"): CONFIRM,
}

# operations that start the multi-agent loop: refused while the kill switch is ON
AGENT_RUNS = {("POST", "/agents/event"), ("POST", "/agents/roundtable"), ("POST", "/agents/scheduler/tick"), ("POST", "/agents/memory/curate"), ("POST", "/review")}

# human question used when a CONFIRM tool is stored (by route; others get a generic one built from the summary)
SAY = {
    ("POST", "/profile"): "That will change your profile and may rebuild your plan.",
    ("POST", "/onboard"): "That will redo your onboarding and rebuild your plan.",
    ("POST", "/coach/undo"): "That will undo your last change.",
    ("POST", "/live/end"): "That will end your live session.",
    ("DELETE", "/meal/{meal_id}"): "That will delete that meal entry.",
    ("DELETE", "/session/set/{set_id}"): "That will delete that logged set.",
    ("DELETE", "/agents/agenda/{event_id}"): "That will remove that calendar event.",
    ("DELETE", "/coach/memory/{item_id}"): "That will make me forget that memory for good.",
    ("POST", "/memory/facts/{fact_id}/forget"): "That will make me forget that fact for good.",
    ("POST", "/agents/decisions/{decision_id}/approve"): "That will approve and apply that team decision.",
    ("POST", "/agents/decisions/{decision_id}/revert"): "That will undo that team decision.",
}


def is_agent_run(method: str, path: str) -> bool:
    return (method.upper(), path) in AGENT_RUNS


def tier_for(method: str, path: str) -> str:
    m = method.upper()
    t = RULES.get((m, path))
    if t:
        return t
    if path.startswith("/voice/") or path.startswith("/tts"):
        return BLOCKED
    return READ if m == "GET" else CONFIRM
