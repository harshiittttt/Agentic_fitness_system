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
