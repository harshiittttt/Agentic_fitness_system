"""Active learning: ask the ONE question whose answer would improve the coaching the most, at most once a day.

    generate() -> dict          queue questions from what we do not know / are unsure of (idempotent, de-duplicated by topic)
    next(limit=1) -> list       the question surfaced today (a new one is chosen and marked asked only if none was asked today)
    peek(limit=3) -> list       what is queued, without surfacing anything
    answer(id, text) -> dict    the answer goes through ingest (or confirms / denies the belief it was about) and is linked to the fact

Value of information = uncertainty x decision relevance x recency:
  uncertainty  1.0 for an empty key slot, 1 - |2c - 1| for a belief held with confidence c, 0.8 for a contradiction, 0.6 for a validated pattern awaiting a yes/no
  relevance    how much a wrong guess hurts a decision (injuries and goal 1.0 ... exercise preferences 0.4)
  recency      newer disagreements matter a little more than stale ones
Topics: slot:<slot>, fact:<id>, dispute:<id>, discovery:<id>, conflict:<name>.
"""
from __future__ import annotations

import importlib
import json
import math
import re
from datetime import timedelta

from backend.memory import db

from .. import clock
from . import ingest, store

dossier = importlib.import_module(f"{__package__}.dossier")   # not `from . import dossier`: the package facade has a function of that name

COOLDOWN_DAYS = 30            # a dismissed or answered topic is not asked again for this long
RE_ASK_DAYS = 3               # an unanswered question may be asked again after this long
STALE_DAYS = 21               # an open question nobody answered in this long is dropped

SLOT_Q = {
    "goal": ("What is the main thing you want from training right now?", "It decides how I set your calories and volume.", ["Lose fat", "Build muscle", "Get stronger", "Stay consistent"]),
    "injuries": ("Is anything hurting, or is there an old injury I should work around?", "I never want to load something that is already complaining.", ["Nothing", "Knee", "Lower back", "Shoulder"]),
    "schedule": ("How many days a week can you realistically train?", "A plan you can keep beats a perfect one you can't.", ["2", "3", "4", "5", "6"]),
    "equipment": ("What do you train with?", "So I never program something you can't do.", ["Full gym", "Dumbbells only", "Home basics", "Bodyweight only"]),
    "diet": ("Do you follow any particular way of eating?", "It changes which meals I suggest.", ["Anything", "Vegetarian", "Eggetarian", "Vegan", "Non-veg"]),
    "motivation": ("What would make this worth it three months from now?", "Knowing what drives you lets me coach to that, not to a generic target.", None),
    "preferred_time": ("When do you like to train?", "I can time reminders and heavy days around it.", ["Morning", "Afternoon", "Evening", "It varies"]),
    "session_length": ("How long can a session be, usually?", "I'll fit the plan to your time.", ["30 min", "45 min", "60 min", "75 min"]),
    "work_pattern": ("What does a normal weekday look like?", "Sleep and stress change how hard I should push.", ["Desk job", "Shift work", "On my feet", "Studying"]),
    "exercise_prefs": ("Is there an exercise you really hate, or love?", "Then I stop programming the ones you dread.", None),
}
YES = re.compile(r"^\s*(?:yes|yeah|yep|yup|correct|right|true|sure|exactly|haan|han|ha|haa|ji|ji haan|bilkul|sahi|that'?s me|that is me|हाँ|हां|सही|जी)\b", re.I)
NO = re.compile(r"^\s*(?:no|nope|nah|not really|wrong|incorrect|false|nahi|nahin|nai|galat|not true|नहीं|ग़लत|गलत)\b", re.I)


def _first(it, default=None):
    for x in it:
        return x
    return default


def _today() -> str:
    return clock.now().date().isoformat()


def _recently_closed(topic: str) -> bool:
    cutoff = (clock.now() - timedelta(days=COOLDOWN_DAYS)).isoformat(timespec="seconds")
    with store._c() as c:
        return bool(c.execute("SELECT 1 FROM mem_question WHERE topic = ? AND status IN ('answered','dismissed') AND ts >= ?", (topic, cutoff)).fetchone())


def _observed_sessions_per_week(days: int = 28) -> tuple[float | None, int]:
    """Distinct training dates per week over the last `days` (from the logged sets: the truth), and how many weeks of history exist."""
    try:
        sets = db.get_sets(limit=800)
    except Exception:
        return None, 0
    dates = sorted({s["session"] for s in sets if s.get("reps")})
    if not dates:
        return None, 0
    cutoff = (clock.now() - timedelta(days=days)).date().isoformat()
    recent = [d for d in dates if d >= cutoff]
    first = clock.parse(dates[0] + "T00:00:00+00:00")
    weeks = min(days, max(0.0, (clock.now() - first).total_seconds() / 86400)) / 7.0
    return (len(recent) / max(1.0, weeks), int(weeks)) if weeks >= 2 else (None, int(weeks))


