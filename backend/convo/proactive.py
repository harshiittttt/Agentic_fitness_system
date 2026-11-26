"""Events from the app that are not an utterance (rest over, idle, set active...) and the talking budget for lines the coach starts by itself.

Policy (docs/CONVERSATIONAL_AGENT_PLAN.md 4.8), plain code, no model: speak first only if the athlete is not mid-set or mid-speech, at least 20 s since they last spoke,
at most 1 unsolicited line per 90 s and 6 per 10 minutes, not twice in one rest. A line is at most 15 words; the template is used when the model is slower than 1.5 s,
fails, or says a number it cannot ground.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Iterator

from backend.convo import ground, llm, prompt, session, state, turn
from backend.convo.guards import Ctx
from backend.coach import live

LINE_TIMEOUT = 1.5
QUIET_AFTER_USER = 20.0
PER_90 = 1
PER_600 = 6
_pool = ThreadPoolExecutor(max_workers=2)
STATE_ONLY = {"set_active", "set_inactive", "rest_started", "set_logged", "session_start", "session_end"}


def allow(dlg, event: str, now: float | None = None) -> tuple[bool, str]:
    now = now or time.time()
    if dlg.set_active:
        return False, "speaking"
    if now - dlg.last_user_ts < QUIET_AFTER_USER:
        return False, "speaking"
    lines = [x for x in dlg.coach_lines if now - x < 600]
    if len([x for x in lines if now - x < 90]) >= PER_90 or len(lines) >= PER_600:
        return False, "budget"
    if event == "rest_over" and now - getattr(dlg, "rest_over_ts", 0.0) < 60:
        return False, "already_said"
    if event == "idle" and dlg.rest_nudges >= 1:
        return False, "already_said"
    return True, ""


def _template(event: str, lang: str, st: dict | None) -> str:
    c = (st or {}).get("current")
    if event == "rest_over":
        if not c:
            return "Rest is up. That's everything planned." if lang == "en" else "Rest khatam. Aaj ka plan poora."
        return (f"Rest is up. {c['name'].title()}, set {c['set_no']}. Go when you're ready." if lang == "en"
                else f"Rest khatam. {c['name'].title()}, set {c['set_no']}. Ready ho toh shuru karo.")
    if event == "idle":
        return "Still with me? Whenever you're ready." if lang == "en" else "Sab theek? Ready ho toh bolo."
    if event == "set_logged":
        return "That's a PR. Nice work." if lang == "en" else "Yeh PR hai. Badhiya."
    return ""


def _model_line(event: str, blk: dict, dlg, lang: str, allowed: set[float]) -> str | None:
    def run() -> str | None:
        msgs = [{"role": "system", "content": prompt.system_prompt(live._ctx()[0], live._ctx()[1], dlg.memory_facts)},
                {"role": "user", "content": prompt.event_prompt(event, blk["text"], lang)}]
        text = ""
        for ev in llm.stream(msgs, None, max_tokens=60, timeout=LINE_TIMEOUT + 1.0):
            if ev["k"] == "text":
                text += ev["d"]
        return text
    try:
        text = _pool.submit(run).result(timeout=LINE_TIMEOUT)
    except (FutureTimeout, Exception):                                           # noqa: BLE001
        return None
    s = ground.clean_for_speech(text or "")
    first = (ground.SentenceStream().feed(s + " ") or [s])[0] if s else ""
    if not first or len(first.split()) > 15 or ground.looks_like_code(first) or ground.has_devanagari(first) or ground.untraceable(first, allowed):
        return None
    return first


def handle_event(req: dict) -> Iterator[dict]:
    event = str(req.get("event") or "")
    turn_id = str(req.get("turn_id") or f"e-{int(time.time() * 1000)}")
    conn_id = str(req.get("conn_id") or "default")
    data = req.get("data") or {}
    t = turn._T(turn_id)
    t.intent = "noop"

    def finish(skipped: str | None = None) -> dict:
        d = turn._done(t, "converse", None, proactive=True)
        if skipped:
            d["skipped"] = skipped
        return d

    if event == "session_start":
        dlg = session.reset(conn_id)
        turn._load_memory_async(dlg)
        yield finish("state_only")
        return
    dlg = session.get(conn_id)
    if event == "session_end":
        try:
            rows = state.todays_sets()
            if rows:
                from backend.memory import db
                db.add_memory("semantic", f"Session summary: {len(rows)} sets logged; recent actions: " + "; ".join(a["text"] for a in dlg.actions[-3:]))
        except Exception:
            pass
        session.drop(conn_id)
        yield finish("state_only")
        return
    if event in ("set_active", "set_inactive"):
        dlg.set_active = event == "set_active"
        yield finish("state_only")
        return
    if event == "rest_started":
        secs = int(data.get("seconds") or 0)
        dlg.rest_until = time.time() + secs if secs > 0 else 0.0
        dlg.rest_nudges = 0
        yield finish("state_only")
        return
    if event == "set_logged":
        dlg.pr_flag = bool(data.get("pr"))
        yield finish("state_only")
        return
    if event not in ("rest_over", "idle"):
        yield finish("unknown_event")
        return

    ok, why = allow(dlg, event)
    if not ok:
        yield finish(why)
        return
    lang = getattr(dlg, "lang", None) or "en"
    blk = state.build(dlg, lang=lang, set_active=False, rest_remaining=0)
    allowed = blk["allowed"]
    line = _model_line(event, blk, dlg, lang, allowed) if llm.have_key() else None
    line = line or _template(event, lang, blk["data"].get("state"))
    if not line:
        yield finish("nothing_to_say")
        return
    now = time.time()
    dlg.coach_lines.append(now)
    if event == "rest_over":
        dlg.rest_over_ts, dlg.rest_nudges, dlg.rest_until = now, 0, 0.0
    else:
        dlg.rest_nudges += 1
    e = t.say(line)
    if e:
        yield e
    t.intent = "cue"
    dlg.add_coach(line, turn_id)
    yield finish()
