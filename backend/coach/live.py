"""The live in-gym coach: tracks where you are in today's session, understands what you say
(set / skip / short on time / question / wrap up), and answers in the chosen persona's voice.
Everything routine is deterministic and works offline; Claude handles open conversation and can polish the wording."""
from __future__ import annotations

import os
import re
import threading
import time
from collections import Counter
from datetime import datetime

from backend.agents import orchestrator as orch
from backend.agents import specialists as sp
from backend.agents.base import have_key, quick_text
from backend.coach import care, compose, hindi, lines, persona as P, session_flow, set_slots, speech_fix
from backend.memory import db
from backend.policy.adaptation import has_safety_flag
from backend.tools import exercises as ex
from backend.tools import plan_edit as pe
from backend.tools.calculators import rpe_adjusted_e1rm
from backend.tools.voice_parser import _clean_rule_parse, parse_voice_log

END_RE = re.compile(r"\b(wrap (it )?up|finish(ed)? (the |my )?(workout|session)|that'?s (it|all)|done for (today|the day)|end (the |my )?(session|workout)|i'?m done)\b")
# Skipping is read by set_slots.skip_intent(): a skip verb that is not negated, not a question and clearly an order. Anything unclear is asked about.
STATUS_RE = re.compile(r"\b(what'?s next|how many (sets )?(left|more|to go)|where (am i|are we)|what do i do next|what now)\b")
YES_RE = re.compile(r"^\W*(?:yes|yeah|yep|yup|sure|ok(?:ay)?|do it|go ahead|please do|haan|han|ha|ji|haan ji|kar do|kardo|skip it|skip)\W*$")
NO_RE = re.compile(r"^\W*(?:no|nope|nah|don'?t|do not|never ?mind|cancel|stay|not now|nahi|nahin|nhi|mat|rehne do|rahne do|wait)\W*$")
# What the mic hears around a workout that is not a command: a greeting gets a short nudge, filler and "copy" get nothing, and a line of
# stage directions ("wipes hands, adjusts straps") is not speech to the coach at all.
GREET_RE = re.compile(r"^\W*(?:(?:hello|hi|hey|yo|namaste|hii+|helo)(?:\s+(?:coach|there|buddy))?)\W*$")
PAUSE_RE = re.compile(r"^\W*(?:(?:wait|weight(?=\W+stop)|hold on|hang on|stop|stop it|pause|ruko|rukna|ruk ja|ruk jao|thehro|one sec(?:ond)?|just a (?:sec|second|minute|moment)|ek (?:minute|second|sec|min))\W*)+(?:please|coach|bhai|yaar|ji)?\W*$")
SILENT_RE = re.compile(r"^\W*(?:hm+|um+|uh+|ah+|oh+|right|copy|got it|thanks|thank you|thanks coach|bye|goodbye|cool|nice|alright|haan|han|ji|acha|accha|theek hai|thik hai|ok thanks|okay thanks)\W*$")
STAGE_RE = re.compile(r"\b(?:wipes?|wiping|adjust(?:s|ing)|chalks?|chalking|takes? a (?:deep )?breath|exhales?|inhales?|grunts?|sighs?|laughs?|nods?|claps?|"
                      r"steps? (?:up|back|away)|walks? (?:to|over|away)|picks? up|puts? down|grabs? (?:the|a|his|water)|unracks?|boxing)\b|\*[^*]+\*")
STAGE_COMMAND_RE = re.compile(r"\d|\?|\b(?:skip|next|wrap|start|done|ready|set|sets|rep|reps|plan|weight|kilo|kilos|kg|what|how|help|stop|pain|hurt|back|put)\b")
WARMUP_RE = re.compile(r"\bwarm(?:ing)?[ -]?up\b")
JUNK_NAMES = {"", "unknown", "none", "null", "n/a", "na", "stretch", "stretching", "exercise", "exercises", "workout", "set", "cable exercise",
              "machine exercise", "movement", "lift", "something", "thing"}
READY_RE = re.compile(r"^\W*(ready|i'?m ready|let'?s go|go|okay|ok)\W*$")
# "let's start today's workout", "start the session", "can we begin training", "okay let's begin". Anchored to the whole utterance so
# "I started late, bench 80 for 8" stays a set and "start the workout, I only have 30 minutes" still reaches the time-fit branch.
START_RE = re.compile(
    r"^\W*(?:(?:ok(?:ay)?|so|alright|right|hey|come on|yes|yeah)\W+)?"
    r"(?:(?:let'?s|lets|let us|i (?:want|wanna|would like|'d like) to|i'?m ready to|ready to|time to|can we|could we|shall we|please|go ahead and)\s+)*"
    r"(?:start|begin|kick ?off|get started(?: with)?|fire up|get going(?: with)?)\s+"
    r"(?:on\s+)?(?:(?:today'?s|todays|the|my|our|this|a)\s+)*"
    r"(?:workout|work out|session|training|lift(?:ing)?|gym(?: session)?|routine)\W*$")
START_BARE_RE = re.compile(r"^\W*(?:(?:ok(?:ay)?|alright|right|so)\W+)?(?:let'?s|lets)\s+(?:start|begin|get started|get going)\W*$")
TIME_CTX_RE = re.compile(r"(short on time|only (have|got)|in a rush|hurry|running late|time crunch)")
MINUTES_RE = re.compile(r"(\d{2,3})\s*(min|minutes|mins)\b")


def _ctx() -> tuple[str, str]:
    prof = db.get_profile() or {}
    return prof.get("coach_persona", P.DEFAULT), prof.get("name", "")


def fmt(x) -> str:
    if x is None:
        return ""
    return str(int(x)) if float(x) == int(x) else f"{float(x):g}"


