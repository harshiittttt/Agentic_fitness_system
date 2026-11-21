"""Correcting a profile-backed belief changes the profile, targets and plan; forgetting does not; refused values change nothing."""
import pytest
from fastapi.testclient import TestClient

from backend.agents import orchestrator as orch
from backend.main import app
from backend.memory import db
from backend.tools import demo
from fitness_os.mem import ingest, store

client = TestClient(app)


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("FITNESS_NOW", "2026-11-20T10:00:00+00:00")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    orch.onboard({**demo.PROFILE, "name": "Alex", "goal": "maintain", "days_per_week": 4, "injuries": ["left knee"], "avoid": []})
    ingest.seed_profile()


def fid(pred):
    return next(f["id"] for f in store.facts(predicate=pred))


def test_goal_correction_updates_profile_and_nutrition_targets():
    kcal = db.get_plan()["nutrition"]["calories"]
    r = client.post(f"/memory/facts/{fid('goal')}/correct", json={"value": "I want to bulk"}).json()
    assert r["ok"] and r["profile"]["nutrition_updated"]
    assert db.get_profile()["goal"] == "bulk"
    assert db.get_plan()["nutrition"]["calories"] > kcal
    assert [f["object_text"] for f in store.facts(predicate="goal")] == ["bulk (gain size)"]
    assert ingest.seed_profile() == []                        # re-seeding does not resurrect or duplicate


def test_training_days_correction_rebuilds_the_plan():
    assert len(db.get_plan()["days"]) == 4
    r = client.post(f"/memory/facts/{fid('trains_days_per_week')}/correct", json={"value": "3 days"}).json()
    assert r["profile"]["plan_rebuilt"] and db.get_profile()["days_per_week"] == 3
    assert len(db.get_plan()["days"]) == 3


def test_injury_correction_replaces_the_item_in_the_profile():
    client.post(f"/memory/facts/{fid('injury_site')}/correct", json={"value": "right shoulder"})
    assert db.get_profile()["injuries"] == ["right shoulder"]


def test_invalid_value_is_refused_and_nothing_changes():
    before, plan = dict(db.get_profile()), db.get_plan()
    r = client.post(f"/memory/facts/{fid('trains_days_per_week')}/correct", json={"value": "9 days"})
    assert r.status_code == 400 and "3 to 5" in r.json()["detail"]
    r = client.post(f"/memory/facts/{fid('goal')}/correct", json={"value": "banana"})
    assert r.status_code == 400
    assert db.get_profile() == before and db.get_plan() == plan
    assert [f["object_text"] for f in store.facts(predicate="trains_days_per_week")] == ["4"]


def test_forget_and_deny_never_wipe_the_profile():
    r = client.post(f"/memory/facts/{fid('injury_site')}/forget").json()
    assert r["ok"] and "unchanged" in r["note"]
    client.post(f"/memory/facts/{fid('goal')}/deny")
    p = db.get_profile()
    assert p["injuries"] == ["left knee"] and p["goal"] == "maintain"


def test_chat_learned_belief_not_in_profile_stays_memory_only():
    ingest.observe("I can only train 5 days a week")      # profile says 4; this belief is not profile-backed after the seeded one is replaced
    f = next(f for f in store.facts(predicate="trains_days_per_week") if f["object_text"] == "5")
    r = client.post(f"/memory/facts/{f['id']}/correct", json={"value": "2"})
    assert r.status_code == 200 and "profile" not in r.json()
    assert db.get_profile()["days_per_week"] in (4, 5)