def _candidates() -> list[dict]:
    d = dossier.build(False)
    out: list[dict] = []
    # 1. empty or weak key slots
    for s in d["slots"]:
        if s["known"]:
            continue
        q = SLOT_Q.get(s["slot"])
        if not q:
            continue
        unc = 1.0 if not s["partial"] else 0.6
        out.append({"topic": f"slot:{s['slot']}", "question": q[0], "why": q[1], "options": q[2], "value": unc * s["weight"]})
    # 2. unsure beliefs, disputes and contradictions
    facts = store.facts(status=("active", "disputed"), limit=300)
    by_id = {f["id"]: f for f in facts}
    for f in facts:
        if f["user_state"] in ("confirmed", "corrected", "denied") or f["predicate"] in dossier.HIDE_PREDS:
            continue
        if f["status"] == "disputed":
            old = _first((o for o in facts if o["predicate"] == f["predicate"] and o["id"] != f["id"] and o["status"] == "active" and o["user_state"] in ("confirmed", "corrected")), None)
            if old:
                out.append({"topic": f"dispute:{f['id']}", "question": f"You told me {dossier.phrase(old)[0].lower() + dossier.phrase(old)[1:]}, but I'm now seeing {dossier.phrase(f)[0].lower() + dossier.phrase(f)[1:]}. Which is right?",
                            "why": "Two things don't fit together and it changes your plan.", "options": [f["object_text"], old["object_text"]], "value": 0.9 * 0.9})
            continue
        c = f["conf"]
        if f["source"] == "inferred" and 0.4 <= c < 0.75:
            unc = 1 - abs(2 * c - 1)
            rec = 0.6 + 0.4 * math.exp(-max(0.0, clock.age_days(f["last_reinforced"])) / 30)
            out.append({"topic": f"fact:{f['id']}", "question": f"I think {dossier.phrase(f)[0].lower() + dossier.phrase(f)[1:]}. Is that right?", "why": "I guessed this from what you do, not from what you told me.",
                        "options": ["Yes, that's right", "Not really"], "value": max(0.3, unc) * 0.6 * rec})
    for f in facts:     # likes and dislikes the same thing
        if f["predicate"] == "likes_exercise" and f["status"] == "active":
            opp = _first((o for o in facts if o["predicate"] == "dislikes_exercise" and o["object_key"] == f["object_key"] and o["status"] == "active"), None)
            if opp:
                out.append({"topic": f"fact:{opp['id']}", "question": f"I have you down as both liking and disliking {f['object_text']}. Which is it?", "why": "I'd rather ask than guess.",
                            "options": [f"I like {f['object_text']}", f"I dislike {f['object_text']}"], "value": 0.8 * 0.4})
    # stated days vs what the logs show
    st = _first((f for f in facts if f["predicate"] == "trains_days_per_week" and f["status"] == "active" and f.get("value_num")), None)
    obs, weeks = _observed_sessions_per_week()
    if st and obs is not None and weeks >= 2 and abs(obs - st["value_num"]) >= 1.5:
        out.append({"topic": "conflict:days", "question": f"You said you can train {int(st['value_num'])} days a week, but you've been training about {obs:.1f}. What is realistic for you?",
                    "why": "A plan built on the real number is one you'll keep.", "options": ["2", "3", "4", "5", "6"], "value": 0.8 * 0.9})
    # 3. validated patterns awaiting a yes / no
    for dsc in store.discoveries(status=("confirmed",), limit=20):
        if dsc["user_state"] == "none":
            out.append({"topic": f"discovery:{dsc['id']}", "question": f"I noticed something about you: {dsc['statement']} Does that sound right?", "why": "Your yes or no teaches me whether to rely on it.",
                        "options": ["Yes, that's me", "Not really"], "value": 0.6 * 0.7})
    return out


def generate() -> dict:
    """Queue the questions worth asking. Idempotent: one open question per topic, cooled down after an answer or a dismissal."""
    try:
        added, skipped = 0, 0
        for c in _candidates():
            if _recently_closed(c["topic"]):
                skipped += 1
                continue
            store.add_question(c["topic"], c["question"], c["why"], round(c["value"], 4), c["options"])
            added += 1
        # drop what is no longer true (slot filled since, fact gone) and what nobody answered for weeks
        live = {c["topic"] for c in _candidates()}
        cutoff = (clock.now() - timedelta(days=STALE_DAYS)).isoformat(timespec="seconds")
        dropped = 0
        for q in store.questions(status=("open", "asked"), limit=100):
            if q["topic"] not in live or q["ts"] < cutoff:
                store.dismiss_question(q["id"])
                dropped += 1
        return {"queued": added, "cooled_down": skipped, "dropped": dropped}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"[:120]}


def _public(q: dict) -> dict:
    topic = q["topic"]
    kind = topic.split(":", 1)[0]
    return {"id": q["id"], "topic": topic, "kind": kind, "question": q["question"], "why": q.get("why") or "", "options": q.get("options") or [], "value": round(q["value"], 3), "status": q["status"],
            "free_text": not q.get("options")}