# ---------------------------------------------------------------- session state
def _skip_rows(today: str) -> list[dict]:
    """Today's skip records, newest first."""
    return [m for m in db.get_memory(400) if m["kind"] == "skip" and m["text"].startswith(today + "|")]


def _skipped(today: str) -> set[str]:
    return {m["text"].split("|", 1)[1] for m in _skip_rows(today)}


def session_state() -> dict | None:
    view = pe.plan_view()
    if not view:
        return None
    ov = view.get("today_override")
    day = ov["day"] if ov else view["days"][view["today_index"]]
    today = session_flow.current_label()
    targets = view.get("next_targets") or {}
    done = Counter(s["exercise"] for s in db.get_sets(limit=500) if s["session"] == today)
    skipped = _skipped(today)
    rows, current = [], None
    for e in day["exercises"]:
        row = {**e, "done": done.get(e["name"], 0), "skipped": e["name"] in skipped, "target": targets.get(e["name"])}
        rows.append(row)
        if current is None and not row["skipped"] and row["done"] < e["sets"]:
            current = {"name": e["name"], "set_no": row["done"] + 1, "sets": e["sets"], "reps": e["reps"],
                       "target_rpe": e.get("target_rpe", 8.0), "target": targets.get(e["name"])}
    remaining = sum(max(0, r["sets"] - r["done"]) for r in rows if not r["skipped"])
    return {"day": day["name"], "date": today, "day_index": view["today_index"], "split_len": len(view["days"]), "exercises": rows,
            "current": current, "remaining_sets": remaining, "finished": current is None}


def _plan_names() -> list[str]:
    st = session_state()
    return [e["name"] for e in st["exercises"]] if st else []


def _last_time(exercise: str) -> str:
    """Best set from the most recent EARLIER session on this lift, e.g. '80 for 8' (empty if none)."""
    today = session_flow.current_label()
    prior = [s for s in db.get_sets(exercise, 300) if s["session"] != today and s["weight"] and s["reps"]]
    if not prior:
        return ""
    last_session = prior[0]["session"]  # newest first
    best = max((s for s in prior if s["session"] == last_session), key=lambda s: s["weight"] * 100 + s["reps"])
    return f"{fmt(best['weight'])} for {best['reps']}"


def _prior_best(exercise: str) -> tuple[float, int]:
    vals = [rpe_adjusted_e1rm(s["weight"], s["reps"], s["rpe"]) for s in db.get_sets(exercise, 500) if s["weight"] and s["reps"]]
    return (max(vals), len(vals)) if vals else (0.0, 0)


def is_pr(exercise: str, weight, reps, rpe) -> bool:
    """A PR needs history (3+ prior sets) and a clear improvement, so first-timers don't get fake confetti."""
    best, n = _prior_best(exercise)
    return bool(weight and reps) and n >= 3 and rpe_adjusted_e1rm(weight, reps, rpe) > best * 1.01


# ---------------------------------------------------------------- wording
def numbers_preserved(original: str, rewritten: str) -> bool:
    nums = lambda t: sorted(re.findall(r"\d+(?:\.\d+)?", t))
    return nums(original) == nums(rewritten)


def polish(text: str, pid: str, name: str) -> str:
    """With an API key, re-voice a templated line so it sounds fresh. Numbers must survive or we keep the original."""
    if not have_key() or os.environ.get("COACH_POLISH", "1") == "0":
        return text
    try:
        out = quick_text(P.system_prompt(pid, name) + " Rewrite the line below in your own voice. Keep every number, weight and "
                         "exercise name and every instruction exactly. Write numbers as digits (80, never eighty). Similar length. Output only the line.", text)
        out = out.strip().strip('"')
        if out and len(out) < len(text) * 2.5 + 60 and numbers_preserved(text, out):
            return out
    except Exception:
        pass
    return text


def _pick(pid, name, key, **c) -> str:
    """The template line for this moment (the offline / failure fallback). It also registers a 'beat': what to convey and the exact facts,
    which _result() hands to the model so the spoken reply is composed fresh instead of read from a bank."""
    text = lines.pick(pid, key, name, **c)
    compose.note(key, c, text)
    return text


def _reply(text: str, pid: str, name: str) -> str:
    return text   # kept for the call sites: the reply is composed once per turn in _result() from the beats, not rewritten line by line


def _brief(st: dict | None) -> str:
    if not st:
        return ""
    c = st.get("current")
    where = f"now {c['name']}, set {c['set_no']} of {c['sets']}; " if c else "all planned sets done; "
    return f"{st['day']}: {where}{st['remaining_sets']} sets left overall"


_mode = threading.local()   # fast=True: a tap on a button, so the template line is returned at once instead of waiting seconds for the composed one


def _fast() -> bool:
    return bool(getattr(_mode, "fast", False))


def _result(intent: str, reply: str, **extra) -> dict:
    st = session_state()
    beats = compose.take()
    if reply and beats and intent not in compose.SAFETY_INTENTS and not _fast():
        pid, name = _ctx()
        composed = compose.compose(pid, name, intent, getattr(compose._local, "said", ""), beats, _brief(st))
        if composed:
            reply = composed
    if reply and intent not in ("noop", "ignored"):
        compose.remember(_ctx()[0], reply)
    return {"intent": intent, "reply": reply, "saved": [], "next_set": None, "pr": False, "safety_flag": None,
            "actions": [], "moment": False, "state": st, **extra}


# ---------------------------------------------------------------- session lifecycle
_last_start = {"label": None, "ts": 0.0}
RESTART_SECS = 20 * 60


