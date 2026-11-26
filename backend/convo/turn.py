"""One conversation turn, as a stream of events (docs/CONVO_CONTRACTS.md).

    request -> gates (noise, care, low-confidence ASR) -> state block + dialogue -> streaming model -> sentences (grounded, spoken as they complete)
                                                                              \\-> tool calls -> guards -> executors -> `say` (no second model round for writes)

The model owns the conversation and decides when to act; code only gates, checks and executes. On any model failure, or when nothing has been spoken after BRAIN_TIMEOUT
seconds and no write has happened, the legacy brain (voice_control.handle) answers instead, so the athlete is never left in silence.
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Iterator

from backend.coach import care, hindi, live, speech_fix, voice_control
from backend.convo import ground, guards, llm, prompt, session, state, tools
from backend.convo.guards import Ctx, T
from backend.memory import db
from backend.policy.adaptation import has_safety_flag

BRAIN_TIMEOUT = float(os.environ.get("CONVO_BRAIN_TIMEOUT", "6.0"))
ONE_SEC_AFTER = 1.6
MAX_ROUNDS = 3
LEGACY_DEADLINE = 15.0
_pool = ThreadPoolExecutor(max_workers=6)
UNIT_RE = re.compile(r"\b(kg|kgs|kilo|kilos|kilogram|kilograms|rep|reps|baar|lb|lbs|pounds?)\b")


def brain_mode(override: str | None = None) -> str:
    if override in ("converse", "legacy"):
        return override
    return "converse" if (os.environ.get("VOICE_BRAIN") or "legacy").strip().lower() == "converse" else "legacy"


class _T:
    """Bookkeeping for one turn: timings, what was said, and the pieces of the final `done.result`."""

    def __init__(self, turn_id: str):
        self.turn_id = turn_id
        self.t0 = time.time()
        self.said: list[str] = []
        self.i = 0
        self.first_say: float | None = None
        self.first_token: float | None = None
        self.ack_ms: int | None = None
        self.intent = "chat"
        self.expects: str | None = None
        self.actions: list[dict] = []
        self.state: dict | None = None
        self.saved: list = []
        self.pr = False
        self.next_set = None
        self.safety_flag = None
        self.moment = False
        self.tools_called: list[str] = []
        self.model = None
        self.model_calls = 0
        self.tool_ms = 0
        self.dropped = 0
        self.fallback = False
        self.acked = False

    def ev(self, t: str, **kw) -> dict:
        return {"t": t, "turn_id": self.turn_id, **kw}

    def ms(self) -> int:
        return int((time.time() - self.t0) * 1000)

    def say(self, text: str, max_sentences: int | None = None) -> dict | None:
        text = ground.clean_for_speech(text)
        if not text:
            return None
        if max_sentences is not None and len(self.said) >= max_sentences:
            return None
        if self.first_say is None:
            self.first_say = time.time() - self.t0
        e = self.ev("say", i=self.i, text=text, final=False)
        self.i += 1
        self.said.append(text)
        return e


def _valid_actions(actions: list[dict]) -> list[dict]:
    return [a for a in actions if voice_control.valid_action(a)]


def _done(t: _T, brain: str, ctx: Ctx | None = None, **extra) -> dict:
    said = " ".join(t.said)
    state_ = t.state if t.state is not None else live.session_state()
    result = {"reply": said, "intent": t.intent, "actions": _valid_actions(t.actions), "expects": t.expects, "moment": t.moment, "pr": t.pr, "safety_flag": t.safety_flag,
              "state": state_, "saved": t.saved, "next_set": t.next_set}
    ms = {"total": t.ms(), "model_calls": t.model_calls, "tool_ms": t.tool_ms}
    if t.ack_ms is not None:
        ms["ack"] = t.ack_ms
    if t.first_token is not None:
        ms["first_token"] = int(t.first_token * 1000)
    if t.first_say is not None:
        ms["first_say"] = int(t.first_say * 1000)
    return t.ev("done", brain=brain, intent=t.intent, said=said, expects=t.expects, interrupted=False, fallback=t.fallback, result=result, ms=ms, model=t.model,
                tools=t.tools_called, dropped_sentences=t.dropped + (ctx.dropped if ctx else 0), **extra)


# ---------------------------------------------------------------- legacy bridge
def _legacy(t: _T, req: dict, dlg, lang: str, fallback: bool, why: str | None = None) -> Iterator[dict]:
    """The old regex brain answers (VOICE_BRAIN=legacy, or the Core failed). Same event shape out."""
    t.fallback = fallback
    if fallback and why:
        yield t.ev("error", code=why, message="the conversation core could not answer; the legacy brain did", fallback=True)
    fut = _pool.submit(voice_control.handle, req.get("text") or "", req.get("screen") or "live", None, bool(req.get("ambient")), bool(dlg.get_pending()), lang)
    try:
        res = fut.result(timeout=LEGACY_DEADLINE)
    except (FutureTimeout, Exception):                                           # noqa: BLE001 - the athlete must always hear something
        res = {"reply": "That is taking too long on my side. Say it again in a few words.", "intent": "fallback", "actions": [], "expects": None, "state": None, "saved": []}
    reply = (res.get("reply") or "").strip()
    e = t.say(reply)
    if e:
        yield e
    for a in _valid_actions(res.get("actions") or []):
        yield t.ev("action", action=a)
    if res.get("state"):
        yield t.ev("state", state=res["state"])
    t.intent, t.expects, t.actions, t.state = res.get("intent", "fallback"), res.get("expects"), list(res.get("actions") or []), res.get("state")
    t.saved, t.pr, t.next_set, t.safety_flag, t.moment = res.get("saved") or [], bool(res.get("pr")), res.get("next_set"), res.get("safety_flag"), bool(res.get("moment"))
    yield _done(t, "legacy")


# ---------------------------------------------------------------- the model round (thread + queue so slow models can be timed out)
def _model_round(messages: list[dict], specs: list[dict], t: _T, cancel: threading.Event, spoke: bool) -> Iterator[tuple[str, object]]:
    q: "queue.Queue" = queue.Queue()

    def work():
        try:
            for ev in llm.stream(messages, specs, cancel=cancel):
                q.put(("ev", ev))
            q.put(("end", None))
        except llm.LLMUnavailable as e:
            q.put(("err", e))
        except Exception as e:                                                  # noqa: BLE001
            q.put(("err", e))

    threading.Thread(target=work, daemon=True).start()
    slow_sent = False
    while True:
        try:
            kind, payload = q.get(timeout=0.1)
        except queue.Empty:
            el = time.time() - t.t0
            if not spoke and not slow_sent and el > ONE_SEC_AFTER and not t.acked:
                slow_sent = True
                yield "slow", None
            if not spoke and el > BRAIN_TIMEOUT:
                cancel.set()
                yield "timeout", None
                return
            continue
        if kind == "ev":
            ev = payload
            if ev["k"] == "text" and t.first_token is None:
                t.first_token = time.time() - t.t0
            elif ev["k"] == "tool" and t.first_token is None:
                t.first_token = time.time() - t.t0
            yield ev["k"], ev
        elif kind == "end":
            return
        else:
            yield "error", payload
            return


DANGLING = {"and", "so", "like", "then", "but", "the", "a", "an", "to", "for", "at", "with", "of", "uh", "um", "because", "or", "if", "when"}


def _dangling(low: str) -> bool:
    """An utterance cut off mid-sentence ('I did like', 'and then'): the app holds it and joins it to what comes next. Narrower than speech_fix.is_fragment, which also
    caught complete sentences ('I want to move on') and left the athlete in silence."""
    words = re.findall(r"[a-z']+|\d+(?:\.\d+)?", low)
    return bool(words) and len(words) <= 8 and words[-1] in DANGLING and not any(w[0].isdigit() for w in words)


def _model_messages(dlg, pid: str, name: str, st_block: dict, text: str) -> list[dict]:
    sysmsg = prompt.system_prompt(pid, name, dlg.memory_facts)
    if dlg.summary:
        sysmsg += "\nEARLIER IN THIS WORKOUT (summary of older talk): " + dlg.summary
    msgs = [{"role": "system", "content": sysmsg}]
    msgs += dlg.history_for_model()
    msgs.append({"role": "user", "content": f"[STATE]\n{st_block['text']}\n[/STATE]\nAthlete said: \"{text}\""})
    return msgs


def _assistant_tool_message(calls: list[dict]) -> dict:
    out = []
    for c in calls:
        tc = {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
        if c.get("extra"):
            tc["extra_content"] = c["extra"]                 # Gemini 3 thought signature: must go back with the call
        out.append(tc)
    return {"role": "assistant", "content": None, "tool_calls": out}


def _load_memory_async(dlg) -> None:
    if getattr(dlg, "memory_started", False):
        return
    dlg.memory_started = True

    def work():
        try:
            dlg.memory_facts = state.memory_facts()
        except Exception:
            dlg.memory_facts = []
    threading.Thread(target=work, daemon=True).start()


def _register_question(dlg, said: str, ctx: Ctx) -> None:
    """The model asked a yes/no question in plain words ('Skip deadlift?'): remember it, so the athlete's 'yes' is accepted by the guard on the next turn."""
    last = re.split(r"(?<=[.!?])\s+", said.strip())[-1] if said.strip() else ""
    if not last.endswith("?"):
        return
    low = last.lower()
    names = ctx.plan_names()
    named = guards.set_slots.find_exercise(low, names) if names else None
    cur = (live.session_state() or {}).get("current")
    exn = named or (cur["name"] if cur else None)
    kind = "skip" if re.search(r"\bskip|chhod|chod", low) else "swap" if re.search(r"\bswap|replace|badal", low) else "end" if re.search(r"\bwrap|finish|khatam|end the", low) else None
    if kind and not dlg.get_pending():
        dlg.set_pending(kind=kind, exercise=exn, question=last)


