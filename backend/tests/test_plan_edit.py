import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.memory import db
from backend.tools import exercises as ex
from backend.tools.demo import PROFILE


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = TestClient(app)
    c.post("/onboard", json=PROFILE)
    return c


def names(day):
    return [e["name"] for e in day["exercises"]]


# ---------- alternatives ----------
def test_alternatives_same_pattern_not_self():
    alts = {a["name"] for a in ex.alternatives("bench press")}
    assert "incline press" in alts and "bench press" not in alts and "squat" not in alts


def test_alternatives_respect_injuries():
    alts = {a["name"] for a in ex.alternatives("bench press", injuries=["shoulder"])}
    assert "dips" not in alts and "incline press" not in alts and "push up" in alts


# ---------- time fit ----------
def test_fit_reduces_time_and_keeps_main_lift(client):
    day = client.get("/plan").json()["days"][0]
    fitted = ex.fit_to_time(day, 30)
    assert fitted["est_minutes"] <= 30 < ex.estimate_minutes(day)
    assert fitted["exercises"][0]["name"] == day["exercises"][0]["name"]


def test_fit_sacrifices_accessories_before_compounds():
    day = {"name": "t", "exercises": [
        {"name": "bench press", "sets": 4, "reps": 6, "target_rpe": 8},
        {"name": "barbell row", "sets": 4, "reps": 8, "target_rpe": 8},
        {"name": "curls", "sets": 3, "reps": 12, "target_rpe": 8},
        {"name": "triceps pushdown", "sets": 3, "reps": 12, "target_rpe": 8}]}
    f = ex.fit_to_time(day, 30)
    assert "bench press" in names(f) and "barbell row" in names(f)
    assert f["est_minutes"] <= 30
    assert next(e for e in f["exercises"] if e["name"] == "bench press")["sets"] == 4  # compounds untouched


def test_fit_never_empties_session():
    day = {"name": "t", "exercises": [{"name": "squat", "sets": 4, "reps": 6, "target_rpe": 8}]}
    assert len(ex.fit_to_time(day, 10)["exercises"]) == 1


def test_fit_endpoint_sets_today_override_not_plan(client):
    before = client.get("/plan").json()
    idx = before["today_index"]
    after = client.post("/session/fit", json={"minutes": 25}).json()
    assert after["today_override"]["day"]["est_minutes"] <= 25
    assert names(after["days"][idx]) == names(before["days"][idx])  # plan itself untouched
    restored = client.post("/session/restore").json()
    assert "today_override" not in restored


def test_refit_longer_restores_exercises(client):
    short = client.post("/session/fit", json={"minutes": 20}).json()["today_override"]["day"]
    longer = client.post("/session/fit", json={"minutes": 90}).json()["today_override"]["day"]
    assert len(longer["exercises"]) >= len(short["exercises"])
    assert longer["est_minutes"] > short["est_minutes"]


def test_stale_override_expires(client):
    client.post("/session/fit", json={"minutes": 25})
    plan = db.get_plan()
    plan["today_override"]["date"] = "2020-01-01"
    db.save_plan(plan)
    assert "today_override" not in client.get("/plan").json()


# ---------- swap ----------
def swap_args(client, scope):
    p = client.get("/plan").json()
    idx = p["today_index"]
    old = p["days"][idx]["exercises"][0]["name"]
    new = client.get("/exercises/alternatives", params={"name": old}).json()[0]["name"]
    return idx, old, new, {"day_index": idx, "exercise": old, "replacement": new, "scope": scope}


def test_swap_today_only_leaves_plan(client):
    idx, old, new, body = swap_args(client, "today")
    r = client.post("/plan/swap", json=body).json()
    assert new in names(r["today_override"]["day"]) and old in names(r["days"][idx])
    assert db.get_changes() and db.get_changes()[0]["summary"] != f"Swapped {old} -> {new}"  # not logged as a plan change


def test_swap_plan_is_permanent_logged_and_remembered(client):
    idx, old, new, body = swap_args(client, "plan")
    r = client.post("/plan/swap", json=body).json()
    assert new in names(r["days"][idx]) and old not in names(r["days"][idx])
    assert db.get_changes()[0]["summary"] == f"Swapped {old} -> {new}"
    assert any(new in m["text"] for m in db.get_memory(5))


def test_swap_rejects_unsafe_or_unrelated(client):
    idx, old, _, _ = swap_args(client, "plan")
    bad = client.post("/plan/swap", json={"day_index": idx, "exercise": old, "replacement": "curls", "scope": "plan"})
    assert bad.status_code == 400


def test_swap_survives_refit(client):
    idx, old, new, body = swap_args(client, "today")
    client.post("/plan/swap", json=body)
    r = client.post("/session/fit", json={"minutes": 60}).json()
    assert new in names(r["today_override"]["day"])
