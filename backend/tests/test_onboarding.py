import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.memory import db
from backend.tools.demo import PROFILE
from backend.tools.planner import derive_avoid


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return TestClient(app)


def all_exercises(plan):
    return {e["name"] for d in plan["days"] for e in d["exercises"]}


def test_no_plan_returns_404_so_app_can_show_wizard(client):
    assert client.get("/plan").status_code == 404


def test_shoulder_injury_removes_risky_moves_and_reports_it(client):
    plan = client.post("/onboard", json={**PROFILE, "injuries": ["shoulder"]}).json()
    ex = all_exercises(plan)
    assert "overhead press" not in ex and "dips" not in ex
    assert "overhead press" in plan["excluded"]
    assert all(d["exercises"] for d in plan["days"])  # no empty training days


def test_free_text_injury_matches(client):
    assert "deadlift" in derive_avoid(["tweaked my lower back last month"], [])


def test_no_injury_excludes_nothing(client):
    assert client.post("/onboard", json=PROFILE).json()["excluded"] == []


def test_experience_sets_starting_volume(client):
    beg = client.post("/onboard", json={**PROFILE, "experience": "beginner"}).json()
    adv = client.post("/onboard", json={**PROFILE, "experience": "advanced"}).json()
    sets = lambda p: sum(e["sets"] for d in p["days"] for e in d["exercises"])
    assert beg["volume_mult"] == 0.8 and adv["volume_mult"] == 1.15
    assert sets(beg) < sets(adv)


def test_bad_experience_rejected(client):
    assert client.post("/onboard", json={**PROFILE, "experience": "god"}).status_code == 422


def test_reset_all_returns_to_onboarding(client):
    client.post("/onboard", json=PROFILE)
    assert client.get("/plan").status_code == 200
    client.post("/reset-all")
    assert client.get("/plan").status_code == 404


def test_rebuilding_replaces_plan_and_does_not_duplicate_injury_memory(client):
    first = client.post("/onboard", json={**PROFILE, "injuries": ["shoulder"], "days_per_week": 4}).json()
    second = client.post("/onboard", json={**PROFILE, "injuries": ["shoulder"], "days_per_week": 3}).json()
    assert len(first["days"]) == 4 and len(second["days"]) == 3
    assert len(client.get("/plan").json()["days"]) == 3  # the rebuilt plan is the only one
    assert [m["text"] for m in db.get_memory(100) if m["kind"] == "injury"] == ["shoulder"]
