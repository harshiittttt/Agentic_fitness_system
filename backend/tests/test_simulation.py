import pytest
from fastapi.testclient import TestClient

from backend.memory import db
from backend.main import app

PROFILE = {"age": 28, "sex": "m", "height_cm": 178, "weight_kg": 80, "goal": "maintain", "days_per_week": 4}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = TestClient(app)
    assert c.post("/onboard", json=PROFILE).status_code == 200
    return c


def log_sessions(rows):
    for i, (w, reps, rpe) in enumerate(rows):
        db.add_set(f"s{i}", "bench press", w, reps, rpe)


def test_onboard_builds_plan(client):
    plan = client.get("/plan").json()
    assert len(plan["days"]) == 4 and plan["nutrition"]["protein_g"] > 0


def test_noisy_steady_progress_does_not_change_plan(client):
    log_sessions([(80, 8, 8), (80, 8, 8.5), (82.5, 8, 8), (82.5, 8, 8), (85, 8, 8), (85, 8, 8.5)])
    r = client.post("/review").json()
    assert r["changed"] is False


def test_rising_effort_without_gains_reduces_volume(client):
    log_sessions([(100, 5, 7), (100, 5, 7.5), (100, 5, 8), (100, 5, 8.5), (100, 5, 9), (100, 5, 9.5), (100, 5, 9.5), (100, 5, 10)])
    r = client.post("/review").json()
    assert r["changed"] and r["plan"]["volume_mult"] < 1.0


def test_pain_triggers_recovery_veto(client):
    client.post("/checkin", json={"sleep_h": 7, "soreness": 3, "energy": 3})
    client.post("/log", json={"session": "x", "transcript": "bench 80 for 8, sharp pain in my shoulder"})
    r = client.post("/review").json()
    assert "Recovery veto" in r["verdict"]


def test_voice_log_returns_next_set(client):
    r = client.post("/log", json={"session": "a", "transcript": "bench press 80 for 8 rpe 9.5"}).json()
    assert r["saved"] and r["next_set"]["weight"] < 80