def start() -> dict:
    compose.begin()
    _pending_clear()
    _pending_skip.clear()
    pid, name = _ctx()
    key = (db.DB_PATH, session_flow.current_label())      # per database, so another user / test never inherits "you just started"
    again = _last_start["label"] == key and time.time() - _last_start["ts"] < RESTART_SECS
    _last_start.update(label=key, ts=time.time())
    try:
        session_flow.close_stale()      # a session from earlier that nobody ended gets closed now, so today starts from the right day
    except Exception:
        pass
    st = session_state()
    if not st:
        return _result("start", _pick(pid, name, "no_plan"))
    if st["finished"] and any(r["done"] for r in st["exercises"]):
        return _result("start", _pick(pid, name, "already_done"))
    cur = st["current"]
    if again and not any(r["done"] for r in st["exercises"]):   # reopened the screen a minute later: no second long intro, just where we are
        return _result("start", _pick(pid, name, "ready_go", ex=cur["name"], n=cur["set_no"], tot=cur["sets"]))
    if any(r["done"] for r in st["exercises"]):   # already training today (left and came back): never greet like a fresh morning
        done = sum(r["done"] for r in st["exercises"])
        return _result("start", _reply(_pick(pid, name, "resume", done=done, ex=cur["name"], n=cur["set_no"], tot=cur["sets"]), pid, name), moment=True)
    rec = sp.recovery_assess()
    key = "open_fresh" if rec["readiness"] is None else "open_low" if rec["readiness"]["label"] == "low" or rec["persistent_low"] else "open_ok"
    parts = [_pick(pid, name, key), _pick(pid, name, "intro", day=st["day"], cnt=len(st["exercises"]), ex=cur["name"])]
    last = _last_time(cur["name"])
    if last:
        parts.append(_pick(pid, name, "last_time", ex=cur["name"], last=last))
    parts.append(_pick(pid, name, "warmup", ex=cur["name"]))
    return _result("start", _reply(" ".join(parts), pid, name), moment=True)


def end() -> dict:
    compose.begin(getattr(compose._local, "said", ""))
    _pending_clear()
    _pending_skip.clear()
    pid, name = _ctx()
    today = session_flow.current_label()
    todays = [s for s in db.get_sets(limit=500) if s["session"] == today and s["weight"] and s["reps"]]
    if not todays:
        return _result("end", _pick(pid, name, "nothing_logged"), actions=[{"type": "end"}], moment=True)
    tonnes = round(sum(s["weight"] * s["reps"] for s in todays) / 1000, 1)
    try:
        fin = session_flow.on_session_finished(today)    # closes the session against its split day, memory, next targets, agent loop
    except Exception:
        fin = {}
    prs = fin.get("prs", [])
    parts = [_pick(pid, name, "end", sets=len(todays), exn=len({s["exercise"] for s in todays}), tonnes=fmt(tonnes))]
    if prs:
        parts.append(_pick(pid, name, "end_pr", prs=lines.prs_phrase(pid, prs)))
    from backend.tools.meal_log import today_summary
    rem = (today_summary().get("remaining") or {}).get("protein")
    if rem and rem >= 15:
        parts.append(_pick(pid, name, "end_prot", prot=round(rem / 5) * 5))
    return _result("end", _reply(" ".join(parts), pid, name), actions=[{"type": "end"}], prs=prs, moment=True, finished=fin)


def cue(event: str) -> dict:
    compose.begin()
    pid, name = _ctx()
    st = session_state()
    if event == "rest_over" and st and st["current"]:
        c = st["current"]
        return _result("cue", _reply(_pick(pid, name, "rest_over", ex=c["name"], n=c["set_no"], tot=c["sets"]), pid, name))
    if event == "rest_over":
        return _result("cue", _pick(pid, name, "rest_done"))
    return _result("cue", "")


# ---------------------------------------------------------------- utterances
def _rest_seconds(exercise: str, rpe) -> int:
    if ex.info(exercise)["compound"]:
        return 180 if (rpe or 0) >= 9.5 else 120
    return 75


def _numbers_in(text: str) -> set[float]:
    from backend.tools.foods import _words_to_digits
    return {float(x) for x in re.findall(r"\d+(?:\.\d+)?", _words_to_digits((text or "").lower()))}


# ---------------------------------------------------------------- what a spoken set is saved as (E-002 / E-003 in docs/ERROR_LEDGER.md)


def _known_exercise(n: str, plan: list[str]) -> bool:
    n = (n or "").lower().strip()
    return bool(n) and n not in JUNK_NAMES and (n in {p.lower() for p in plan} or n in ex.EXERCISES or n in set(ex.ALIASES.values()))


def _pin_to_plan(sets: list[dict], text: str, plan: list[str]) -> tuple[list[dict], list[dict]]:
    """A spoken set belongs to an exercise the coach knows. A name it cannot place ('unknown', 'stretch', 'cable exercise', anything made up)
    becomes the plan exercise named in the sentence, else the exercise you are on now. With neither: returned in `unplaced` so the coach asks."""
    low = text.lower()
    named = set_slots.find_exercise(hindi.normalize(text), plan) if plan else None
    cur = (session_state() or {}).get("current")
    placed, unplaced = [], []
    for s in sets:
        if _known_exercise(s["exercise"], plan) or (WARMUP_RE.search(low) and s["exercise"] not in JUNK_NAMES):
            placed.append(s)
        elif named:
            placed.append({**s, "exercise": named})
        elif cur:
            placed.append({**s, "exercise": cur["name"]})
        else:
            unplaced.append(s)
    return placed, unplaced


def _ground_numbers(sets: list[dict], text: str) -> list[dict]:
    """With one set in a long sentence, a model can pick the wrong number ('five reps' said, 8 saved). When the sentence itself says the reps
    or the weight with a unit ('5 reps', '60 kilos'), those words win."""
    if len(sets) != 1:
        return sets
    g = set_slots.extract(hindi.normalize(text))
    if g["invalid"]:
        return sets
    s = dict(sets[0])
    if g["r_explicit"] and g["r"] is not None:
        s["reps"] = int(g["r"])
    if g["w_explicit"] and g["w"] is not None:
        s["weight"] = float(g["w"])
    return [s]


