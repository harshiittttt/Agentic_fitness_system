"""Memory Curator (docs: 'Memory maintenance', nightly). Summarises the day into episodic memory, merges duplicates, decays
stale facts, and flags contradictions for the athlete to confirm. Deterministic first; the LLM only polishes wording and can
never change a number."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from backend.agents.base import have_key, quick_text
from backend.memory import db
from backend.tools import calculators as calc

from . import clock, ledger, memory

MEAT = {"chicken", "mutton", "lamb", "beef", "pork", "fish", "tuna", "salmon", "prawn", "prawns", "shrimp", "bacon", "turkey", "ham", "meat"}
DAIRY_EGG = {"milk", "paneer", "curd", "yogurt", "yoghurt", "egg", "eggs", "whey", "ghee", "butter", "cheese", "buttermilk"}


def _day_of(ts: str) -> str:
    return ts[:10]


def day_facts(day: str) -> dict:
    sets = [s for s in db.get_sets(None, 2000) if _day_of(s["ts"]) == day]
    checks = [c for c in db.get_checkins(200) if _day_of(c["ts"]) == day]
    with db.conn() as c:
        meals = [dict(r) for r in c.execute("SELECT * FROM meals WHERE day = ?", (day,)).fetchall()]
    decisions = [d for d in ledger.recent(300) if _day_of(d["ts"]) == day and d["status"] in ("applied", "escalated", "blocked", "pending_approval")]
    return {"day": day, "sets": sets, "checkins": checks, "meals": meals, "decisions": decisions}


def deterministic_summary(f: dict) -> str:
    d = datetime.fromisoformat(f["day"])
    parts: list[str] = []
    if f["sets"]:
        best: dict[str, dict] = {}
        for s in f["sets"]:
            if s["weight"] and s["reps"]:
                e = calc.epley_e1rm(float(s["weight"]), int(s["reps"]))
                if s["exercise"] not in best or e > best[s["exercise"]]["e"]:
                    best[s["exercise"]] = {"e": e, "w": s["weight"], "r": s["reps"], "rpe": s.get("rpe")}
        top = ", ".join(f"{ex} {b['w']:g}x{b['r']}" + (f" @{b['rpe']:g}" if b["rpe"] else "") for ex, b in list(best.items())[:3])
        parts.append(f"Trained {len(f['sets'])} sets" + (f" (top: {top})" if top else ""))
    if f["checkins"]:
        c = f["checkins"][0]
        bits = [x for x in (f"slept {c['sleep_h']:g}h" if c["sleep_h"] else "", f"soreness {c['soreness']}/5" if c["soreness"] else "",
                            f"energy {c['energy']}/5" if c["energy"] else "", f"weighed {c['bodyweight']:g}kg" if c["bodyweight"] else "") if x]
        if bits:
            parts.append("Check-in: " + ", ".join(bits))
        if c.get("note"):
            parts.append(f"Said: \"{c['note'][:80]}\"")
    if f["meals"]:
        parts.append(f"Ate {round(sum((m['kcal'] or 0) for m in f['meals']))} kcal, {round(sum((m['protein'] or 0) for m in f['meals']))}g protein")
    for dec in f["decisions"][:3]:
        parts.append(f"Team {dec['status']}: {dec['rationale'][:90]}")
    return f"{d.strftime('%b')} {d.day}: " + ". ".join(parts) + "." if parts else ""


def _numbers(t: str) -> list[str]:
    return sorted(re.findall(r"\d+(?:\.\d+)?", t))


def summarize_day(day: str, use_llm: bool = True) -> dict | None:
    """-> {id, merged, text} or None when nothing happened that day."""
    f = day_facts(day)
    text = deterministic_summary(f)
    if not text:
        return None
    if use_llm and have_key():
        try:
            out = quick_text("Rewrite this training-log line as two warm, plain sentences about the athlete. Keep every number and "
                             "exercise name exactly. Output only the sentences.", text, max_tokens=140, timeout=10.0).strip().strip('"')
            if out and _numbers(out) == _numbers(text) and len(out) < len(text) * 2 + 80:  # a rewrite may never change a number
                text = out
        except Exception:
            pass
    sal = 0.7 if f["decisions"] else 0.5 if f["sets"] else 0.3
    r = memory.add("episodic", text, sal, min_sim=0.98)  # days resemble each other: only an exact repeat merges
    return {**r, "text": text}


def dedupe() -> int:
    """Archive near-duplicate durable facts, keeping the stronger one. (Episodic days are never merged.)"""
    n = 0
    for kind in ("semantic", "procedural"):
        live = sorted(memory.items(kind=kind, limit=500), key=lambda i: (i["pinned"], i["salience"], i["id"]), reverse=True)
        kept: list[dict] = []
        for it in live:
            if any(memory.similarity(k["body"], it["body"]) >= 0.85 for k in kept):
                memory.set_status(it["id"], "archived")
                n += 1
            else:
                kept.append(it)
    return n


def contradictions() -> list[dict]:
    """Facts the athlete told us vs what they actually log. Flag it; never silently pick a side."""
    prefs = " ".join(m["text"].lower() for m in db.get_memory(60) if m["kind"] in ("preference", "semantic"))
    if not prefs:
        return []
    since = (clock.now() - timedelta(days=3)).date().isoformat()
    with db.conn() as c:
        names = [r["name"].lower() for r in c.execute("SELECT name FROM meals WHERE day >= ?", (since,)).fetchall()]
    words = {w for n in names for w in re.findall(r"[a-z]+", n)}
    out = []
    if re.search(r"\bvegan\b", prefs) and (words & (MEAT | DAIRY_EGG)):
        out.append(f"You said you are vegan, but you logged {', '.join(sorted(words & (MEAT | DAIRY_EGG)))} recently. Is that still true?")
    elif re.search(r"\bvegetarian\b|no meat", prefs) and (words & MEAT):
        out.append(f"You said you are vegetarian, but you logged {', '.join(sorted(words & MEAT))} recently. Is that still true?")
    flagged = []
    for text in out:
        r = memory.add("semantic", text, 0.9, status="flagged", min_sim=0.6)
        flagged.append({**r, "text": text})
    return flagged


def run_nightly(day: str | None = None, use_llm: bool = True) -> dict:
    day = day or (clock.now().date() - timedelta(days=1)).isoformat()
    summary = summarize_day(day, use_llm=use_llm)
    return {"day": day, "summary": summary, "deduped": dedupe(), "decayed": memory.archive_stale(), "flagged": contradictions()}