# ---------------------------------------------------------------- the turn
def run_turn(req: dict, override: str | None = None) -> Iterator[dict]:
    """Yields the NDJSON events of one turn. Idempotent per turn_id."""
    turn_id = str(req.get("turn_id") or uuid.uuid4().hex[:12])
    dlg = session.get(str(req.get("conn_id") or "default"))
    inflight = getattr(dlg, "inflight", None)
    if inflight is None:
        inflight = dlg.inflight = {}
    cached = dlg.cached_turn(turn_id)
    if cached is None and turn_id in inflight:                    # the same turn sent twice at once: wait for the first, then replay it
        inflight[turn_id].wait(15.0)
        cached = dlg.cached_turn(turn_id)
    if cached is not None:
        yield from cached["events"]
        return
    done_evt = inflight[turn_id] = threading.Event()
    events: list[dict] = []
    t = _T(turn_id)
    try:
        for e in _run(req, dlg, t, override):
            events.append(e)
            yield e
    finally:
        dlg.store_turn(turn_id, events, " ".join(t.said))
        done_evt.set()
        inflight.pop(turn_id, None)
        try:
            session.persist(dlg.conn_id, {"ts": time.time(), "turn_id": turn_id, "text": req.get("text"), "said": " ".join(t.said), "intent": t.intent, "tools": t.tools_called,
                                          "brain": "legacy" if t.fallback else None, "ms": t.ms(), "first_say": t.first_say, "model": t.model})
        except Exception:
            pass


