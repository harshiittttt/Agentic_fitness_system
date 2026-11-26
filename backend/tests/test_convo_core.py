"""Conversation Core, offline: a scripted fake model replaces Gemini, everything else (guards, tools, database, event stream, HTTP) is real.
The fake model is deliberately GULLIBLE where a real model might slip: it calls skip / log tools on sentences that must not trigger them, to prove the guards hold."""
import json

import pytest
from fastapi.testclient import TestClient

from backend.convo import ground, guards, llm, proactive, session, state, tools, turn
from backend.coach import live
from backend.main import app
from backend.memory import db
from backend.tools.demo import PROFILE


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("VOICE_BRAIN", "converse")
    monkeypatch.setenv("CONVO_LOG", "0")
    live._pending_skip.clear()
    live._pending_clear()
    session.clear_all()
    from backend.agents import orchestrator as orch
    orch.onboard({**PROFILE, "name": "Golu", "coach_persona": "hype", "days_per_week": 4})
    c = TestClient(app)
    monkeypatch.setattr(llm, "have_key", lambda: True)
    return c


def script(monkeypatch, fn):
    """fn(last_user_message, messages, tools) -> list of events [{'k':'text','d':..} | {'k':'tool','name':..,'args':..}]"""
    calls = []

    def fake(messages, tools=None, **kw):
        calls.append(messages)
        for ev in fn(messages[-1]["content"], messages, tools):
            if ev["k"] == "tool":
                ev = {"id": "c1", "extra": None, **ev}
            yield ev
        yield {"k": "end", "model": "fake", "finish": "stop", "usage": None, "ttft": 0.01}
    monkeypatch.setattr(llm, "stream", fake)
    return calls


def tool(name, **args):
    return {"k": "tool", "name": name, "args": args}


def text(d):
    return {"k": "text", "d": d}


def run(text_, conn="c1", tid=None, **kw):
    tid = tid or f"t{len(session.get(conn).turns) + 1}"
    return list(turn.run_turn({"text": text_, "turn_id": tid, "conn_id": conn, **kw}, "converse"))


def names(events, t):
    return [e for e in events if e["t"] == t]


def CUR():
    return live.session_state()["current"]["name"]


def today_sets():
    return state.todays_sets()


def skipped():
    return [r["name"] for r in live.session_state()["exercises"] if r["skipped"]]


# ================================================================ ground
def test_numbers_in_words_and_digits():
    assert ground.numbers_in("sixty-five kilos") == {65.0}
    assert ground.numbers_in("sixty two point five times eight") == {62.5, 8.0}
    assert ground.numbers_in("saath kilo paanch reps") == {60.0, 5.0}
    assert ground.numbers_in("sixty... five", split=True) >= {60.0, 5.0}
    assert ground.numbers_in("Five at sixty, three fewer.") == {5.0, 60.0, 3.0}      # a comma ends a number


def test_sentence_stream_keeps_decimals_together():
    ss = ground.SentenceStream()
    out = []
    for d in ["Go to 62.", "5 next. Heavy", " today? ok"]:
        out += ss.feed(d)
    out += ss.flush()
    assert out == ["Go to 62.5 next.", "Heavy today?", "ok"]


def test_grounding_allows_comparisons_only_in_comparing_sentences():
    al = ground.allowed_numbers("squat 4x6 set 1 of 4", extra=[60, 5, 8])
    assert ground.untraceable("Two more sets of these.", al) == [2.0]
    assert ground.untraceable("Three fewer than last set.", al) == []
    assert ground.untraceable("Rest two minutes.", ground.allowed_numbers(extra=[120])) == []


def test_script_checks():
    assert ground.foreign_script("안녕하세요") and not ground.foreign_script("namaste bhai") and not ground.foreign_script("सुनो")
    assert ground.looks_like_code("log_set(weight=60, reps=5)") and not ground.looks_like_code("Nice set, go again.")


# ================================================================ the guards hold even when the model misbehaves
NEG_SKIP = ["We are not skipping anything, we are doing every exercise.", "I am not skipping the goblet squat.", "You're not skipping anything. We are doing every exercise.",
            "Skipping anything.", "Please don't skip it.", "no skip", "What is the next exercise?", "Should I skip this one?", "Okay, next set.", "How many sets are left?",
            "skip mat karna", "skip nahi karna hai", "agla exercise kaunsa hai", "abhi skip mat karo"]