def _collapse_repeats(sets: list[dict], text: str) -> list[dict]:
    """The same set repeated inside ONE parse result (same exercise, weight, reps) is one set unless the sentence asked for several."""
    if len(sets) < 2 or re.search(r"(?:^|\W)(?:\d+|two|three|four|five|six)\s*(?:sets|times|x)(?:\W|$)|(?:^|\W)sets? of(?:\W|$)|(?:^|\W)(?:twice|thrice)(?:\W|$)|(?:^|\W)x\s*\d", text.lower()):
        return sets
    out: list[dict] = []
    for s in sets:
        sig = (s["exercise"], s.get("weight"), s.get("reps"), s.get("rpe"))
        if not any((o["exercise"], o.get("weight"), o.get("reps"), o.get("rpe")) == sig for o in out):
            out.append(s)
    return out


def _log_sets(text: str, pid: str, name: str, parsed: dict | None = None) -> dict | None:
    explicit = parsed is not None               # built by code from checked numbers (a slot answer, the dials), not read from speech
    if parsed is None and not _numbers_in(text):
        return None                              # a set needs numbers: no digits or number words, no set, and no model call to find out
    parsed = parsed if parsed is not None else parse_voice_log(text, fast=True)
    sets = parsed.get("sets", [])
    if not sets:
        return None
    names = _plan_names()
    for s in sets:
        s["exercise"] = ex.resolve_exercise(s["exercise"], names)
    if explicit is False:                       # read out of free speech (rules or the model): the numbers must really be in the sentence
        heard = _numbers_in(text)
        sets = [s for s in sets if s.get("reps") is not None and float(s["reps"]) in heard and (s.get("weight") in (None, 0, 0.0) or float(s["weight"]) in heard)]
        if not sets:
            return None                         # "tell me how much protein", "what should I eat": not a set at all, never answered as one
        sets = _ground_numbers(sets, text)      # the saved numbers are the ones SAID, not a model's guess from a long sentence
        sets = _collapse_repeats(sets, text)    # one spoken set that a parser returned twice is one set
        sets, unplaced = _pin_to_plan(sets, text, names)
        if unplaced:                            # a set with no exercise the coach can place: ask, never save it as "unknown"
            b = unplaced[0]
            return _ask(pid, name, {"ex": None, "w": b.get("weight"), "r": b.get("reps"), "sets": None, "rpe": b.get("rpe"), "tries": 0,
                                    "label": session_flow.current_label()})
    # A model's reading of "first set done" can come back as {bench press, no weight, 1 rep}. That is a guess, not a set: never save it (it also
    # tags today as a day you did not train). Ask for the missing numbers instead.
    good = [s for s in sets if s.get("reps") and (s.get("weight") is not None or s["exercise"] in set_slots.BODYWEIGHT)]
    if not good:
        b = sets[0]
        pend = {"ex": b["exercise"], "w": b.get("weight"), "r": None if b.get("weight") is None else b.get("reps"), "sets": None, "rpe": None, "tries": 0,
                "label": session_flow.current_label()}
        return _ask(pid, name, pend)
    sets = parsed["sets"] = good
    prs = [s for s in sets if is_pr(s["exercise"], s.get("weight"), s.get("reps"), s.get("rpe"))]
    res = orch.log_and_coach(session_flow.current_label(), text, parsed={**parsed, "sets": sets})
    last = sets[-1]
    after = session_state()
    row = next((r for r in after["exercises"] if r["name"] == last["exercise"]), None)
    target_reps = row["reps"] if row else None

    w, r, rpe = last.get("weight"), last.get("reps"), last.get("rpe")
    parts = []
    if prs:
        parts.append(_pick(pid, name, "react_pr", pr_ex=prs[0]["exercise"], w=fmt(prs[0].get("weight")), r=prs[0].get("reps")))
    else:
        cat = ("miss" if target_reps and r is not None and r <= target_reps - 2
               else "grind" if (rpe or 0) >= 9.5 else "easy" if rpe is not None and rpe <= 7 else "good")
        parts.append(_pick(pid, name, f"react_{cat}", w=fmt(w), r=r))

    ex_done = bool(row) and row["done"] >= row["sets"]
    nxt = after["current"]
    nset = res.get("next_set")
    if ex_done and nxt:
        t = _pick(pid, name, "ex_done", ex=last["exercise"], nxt=nxt["name"], nsets=nxt["sets"], nreps=nxt["reps"])
        lt = _last_time(nxt["name"])
        parts.append(t + (" " + _pick(pid, name, "last_time_short", last=lt) if lt else ""))
    elif ex_done:
        parts.append(_pick(pid, name, "ex_done_last", ex=last["exercise"]))
    elif nset and w:
        key = "inst_up" if nset["weight"] > w else "inst_down" if nset["weight"] < w else "inst_same"
        parts.append(_pick(pid, name, key, nw=fmt(nset["weight"]), nr=nset["reps"]))
    actions = []
    if not after["finished"]:
        sec = _rest_seconds(last["exercise"], rpe)
        _pick(pid, name, "rest_cue", rest=lines.rest_text(sec))   # registered for the offline template only; the rest timer is on screen, so it is not spoken
        actions.append({"type": "rest", "seconds": sec})
    flag = res.get("safety_flag")
    if flag:
        parts = [_pick(pid, name, "pain")]
        actions = []
    out = _result("set", _reply(" ".join(parts), pid, name), saved=res["saved"], next_set=nset,
                  pr=bool(prs), safety_flag=flag, actions=actions, moment=bool(prs) or ex_done or bool(flag))
    return out


