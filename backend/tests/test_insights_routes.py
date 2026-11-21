import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.memory import db
from backend.tools import exercises as ex


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    return TestClient(app)


def test_summary_empty_for_new_user(client):
    r = client.get("/insights/summary").json()
    assert r["prs"] == [] and r["recent"] == [] and r["total_sessions"] == 0 and r["first_session"] is None


def test_summary_has_prs_recent_and_week(client):
    client.post("/demo/seed")
    r = client.get("/insights/summary").json()
    lifts = {p["lift"] for p in r["prs"]}
    assert {"squat", "bench press", "deadlift"} <= lifts
    bench = next(p for p in r["prs"] if p["lift"] == "bench press")
    assert bench["best_e1rm"] >= bench["latest_e1rm"] and bench["heaviest"]["weight"] > 0
    assert 1 <= len(r["recent"]) <= 8
    assert r["recent"][0]["date"] >= r["recent"][-1]["date"]  # newest first
    assert r["recent"][0]["exercises"][0]["top"]["weight"] > 0
    assert r["week"]["target"] == 4 and r["total_sessions"] >= 20


def test_today_no_plan(client):
    assert client.get("/insights/today").json() == {"has_plan": False}


def test_today_before_and_after_training(client):
    client.post("/demo/seed")
    before = client.get("/insights/today").json()
    assert before["has_plan"] and before["trained_today"] is False and before["next"]["lifts"] > 0
    plan = client.get("/plan").json()
    assert before["next"]["name"] == plan["days"][plan["today_index"]]["name"]
    client.post("/log/set", json={"session": ex.today(), "exercise": "squat", "weight": 100, "reps": 5})
    after = client.get("/insights/today").json()
    assert after["trained_today"] is True and after["sets_today"] == 1 and after["tonnage_today"] == 500
    plan2 = client.get("/plan").json()
    assert after["next"]["name"] == plan2["days"][(plan2["today_index"] + 1) % len(plan2["days"])]["name"]


def test_meal_restore_round_trip(client):
    day = ex.today()
    r = client.post("/meal/restore", json={"day": day, "name": "dal", "grams": 200, "kcal": 230, "protein": 12, "carbs": 30, "fat": 5})
    assert r.status_code == 200
    meals = client.get(f"/nutrition/day?day={day}").json()["meals"]
    assert [m["name"] for m in meals] == ["dal"] and meals[0]["kcal"] == 230
    client.delete(f"/meal/{r.json()['id']}")
    assert client.get(f"/nutrition/day?day={day}").json()["meals"] == []


def test_meal_restore_rejects_bad_input(client):
    assert client.post("/meal/restore", json={"day": "tomorrow", "name": "x", "kcal": 1}).status_code == 422
    assert client.post("/meal/restore", json={"day": "2026-11-19", "name": "x", "kcal": -5}).status_code == 422
    assert client.post("/meal/restore", json={"day": "2026-13-45", "name": "x", "kcal": 5}).status_code == 422