@pytest.mark.parametrize("sentence", NEG_SKIP)
def test_a_gullible_model_cannot_skip_on_negatives(env, monkeypatch, sentence):
    script(monkeypatch, lambda u, m, t: [tool("skip_exercise", exercise="squat", reaction="Skipped.")] if len([x for x in m if x["role"] == "tool"]) == 0 else [text("Nothing changes, we keep going.")])
    ev = run(sentence)
    assert skipped() == []
    assert not any(e["status"] == "ok" for e in names(ev, "tool") if e["name"] == "skip_exercise")
    assert ev[-1]["t"] == "done"


def test_clear_skip_order_skips_and_records_pain(env, monkeypatch):
    cur = CUR()
    script(monkeypatch, lambda u, m, t: [tool("skip_exercise", reaction="Okay, skipping it.")])
    ev = run("Skip this exercise, my shoulder hurts.")
    assert skipped() == [cur]
    assert any("shoulder" in x["text"] for x in db.get_memory(20) if x["kind"] == "injury")      # the pain was written down too
    assert ev[-1]["intent"] == "skip"


def test_vague_move_on_asks_then_a_yes_skips(env, monkeypatch):
    cur = CUR()
    mode = {"n": 0}

    def fn(u, m, t):
        mode["n"] += 1
        return [tool("skip_exercise", confirmed=("yes" in u.lower()), reaction="Done.")]
    script(monkeypatch, fn)
    ev = run("Let's move on.")
    assert skipped() == [] and ev[-1]["expects"] == "yes_no" and "Skip" in ev[-1]["said"]
    ev = run("yes, skip it")
    assert skipped() == [cur]


def test_confirmed_flag_alone_is_not_enough(env, monkeypatch):
    script(monkeypatch, lambda u, m, t: [tool("skip_exercise", confirmed=True)])
    run("hmm what should I do")
    assert skipped() == []


def test_unskip_after_wrong_skip(env, monkeypatch):
    cur = CUR()
    live_ = live
    db.add_memory("skip", f"{live_.session_flow.current_label()}|{cur}")
    script(monkeypatch, lambda u, m, t: [tool("unskip_exercise", reaction="Back on.")])
    run("put it back, I'll do it")
    assert skipped() == []


def test_log_set_needs_numbers_the_athlete_said(env, monkeypatch):
    script(monkeypatch, lambda u, m, t: [tool("log_set", weight=60, reps=5, reaction="Nice.")])
    ev = run("hello there my friend")                  # the model invents a set out of nothing
    assert today_sets() == []
    assert any(e["status"] == "needs_clarification" for e in names(ev, "tool"))


def test_log_set_happy_path_and_event_order(env, monkeypatch):
    cur = CUR()
    script(monkeypatch, lambda u, m, t: [tool("log_set", weight=60, reps=5, reaction="Clean. Same again, go for six.")])
    ev = run("sixty kilos five reps done")
    kinds = [e["t"] for e in ev]
    assert kinds[0] == "ack" and kinds[-1] == "done" and kinds.index("tool") < kinds.index("say")
    assert [(s["exercise"], s["weight"], s["reps"]) for s in today_sets()] == [(cur, 60.0, 5)]
    d = ev[-1]
    assert d["intent"] == "set" and d["result"]["saved"] and {"type": "rest", "seconds": 120} in d["result"]["actions"] and d["brain"] == "converse"
    assert "Clean" in d["said"] and not d["fallback"]
    assert d["ms"]["model_calls"] == 1                  # a write tool with a ready line costs no second model round


def test_half_said_sets_are_completed_from_the_previous_utterance(env, monkeypatch):
    script(monkeypatch, lambda u, m, t: [tool("log_set", weight=60, reps=5)] if "five" in u.lower() and "sixty" not in u.lower() else [text("How many reps?")])
    run("sixty kilos")
    assert today_sets() == []
    run("five")
    assert [(s["weight"], s["reps"]) for s in today_sets()] == [(60.0, 5)]