# ---------------------------------------------------------------- half-described sets: ask for what is missing
PENDING_TTL = 600.0
_pending: dict = {}


def _pending_get() -> dict | None:
    p = _pending
    if p and time.time() - p["ts"] < PENDING_TTL and p.get("label") == session_flow.current_label():
        return p
    _pending.clear()
    return None


def _pending_clear() -> None:
    _pending.clear()


def _ask(pid: str, name: str, pend: dict, intent: str = "ask") -> dict:
    """Ask for exactly what is missing, in the coach's own words (composed live; the template is only the fallback)."""
    w, r, exn = pend["w"], pend["r"], pend["ex"]
    bodyweight = exn in set_slots.BODYWEIGHT
    if exn is None:
        key, ctx = "ask_which", {"w": fmt(w), "r": r}
    elif bodyweight and r is None:
        key, ctx = "ask_reps_only", {"ex": exn}
    elif w is None and r is None:
        key, ctx = "ask_both", {"ex": exn}
    elif w is None:
        key, ctx = "ask_weight", {"ex": exn, "r": r}
    else:
        key, ctx = "ask_reps", {"ex": exn, "w": fmt(w)}
    pend["missing"] = {k for k, v in (("w", w), ("r", r)) if v is None and not (k == "w" and bodyweight)}
    pend["ts"] = time.time()
    _pending.clear()
    _pending.update(pend)
    return _result(intent, _pick(pid, name, key, **ctx), expects="set_details")


def _finish(pid: str, name: str, pend: dict) -> dict | None:
    exn, w, r = pend["ex"], pend["w"], pend["r"]
    _pending_clear()
    row = {"exercise": exn, "weight": float(w or 0.0), "reps": int(r)}
    if pend.get("rpe") is not None:
        row["rpe"] = pend["rpe"]
    n = max(1, min(int(pend.get("sets") or 1), 10))
    shown = f"{exn} {fmt(w or 0)} for {r}"
    return _log_sets(shown, pid, name, parsed={"sets": [dict(row) for _ in range(n)], "safety_note": ""})


def _set_flow(pid: str, name: str, norm: str, low: str) -> dict | None:
    """'1 set done' -> ask for the weight and reps; '80' / '8 reps' after that -> fill in, then log. Returns None when this is not that case."""
    plan = _plan_names()
    cur = (session_state() or {}).get("current")
    pend = _pending_get()
    if pend:
        if set_slots.CANCEL_RE.search(low):
            _pending_clear()
            return _result("ask_cancel", _pick(pid, name, "ask_cancel"))
        if _clean_rule_parse(norm) is not None:       # a complete new log ("bench 80 for 8"): that replaces the half-finished one
            _pending_clear()
            return None
        got = set_slots.extract(norm, ask=pend["missing"])
        if (pend["missing"] == {"r"} and pend.get("w") and got["r"] is not None and not got.get("r_explicit") and float(got["r"]) == float(pend["w"])):
            return _ask(pid, name, dict(pend))                 # (a copy: _ask rewrites the pending state) they repeated the weight: ask for the reps again instead of logging that many reps
        mentioned = set_slots.find_exercise(norm, plan)
        useful = got["w"] is not None or got["r"] is not None
        if not useful:
            if QUESTION_RE.match(low) or len(low.split()) > 6 or mentioned:
                return None                                  # a real question or something else entirely: let the rest of the coach handle it
            pend["tries"] = pend.get("tries", 0) + 1
            if pend["tries"] >= 3:
                _pending_clear()
                return None
            return _result("ask_unclear", _pick(pid, name, "ask_unclear"), expects="set_details")
        if got["leftover"]:
            _pending_clear()
            return None                                      # more numbers than slots: leave it to the model rather than guess
        w_now = got["w"] if (pend.get("w_default") and got["w"] is not None) else (pend["w"] if pend["w"] is not None else got["w"])   # "80 for 8" on a pull-up is a weighted pull-up
        new = {**pend, "w": w_now, "w_default": pend.get("w_default") and got["w"] is None, "r": pend["r"] if pend["r"] is not None else got["r"],
               "sets": pend.get("sets") or got["sets"], "rpe": pend.get("rpe") if pend.get("rpe") is not None else got["rpe"]}
        if mentioned and pend["ex"] is None:
            new["ex"] = mentioned
        if new["ex"] in set_slots.BODYWEIGHT and new["w"] is None:
            new["w"], new["w_default"] = 0.0, True
        if new["ex"] is not None and new["r"] is not None and new["w"] is not None:
            return _finish(pid, name, new)
        return _ask(pid, name, new)
    # nothing pending: only act on a clearly set-like sentence the normal parser could not read
    if _clean_rule_parse(norm) is not None:
        return None
    if "?" in norm or QUESTION_RE.match(low):
        return None                                           # a question about weights or reps, not a report
    got = set_slots.extract(norm)
    done = set_slots.is_set_done(norm, plan)
    exn = set_slots.find_exercise(norm, plan)
    explicit = got["w"] is not None and got["r"] is not None
    said_a_unit = got["w_explicit"] or got["r_explicit"]      # "8 reps" or "seventy kilos" on its own is still a half-reported set
    if not (done or explicit or said_a_unit or (exn and (got["w"] is not None or got["r"] is not None))):
        return None
    if got["leftover"] or got["invalid"]:
        return None
    exn = exn or (cur["name"] if cur else None)
    if exn is None and not explicit:
        return None
    pend = {"ex": exn, "w": got["w"], "r": got["r"], "sets": got["sets"], "rpe": got["rpe"], "tries": 0, "label": session_flow.current_label()}
    if exn in set_slots.BODYWEIGHT and pend["w"] is None:
        pend["w"], pend["w_default"] = 0.0, True               # bodyweight unless they say otherwise
    if exn is not None and pend["r"] is not None and pend["w"] is not None:
        return _finish(pid, name, pend)
    return _ask(pid, name, pend)