def _run(req: dict, dlg, t: _T, override: str | None) -> Iterator[dict]:
    raw = (req.get("text") or "").strip()
    asr = req.get("asr") or {}
    pid, name = live._ctx()
    lang = state.detect_lang(raw, req.get("lang"), getattr(dlg, "lang", None))
    dlg.lang = lang
    ctx = Ctx(text=raw, turn_id=t.turn_id, conn_id=dlg.conn_id, dlg=dlg, lang=lang, asr=asr, pid=pid, name=name)
    if req.get("set_active") is not None:
        dlg.set_active = bool(req["set_active"])

    if brain_mode(override) == "legacy":
        yield from _legacy(t, req, dlg, lang, False)
        return

    # ---- gates that need no model ----------------------------------------------------------------------------------------------------------------------
    text = speech_fix.clean(raw)
    if not text:
        t.intent = "noop"
        yield _done(t, "converse")
        return
    why = guards.asr_problem(raw, asr)
    if why:
        t.intent = "clarify"
        yield t.say(T(lang, "say_again"))
        yield _done(t, "converse", ctx, asr_problem=why)
        return
    cared = care.handle(text, False)
    if cared:
        t.intent, t.safety_flag, t.moment = "care", care.check(text), True
        yield t.say(cared)
        dlg.add_user(text, t.turn_id)
        dlg.add_coach(cared, t.turn_id)
        yield _done(t, "converse", ctx)
        return
    norm = hindi.normalize(text)
    body, woke = voice_control.strip_wake(norm)
    low = " ".join(body.lower().replace("’", "'").split()).strip(" .!?,")
    if not low:
        t.intent = "noop"
        if woke:
            yield t.say(T(lang, "here"))
        yield _done(t, "converse")
        return
    if _dangling(low) and not dlg.get_pending():
        t.intent, t.expects = "fragment", "answer"
        yield _done(t, "converse")
        return
    if live.STAGE_RE.search(low) and not live.STAGE_COMMAND_RE.search(low) or live.SILENT_RE.match(low) or live.PAUSE_RE.match(low):
        t.intent = "ignored"
        yield _done(t, "converse")
        return
    if live.GREET_RE.match(low):
        t.intent = "greet"
        e = t.say(T(lang, "here"))
        yield e
        yield _done(t, "converse")
        return
    if not dlg.get_pending() and live.NEXTSET_RE.match(low) and not req.get("set_active"):          # "okay, next set", "ready": the cue line from the session state, no model
        cur = (live.session_state() or {}).get("current")
        if cur:
            line = live._pick(pid, prompt.P.get(pid)["name"], "ready_go", ex=cur["name"], n=cur["set_no"], tot=cur["sets"])
            t.intent = "ready"
            dlg.add_user(body, t.turn_id)
            dlg.add_coach(line, t.turn_id)
            yield t.say(line)
            yield _done(t, "converse", ctx)
            return
    if not dlg.get_pending():
        qa = voice_control._session_q(low)                 # "how many sets are left", "what's the plan": the session state answers, no model (~25 ms)
        if qa and qa.get("reply"):
            t.intent = qa.get("intent") or "status"
            dlg.add_user(body, t.turn_id)
            dlg.add_coach(qa["reply"], t.turn_id)
            yield t.say(qa["reply"])
            yield _done(t, "converse", ctx)
            return
    if req.get("ambient") and not woke and not live._addressed(low, prompt.P.get(pid)["name"]) and not ground.numbers_in(low) and not live.START_RE.match(low):
        t.intent = "ignored"
        yield _done(t, "converse")
        return
    if not llm.have_key():
        yield from _legacy(t, req, dlg, lang, True, "llm_unavailable")
        return

    # ---- the conversation ------------------------------------------------------------------------------------------------------------------------------
    ctx.text = body
    ctx.pain = has_safety_flag(low)
    _load_memory_async(dlg)
    dlg.add_user(body, t.turn_id)
    blk = state.build(dlg, lang=lang, set_active=req.get("set_active"), rest_remaining=req.get("rest_remaining"), pain=ctx.pain)
    ctx.allowed = blk["allowed"] | ground.numbers_in(body, split=True) | ground.numbers_in(dlg.recent_user_text(120.0, 3), split=True)
    if ground.numbers_in(low) and UNIT_RE.search(low):
        t.acked = True
        t.ack_ms = t.ms()
        yield t.ev("ack", kind="got_it", text="Got it." if lang == "en" else "Theek hai.")
    messages = _model_messages(dlg, pid, name, blk, body)
    specs = tools.model_tools()
    active = blk["data"]["set_active"]
    max_sent = 1 if active else 4
    cancel = threading.Event()
    spoke = False
    pre_tool_said = 0
    results: list[tuple[dict, dict]] = []
    wrote_texts: list[str] = []
    rounds = 0
    try:
        while rounds < MAX_ROUNDS:
            rounds += 1
            t.model_calls += 1
            ss = ground.SentenceStream()
            calls: list[dict] = []
            failed: str | None = None

            def emit(sentence: str):
                nonlocal spoke
                s = ground.clean_for_speech(sentence)
                if not s:
                    return None
                if ground.looks_like_code(s) or ground.has_devanagari(s) or ground.untraceable(s, ctx.allowed):
                    t.dropped += 1
                    return None
                if ctx.claim_dropped:                                # the model believed it had acted: nothing else it wrote this turn can be trusted
                    t.dropped += 1
                    return None
                if ground.claims_action(s):                       # 'logged', 'saved'...: only a write tool may say so (it speaks for itself); the model's own words never
                    t.dropped += 1
                    ctx.claim_dropped = True
                    return None
                e = t.say(s, max_sent)
                if e:
                    spoke = True
                return e

            for kind, payload in _model_round(messages, specs, t, cancel, spoke):
                if kind == "slow":
                    t.acked = True
                    yield t.ev("ack", kind="one_sec", text="One sec." if lang == "en" else "Ek second.")
                elif kind == "text":
                    for s in ss.feed(payload["d"]):
                        e = emit(s)
                        if e:
                            yield e
                elif kind == "tool":
                    calls.append(payload)
                elif kind == "end":
                    t.model = payload.get("model") or t.model
                elif kind == "error":
                    failed = "llm_unavailable"
                elif kind == "timeout":
                    failed = "llm_timeout"
            for s in ss.flush():
                e = emit(s)
                if e:
                    yield e
            if failed:
                if not spoke and not ctx.wrote and not results:
                    yield from _legacy(t, req, dlg, lang, True, failed)
                    return
                yield t.ev("error", code=failed, message="the model stopped mid-turn", fallback=False)
                break
            if not calls:
                break
            pre_tool_said = len(t.said)
            spoken_by_tool: list[str] = []
            for c in calls:
                t0 = time.time()
                res = tools.execute(c["name"], c["args"], ctx)
                t.tool_ms += int((time.time() - t0) * 1000)
                shown = res.get("tool") or c["name"]                     # the tool that really ran (the guard may have swapped near-identical ones)
                t.tools_called.append(shown)
                results.append(({**c, "name": shown, "asked": c["name"]}, res))
                yield t.ev("tool", name=shown, status=res["status"], facts=res["facts"] if res["status"] != "error" else {"reason": res["facts"].get("reason")})
                for a in res.get("actions") or []:
                    t.actions.append(a)
                    if voice_control.valid_action(a):
                        yield t.ev("action", action=a)
                if res.get("state"):
                    t.state = res["state"]
                    yield t.ev("state", state=res["state"])
                _absorb(t, shown, res, dlg)
                if res.get("say") and not res.get("duplicate"):
                    spoken_by_tool.append((shown, res["say"]))
            if spoken_by_tool:
                for nm, sy in _pick_says(spoken_by_tool):
                    s = ground.clean_for_speech(sy)
                    if ground.looks_like_code(s) or ground.has_devanagari(s):
                        continue
                    for piece in ground.SentenceStream().feed(s + " ") or [s]:
                        e = t.say(piece)
                        if e:
                            spoke = True
                            yield e
                break                                           # a write tool (or a clarifying question) spoke: no second model round
            # nothing was said by the tools (read tools, or a veto with a reason): the model gets the facts and speaks
            messages.append(_assistant_tool_message(calls))
            for (c, res), orig in zip(results[-len(calls):], calls):
                payload = {"status": res["status"], **res["facts"]}
                if res["status"] == "refused":
                    payload["note"] = "Nothing was changed. Say so briefly in your own words and carry on the conversation."
                messages.append({"role": "tool", "tool_call_id": orig["id"], "content": json.dumps(payload, default=str)})
            ctx.allowed |= tools.fact_numbers([r["facts"] for _, r in results])
        if not spoke:
            fb = T(lang, "numbers_q") if (ctx.claim_dropped and not results) else T(lang, "say_again") if not results else T(lang, "ok")
            e = t.say(fb)
            if e:
                yield e
            t.intent = "clarify" if not results else t.intent
        if ctx.pain and not any(c["name"] in ("log_note", "skip_exercise") and r["status"] == "ok" for c, r in results):
            db.add_memory("injury", body)                         # whatever the model did, a pain sentence is always written down
            db.add_checkin(note=body)
            t.safety_flag = t.safety_flag or has_safety_flag(low)
    finally:
        cancel.set()
    said = " ".join(t.said)
    if t.expects is None:
        _register_question(dlg, said, ctx)
        if dlg.get_pending() and said.rstrip().endswith("?"):
            t.expects = "yes_no"
    dlg.add_coach(said, t.turn_id)
    if t.intent == "chat" and results:
        t.intent = _intent_of(results)
    yield _done(t, "converse", ctx)