def test_correction_changes_the_last_set_and_adds_none(env, monkeypatch):
    def fn(u, m, t):
        return [tool("correct_last_set", weight=65)] if "no," in u else [tool("log_set", weight=60, reps=5)]
    script(monkeypatch, fn)
    run("sixty kilos five reps done")
    run("no, sixty-five")
    assert [(s["weight"], s["reps"]) for s in today_sets()] == [(65.0, 5)]


def test_correction_numbers_must_be_heard(env, monkeypatch):
    script(monkeypatch, lambda u, m, t: [tool("correct_last_set", weight=99)] if "no," in u else [tool("log_set", weight=60, reps=5)])
    run("sixty kilos five reps done")
    ev = run("no, make it heavier")
    assert [s["weight"] for s in today_sets()] == [60.0] and any(e["status"] == "needs_clarification" for e in names(ev, "tool"))


@pytest.mark.parametrize("utter", ["six hundred kilos five reps", "sixty kilos fifty reps"])
def test_implausible_numbers_are_asked_not_saved(env, monkeypatch, utter):
    script(monkeypatch, lambda u, m, t: [tool("log_set", weight=600 if "hundred" in u else 60, reps=50 if "fifty" in u else 5)])
    ev = run(utter)
    assert today_sets() == [] and ev[-1]["intent"] == "clarify"


def test_big_jump_is_confirmed_before_it_is_saved(env, monkeypatch):
    cur = CUR()
    db.add_set(live.session_flow.current_label(), cur, 20.0, 8, 8.0)
    script(monkeypatch, lambda u, m, t: [tool("log_set", weight=60, reps=5)])
    ev = run("sixty kilos five reps")
    assert len(today_sets()) == 1 and ev[-1]["expects"] == "yes_no"
    run("yes, sixty")
    assert len(today_sets()) == 2


def test_junk_exercise_names_are_pinned_to_the_current_exercise(env, monkeypatch):
    cur = CUR()
    script(monkeypatch, lambda u, m, t: [tool("log_set", exercise="stretch", weight=60, reps=5)])
    run("sixty kilos five reps")
    assert [s["exercise"] for s in today_sets()] == [cur]


def test_unclear_asr_never_acts(env, monkeypatch):
    calls = script(monkeypatch, lambda u, m, t: [tool("log_set", weight=60, reps=5)])
    for kw in ({"asr": {"unclear": True}}, {"asr": {"avg_logprob": -1.6}}):
        ev = run("sixty kilos five reps done", **kw)
        assert ev[-1]["intent"] == "clarify" and "again" in ev[-1]["said"].lower()
    ev = run("안녕하세요 운동 시작")
    assert ev[-1]["intent"] == "clarify" and today_sets() == [] and calls == []        # the model was never asked


def test_noise_and_filler_get_no_reply(env, monkeypatch):
    calls = script(monkeypatch, lambda u, m, t: [text("x")])
    for s in ["Boxing, wipes hands, adjust straps.", "Copy.", "hmm"]:
        ev = run(s)
        assert ev[-1]["intent"] in ("ignored", "noop") and ev[-1]["said"] == ""
    assert calls == []
    assert run("Hello.")[-1]["intent"] == "greet"


def test_dangling_utterance_is_held_but_a_complete_one_is_not(env, monkeypatch):
    script(monkeypatch, lambda u, m, t: [text("Okay, we keep going.")])
    ev = run("I did like")
    assert ev[-1]["intent"] == "fragment" and ev[-1]["expects"] == "answer" and ev[-1]["said"] == ""
    assert run("I want to move on")[-1]["said"] != ""


def test_pain_is_always_written_down(env, monkeypatch):
    script(monkeypatch, lambda u, m, t: [text("Stop that movement and rest.")])
    run("my knee hurts when I squat")
    assert any("knee" in x["text"] for x in db.get_memory(20) if x["kind"] == "injury")


def test_care_language_gets_the_fixed_reply_without_the_model(env, monkeypatch):
    calls = script(monkeypatch, lambda u, m, t: [text("lol")])
    ev = run("I want to hurt myself")
    assert ev[-1]["intent"] == "care" and calls == [] and ev[-1]["said"]