def live_context() -> str:
    """Accurate one-paragraph state of the session for the model: split day, what is done, what is left, what is next."""
    st = session_state()
    if not st:
        return ""
    c = st["current"]
    done = [f"{r['name']} {r['done']}/{r['sets']}" for r in st["exercises"] if r["done"] and not r["skipped"]]
    left = [f"{r['name']} {r['sets'] - r['done']} sets" for r in st["exercises"] if not r["skipped"] and r["done"] < r["sets"]]
    skipped = [r["name"] for r in st["exercises"] if r["skipped"]]
    out = f"Live session, {st['day']} (day {st['day_index'] + 1} of {st['split_len']} in the split); "
    out += (f"currently {c['name']}, set {c['set_no']} of {c['sets']} ({c['reps']} reps, RPE {c['target_rpe']:g}"
            + (f", target {c['target']['weight']:g} kg" if c.get("target") else "") + "); ") if c else "all planned sets done; "
    out += f"{st['remaining_sets']} sets left"
    total = sum(r["done"] for r in st["exercises"])
    out += f"; sets logged today (exact, from the log): {total}" + ("" if total else " (none yet, so never say an exercise is done)")
    if done:
        out += "; done: " + ", ".join(done)
    if left:
        out += "; still to do: " + ", ".join(left[:6])
    if skipped:
        out += "; skipped: " + ", ".join(skipped)
    return out


QUESTION_RE = re.compile(r"^(what|how|should|can|could|why|is|are|do|does|when|where|which|am i|any)\b")


def _addressed(low: str, coach_name: str) -> bool:
    """In hands-free mode the mic hears the whole gym. Only treat free speech as ours if it names the coach or is a question."""
    return coach_name.lower() in low or bool(QUESTION_RE.match(low))


def log_set(exercise: str | None, weight: float, reps: int, rpe: float | None = None, sets: int = 1) -> dict:
    """A set entered with the dials on the workout screen: structured, nothing is parsed from speech, and the answer comes back at once
    (template line; the spoken, composed version is for talking). Same result shape as handle(): reply, state, rest action, pr, safety_flag."""
    _mode.fast = True
    try:
        compose.begin()
        _pending_clear()
        pid, name = _ctx()
        st = session_state()
        if not st:
            return _result("noop", _pick(pid, name, "no_plan"))
        cur = st["current"]
        exn = ex.resolve_exercise(exercise, _plan_names()) if exercise else (cur["name"] if cur else None)
        if not exn:
            return _result("noop", _pick(pid, name, "all_done"))
        row = {"exercise": exn, "weight": float(weight or 0.0), "reps": int(reps)}
        if rpe is not None:
            row["rpe"] = float(rpe)
        n = max(1, min(int(sets or 1), 10))
        shown = f"{exn} {fmt(row['weight'])} for {row['reps']}"
        return _log_sets(shown, pid, name, parsed={"sets": [dict(row) for _ in range(n)], "safety_note": ""})
    finally:
        _mode.fast = False


def handle(transcript: str, ambient: bool = False, allow_chat: bool = True, fast: bool = False) -> dict:
    if fast:     # a button ("skip this", "what's next"): same logic, instant template line
        _mode.fast = True
        try:
            return handle(transcript, ambient, allow_chat, False)
        finally:
            _mode.fast = False
    return _handle(transcript, ambient, allow_chat)


# ---------------------------------------------------------------- skipping (E-001) and putting an exercise back
SKIP_CONFIRM_TTL = 90.0
_pending_skip: dict = {}
UNSKIP_RECENT_SECS = 600.0     # "put it back" with no name means the exercise skipped in the last ten minutes


def _pending_skip_get() -> dict | None:
    p = _pending_skip
    if p and time.time() - p["ts"] < SKIP_CONFIRM_TTL and p.get("label") == session_flow.current_label() and p.get("db") == db.DB_PATH:
        return p
    _pending_skip.clear()
    return None


def _skip_target(st: dict, named: str | None) -> dict | None:
    """The exercise a skip is about: the one named in the sentence if it can still be skipped, else the one you are on."""
    row = next((r for r in st["exercises"] if r["name"] == named and not r["skipped"] and r["done"] < r["sets"]), None) if named else None
    return row or (next((r for r in st["exercises"] if r["name"] == st["current"]["name"]), None) if st["current"] else None)


def _do_skip(pid: str, name: str, skipped: str, text: str = "", reason: str = "") -> dict:
    today = session_flow.current_label()
    if skipped not in _skipped(today):                 # a second "skip" of the same exercise must not write a second record
        db.add_memory("skip", f"{today}|{skipped}")
        try:                                           # repeated skips of one exercise become a low-confidence "you may dislike this" belief
            from fitness_os.mem import ingest
            ingest.from_event("exercise.skipped", {"exercise": skipped, "date": today, "reason": reason})
        except Exception:
            pass
    if text and has_safety_flag(text.lower()):         # "skip this, my shoulder hurts": skipping is right, and the pain is still written down
        db.add_memory("injury", text)
        db.add_checkin(note=text)
    after = session_state()
    if after["current"]:
        reply = _pick(pid, name, "skip", ex=skipped, nxt=after["current"]["name"])
    else:
        reply = _pick(pid, name, "skip_last", ex=skipped)
    return _result("skip", _reply(reply, pid, name))


def _skip_from_speech(pid: str, name: str, intent: dict, text: str, low: str) -> dict | None:
    st = session_state()
    if not st or not st["current"]:
        return None
    row = _skip_target(st, intent["named"])
    if not row:
        return None
    _pending_skip.clear()
    reason = text if has_safety_flag(low) else ""
    return _do_skip(pid, name, row["name"], text, reason)


