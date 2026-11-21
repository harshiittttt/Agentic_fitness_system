"""The app-facing HTTP layer: decision feed, approve/reject/revert, and the background agent run after a check-in."""
import pytest
from fastapi.testclient import TestClient

from backend.agents import orchestrator as orch
from backend.main import app
from backend.memory import db
from backend.tools import demo
from fitness_os import control, critic, ledger, runtime
from fitness_os.schema import Proposal

client = TestClient(app)


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    orch.onboard({**demo.PROFILE, "name": "Alex"})
    demo.seed(3)


def test_status_reports_offline_and_starter_plan():
    s = client.get("/agents/status").json()
    assert s["llm"] is False and s["has_plan"] and s["designed_by"] == "starter-template"


def test_decision_feed_why_and_revert_roundtrip():
    before = db.get_plan()["volume_mult"]
    res = control.submit(Proposal(agent="training", kind="volume_change", params={"fraction": -0.1}, rationale="tired", evidence="readiness 30"))
    assert res["status"] == "applied"
    feed = client.get("/agents/decisions").json()
    row = next(d for d in feed if d["id"] == res["id"])
    assert row["summary"].startswith("Volume") and row["can_revert"] and not row["pending"]
    assert "tired" in client.get(f"/agents/decisions/{res['id']}/why").json()["text"]
    assert client.post(f"/agents/decisions/{res['id']}/revert").json()["status"] == "reverted"
    assert db.get_plan()["volume_mult"] == before


def test_approve_and_reject_pending_items():
    big = control.submit(Proposal(agent="training", kind="volume_change", params={"fraction": -0.4}, rationale="deload", evidence="x"))
    assert big["status"] == "pending_approval"
    assert [d["id"] for d in client.get("/agents/status").json()["pending"]] == [big["id"]]
    assert client.post(f"/agents/decisions/{big['id']}/approve").json()["status"] == "applied"
    again = client.post(f"/agents/decisions/{big['id']}/approve")
    assert again.status_code == 400  # not pending any more


def test_bad_inputs_are_rejected_cleanly():
    assert client.post("/agents/event", json={"type": "nope"}).status_code == 400
    assert client.post("/agents/dial", json={"mode": "yolo"}).status_code == 400
    assert client.post("/agents/kill/maybe").status_code == 400
    assert client.post("/agents/decisions/9999/revert").status_code == 400


def test_kill_switch_blocks_agent_writes_via_api():
    client.post("/agents/kill/on")
    res = control.submit(Proposal(agent="training", kind="volume_change", params={"fraction": -0.1}, rationale="tired"))
    assert res["status"] == "blocked" and "kill_switch" in res["fired"]
    client.post("/agents/kill/off")
    assert ledger.kill_switch() is False


def test_checkin_endpoint_saves_then_agents_run_in_background(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    seen = {}

    def script(system, user, tool):
        if tool["name"] == "submit_reviews":
            return {"assessment": "ok", "reviews": [{"index": 0, "verdict": "approve", "reason": "fine"}]}
        seen[tool["input_schema"]["properties"]["proposals"]["items"]["properties"]["kind"]["enum"][0]] = True
        if "TRAINING" in system:
            return {"reasoning": "wrecked", "proposals": [{"kind": "volume_change", "fraction": -0.15, "rationale": "readiness is very low",
                                                          "evidence": "sleep 4.5h, energy 1", "confidence": 0.8}]}
        return {"reasoning": "nothing to do", "proposals": []}
    for mod in (runtime, critic):
        monkeypatch.setattr(mod, "extract_structured", lambda s, u, t, max_tokens=0: script(s, u, t))
    before_checkins = len(db.get_checkins(100))
    before_vol = db.get_plan()["volume_mult"]
    r = client.post("/checkin", json={"sleep_h": 4.5, "soreness": 5, "energy": 1})
    assert r.json() == {"ok": True}
    assert len(db.get_checkins(100)) == before_checkins + 1  # saved exactly once (agents must not double-record)
    assert db.get_plan()["volume_mult"] < before_vol          # TestClient runs the background task before returning
    assert any(d["agent"] == "training" and d["status"] == "applied" for d in client.get("/agents/decisions").json())


def test_checkin_without_key_still_just_saves():
    n = len(db.get_checkins(100))
    assert client.post("/checkin", json={"sleep_h": 7, "soreness": 2, "energy": 4}).json() == {"ok": True}
    assert len(db.get_checkins(100)) == n + 1


def test_feed_summary_shows_the_size_that_will_actually_apply():
    res = control.submit(Proposal(agent="training", kind="volume_change", params={"fraction": 0.25}, rationale="stronger", evidence="x"))
    row = next(d for d in client.get("/agents/decisions").json() if d["id"] == res["id"])
    assert row["summary"] != "Volume +25%"          # trimmed by the weekly cap, and the feed says so
    assert "max_weekly_load_increase" in row["fired"]