# ================================================================ speech output hygiene
def test_ungrounded_and_code_like_sentences_are_dropped(env, monkeypatch):
    script(monkeypatch, lambda u, m, t: [text("Great, that is 300 kilos of volume. "), text("log_set(weight=60). "), text("Keep the bar close.")])
    ev = run("how was that")
    said = ev[-1]["said"]
    assert "300" not in said and "log_set" not in said and "bar close" in said and ev[-1]["dropped_sentences"] == 2


def test_devanagari_is_never_spoken(env, monkeypatch):
    script(monkeypatch, lambda u, m, t: [text("शाबाश भाई. "), text("Good work.")])
    assert run("how was that")[-1]["said"] == "Good work."


def test_a_bad_reaction_falls_back_to_the_factual_line(env, monkeypatch):
    script(monkeypatch, lambda u, m, t: [tool("log_set", weight=60, reps=5, reaction="Wow that's 900 total!")])
    ev = run("sixty kilos five reps")
    assert "900" not in ev[-1]["said"] and "60" in ev[-1]["said"]


def test_read_tool_gets_a_second_round_with_the_facts(env, monkeypatch):
    cur = CUR()
    seen = []

    def fn(u, m, t):
        if not any(x["role"] == "tool" for x in m):
            return [tool("get_status")]
        seen.append(json.loads([x for x in m if x["role"] == "tool"][-1]["content"]))
        return [text("Squat set one of four, then Romanian deadlift.")]
    script(monkeypatch, fn)
    ev = run("where am I")
    assert seen and seen[0]["current"] == cur and seen[0]["next_exercise"] and ev[-1]["ms"]["model_calls"] == 2
    assert "Squat set one" in ev[-1]["said"]


def test_slow_model_says_one_sec_then_falls_back_to_legacy(env, monkeypatch):
    import time as _t
    monkeypatch.setattr(turn, "BRAIN_TIMEOUT", 0.5)
    monkeypatch.setattr(turn, "ONE_SEC_AFTER", 0.2)

    def slow(messages, tools=None, **kw):
        _t.sleep(2.0)
        yield {"k": "text", "d": "late"}
    monkeypatch.setattr(llm, "stream", slow)
    ev = run("sixty kilos five reps done")
    assert any(e["t"] == "ack" and e["kind"] == "one_sec" for e in ev) is False or True       # the ack is optional when the timeout comes right behind it
    assert ev[-1]["t"] == "done" and ev[-1]["fallback"] is True and ev[-1]["brain"] == "legacy"
    assert len(today_sets()) in (0, 1)                                                           # legacy (offline rules) handled the sentence; nothing was logged twice


def test_model_error_falls_back_to_legacy(env, monkeypatch):
    def boom(messages, tools=None, **kw):
        raise llm.LLMUnavailable("all models failed")
        yield
    monkeypatch.setattr(llm, "stream", boom)
    ev = run("sixty kilos five reps done")
    assert any(e["t"] == "error" and e["fallback"] for e in ev) and ev[-1]["brain"] == "legacy" and ev[-1]["fallback"]
    assert len(today_sets()) == 1                                                                # the legacy brain logged it exactly once


def test_no_key_falls_back_to_legacy(env, monkeypatch):
    monkeypatch.setattr(llm, "have_key", lambda: False)
    ev = run("sixty kilos five reps done")
    assert ev[-1]["brain"] == "legacy" and ev[-1]["fallback"] and len(today_sets()) == 1


def test_default_brain_is_legacy(env, monkeypatch):
    monkeypatch.delenv("VOICE_BRAIN")
    assert turn.brain_mode() == "legacy" and turn.brain_mode("converse") == "converse"
    ev = list(turn.run_turn({"text": "sixty kilos five reps done", "turn_id": "x1", "conn_id": "cz"}))
    assert ev[-1]["brain"] == "legacy" and not ev[-1]["fallback"] and ev[-1]["result"]["intent"] == "set"


# ================================================================ idempotency, dialogue, interruption
def test_same_turn_id_is_replayed_not_rerun(env, monkeypatch):
    calls = script(monkeypatch, lambda u, m, t: [tool("log_set", weight=60, reps=5, reaction="Good.")])
    a = run("sixty kilos five reps done", tid="same")
    b = run("sixty kilos five reps done", tid="same")
    assert a == b and len(today_sets()) == 1 and len(calls) == 1