def _ask_to_skip(pid: str, name: str, intent: dict) -> dict | None:
    """Something that might mean 'skip' ("next exercise", "machine's busy", "move on"): ask, do not skip."""
    st = session_state()
    if not st or not st["current"]:
        return None
    row = _skip_target(st, intent["named"])
    if not row:
        return None
    _pending_skip.clear()
    _pending_skip.update(ex=row["name"], ts=time.time(), label=session_flow.current_label(), db=db.DB_PATH)
    return _result("skip_ask", _pick(pid, name, "skip_confirm", ex=row["name"]), expects="yes_no")


def _next_is(pid: str, name: str) -> dict | None:
    """'What is the next exercise?' is a question: answer it, skip nothing."""
    st = session_state()
    if not st:
        return None
    c = st["current"]
    if not c:
        return _result("status", _pick(pid, name, "all_done"))
    names = [r["name"] for r in st["exercises"]]
    later = [r for r in st["exercises"][names.index(c["name"]) + 1:] if not r["skipped"] and r["done"] < r["sets"]]
    if later:
        return _result("status", _pick(pid, name, "next_is", ex=c["name"], nxt=later[0]["name"]))
    return _result("status", _pick(pid, name, "status", ex=c["name"], n=c["set_no"], tot=c["sets"], rem=st["remaining_sets"]))


def _meta(f: dict) -> dict:
    m = f.get("meta") or {}
    if isinstance(m, str):
        import json
        try:
            m = json.loads(m or "{}")
        except ValueError:
            m = {}
    return m if isinstance(m, dict) else {}


def _retract_skip_memory(exercise: str, day: str) -> None:
    """The memory layer was told 'skipped <exercise> on <day>'. Put the athlete's record straight: that belief is retracted (not 'denied':
    skipping it for real later the same day is still allowed to be written), and an inferred dislike that rested on it is withdrawn."""
    try:
        from fitness_os import learn
        from fitness_os.mem import store
        canon = learn._exercise_in(exercise) or exercise
        for f in store.facts(predicate="happened", status=("active",), limit=500):
            m = _meta(f)
            if m.get("event") == "skip" and m.get("date") == day and m.get("exercise") in (exercise, canon):
                store.update_fact(f["id"], status="retracted", valid_to=store.now())
                store.audit("fact.unskip", f["id"], f"{exercise} on {day}")
        days = {_meta(f).get("date") for f in store.facts(predicate="happened", status=("active",), limit=500)
                if _meta(f).get("event") == "skip" and _meta(f).get("exercise") in (exercise, canon)}
        if len(days) < 3:                              # the "often skips it" inference needs 3 days of skips; it no longer has them
            for f in store.facts(predicate="dislikes_exercise", status=("active",), source="inferred", limit=100):
                if store.norm(f["object_text"]) == store.norm(canon):
                    store.update_fact(f["id"], status="retracted", valid_to=store.now())
                    store.audit("fact.unskip", f["id"], "inferred dislike withdrawn")
    except Exception:
        pass


def unskip_now(exercise: str | None = None, everything: bool = False) -> dict:
    """The app's 'put it back' button: same as unskip(), answered at once with the template line (no model call)."""
    _mode.fast = True
    try:
        return unskip(exercise, everything=everything, vague=False)
    finally:
        _mode.fast = False


def unskip(exercise: str | None = None, everything: bool = False, vague: bool = True) -> dict:
    """Put skipped exercises back into today's session. exercise = one by name; everything = all of today's skips; neither = the latest skip
    (when `vague`, only if it was made in the last ten minutes, so 'put it back' about a barbell never undoes this morning's skip).
    Deletes today's skip record(s) and retracts the matching 'skipped X' belief in the memory layer."""
    compose.begin()
    _pending_skip.clear()
    pid, name = _ctx()
    today = session_flow.current_label()
    rows = _skip_rows(today)
    if exercise:
        want = ex.resolve_exercise(exercise, [r["text"].split("|", 1)[1] for r in rows])
        rows = [r for r in rows if r["text"].split("|", 1)[1] == want]
    elif rows and not everything:
        latest = rows[0]
        try:
            age = (datetime.fromisoformat(db.now()) - datetime.fromisoformat(latest["ts"])).total_seconds()
        except Exception:
            age = 0.0
        rows = [r for r in rows if r["text"] == latest["text"]] if (not vague or age <= UNSKIP_RECENT_SECS) else []
    if not rows:
        st = session_state()
        out = [_pick(pid, name, "unskip_none")]
        c = st["current"] if st else None
        if c:
            out.append(_pick(pid, name, "ready_go", ex=c["name"], n=c["set_no"], tot=c["sets"]))
        return _result("unskip", _reply(" ".join(out), pid, name), restored=[])
    names = list(dict.fromkeys(r["text"].split("|", 1)[1] for r in rows))
    with db.conn() as c:
        for r in rows:
            c.execute("DELETE FROM memory WHERE id = ?", (r["id"],))
    for n in names:
        _retract_skip_memory(n, today)
    st = session_state()
    cur = st["current"]["name"] if st and st["current"] else names[0]
    return _result("unskip", _reply(_pick(pid, name, "unskip", ex=" and ".join(names), nxt=cur), pid, name), restored=names)


def _unskip_from_speech(intent: dict) -> dict | None:
    if not _skip_rows(session_flow.current_label()):
        return None                                    # "I will do it" with nothing skipped is not about skipping at all
    return unskip(intent["named"], vague=intent["vague"])


