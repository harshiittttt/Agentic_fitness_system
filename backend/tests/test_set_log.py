"""Manual set logging (steppers): structured input, validated, same response shape as voice logging."""
import pytest
from fastapi.testclient import TestClient

from backend.agents import orchestrator as orch
from backend.main import app
from backend.memory import db
from backend.tools import demo

client = TestClient(app)


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    orch.onboard({**demo.PROFILE, "name": "Alex"})


def test_log_set_saves_and_returns_next_set():
    r = client.post("/log/set", json={"session": "2026-11-19", "exercise": "Squat", "weight": 100, "reps": 5, "rpe": 7})
    assert r.status_code == 200
    body = r.json()
    assert body["saved"][0]["exercise"] == "squat" and body["saved"][0]["weight"] == 100
    assert body["next_set"]["weight"] > 100            # RPE 7 vs target 8: it was easy, so the next set goes up
    assert db.get_sets("squat", 5)[0]["weight"] == 100


def test_bodyweight_set_with_zero_added_weight_is_valid():
    r = client.post("/log/set", json={"session": "s", "exercise": "pull up", "weight": 0, "reps": 8})
    assert r.status_code == 200 and r.json()["saved"][0]["weight"] == 0


@pytest.mark.parametrize("bad", [{"reps": 0}, {"reps": 500}, {"weight": -5}, {"weight": 9999}, {"rpe": 11}, {"exercise": ""}])
def test_log_set_rejects_nonsense(bad):
    body = {"session": "s", "exercise": "squat", "weight": 60, "reps": 5, **bad}
    assert client.post("/log/set", json=body).status_code == 422


def test_prefill_is_null_without_history_and_never_invents_a_weight():
    j = client.get("/exercise/prefill", params={"name": "deadlift"}).json()
    assert j["last"] is None and j["suggest"] is None and j["increment"] == 2.5


def test_prefill_returns_last_set_and_autoregulated_suggestion():
    client.post("/log/set", json={"session": "s", "exercise": "bench press", "weight": 80, "reps": 8, "rpe": 9.5})
    j = client.get("/exercise/prefill", params={"name": "bench press", "target_reps": 8, "target_rpe": 8}).json()
    assert j["last"]["weight"] == 80 and j["last"]["reps"] == 8
    assert j["suggest"]["weight"] < 80                   # much harder than planned, so it backs off


def test_log_set_does_not_flag_pain_from_its_own_text():
    r = client.post("/log/set", json={"session": "s", "exercise": "squat", "weight": 100, "reps": 5})
    assert r.json()["safety_flag"] is None
