"""What a phone's speech recognizer really returns (taken from the user's own session log), cleaned before anything tries to understand it."""
import pytest
from fastapi.testclient import TestClient

from backend.coach import speech_fix as sf, voice_control as vc
from backend.main import app
from backend.memory import db
from backend.tools.demo import PROFILE


@pytest.mark.parametrize("raw,want", [
    ("I did like uhh one set of overhead press 15 wraps 20 kg", "I did like one set of overhead press 15 reps 20 kg"),
    ("10 raps", "10 reps"),
    ("um so I did uh bench", "so I did bench"),
    ("wrap it up", "wrap it up"),                                   # a real command is untouched
    ("fifteen wraps at forty", "fifteen reps at forty"),
    ("the second set I did like 15th them for 30 using", "the second set I did like 15th them for 30 using"),
])
def test_clean(raw, want):
    assert sf.clean(raw) == want


@pytest.mark.parametrize("text,frag", [
    ("I did like", True), ("I did like uhh", True), ("and then", True), ("so I", True), ("I just", True), ("my", True),
    ("can you just tell me", True), ("should I go till", True), ("okay can you tell me", True), ("what is the", True), ("how do I", True), ("give me", True),
    ("what is next", False), ("tell me how much protein", False), ("can you skip this", False), ("I am done", False),
    ("I did bench press", False), ("bench press 60 for 8", False), ("I did like one set of overhead press", False), ("what's next", False),
    ("sixty", False), ("done", False), ("skip this", False), ("I am done with bench", False), ("I did like 15", False),
])
def test_is_fragment(text, frag):
    assert sf.is_fragment(text) is frag


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    vc.reset()
    c = TestClient(app)
    c.post("/onboard", json={**PROFILE, "name": "Alex"})
    return c


def test_a_fragment_gets_no_answer_so_the_app_keeps_listening(client):
    r = client.post("/voice/command", json={"transcript": "I did like uhh", "screen": "live"}).json()
    assert r["intent"] == "fragment" and r["reply"] == ""


def test_mishearings_are_repaired_before_a_set_is_parsed(client):
    first = client.get("/live/state").json()["current"]["name"]
    r = client.post("/voice/command", json={"transcript": f"{first} 50 for 10 raps", "screen": "live"}).json()
    assert r["intent"] == "set", r
    assert any(s["reps"] == 10 and s["weight"] == 50 for s in db.get_sets(first, 5))


def test_the_router_only_gets_the_tools_a_sentence_can_need():
    tools, _ = vc.build_tools("x")
    names = lambda t: {x["name"] for x in vc.tools_for(t, tools)}
    assert len(tools) > 35
    food = names("I want to eat more protein")
    assert "log_meal" in food and "rebuild_plan" not in food and len(food) < 16
    plan = names("remove squat from my split")
    assert "remove_exercise" in plan and "set_calories" not in plan
    assert 5 < len(names("blah blah")) < 12                        # unknown: a small core, never the whole catalogue


@pytest.mark.parametrize("text,flag", [("the number", None), ("what is the number", None), ("Spain is nice", None), ("sharpen the pencil", None), ("this is so bad", None),
                                       ("my knee hurts", "hurt"), ("numbness in my hand", "numb"), ("it is numb", "numb"), ("I am dizzy", "dizzy"), ("I strained my back", "strain"),
                                       ("tingling in my fingers", "tingl"), ("my elbow popped", "popped"), ("sharp pain in my shoulder", "pain")])
def test_pain_words_match_whole_words_only(text, flag):
    from backend.policy.adaptation import has_safety_flag
    assert has_safety_flag(text) == flag


# ------------------------------------------------------------------ Hinglish (Hindi in Roman letters), taken from the user's own session
@pytest.mark.parametrize("raw,want", [
    ("aath bar uthaya aath bar", "8 reps uthaya"),
    ("aath bar", "8 reps"),
    ("okay Maine uthaya Hai 30 kilo aath", "okay Maine uthaya Hai 30 kilo 8"),
    ("bhajan tha 30 kilo aur Maine uthaya", "weight tha 30 kilo aur Maine uthaya"),
    ("theek hai yaar maine Ek set Maar Diya bench press ka", "theek hai yaar maine 1 set done bench press ka"),
    ("do set kiye 60 kilo paanch bar", "2 set kiye 60 kilo 5 reps"),
    ("do it now", "do it now"), ("what do you do", "what do you do"),          # English 'do' is never turned into 2
])
def test_hinglish_numbers_and_phrases(raw, want):
    assert sf.clean(raw) == want


# ------------------------------------------------------------------ found by the live-voice test: a QUESTION about what is next must never skip it
@pytest.mark.parametrize("raw", ["Agla exercise batao", "Aagla exercise bitao.", "agla exercise kaun sa hai", "agla wala kya hai"])
def test_asking_what_is_next_in_hinglish_is_a_question_not_a_skip(raw):
    assert sf.clean(raw) == "what is next"


def test_a_bare_agla_still_means_next():
    assert sf.clean("agla") == "next"


@pytest.mark.parametrize("raw,want", [("how should I do the squad properly", "how should I do the squat properly"), ("6 rapes weight 50 kilos", "6 reps weight 50 kilos"),
                                      ("three squads", "three squats")])
def test_systematic_mishearings_are_repaired(raw, want):
    assert sf.clean(raw) == want


def test_do_i_need_is_an_unfinished_sentence_even_with_the_question_mark_whisper_adds():
    assert sf.is_fragment("Do I need?") and sf.is_fragment("do I need")
    assert not sf.is_fragment("how is it going")