def peek(limit: int = 3) -> list[dict]:
    return [_public(q) for q in store.questions(status=("open", "asked"), limit=limit)]


def next_questions(limit: int = 1, skip_kinds: tuple[str, ...] = ()) -> list[dict]:
    """At most ONE question is surfaced per day: if one was already asked today it is returned again (still unanswered), otherwise the best open one
    is chosen and marked asked."""
    try:
        today = _today()
        ok = lambda q: q["topic"].split(":", 1)[0] not in skip_kinds        # a screen that already shows validated patterns as cards skips their questions
        asked_today = [q for q in store.questions(status=("asked",), limit=50) if (q.get("asked_ts") or "")[:10] == today]
        if asked_today:
            return [_public(q) for q in asked_today if ok(q)][:1]
        if not store.questions(status=("open",), limit=1):
            generate()
        cands = [q for q in store.questions(status=("open",), limit=30) if ok(q)]
        if not cands:     # nothing new: an old unanswered question may come back, but not more than every few days
            cutoff = (clock.now() - timedelta(days=RE_ASK_DAYS)).isoformat(timespec="seconds")
            cands = [x for x in store.questions(status=("asked",), limit=20) if (x.get("asked_ts") or "") < cutoff and ok(x)]
        if not cands:
            return []
        q = cands[0]
        store.mark_asked(q["id"])
        return [_public({**q, "status": "asked"})]
    except Exception:
        return []


def _yes_no(text: str) -> bool | None:
    if YES.search(text):
        return True
    if NO.search(text):
        return False
    return None


def answer(question_id: int, text: str) -> dict:
    """Turn the answer into beliefs (through ingest) or a confirm / deny of the belief the question was about; link it, close the question."""
    with store._c() as c:
        r = c.execute("SELECT * FROM mem_question WHERE id = ?", (question_id,)).fetchone()
    if not r:
        return {"ok": False, "error": "no such question"}
    q = dict(r)
    ans = ingest.sanitize(text, 300)
    if not ans:
        return {"ok": False, "error": "empty answer"}
    if ingest.is_instruction(ans):
        return {"ok": False, "error": "I can only take plain answers here."}
    kind, _, ref = q["topic"].partition(":")
    learned: list[dict] = []
    fact_id = None
    try:
        if kind == "slot":
            learned = ingest.answer_slot(ref, ans)
        elif kind == "conflict":
            learned = ingest.answer_slot("schedule", ans)
        elif kind == "fact":
            fid, yn = int(ref), _yes_no(ans)
            f = store.get_fact(fid)
            if f is None:
                pass
            elif yn is True or (yn is None and store.norm(ans) == store.norm(f["object_text"])):
                store.user_confirm(fid)
                learned = [{"id": fid, "predicate": f["predicate"], "object": f["object_text"], "confirmed": True}]
            elif yn is False:
                store.user_deny(fid, "answered no")
                learned = [{"id": fid, "predicate": f["predicate"], "object": f["object_text"], "denied": True}]
            else:      # "I like lunges" style choice from a contradiction question
                learned = ingest.observe(ans, source="answer", channel="question")
        elif kind == "dispute":
            nid = int(ref)
            new = store.get_fact(nid)
            if new:
                old = _first((o for o in store.facts(predicate=new["predicate"], status=("active",), limit=20) if o["id"] != nid), None)
                if store.norm(ans) == store.norm(new["object_text"]) or _yes_no(ans) is True:
                    store.user_confirm(nid)
                    if old:
                        store.supersede(old["id"], nid)
                    learned = [{"id": nid, "predicate": new["predicate"], "object": new["object_text"], "confirmed": True}]
                else:
                    if old:
                        store.user_confirm(old["id"])
                    store.user_deny(nid, "answered: the other one")
                    learned = [{"id": (old or {}).get("id"), "predicate": new["predicate"], "object": (old or {}).get("object_text"), "confirmed": True}]
        elif kind == "discovery":
            did, yn = int(ref), _yes_no(ans)
            if yn is True:
                store.set_discovery(did, user_state="confirmed")
                learned = [{"discovery_id": did, "confirmed": True}]
            elif yn is False:
                store.set_discovery(did, user_state="denied")
                learned = [{"discovery_id": did, "denied": True}]
        else:
            learned = ingest.observe(ans, source="answer", channel="question")
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}"}
    fact_id = _first((x.get("id") for x in learned if x.get("id")), None)
    store.answer_question(question_id, ans, fact_id)
    try:
        dossier.refresh()
    except Exception:
        pass
    return {"ok": True, "learned": [{k: v for k, v in x.items() if k in ("id", "predicate", "object", "body", "confirmed", "denied", "discovery_id", "created")} for x in learned],
            "fact_id": fact_id, "understood": bool(learned)}


def dismiss(question_id: int) -> dict:
    store.dismiss_question(question_id)
    return {"ok": True}


next = next_questions   # the contract's name; the module itself never uses the builtin next()
