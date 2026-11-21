"""/coach/chat and /coach/undo wiring (the model and the tools are tested elsewhere; this checks the HTTP contract)."""
from fastapi.testclient import TestClient

from backend import main
from backend.agents import orchestrator as orch


def test_chat_returns_reply_and_actions(monkeypatch):
    acts = [{"kind": "changed", "summary": "Switched to PPL", "decision_id": 7, "can_undo": True}]
    monkeypatch.setattr(orch, "chat_with_actions", lambda message, *a, **k: {"reply": "Done.", "actions": acts})
    with TestClient(main.app) as c:
        for path in ("/chat", "/coach/chat"):
            r = c.post(path, json={"message": "change my split to PPL"})
            assert r.status_code == 200
            assert r.json() == {"reply": "Done.", "actions": acts}


def test_undo_conflict_when_nothing_to_undo(monkeypatch):
    from fitness_os import coach_tools
    monkeypatch.setattr(coach_tools, "undo_last_change", lambda: {"status": "invalid", "error": "There is no recent change of yours to undo."})
    with TestClient(main.app) as c:
        r = c.post("/coach/undo", json={})
        assert r.status_code == 409


def test_undo_by_id(monkeypatch):
    from fitness_os import coach_tools
    monkeypatch.setattr(coach_tools, "undo_decision", lambda i: {"status": "reverted", "decision_id": i, "summary": "Undid: x"})
    with TestClient(main.app) as c:
        r = c.post("/coach/undo", json={"decision_id": 5})
        assert r.status_code == 200 and r.json()["decision_id"] == 5
