"""Push notifications: token store, opt-in, quiet hours, cap, dedupe, dial, pruning, receipts. The Expo network is mocked; nothing is sent."""
import pytest
from fastapi.testclient import TestClient

from backend.agents import orchestrator as orch
from backend.main import app
from backend.memory import db
from backend.tools import demo
from fitness_os import comms, control, ledger, push
from fitness_os.schema import Proposal

client = TestClient(app)
TOK = "ExponentPushToken[abcDEF123_-xyz]"


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("FITNESS_NOW", "2026-03-10T12:00:00+00:00")      # midday: outside quiet hours
    orch.onboard({**demo.PROFILE, "name": "Alex"})
    sent = []
    state = {"mode": "ok"}

    def fake(url, payload, timeout=15.0):
        if url == push.EXPO_SEND:
            sent.extend(payload)
            if state["mode"] == "dead":
                return {"data": [{"status": "error", "message": "x", "details": {"error": "DeviceNotRegistered"}} for _ in payload]}
            if state["mode"] == "down":
                raise ConnectionError("offline")
            return {"data": [{"status": "ok", "id": f"t{len(sent)}-{i}"} for i, _ in enumerate(payload)]}
        return {"data": {i: {"status": "error", "details": {"error": "DeviceNotRegistered"}} for i in payload["ids"]}}

    monkeypatch.setattr(push, "_post", fake)
    return sent, state


def enable(**cats):
    push.register(TOK, "ios")
    push.set_prefs(True, cats or None)


def test_token_validation_and_single_user():
    assert client.post("/push/register", json={"token": "nope"}).status_code == 422
    assert client.post("/push/register", json={"token": TOK, "platform": "ios"}).json()["registered"]
    other = "ExpoPushToken[zzzzzzzz1]"
    client.post("/push/register", json={"token": other})
    assert push.token() == other                                  # latest wins, only one stored
    assert client.delete("/push/register").json()["removed"] and push.token() is None


def test_off_until_opt_in(scratch):
    sent, _ = scratch
    push.register(TOK)
    assert push.notify("nudge", "t", "hello")["reason"] == "opted_out" and not sent
    assert push.status()["enabled"] is False
    client.put("/push/settings", json={"enabled": True})
    assert push.notify("nudge", "t", "hello")["sent"] and len(sent) == 1


def test_category_off_and_unknown(scratch):
    enable(nudge=False)
    assert push.notify("nudge", "t", "x")["reason"] == "category_off"
    assert push.notify("report", "t", "x")["reason"] == "category_off"      # report defaults off
    assert client.put("/push/settings", json={"categories": {"bogus": True}}).status_code == 422


def test_quiet_hours_block_except_safety(scratch, monkeypatch):
    sent, _ = scratch
    enable()
    monkeypatch.setenv("FITNESS_NOW", "2026-03-10T02:00:00+00:00")
    assert push.notify("nudge", "t", "late")["reason"] == "quiet_hours"
    assert push.notify_safety("pause")["sent"] and sent[-1]["priority"] == "high"


def test_daily_cap_and_dedupe(scratch):
    sent, _ = scratch
    enable()
    assert push.notify("nudge", "t", "one")["sent"]
    assert push.notify("nudge", "t", "one")["reason"] == "duplicate"
    assert push.notify("nudge", "t", "two")["sent"] and push.notify("nudge", "t", "three")["sent"]
    assert push.notify("nudge", "t", "four")["reason"] == "daily_cap"
    assert push.notify_safety("pause")["sent"]                       # safety ignores the cap
    assert len(sent) == 4


def test_dial_and_kill_switch(scratch):
    sent, _ = scratch
    enable()
    ledger.set_setting("dial", "hands_on")
    assert push.notify("nudge", "t", "a")["reason"] == "dial_hands_on"
    assert push.notify_pending("Cut volume 10%", 3)["sent"] and sent[-1]["data"]["screen"] == "decisions"
    ledger.set_setting("dial", "balanced")
    ledger.set_setting("kill_switch", True)
    assert push.notify("nudge", "t", "b")["reason"] == "kill_switch"


def test_device_not_registered_prunes_token(scratch):
    _, state = scratch
    enable()
    state["mode"] = "dead"
    r = push.notify("nudge", "t", "x")
    assert r["sent"] is False and r["error"] == "DeviceNotRegistered" and push.token() is None


def test_network_failure_never_raises(scratch):
    _, state = scratch
    enable()
    state["mode"] = "down"
    assert push.notify("nudge", "t", "x")["reason"] == "send_failed" and push.token() == TOK


def test_receipts_prune_after_delay(scratch, monkeypatch):
    enable()
    push.notify("nudge", "t", "x")
    assert push.check_receipts()["checked"] == 0                     # too early to ask
    monkeypatch.setenv("FITNESS_NOW", "2026-03-10T12:30:00+00:00")
    r = push.check_receipts()
    assert r["checked"] == 1 and r["errors"] == 1 and r["pruned"] and push.token() is None


def test_batching(scratch):
    sent, _ = scratch
    push.register(TOK)
    t = push.send_raw([{"to": TOK, "body": str(i)} for i in range(250)])
    assert len(t) == 250 and len(sent) == 250


def test_test_button(scratch):
    sent, _ = scratch
    assert client.post("/push/test").status_code == 409
    push.register(TOK)                                               # not opted in: the test still works
    assert client.post("/push/test").json()["sent"] and sent[0]["data"]["category"] == "test"


def test_applied_nudge_pushes_through_control_plane(scratch):
    sent, _ = scratch
    enable()
    res = control.submit(Proposal(agent="adherence", kind="nudge", params={"text": "Nice week. Ready for Thursday?"}, rationale="check-in", evidence="quiet week"))
    assert res["status"] == "applied" and len(comms.unread()) == 1
    assert sent and sent[0]["body"].startswith("Nice week")


def test_nudge_without_opt_in_sends_nothing(scratch):
    sent, _ = scratch
    control.submit(Proposal(agent="adherence", kind="nudge", params={"text": "Hi there"}, rationale="check-in", evidence="quiet week"))
    assert not sent and len(comms.unread()) == 1
