"""Proactive lines (rest over, idle), offline: the policy (who may speak first, how often), the template fallback, grounding of a model line, and state-only events."""
import time

import pytest

from backend.convo import llm, proactive, session
from backend.coach import live
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
    monkeypatch.setattr(llm, "have_key", lambda: False)


def ev(conn, event, tid, **data):
    return list(proactive.handle_event({"conn_id": conn, "event": event, "turn_id": tid, "data": data}))


def quiet(conn):
    d = session.get(conn)
    d.last_user_ts = time.time() - 120
    d.set_active = False
    return d


def test_rest_over_speaks_once_with_the_real_next_exercise(env):
    quiet("a")
    out = ev("a", "rest_over", "r1")
    said = [e for e in out if e["t"] == "say"]
    assert len(said) == 1 and live.session_state()["current"]["name"].title() in said[0]["text"]
    assert out[-1]["t"] == "done" and out[-1]["proactive"] is True
    assert ev("a", "rest_over", "r2")[0]["skipped"] in ("budget", "already_said")      # never twice in one rest


def test_never_speaks_first_while_the_athlete_is_lifting_or_talking(env):
    d = quiet("b")
    d.set_active = True
    assert ev("b", "rest_over", "r1")[0]["skipped"] == "speaking"
    d.set_active = False
    d.last_user_ts = time.time() - 5                                                    # spoke 5 s ago
    assert ev("b", "idle", "i1")[0]["skipped"] == "speaking"
    d.last_user_ts = time.time() - 60
    assert [e["t"] for e in ev("b", "idle", "i2")] == ["say", "done"]


def test_idle_nudges_once_per_rest(env):
    d = quiet("c")
    assert [e["t"] for e in ev("c", "idle", "i1")] == ["say", "done"]
    d.coach_lines.clear()                                                               # budget is not what stops the second one
    assert ev("c", "idle", "i2")[0]["skipped"] == "already_said"
    ev("c", "rest_started", "s1", seconds=90)                                           # a new rest resets the nudge counter
    d.coach_lines.clear()
    assert [e["t"] for e in ev("c", "idle", "i3")] == ["say", "done"]


def test_budget_is_one_line_per_90_seconds_and_six_per_ten_minutes(env):
    d = quiet("d")
    now = time.time()
    d.coach_lines[:] = [now - 30]
    assert ev("d", "rest_over", "r1")[0]["skipped"] == "budget"
    d.coach_lines[:] = [now - 100 - 80 * k for k in range(6)]                           # 6 lines inside 10 minutes, none in the last 90 s
    assert proactive.allow(d, "rest_over", now) == (False, "budget")
    d.coach_lines[:] = [now - 100]
    assert proactive.allow(d, "rest_over", now)[0] is True


def test_state_only_events_say_nothing_and_change_state(env):
    d = quiet("e")
    out = ev("e", "set_active", "x1")
    assert [e["t"] for e in out] == ["done"] and out[0]["skipped"] == "state_only" and d.set_active is True
    ev("e", "set_inactive", "x2")
    assert d.set_active is False
    ev("e", "rest_started", "x3", seconds=60)
    assert 50 < d.rest_until - time.time() <= 60
    assert ev("e", "set_logged", "x4", pr=True)[0]["skipped"] == "state_only" and d.pr_flag is True
    assert ev("e", "no_such_event", "x5")[0]["skipped"] == "unknown_event"


def test_model_line_is_used_when_clean_and_grounded_else_the_template(env, monkeypatch):
    quiet("f")
    monkeypatch.setattr(llm, "have_key", lambda: True)

    def good(messages, tools=None, **kw):
        yield {"k": "text", "d": "Rest is done, back to it."}
        yield {"k": "end", "model": "fake", "finish": "stop", "usage": None, "ttft": 0.01}
    monkeypatch.setattr(llm, "stream", good)
    out = ev("f", "rest_over", "g1")
    assert [e for e in out if e["t"] == "say"][0]["text"] == "Rest is done, back to it."

    quiet("g")

    def invented(messages, tools=None, **kw):                                           # a number nobody gave it: dropped, the template speaks
        yield {"k": "text", "d": "Go for 137 kilos now."}
        yield {"k": "end", "model": "fake", "finish": "stop", "usage": None, "ttft": 0.01}
    monkeypatch.setattr(llm, "stream", invented)
    said = [e for e in ev("g", "rest_over", "g2") if e["t"] == "say"][0]["text"]
    assert "137" not in said and said.startswith("Rest is up")

    quiet("h")

    def slow(messages, tools=None, **kw):                                               # slower than the 1.5 s line timeout: the template speaks
        time.sleep(proactive.LINE_TIMEOUT + 0.6)
        yield {"k": "text", "d": "Late line."}
        yield {"k": "end", "model": "fake", "finish": "stop", "usage": None, "ttft": 0.01}
    monkeypatch.setattr(llm, "stream", slow)
    t0 = time.time()
    said = [e for e in ev("h", "rest_over", "g3") if e["t"] == "say"][0]["text"]
    assert said.startswith("Rest is up") and time.time() - t0 < proactive.LINE_TIMEOUT + 1.0


def test_hinglish_template_and_over_long_model_line_rejected(env, monkeypatch):
    d = quiet("i")
    d.lang = "hinglish"
    said = [e for e in ev("i", "rest_over", "h1") if e["t"] == "say"][0]["text"]
    assert said.startswith("Rest khatam")
    quiet("j")
    monkeypatch.setattr(llm, "have_key", lambda: True)

    def long(messages, tools=None, **kw):
        yield {"k": "text", "d": "Okay this is a very long line that keeps going well past the fifteen word limit for a coach that speaks first."}
        yield {"k": "end", "model": "fake", "finish": "stop", "usage": None, "ttft": 0.01}
    monkeypatch.setattr(llm, "stream", long)
    assert [e for e in ev("j", "rest_over", "h2") if e["t"] == "say"][0]["text"].startswith("Rest is up")


def test_session_start_clears_the_dialogue_and_session_end_summarises(env):
    d = quiet("k")
    d.add_user("sixty five", "u1")
    ev("k", "session_start", "ss1")
    assert session.get("k").messages == []
    ev("k", "session_end", "se1")
    assert session.get("k", create=False) is None