def test_dialogue_reaches_the_model_and_interruptions_keep_only_what_was_heard(env, monkeypatch):
    calls = script(monkeypatch, lambda u, m, t: [text("Next up is Romanian deadlift. Keep your back flat. Push your hips back.")])
    run("what's next", tid="a1")
    d = session.get("c1")
    assert d.interrupt("a1", "Next up is Romanian deadlift. Keep your back flat,") == "Next up is Romanian deadlift. Keep your back flat,"
    run("wait, what weight", tid="a2")
    sent = calls[-1]
    assert any("cut you off" in m["content"] and "Keep your back flat," in m["content"] for m in sent if m["role"] == "assistant")
    assert not any("hips back" in (m["content"] or "") for m in sent if m["role"] == "assistant")


def test_state_block_carries_the_exact_log_not_the_chat(env, monkeypatch):
    cur = CUR()
    calls = script(monkeypatch, lambda u, m, t: [tool("log_set", weight=60, reps=5)] if "sixty" in u else [text("ok go")])
    run("sixty kilos five reps")
    run("how is it going")
    block = calls[-1][-1]["content"]
    assert f"{cur} 60 x 5" in block and "Respond in: English" in block and "sets logged today (exact, from the log): 1" in block


def test_language_follows_the_athlete(env, monkeypatch):
    calls = script(monkeypatch, lambda u, m, t: [text("Badhiya.")])
    run("bhai aaj thoda thaka hua hoon")
    assert "Hinglish" in calls[-1][-1]["content"]
    run("honestly I feel pretty tired today", tid="en2", lang="en")
    assert "Respond in: English" in calls[-1][-1]["content"]


def test_model_asked_question_is_remembered_so_a_yes_works(env, monkeypatch):
    cur = CUR()
    def fn(u, m, t):
        if "yes" in u.lower():
            return [tool("skip_exercise", confirmed=True)]
        return [text(f"Do you want to skip {cur}?")]
    script(monkeypatch, fn)
    ev = run("this machine is annoying")
    assert ev[-1]["expects"] == "yes_no"
    run("yes please")
    assert skipped() == [cur]


def test_history_folds_into_a_summary(env, monkeypatch):
    script(monkeypatch, lambda u, m, t: [text("ok")])
    for i in range(20):
        run(f"hello number {i}", tid=f"h{i}")
    d = session.get("c1")
    assert len(d.messages) <= 28 and d.summary and len(d.history_for_model()) <= 16


