"""The state block the model sees on every turn (exact numbers from the log, never from memory of the conversation), plus language detection and the long-term
facts loaded when a session starts. Rebuilt per turn, so it is always true; the dialogue history only carries what was said."""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from backend.coach import live, session_flow
from backend.convo import ground
from backend.memory import db

HINGLISH_WORDS = {"haan", "han", "nahi", "nahin", "nhi", "kya", "hai", "hain", "kitna", "kitne", "bhai", "yaar", "acha", "accha", "theek", "thik", "karo", "kar", "do", "mat", "ruko",
                  "agla", "agle", "wapas", "vapas", "bas", "abhi", "ho", "gaya", "hogaya", "kilo", "paanch", "chhe", "saat", "aath", "nau", "das", "saath", "pachas", "kaun", "kaunsa",
                  "mujhe", "mera", "meri", "main", "tum", "aap", "chalo", "chal", "shuru", "khatam", "baki", "baaki", "bacha", "wala", "wali", "dukh", "dard", "thoda", "zyada", "jyada"}


def detect_lang(text: str, hint: str | None, previous: str | None) -> str:
    """'en' or 'hinglish' (Roman letters). The caller's hint wins; else Devanagari or Hinglish words mean hinglish; a very short utterance keeps the previous language."""
    if hint in ("en", "hinglish"):
        return hint
    if hint == "hi":
        return "hinglish"
    if ground.has_devanagari(text):
        return "hinglish"
    words = re.findall(r"[a-zA-Z']+", (text or "").lower())
    if sum(1 for w in words if w in HINGLISH_WORDS) >= (1 if len(words) <= 4 else 2):
        return "hinglish"
    if len(words) <= 2 and previous:
        return previous
    return "en"


def _age(ts: str) -> str:
    try:
        secs = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
    except ValueError:
        return ""
    if secs < 90:
        return f"{int(max(secs, 0))} s ago"
    return f"{int(secs // 60)} min ago"


def todays_sets() -> list[dict]:
    label = session_flow.current_label()
    rows = [s for s in db.get_sets(limit=200) if s["session"] == label and s.get("reps")]
    return rows    # newest first


def memory_facts(limit: int = 5) -> list[str]:
    """3-5 short facts about this athlete for the start of a session (injuries, preferences, patterns). Empty when the memory layer has nothing or is unavailable."""
    try:
        from fitness_os import coach_tools
        res = coach_tools.recall_about_me("injury pain preference dislike plateau goal")
        out = []
        for m in (res.get("memories") or [])[:limit]:
            t = re.sub(r"\s+", " ", str(m.get("text") or "")).strip()
            if t and t not in out:
                out.append(t[:140])
        return out
    except Exception:
        return []


def build(dlg, *, lang: str, set_active: bool | None, rest_remaining: int | None, pain: bool = False) -> dict:
    """-> {"text": state block, "allowed": numbers the coach may say, "data": structured copy for tools/guards}."""
    st = live.session_state()
    now = time.time()
    lines: list[str] = []
    stamp = datetime.now().strftime("%A %H:%M")
    if rest_remaining is None and dlg.rest_until > now:
        rest_remaining = int(dlg.rest_until - now)
    active = dlg.set_active if set_active is None else bool(set_active)
    if active:
        mode = "MID-SET: the athlete is lifting right now. At most 6 words, and only if it helps."
    elif rest_remaining and rest_remaining > 0:
        mode = f"RESTING: {rest_remaining} s left on the timer. A short exchange is fine, two sentences at most."
    else:
        mode = "BETWEEN SETS: no timer running. Short replies; explain only if asked."
    lines.append(f"Now: {stamp}. Mode: {mode}")
    lines.append("Respond in: " + ("English." if lang == "en" else "Hinglish (Hindi and English mixed, written in Roman letters only, never Devanagari)."))
    if pain:
        lines.append("The athlete just mentioned pain or discomfort: tell them to stop that movement. Do not advise treatment.")
    data: dict = {"state": st, "lang": lang, "rest_remaining": rest_remaining, "set_active": active}
    if st:
        lines.append(live.live_context())
        rows = todays_sets()
        if rows:
            lines.append("Last sets today (exact): " + "; ".join(
                f"{r['exercise']} {live.fmt(r['weight'])} x {r['reps']}" + (f" at RPE {live.fmt(r['rpe'])}" if r.get("rpe") else "") + f" ({_age(r['ts'])})" for r in rows[:3]))
        c = st["current"]
        if c:
            lt = live._last_time(c["name"])
            if lt:
                lines.append(f"Last time on {c['name']} (earlier session): {lt}.")
            if c.get("target"):
                lines.append(f"Suggested weight for this set: {live.fmt(c['target']['weight'])} kg.")
            names = [r["name"] for r in st["exercises"]]
            later = [r["name"] for r in st["exercises"][names.index(c["name"]) + 1:] if not r["skipped"] and r["done"] < r["sets"]]
            data["next_exercise"] = later[0] if later else None
            lines.append("Next exercise after this: " + (later[0] if later else "none (this is the last)") + ".")
        plan = ", ".join(f"{r['name']} ({r['sets']}x{r['reps']})" for r in st["exercises"])
        lines.append("Today's plan: " + plan + ".")
    else:
        lines.append("There is no plan yet: do not log sets; tell the athlete to finish setup in the app.")
    if dlg.next_weights:
        lines.append("Athlete's chosen next weights: " + ", ".join(f"{k} {live.fmt(v)} kg" for k, v in dlg.next_weights.items()) + ".")
    recent = [a for a in dlg.actions if now - a["ts"] < 600]
    if recent:
        lines.append("What you did just now (tools): " + "; ".join(f"{a['text']} ({int(now - a['ts'])} s ago)" for a in recent[-4:]) + ".")
    p = dlg.get_pending()
    if p:
        lines.append(f"Open question you asked: \"{p.get('question', '')}\" (waiting for yes / no).")
    text = "\n".join(lines)
    allowed = ground.allowed_numbers("\n".join(lines[1:]), extra=[rest_remaining] if rest_remaining else [])    # not line 0: the clock's digits are no license to say '40'
    return {"text": text, "allowed": allowed, "data": data}
