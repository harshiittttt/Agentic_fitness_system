"""Export / delete everything, the access log, and the success metrics."""
import json

import pytest
from fastapi.testclient import TestClient

from backend.agents import orchestrator as orch
from backend.memory import db
from backend.tools import demo
from fitness_os import comms, control, disruption, ledger, metrics, privacy
from fitness_os.schema import Proposal


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("FITNESS_NOW", "2026-03-10T12:00:00+00:00")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    orch.onboard({**demo.PROFILE, "name": "Alex"})
    demo.seed(6)
    control.submit(Proposal(agent="recovery", kind="volume_change", params={"fraction": -0.1}, rationale="Trim for recovery.", confidence=0.7))
    comms.add("adherence", "Hello there")


def test_export_contains_every_table_and_is_json_serialisable():
    out = privacy.export_all()
    t = out["tables"]
    for name in ("profile", "plan", "sets", "checkins", "decision", "settings", "nudge"):
        assert name in t, name
    assert out["counts"]["sets"] > 0 and out["counts"]["decision"] >= 1 and out["counts"]["nudge"] == 1
    json.dumps(out, default=str)
    assert privacy.access_log()[0]["action"] == "export"


def test_delete_needs_the_exact_phrase_and_changes_nothing_without_it():
    before = privacy.export_all()["counts"]["sets"]
    with pytest.raises(ValueError):
        privacy.delete_all("yes please")
    assert privacy.export_all()["counts"]["sets"] == before


def test_delete_removes_everything_and_leaves_only_a_tombstone():
    res = privacy.delete_all(privacy.CONFIRM)
    assert res["deleted"] and res["rows_removed"] > 50
    after = privacy.export_all()["tables"]
    assert not after.get("sets") and not after.get("decision") and not after.get("nudge") and not after.get("checkins")
    assert db.get_profile() is None and db.get_plan() is None
    log = [r["action"] for r in after["access_log"]]
    assert "delete_all" in log                                           # the tombstone: that a deletion happened, and when


def test_endpoints():
    from backend.main import app
    with TestClient(app) as c:
        assert c.get("/agents/export").json()["counts"]["sets"] > 0
        assert c.post("/agents/data/delete", json={"confirm": "no"}).status_code == 400
        assert c.get("/agents/metrics").status_code == 200
        assert c.get("/agents/access-log").json()
        assert c.post("/agents/data/delete", json={"confirm": privacy.CONFIRM}).json()["deleted"] is True
        assert c.get("/agents/export").json()["counts"].get("sets", 0) == 0


# ---------------------------------------------------------------- metrics
def test_autonomy_counts_acceptance_and_override():
    a = metrics.autonomy()
    assert a["accepted"] >= 1 and a["overridden"] == 0 and a["acceptance_rate"] == 1.0
    did = ledger.recent(1)[0]["id"]
    control.revert(did)
    b = metrics.autonomy()
    assert b["overridden"] >= 1 and b["override_rate"] and b["override_rate"] > 0


def test_safety_false_negatives_are_zero_when_the_watchdog_froze_and_positive_when_it_did_not():
    db.add_checkin(7, 3, 3, None, "I had chest pain on the last set")
    assert metrics.safety()["false_negatives"] == 1                      # a red flag exists and nothing is frozen yet: surfaced, not hidden
    from fitness_os import events
    events.ingest("checkin.logged", {}, record=False)                    # the watchdog runs and freezes training
    s = metrics.safety()
    assert s["false_negatives"] == 0 and "training" in s["frozen_domains"] and s["escalations_30d"] >= 1


def test_time_to_recover_from_a_disruption_is_logged(monkeypatch):
    with db.conn() as c:
        c.execute("DELETE FROM sets")                                    # the demo seed is dated in real time; this test needs its own timeline
    for day in ("2026-03-01", "2026-03-03"):
        for i in range(3):
            with db.conn() as c:
                c.execute("INSERT INTO sets (ts, session, exercise, weight, reps, rpe, note) VALUES (?,?,?,?,?,?,?)",
                          (f"{day}T10:0{i}:00+00:00", day, "bench press", 60, 8, 7, None))
    disruption.tick()
    for d in ("2026-03-11", "2026-03-19", "2026-03-26"):
        monkeypatch.setenv("FITNESS_NOW", f"{d}T12:00:00+00:00")
        db.add_set(d, "bench press", 60, 8, 7)
        disruption.tick()
    m = metrics.disruptions()
    assert m["count"] == 1 and m["avg_days_to_recover"] and m["avg_days_to_recover"] > 14 and m["active"] is None


def test_cost_counter_and_full_report_shape():
    from backend.agents import base
    base._count("main")
    c = metrics.cost()
    assert c["model_calls_since_start"] >= 1 and c["by_role"]["main"] >= 1
    rep = metrics.report()
    assert set(rep) >= {"adherence", "goal_progress", "autonomy", "safety", "disruptions", "cost"}
