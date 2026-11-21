"""push.notify_pending_decisions wired into scheduler.run_due: offline, Expo network mocked, nothing is sent for real."""
import pytest

from backend.agents import orchestrator as orch
from backend.memory import db
from backend.tools import demo
from fitness_os import ledger, push, scheduler

TOK = "ExponentPushToken[abcDEF123_-xyz]"


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("FITNESS_NOW", "2026-03-10T12:00:00+00:00")   # midday UTC, outside quiet hours
    orch.onboard({**demo.PROFILE, "name": "Alex"})
    sent = []

    def fake(url, payload, timeout=15.0):
        if url == push.EXPO_SEND:
            sent.extend(payload)
            return {"data": [{"status": "ok", "id": f"t{len(sent)}-{i}"} for i, _ in enumerate(payload)]}
        return {"data": {}}

    monkeypatch.setattr(push, "_post", fake)
    monkeypatch.setattr(scheduler, "_run", lambda name: {"skipped": True})   # no jobs: only the push step is under test
    return sent


def pending(n=1):
    return [ledger.append(agent="training", kind="volume_change", proposal={}, action=None, rationale="r", evidence="e",
                          tier="approval", fired=[], status="pending_approval") for _ in range(n)]


def enable():
    push.register(TOK, "ios")
    push.set_prefs(True)


def test_no_token_no_crash_no_network(scratch):
    pending()
    r = scheduler.run_due()
    assert not scratch and "push_pending" not in r
    assert push.notify_pending_decisions()["sent"] == 0


def test_opted_out_sends_nothing(scratch):
    push.register(TOK)
    pending()
    scheduler.run_due()
    assert not scratch


def test_tick_sends_once_and_never_duplicates(scratch):
    enable()
    ids = pending()
    r = scheduler.run_due()
    assert r["push_pending"]["sent"] == 1 and len(scratch) == 1
    assert scratch[0]["data"]["decision_id"] == ids[0] and scratch[0]["data"]["screen"] == "decisions"
    scheduler.run_due()
    scheduler.run_due()
    assert len(scratch) == 1


def test_rate_limited_one_per_tick(scratch):
    enable()
    pending(2)
    scheduler.run_due()
    assert len(scratch) == 1
    scheduler.run_due()
    assert len(scratch) == 2
    scheduler.run_due()
    assert len(scratch) == 2


def test_quiet_hours_defers_then_sends(scratch, monkeypatch):
    enable()
    pending()
    monkeypatch.setenv("FITNESS_NOW", "2026-03-10T23:30:00+00:00")
    assert push.notify_pending_decisions()["sent"] == 0 and not scratch
    monkeypatch.setenv("FITNESS_NOW", "2026-03-11T12:00:00+00:00")
    assert push.notify_pending_decisions()["sent"] == 1 and len(scratch) == 1


def test_failure_is_logged_not_raised_and_retried(scratch, monkeypatch):
    enable()
    pending()
    monkeypatch.setattr(push, "_post", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    r = push.notify_pending_decisions()
    assert r["sent"] == 0 and r.get("skipped") == "send_failed"
    monkeypatch.setattr(push, "_post", lambda url, payload, timeout=15.0: {"data": [{"status": "ok", "id": "x"}] * len(payload)})
    assert push.notify_pending_decisions()["sent"] == 1


def test_scheduler_swallows_push_exceptions(monkeypatch):
    def boom():
        raise RuntimeError("kaboom")
    monkeypatch.setattr(push, "notify_pending_decisions", boom)
    assert "error" in scheduler.run_due()["push_pending"]


def test_old_and_resolved_decisions_ignored(scratch):
    enable()
    d = pending()[0]
    ledger.update(d, status="applied")
    assert push.notify_pending_decisions()["sent"] == 0 and not scratch