def _negated_skip(pid: str, name: str, intent: dict) -> dict:
    """'I am NOT skipping the goblet squat', 'we are not skipping anything', "don't skip it": nothing gets skipped. If something was skipped
    earlier the athlete is saying they want it back, so it is restored (the named one, all of them for 'anything', else the latest)."""
    _pending_skip.clear()
    skipped = _skipped(session_flow.current_label())
    if intent["named"]:
        if intent["named"] in skipped:
            return unskip(intent["named"])
    elif skipped:
        return unskip(everything=intent["everything"], vague=False)
    st = session_state()
    c = st["current"] if st else None
    if not c:
        return _result("status", _pick(pid, name, "all_done"))
    return _result("ready", _pick(pid, name, "ready_go", ex=c["name"], n=c["set_no"], tot=c["sets"]))


def _handle(transcript: str, ambient: bool = False, allow_chat: bool = True) -> dict:
    """allow_chat=False (the universal voice layer): never call the model; unrecognised talk returns intent 'fallback' with an empty reply.
    ambient=True: the mic is always on. Sets, commands and pain are always handled;
    open chat only happens when the lifter addresses the coach, so gym chatter is ignored."""
    pid, name = _ctx()
    text = speech_fix.clean(transcript.strip())
    compose.begin(text)
    norm = hindi.normalize(text)   # Hindi/Haryanvi (Devanagari) speech -> the English-ish text the parsers understand; English passes through
    low = norm.lower()
    if not text:
        return _result("noop", "")
    cared = care.handle(text, pid in lines.HI_PERSONAS)    # disordered-eating / crisis language: every persona drops the act, always first
    if cared:
        return _result("care", cared, safety_flag=care.check(text), moment=True)
    try:                                   # facts the athlete states ("I hate lunges", "my knee hurts on deep squats") become memory
        from fitness_os import learn
        learn.learn_from_user_turn(text, "live")
    except Exception:
        pass
    try:                                   # ... and the same words as beliefs with provenance in the athlete model
        from fitness_os.mem import ingest
        ingest.observe(text, source="live", channel="live", defer_llm=True)
    except Exception:
        pass
    if not session_state():
        return _result("noop", _pick(pid, name, "no_plan"))

    ps = _pending_skip_get()
    if ps:                                 # the coach asked "skip X?": only a clear yes skips; a no or anything else drops the question
        ps = dict(ps)
        _pending_skip.clear()
        if YES_RE.match(low):
            return _do_skip(pid, name, ps["ex"])
        if NO_RE.match(low):
            c = (session_state() or {}).get("current")
            if c:
                return _result("ready", _pick(pid, name, "ready_go", ex=c["name"], n=c["set_no"], tot=c["sets"]))

    if STAGE_RE.search(low) and not STAGE_COMMAND_RE.search(low):
        return _result("ignored", "")      # "wipes hands, adjusts straps": stage directions heard as speech, not a message to the coach
    if SILENT_RE.match(low) or PAUSE_RE.match(low):      # "wait, stop" is the athlete cutting the coach off: the agent already stopped the audio, say nothing
        return _result("ignored", "")
    if GREET_RE.match(low):                # "Hello." gets one short line (where you are), not a paragraph from the model
        c = (session_state() or {}).get("current")
        if c:
            return _result("ready", _pick(pid, name, "ready_go", ex=c["name"], n=c["set_no"], tot=c["sets"]))

    if END_RE.search(low):
        return end()

    if START_RE.match(low) or START_BARE_RE.match(low):
        return start()                     # "let's start today's workout": the same greeting, first exercise and warm-up as opening the session

    if TIME_CTX_RE.search(low) or (MINUTES_RE.search(low) and "only" in low):
        m = MINUTES_RE.search(low)
        if not m:
            return _result("ask_time", _pick(pid, name, "ask_time"))
        view = pe.fit_today(int(m.group(1)))
        day = view["today_override"]["day"]
        sets = sum(e["sets"] for e in day["exercises"])
        reply = _pick(pid, name, "fit", min=m.group(1), cnt=len(day["exercises"]), sets=sets)
        return _result("fit", _reply(reply, pid, name), moment=True)

    if not MINUTES_RE.search(low):
        intent = set_slots.skip_intent(norm, _plan_names())
        kind = intent["kind"]
        if kind == "unskip":
            res = _unskip_from_speech(intent)
            if res:
                return res
        elif kind == "negated":
            return _negated_skip(pid, name, intent)
        elif kind == "skip":
            res = _skip_from_speech(pid, name, intent, text, low)
            if res:
                return res
        elif kind == "ask":
            res = _ask_to_skip(pid, name, intent)
            if res:
                return res
        elif kind == "question" and not STATUS_RE.search(low):
            res = _next_is(pid, name)
            if res:
                return res

    if STATUS_RE.search(low):
        st = session_state()
        c = st["current"]
        if not c:
            return _result("status", _pick(pid, name, "all_done"))
        return _result("status", _pick(pid, name, "status", ex=c["name"], n=c["set_no"], tot=c["sets"], rem=st["remaining_sets"]))

    if READY_RE.match(low):
        c = session_state()["current"]
        if c:
            return _result("ready", _pick(pid, name, "ready_go", ex=c["name"], n=c["set_no"], tot=c["sets"]))

    if not has_safety_flag(low):
        asked = _set_flow(pid, name, norm, low)
        if asked:
            return asked

    logged = _log_sets(norm, pid, name)
    if logged:
        return logged

    if has_safety_flag(low):
        db.add_memory("injury", text)
        db.add_checkin(note=text)
        return _result("pain", _pick(pid, name, "pain"), safety_flag=has_safety_flag(low), moment=True)

    if ambient and not _addressed(low, P.get(pid)["name"]):
        return _result("ignored", "")

    if not allow_chat:
        return _result("fallback", "")

    if have_key():
        try:
            return _result("chat", orch.chat(text, live_context=live_context(), channel="live"), moment=True)
        except Exception:
            pass
    return _result("fallback", _pick(pid, name, "fallback"))
