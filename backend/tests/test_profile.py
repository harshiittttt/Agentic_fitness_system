import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.memory import db
from backend.tools.demo import PROFILE

FULL = {**PROFILE, "name": "Alex", "coach_persona": "mentor", "experience": "intermediate"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = TestClient(app)
    c.post("/onboard", json=FULL)
    return c


def days(client):
    return [[e["name"] for e in d["exercises"]] for d in client.get("/plan").json()["days"]]


def test_profile_requires_setup_first(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "empty.db"))
    c = TestClient(app)
    assert c.get("/profile").status_code == 404
    assert c.post("/profile", json={"age": 30}).status_code == 400


def test_get_profile_returns_the_real_stored_details(client):
    r = client.get("/profile").json()
    p = r["profile"]
    assert p["name"] == "Alex" and p["coach_persona"] == "mentor" and p["weight_kg"] == FULL["weight_kg"] and p["age"] == FULL["age"]
    assert r["plan"]["days"] == 4 and r["plan"]["nutrition"]["calories"] > 0


def test_changing_weight_updates_nutrition_only_and_keeps_everything_else(client):
    before_days, before = days(client), client.get("/profile").json()["plan"]["nutrition"]["calories"]
    db.add_set("2026-01-05", "squat", 100, 5, 8)
    r = client.post("/profile", json={"weight_kg": 95}).json()
    assert r["changed"] == ["weight_kg"] and r["nutrition_updated"] and not r["needs_rebuild"] and not r["rebuilt"]
    assert r["plan"]["nutrition"]["calories"] > before                        # heavier body, higher maintenance calories
    assert days(client) == before_days                                        # training plan untouched
    assert len(db.get_sets()) == 1                                            # history untouched
    assert db.get_checkins(1)[0]["bodyweight"] == 95                          # feeds the bodyweight chart
    assert db.get_changes(1)[0]["summary"] == "Updated nutrition targets" and db.get_changes(1)[0]["agent"] == "you"


def test_saving_identical_values_is_a_no_op(client):
    n_changes, n_checkins = len(db.get_changes()), len(db.get_checkins())
    r = client.post("/profile", json={"weight_kg": FULL["weight_kg"], "age": FULL["age"], "name": "Alex"}).json()
    assert r["changed"] == [] and not r["nutrition_updated"]
    assert len(db.get_changes()) == n_changes and len(db.get_checkins()) == n_checkins


def test_goal_change_recomputes_targets_and_resets_old_calorie_nudges(client):
    plan = db.get_plan(); plan["calorie_mult"] = 1.07; db.save_plan(plan)
    r = client.post("/profile", json={"goal": "cut"}).json()
    assert r["nutrition_updated"] and db.get_plan()["calorie_mult"] == 1.0 and db.get_plan()["goal"] == "cut"
    assert r["plan"]["nutrition"]["protein_g"] > 0


def test_name_and_coach_changes_touch_nothing_else(client):
    plan_before = db.get_plan()
    r = client.post("/profile", json={"name": "Sam", "coach_persona": "straight"}).json()
    assert r["changed"] == ["coach_persona", "name"] and not r["nutrition_updated"] and not r["needs_rebuild"]
    assert db.get_plan() == plan_before
    assert client.get("/coach").json()["persona"] == "straight" and client.get("/coach").json()["user_name"] == "Sam"


def test_training_changes_ask_before_rebuilding(client):
    before = days(client)
    r = client.post("/profile", json={"days_per_week": 5}).json()
    assert r["needs_rebuild"] and not r["rebuilt"] and days(client) == before


def test_rebuild_replaces_training_but_keeps_nutrition_history_and_memory(client):
    db.add_set("2026-01-05", "squat", 100, 5, 8)
    db.add_memory("preference", "Prefers incline press over bench")
    nutrition = db.get_plan()["nutrition"]
    r = client.post("/profile", json={"days_per_week": 5, "rebuild": True}).json()
    assert r["rebuilt"] and not r["needs_rebuild"] and len(days(client)) == 5
    assert db.get_plan()["nutrition"] == nutrition
    assert len(db.get_sets()) == 1 and any("incline" in m["text"] for m in db.get_memory(10))
    assert "today_override" not in db.get_plan()
    assert db.get_changes(1)[0]["summary"] == "Rebuilt training plan"


def test_new_injury_is_remembered_and_drops_risky_moves_on_rebuild(client):
    r = client.post("/profile", json={"injuries": ["shoulder"], "rebuild": True}).json()
    assert r["rebuilt"]
    assert not any("overhead press" in d for d in days(client))
    assert any(m["kind"] == "injury" and "shoulder" in m["text"] for m in db.get_memory(10))
    assert "overhead press" in db.get_plan()["excluded"]


def test_experience_change_resets_the_volume_baseline_on_rebuild(client):
    client.post("/profile", json={"experience": "beginner", "rebuild": True})
    assert db.get_plan()["volume_mult"] == 0.8 and db.get_plan()["base_volume"] == 0.8


@pytest.mark.parametrize("bad", [{"age": 5}, {"weight_kg": 10}, {"goal": "shred"}, {"days_per_week": 9}, {"sex": "x"},
                                  {"experience": "god"}, {"name": "x" * 31}, {"coach_persona": "robot"}])
def test_invalid_values_are_rejected(client, bad):
    assert client.post("/profile", json=bad).status_code == 422


def test_onboarding_still_builds_the_same_plan_after_the_refactor(client):
    plan = db.get_plan()
    assert len(plan["days"]) == 4 and plan["nutrition"]["calories"] > 0 and plan["base_volume"] == 1.0 and plan["base_calories"] == plan["nutrition"]["calories"]