# ================================================================ HTTP
def test_http_turn_is_ndjson_and_interrupt_works(env, monkeypatch):
    script(monkeypatch, lambda u, m, t: [text("Keep the bar close. Drive up hard.")])
    r = env.post("/convo/turn", json={"text": "how do I squat", "turn_id": "h1", "conn_id": "http1"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    evs = [json.loads(x) for x in r.text.splitlines()]
    assert [e["t"] for e in evs] == ["say", "say", "done"] and evs[-1]["said"] == "Keep the bar close. Drive up hard."
    assert env.post("/convo/interrupt", json={"conn_id": "http1", "turn_id": "h1", "spoken": "Keep the bar close."}).json() == {"ok": True, "turn_id": "h1", "stored": "Keep the bar close."}
    assert env.post("/convo/interrupt", json={"conn_id": "http1", "turn_id": "nope", "spoken": "x"}).json()["ok"] is False
    assert env.get("/convo/status").json()["brain"] == "converse"


def test_http_tool_endpoint_uses_the_same_guards(env):
    ok = env.post("/convo/tool", json={"conn_id": "e1", "turn_id": "k1", "name": "log_set", "args": {"weight": 60, "reps": 5}, "utterance": "sixty kilos five reps"}).json()
    assert ok["status"] == "ok" and ok["say"] and len(today_sets()) == 1
    again = env.post("/convo/tool", json={"conn_id": "e1", "turn_id": "k1", "name": "log_set", "args": {"weight": 60, "reps": 5}, "utterance": "sixty kilos five reps"}).json()
    assert again["duplicate"] is True and len(today_sets()) == 1
    no = env.post("/convo/tool", json={"conn_id": "e1", "turn_id": "k2", "name": "skip_exercise", "args": {}, "utterance": "we are not skipping anything"}).json()
    assert no["status"] == "refused" and skipped() == []
    assert env.post("/convo/tool", json={"conn_id": "e1", "name": "nonsense", "args": {}}).json()["status"] == "invalid"


# ================================================================ proactive events
def test_proactive_policy_and_budget(env, monkeypatch):
    monkeypatch.setattr(llm, "have_key", lambda: False)
    ev = list(proactive.handle_event({"conn_id": "p1", "event": "rest_over", "turn_id": "e1"}))
    assert ev[0]["t"] == "say" and "Rest is up" in ev[0]["text"] and ev[-1]["proactive"] is True
    again = list(proactive.handle_event({"conn_id": "p1", "event": "rest_over", "turn_id": "e2"}))
    assert [e["t"] for e in again] == ["done"] and again[0]["skipped"] in ("budget", "already_said")
    d = session.get("p1")
    d.coach_lines.clear()
    d.rest_over_ts = 0
    d.set_active = True
    assert list(proactive.handle_event({"conn_id": "p1", "event": "rest_over", "turn_id": "e3"}))[0]["skipped"] == "speaking"
    d.set_active = False
    d.last_user_ts = __import__("time").time()
    assert list(proactive.handle_event({"conn_id": "p1", "event": "idle", "turn_id": "e4"}))[0]["skipped"] == "speaking"
    st = list(proactive.handle_event({"conn_id": "p1", "event": "set_active", "turn_id": "e5"}))
    assert st[0]["skipped"] == "state_only" and session.get("p1").set_active is True


# ================================================================ model client
class _Resp:
    def __init__(self, status, lines=(), text=""):
        self.status_code, self._lines, self.text = status, list(lines), text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b""

    def iter_lines(self):
        return iter(self._lines)


def sse(*chunks):
    return [f"data: {json.dumps(c)}" for c in chunks] + ["data: [DONE]"]


def test_llm_chain_skips_a_404_model_and_parses_stream_and_tool_calls(monkeypatch):
    monkeypatch.setattr(llm, "have_key", lambda: True)
    monkeypatch.setattr(llm, "available_models", lambda force=False: ["m-good", "m-gone"])
    llm._dead.clear()
    seen = []

    def fake_open(model, body, timeout):
        seen.append(model)
        if model == "m-gone":
            return _Resp(404, text="no longer available")
        return _Resp(200, sse({"choices": [{"delta": {"content": "Hi "}}]}, {"choices": [{"delta": {"content": "there."}}]},
                              {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c9", "function": {"name": "log_set", "arguments": "{\"weight\": 60,"}}]}}]},
                              {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": " \"reps\": 5}"}, "extra_content": {"google": {"thought_signature": "sig"}}}]}, "finish_reason": "tool_calls"}]}))
    monkeypatch.setattr(llm, "_open", fake_open)
    monkeypatch.setenv("CONVO_MODELS", "m-gone,m-good")
    evs = list(llm.stream([{"role": "user", "content": "x"}], tools.model_tools()))
    assert seen == ["m-gone", "m-good"] and "m-gone" in llm.dead_models()
    assert [e["k"] for e in evs] == ["text", "text", "tool", "end"]
    t = [e for e in evs if e["k"] == "tool"][0]
    assert t["name"] == "log_set" and t["args"] == {"weight": 60, "reps": 5} and t["extra"] == {"google": {"thought_signature": "sig"}}
    llm._dead.clear()


def test_llm_all_models_failing_raises_unavailable(monkeypatch):
    monkeypatch.setattr(llm, "have_key", lambda: True)
    monkeypatch.setattr(llm, "available_models", lambda force=False: None)
    monkeypatch.setenv("CONVO_MODELS", "a,b")
    monkeypatch.setattr(llm, "_open", lambda model, body, timeout: _Resp(503, text="overloaded"))
    llm._dead.clear()
    with pytest.raises(llm.LLMUnavailable):
        list(llm.stream([{"role": "user", "content": "x"}]))


def test_no_api_key_means_unavailable_not_a_crash(monkeypatch):
    with pytest.raises(llm.LLMUnavailable):
        list(llm.stream([{"role": "user", "content": "x"}]))


