"""Spoken commands for the live coach: "let's start today's workout" opens the session, "log this set" asks for the numbers.
Everything here is offline (conftest strips every LLM key), so these pin the deterministic paths only."""
import pytest
from fastapi.testclient import TestClient

from backend.coach import hindi, live, set_slots
from backend.main import app
from backend.memory import db
from backend.tools.demo import PROFILE


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "voice.db"))
    live._pending_clear()
    c = TestClient(app)
    c.post("/onboard", json={**PROFILE, "name": "Alex", "coach_persona": "hype"})
    return c


def say(c, text, ambient=False):
    r = c.post("/live/say", json={"transcript": text, "ambient": ambient})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------- start the session by voice
@pytest.mark.parametrize("phrase", [
    "let's start today's workout",
    "Let's start today's workout.",
    "start the workout",
    "Start my session",
    "can we begin training",
    "okay let's start",
    "let's begin",
    "I want to start the workout",
    "let's get started with today's session",
    "kick off the workout",
    "fire up the session",
    "please start the gym session",
])
def test_start_phrases_open_the_session(client, phrase):
    r = say(client, phrase)
    assert r["intent"] == "start", phrase
    assert r["reply"]
    assert r["saved"] == []


def test_start_works_in_hands_free_mode(client):
    assert say(client, "let's start today's workout", ambient=True)["intent"] == "start"


@pytest.mark.parametrize("phrase", [
    "start",                                   # a lone word in a noisy gym is not a command
    "when does the workout start",             # a question
    "the workout starts at six",               # a statement
    "I started the gym late today",            # past tense
    "don't start the workout yet",
])
def test_start_does_not_fire_on_other_talk(client, phrase):
    assert say(client, phrase)["intent"] != "start", phrase


def test_a_set_report_that_mentions_starting_is_still_a_set(client):
    r = say(client, "I started the workout late, bench 80 for 8")
    assert r["intent"] == "set" and len(r["saved"]) == 1


def test_start_with_a_time_limit_still_fits_the_session(client):
    assert say(client, "start the workout, I only have 30 minutes")["intent"] == "fit"


def test_start_after_training_resumes_instead_of_restarting(client):
    say(client, "bench 80 for 8")
    r = say(client, "let's start today's workout")
    assert r["intent"] == "start" and r["reply"]
    assert len(client.get("/live/state").json()["exercises"][0:1]) == 1
    done = {e["name"]: e["done"] for e in client.get("/live/state").json()["exercises"]}
    assert done["bench press"] == 1                    # nothing was logged or lost by saying it again


# ---------------- "log this set" without numbers asks for them
@pytest.mark.parametrize("phrase", [
    "log this set",
    "Log this set.",
    "log my set",
    "record that set",
    "mark this set as done",
    "mark set done",
    "log it",
    "log that",
    "okay log this set",
])
def test_log_this_set_asks_for_the_numbers(client, phrase):
    r = say(client, phrase)
    assert r["intent"] == "ask", phrase
    assert r["saved"] == []


def test_log_this_set_then_the_numbers_logs_it(client):
    assert say(client, "log this set")["intent"] == "ask"
    r = say(client, "80 for 8")
    assert r["intent"] == "set" and len(r["saved"]) == 1
    assert r["saved"][0]["exercise"] == "bench press"           # the current exercise of today's plan
    assert r["saved"][0]["weight"] == 80 and r["saved"][0]["reps"] == 8


def test_log_this_set_can_be_cancelled(client):
    say(client, "log this set")
    assert say(client, "never mind")["intent"] == "ask_cancel"
    assert client.get("/live/state").json()["exercises"][0]["done"] == 0


def test_log_something_else_is_not_a_set(client):
    for phrase in ("log my meal", "log my weight", "log the whole workout"):
        assert say(client, phrase)["intent"] != "ask", phrase


def test_set_slot_phrase_matcher():
    plan = ["bench press"]
    for ok in ("log this set", "log it", "mark the set as completed"):
        assert set_slots.is_set_done(ok, plan), ok
    for no in ("log my meal", "what should I log", "log this set?"):
        assert not set_slots.is_set_done(no, plan), no


# ---------------- Hindi / Haryanvi
def test_hindi_start_and_set_commands_are_normalised():
    assert hindi.normalize("वर्कआउट शुरू करो") == "start the workout"
    assert hindi.normalize("चलो शुरू करते हैं") == "let's start"
    assert hindi.normalize("सेट हो गया") == "set done"
    assert hindi.normalize("सेट पूरा हो गया") == "set done"
    assert hindi.normalize("यह सेट लॉग करो") == "log this set"


def test_hindi_start_and_set_commands_work_end_to_end(client):
    assert say(client, "वर्कआउट शुरू करो")["intent"] == "start"
    assert say(client, "सेट हो गया")["intent"] == "ask"


def test_existing_voice_commands_still_work(client):
    assert say(client, "I'm ready")["intent"] == "ready"
    assert say(client, "let's go")["intent"] == "ready"
    assert say(client, "what's next")["intent"] == "status"
    assert say(client, "skip this exercise")["intent"] == "skip"
    assert say(client, "my knee hurts")["intent"] == "pain"
    assert say(client, "wrap up")["intent"] == "end"
