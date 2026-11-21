"""Undo a set, today's sets, the workout summary and the weigh-in (backend/routes_session.py)."""
import pytest
from fastapi.testclient import TestClient

from backend.agents import orchestrator as orch
from backend.main import app
from backend.memory import db
from backend.tools import demo
from backend.tools import exercises as ex

client = TestClient(app)


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    orch.onboard({**demo.PROFILE, "name": "Alex"})


def _log(exercise="squat", weight=100, reps=5, rpe=8, session=None):
    r = client.post("/log/set", json={"session": session or ex.today(), "exercise": exercise, "weight": weight, "reps": reps, "rpe": rpe})
    assert r.status_code == 200


def test_session_sets_lists_only_that_day_and_exercise():
    _log("squat", 100, 5); _log("squat", 105, 5); _log("bench press", 60, 8); _log("squat", 90, 5, session="2020-01-01")
    allsets = client.get("/session/sets").json()
    assert [s["exercise"] for s in allsets] == ["squat", "squat", "bench press"]    # oldest first, other day excluded
    squats = client.get("/session/sets", params={"exercise": "Squat"}).json()
    assert [s["weight"] for s in squats] == [100, 105] and all("id" in s for s in squats)


def test_delete_set_removes_it_and_returns_values():
    _log("squat", 100, 5); _log("squat", 105, 5)
    last = client.get("/session/sets", params={"exercise": "squat"}).json()[-1]
    r = client.delete(f"/session/set/{last['id']}")
    assert r.status_code == 200 and r.json()["deleted"]["weight"] == 105
    assert [s["weight"] for s in client.get("/session/sets").json()] == [100]
    assert client.delete(f"/session/set/{last['id']}").status_code == 404           # second tap is harmless


def test_summary_counts_volume_and_next_day():
    _log("squat", 100, 5); _log("squat", 100, 5); _log("bench press", 60, 10)
    s = client.get("/session/summary").json()
    assert s["sets"] == 3 and s["volume_kg"] == 100 * 5 * 2 + 600
    assert {e["name"] for e in s["exercises"]} == {"squat", "bench press"}
    assert s["next"] and s["next"]["name"] != s["day_name"]
    assert s["planned_sets"] > 0 and s["done_planned_sets"] <= s["planned_sets"]


def test_summary_flags_a_pr_only_with_enough_history():
    for i in range(4):
        _log("squat", 100, 5, session=f"2026-01-0{i + 1}")
    _log("squat", 120, 5)
    assert client.get("/session/summary").json()["prs"] == ["squat"]
    db.reset_history()
    _log("squat", 120, 5)                                                              # no history: nothing to beat
    assert client.get("/session/summary").json()["prs"] == []


def test_summary_of_an_empty_day_is_calm():
    s = client.get("/session/summary").json()
    assert s["sets"] == 0 and s["volume_kg"] == 0 and s["duration_min"] is None and s["prs"] == []


def test_weighin_updates_todays_checkin_in_place_and_keeps_readiness_inputs():
    client.post("/checkin", json={"sleep_h": 8, "soreness": 1, "energy": 5})
    before = client.get("/readiness").json()["readiness"]["score"]
    assert client.post("/session/weighin", json={"bodyweight": 81.4}).status_code == 200
    assert client.get("/readiness").json()["readiness"]["score"] == before
    rows = db.get_checkins(10)
    assert len(rows) == 1 and rows[0]["bodyweight"] == 81.4 and rows[0]["sleep_h"] == 8


def test_weighin_on_a_new_day_carries_over_the_last_checkin():
    with db.conn() as c:
        c.execute("INSERT INTO checkins (ts, sleep_h, soreness, energy, bodyweight) VALUES ('2020-01-01T08:00:00+00:00', 6, 4, 2, 80)")
    client.post("/session/weighin", json={"bodyweight": 79.5})
    newest = db.get_checkins(1)[0]
    assert newest["bodyweight"] == 79.5 and newest["sleep_h"] == 6 and newest["soreness"] == 4


def test_weighin_rejects_nonsense():
    assert client.post("/session/weighin", json={"bodyweight": 5}).status_code == 422


def test_checkin_summary_has_trend_and_todays_values():
    client.post("/checkin", json={"sleep_h": 7, "soreness": 2, "energy": 4, "bodyweight": 82})
    d = client.get("/session/checkins").json()
    assert d["last_weight"]["kg"] == 82 and d["today"] == {"sleep_h": 7, "soreness": 2, "energy": 4}


def test_onboard_accepts_equipment():
    r = client.post("/onboard", json={**demo.PROFILE, "equipment": ["dumbbell", "bodyweight"]})
    assert r.status_code == 200
    assert db.get_profile()["equipment"] == ["dumbbell", "bodyweight"]
