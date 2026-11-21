import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.memory import db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return TestClient(app)


def test_demo_seed_populates_charts(client):
    r = client.post("/demo/seed").json()
    assert r["sessions"] >= 20
    s = client.get("/progress/series").json()
    assert len(s["lifts"]["bench press"]) == r["sessions"]
    assert s["lifts"]["bench press"][-1] > s["lifts"]["bench press"][0]  # progressing
    assert len(s["bodyweight"]) > 10


def test_demo_plateau_is_detected_and_review_runs(client):
    client.post("/demo/seed")
    p = client.get("/progress").json()
    assert "overhead press" in p["plateaus"]
    assert client.post("/review").status_code == 200


def test_today_index_rotates_with_sessions(client):
    from backend.tools.demo import PROFILE
    client.post("/onboard", json=PROFILE)
    assert client.get("/plan").json()["today_index"] == 0
    db.add_set("2020-01-01", "squat", 100, 5, 8)
    assert client.get("/plan").json()["today_index"] == 1


def test_reset_clears_history(client):
    client.post("/demo/seed")
    client.post("/demo/reset")
    assert client.get("/progress/series").json()["sessions"] == 0


def test_series_carries_dates_and_activity_for_the_charts(client):
    import re
    r = client.post("/demo/seed").json()
    s = client.get("/progress/series").json()
    iso = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for lift, vals in s["lifts"].items():
        dates = s["lift_dates"][lift]
        assert len(dates) == len(vals) and dates == sorted(dates) and all(iso.match(d) for d in dates)
    assert len(s["bodyweight_dates"]) == len(s["bodyweight"]) and len(s["readiness_dates"]) == len(s["readiness"])
    assert len(s["activity"]) == r["sessions"]
    first = s["activity"][0]
    assert first["sets"] == 12 and first["tonnage"] > 0 and iso.match(first["date"])
    assert [a["date"] for a in s["activity"]] == sorted(a["date"] for a in s["activity"])


def test_series_is_empty_safe(client):
    s = client.get("/progress/series").json()
    assert s["lifts"] == {} and s["activity"] == [] and s["lift_dates"] == {}