def _intent_of(results: list[tuple[dict, dict]]) -> str:
    for c, r in results:
        if r["status"] == "needs_clarification":
            return "clarify"
    for c, r in results:
        if c["name"] in guards.WRITE and r["status"] == "ok":
            return tools.INTENT.get(c["name"], "chat")
    return tools.INTENT.get(results[0][0]["name"], "chat") if results else "chat"


def _pick_says(says: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Several tools spoke in one turn (log_set + control_rest): say the first, plus any clarifying question; do not read the rest timer out after a logged set."""
    if len(says) <= 1:
        return says
    first = says[0]
    rest = [s for s in says[1:] if s[0] not in ("control_rest", "set_next_weight") and s[1] not in first[1]]
    return [first] + rest[:1]


def _absorb(t: _T, name: str, res: dict, dlg) -> None:
    ex_ = res.get("extra") or {}
    if res["status"] == "needs_clarification":
        t.expects = res.get("expects") or t.expects
        t.intent = "clarify"
        return
    if res["status"] != "ok":
        return
    t.intent = tools.INTENT.get(name, t.intent) if name in guards.WRITE or t.intent == "chat" else t.intent
    if name == "log_set":
        t.saved, t.pr, t.next_set = ex_.get("saved") or [], bool(ex_.get("pr")), ex_.get("next_set")
        t.safety_flag, t.moment = ex_.get("safety_flag"), bool(ex_.get("moment"))
    elif name == "correct_last_set":
        t.saved = ex_.get("saved") or []
    elif name == "log_note" and res["facts"].get("kind") == "pain":
        t.safety_flag, t.moment = True, True
    elif name == "end_workout":
        t.moment = True


# ---------------------------------------------------------------- /convo/tool (engine B)
def run_tool(req: dict) -> dict:
    dlg = session.get(str(req.get("conn_id") or "default"))
    pid, name = live._ctx()
    utter = str(req.get("utterance") or "").strip()
    lang = state.detect_lang(utter, req.get("lang"), getattr(dlg, "lang", None))
    turn_id = str(req.get("turn_id") or uuid.uuid4().hex[:12])
    cached = dlg.cached_turn("tool:" + turn_id + ":" + str(req.get("name")) + json.dumps(req.get("args") or {}, sort_keys=True, default=str))
    if cached is not None:
        return {**cached["events"][0], "duplicate": True, "say": None, "actions": []}
    ctx = Ctx(text=utter, turn_id=turn_id, conn_id=dlg.conn_id, dlg=dlg, lang=lang, asr=req.get("asr") or {}, pid=pid, name=name, pain=has_safety_flag(utter.lower()))
    if utter and not any(m.get("turn_id") == turn_id for m in dlg.messages):
        dlg.add_user(utter, turn_id)
    blk = state.build(dlg, lang=lang, set_active=req.get("set_active"), rest_remaining=req.get("rest_remaining"))
    ctx.allowed = blk["allowed"] | ground.numbers_in(utter, split=True)
    name_ = str(req.get("name") or "")
    if name_ not in tools.BY_NAME:
        return {"status": "invalid", "say": None, "facts": {"reason": "unknown tool"}, "actions": [], "state": None, "expects": None}
    if name_ in guards.WRITE and not utter and name_ not in ("log_set", "control_rest", "set_next_weight", "correct_last_set", "log_note"):
        return {"status": "needs_clarification", "say": T(lang, "say_again"), "facts": {"reason": "no utterance for a class 2 action"}, "actions": [], "state": None, "expects": None}
    res = tools.execute(name_, dict(req.get("args") or {}), ctx)
    out = {"status": res["status"], "say": res.get("say"), "facts": res["facts"], "actions": _valid_actions(res.get("actions") or []), "state": res.get("state"),
           "expects": res.get("expects")}
    dlg.store_turn("tool:" + turn_id + ":" + name_ + json.dumps(req.get("args") or {}, sort_keys=True, default=str), [out], "")
    return out
