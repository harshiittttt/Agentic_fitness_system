"""Plain questions about the session ("what's the plan", "how many sets are left", "how much weight for the next set") are answered from the session state in
milliseconds, never by the model (that took 2-6 s and the athlete is standing there waiting), and without hijacking commands ("start today's workout")."""
import pytest
from fastapi.testclient import TestClient

from backend.coach import live, voice_control as vc
from backend.main import app
from backend.memory import db
from backend.tools.demo import PROFILE


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "sq.db"))
    live._pending_clear()
    vc.reset()
    monkeypatch.setattr(vc, "_fire", lambda *a, **k: None)
    monkeypatch.setattr(vc, "run_agent", lambda *a, **k: pytest.fail("this question must not reach the model"))
    c = TestClient(app)
    c.post("/onboard", json={**PROFILE, "name": "Alex", "coach_persona": "hype"})
    return c


def say(c, text, screen="live", lang=None):
    r = c.post("/voice/command", json={"transcript": text, "screen": screen, "lang": lang})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("q", ["what is the plan for today", "What's the plan for today?", "what are we hitting today", "tell me today's workout", "Aaj ka plan kya hai?"])
def test_the_plan_is_read_from_the_session(client, q):
    r = say(client, q)
    names = [e["name"] for e in live.session_state()["exercises"]]
    assert r["intent"] == "status" and all(n in r["reply"] for n in names[:2]), r


@pytest.mark.parametrize("q", ["how many sets are left", "How many sets are left?", "sets left", "Kitne set baki hain?"])
def test_sets_left_come_from_the_session(client, q):
    st = live.session_state()
    r = say(client, q)
    assert r["intent"] == "status" and str(st["remaining_sets"]) in r["reply"] and st["current"]["name"] in r["reply"], r


def test_the_weight_for_the_next_set_comes_from_the_session(client):
    r = say(client, "how much weight should I use for the next set")
    st = live.session_state()
    assert r["intent"] == "status" and st["current"]["name"] in r["reply"], r


def test_a_start_command_is_not_a_plan_question(client):
    r = say(client, "let's start today's workout")
    assert r["intent"] == "start", r


def test_a_question_about_another_day_still_goes_to_the_normal_path(client, monkeypatch):
    seen = []
    monkeypatch.setattr(vc, "run_agent", lambda system, user, *a, **k: seen.append(system) or "Tomorrow is Upper A.")
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    r = say(client, "what is my plan for tomorrow")
    assert r["intent"] == "router" and seen


@pytest.mark.parametrize("lang,needle", [("en", "plain English"), ("hinglish", "Hinglish")])
def test_the_model_is_told_which_language_the_athlete_spoke(client, monkeypatch, lang, needle):
    seen = []
    monkeypatch.setattr(vc, "run_agent", lambda system, user, *a, **k: seen.append(system) or "Okay.")
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    say(client, "tell me a joke about squats", lang=lang)
    assert seen and needle in seen[0]