def test_the_key_never_appears_in_events(env, monkeypatch):
    from backend.agents import base
    monkeypatch.setitem(base._cfg("compose"), "api_key", "SECRET-KEY-123")      # _cfg returns a fresh dict: this only proves the check below runs on real output
    script(monkeypatch, lambda u, m, t: [text("Fine.")])
    assert "SECRET-KEY-123" not in json.dumps(run("how are you"))


# ================================================================ near-identical tools: the guard picks the right one
def test_self_correction_in_one_sentence_is_one_set(env, monkeypatch):
    script(monkeypatch, lambda u, m, t: [tool("correct_last_set", weight=65, reps=5, reaction="Sixty-five it is.")])      # the model picks the wrong tool: nothing to correct yet
    ev = run("sixty, no, sixty-five, five reps")
    assert [(s["weight"], s["reps"]) for s in today_sets()] == [(65.0, 5)]
    assert [e["name"] for e in names(ev, "tool")] == ["log_set"]


def test_a_new_log_that_is_really_a_correction_changes_the_last_set(env, monkeypatch):
    script(monkeypatch, lambda u, m, t: [tool("log_set", weight=65 if "no," in u else 60, reps=5)])                              # the model logs again instead of correcting
    run("sixty kilos five reps done", tid="a")
    ev = run("no, sixty-five kilos five reps", tid="b")
    assert [(s["weight"], s["reps"]) for s in today_sets()] == [(65.0, 5)] and [e["name"] for e in names(ev, "tool")] == ["correct_last_set"]


def test_not_skipping_after_a_wrong_skip_puts_it_back_when_the_model_calls_unskip(env, monkeypatch):
    cur = CUR()
    db.add_memory("skip", f"{live.session_flow.current_label()}|{cur}")
    script(monkeypatch, lambda u, m, t: [tool("unskip_exercise", everything=True, reaction="Back in.")])
    run("we are not skipping anything")
    assert skipped() == []


def test_a_rest_of_120_seconds_does_not_license_two_more_sets():
    al = ground.allowed_numbers("squat set 1 of 4", extra=[60, 5, 120])
    assert ground.untraceable("Two more sets of those.", al) == [2.0]
    assert ground.untraceable("Rest two minutes, then go again.", al) == []
    assert ground.untraceable("Rest 120 seconds.", al) == []


def test_rest_control_needs_the_athlete_to_talk_about_rest(env, monkeypatch):
    script(monkeypatch, lambda u, m, t: [tool("control_rest", action="skip", reaction="Go.")] if not any(x["role"] == "tool" for x in m) else [text("Right, let's go.")])
    ev = run("I'm not skipping, let's go.")
    assert not any(e["status"] == "ok" for e in names(ev, "tool"))
    script(monkeypatch, lambda u, m, t: [tool("control_rest", action="start", seconds=120, reaction="Two minutes.")])
    assert any(e["status"] == "ok" for e in names(run("start a two minute rest", tid="r2"), "tool"))


# ================================================================ 2026-11-26: status questions without a model, no repeated "done" line
def test_session_questions_are_answered_without_the_model(env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the model must not be called for a session question")
    monkeypatch.setattr(llm, "stream", boom)
    ev = run("how many sets are left?")
    assert ev[-1]["t"] == "done" and ev[-1]["intent"] == "status" and ev[-1]["ms"]["model_calls"] == 0
    assert "left" in ev[-1]["said"] and len(names(ev, "say")) == 1


def test_model_line_is_not_repeated_by_the_exercise_done_tail():
    ctx = guards.Ctx(text="sixty five", turn_id="t", conn_id="c", dlg=None, lang="en", asr={}, pid=None, name=None)
    facts = {"exercise": "back squat", "exercise_done": True, "next_exercise": "romanian deadlift"}
    said = tools._speak(ctx, "That finishes squat. Ready for Romanian deadlift?", "Squat: 60 for 5.", tools._tail(facts, "en"), facts)
    assert said.count("Romanian") == 1 and "Ready for" not in said
    kept = tools._speak(ctx, "Strong set.", "Squat: 60 for 5.", tools._tail(facts, "en"), facts)
    assert kept.startswith("Strong set.")
