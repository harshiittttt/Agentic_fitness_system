"""Local tracing: span tree, redaction (no raw text, no secrets), retention cap, never breaks a run, HTTP + CLI, and the offline scheduler."""
import json
import sqlite3
import time

import pytest

from backend.agents import orchestrator as orch
from backend.memory import db
from backend.tools import demo
from fitness_os import loop, scheduler, trace

T0 = "2026-11-02T09:00:00+00:00"


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("FITNESS_NOW", T0)
    monkeypatch.delenv("FITNESS_TRACE", raising=False)
    orch.onboard({**demo.PROFILE, "name": "Alex"})


def rows():
    c = sqlite3.connect(db.DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute("SELECT * FROM trace_span ORDER BY t0")]
    finally:
        c.close()


def test_nested_spans_form_a_tree_with_counts_and_outcomes():
    with trace.span("root", kind="event", event="x") as r:
        with trace.span("llm", kind="llm", role="main") as s:
            s.set(tokens_in=100, tokens_out=20, cost=0.002)
        with pytest.raises(ValueError):
            with trace.span("boom", kind="tool", tool="t"):
                raise ValueError("bad thing")
        r.set(outcome="partial")
    runs = trace.recent_runs()
    assert len(runs) == 1 and runs[0]["spans"] == 3 and runs[0]["errors"] == 1 and runs[0]["tokens_in"] == 100 and runs[0]["outcome"] == "partial"
    tree = trace.run_tree(runs[0]["run_id"])
    root = tree["tree"][0]
    assert [c["name"] for c in root["children"]] == ["llm", "boom"] and all(c["parent_id"] == root["id"] for c in root["children"])
    assert root["children"][1]["outcome"] == "error" and "ValueError" in root["children"][1]["error"]
    assert "kind" not in root["attrs"] and root["children"][0]["cost"] == 0.002 and root["dur_ms"] >= 0
    assert "llm:llm" in trace.render(tree)
    assert trace.run_tree("nope") is None


def test_no_raw_text_or_secrets_are_stored():
    secret = "AIzaSyA-1234567890abcdefghijklmnop"
    with trace.span("a", kind="agent", agent="training", note="my knee hurts when I squat", payload={"text": "private words"}, tool="x y z"):
        with pytest.raises(RuntimeError):
            with trace.span("b", kind="llm"):
                raise RuntimeError(f"HTTP 400 key={secret} Bearer abcdefghijkl1234")
    blob = json.dumps(rows())
    assert "knee" not in blob and "private" not in blob and secret not in blob and "abcdefghijkl1234" not in blob and "[redacted]" in blob
    a = [r for r in rows() if r["name"] == "a"][0]
    at = json.loads(a["attrs"])
    assert at["agent"] == "training" and at["note"]["len"] == 26 and len(at["note"]["sha"]) == 10 and at["payload"]["keys"] == 1
    assert at["tool"]["len"] == 5   # not identifier-shaped, so hashed even under a safe key


def test_a_broken_trace_store_never_breaks_the_run(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "no" / "such" / "dir" / "x.db"))   # unopenable
    with trace.span("root") as sp:
        sp.set(tokens_in="not a number")           # bad input is swallowed too
        out = 41 + 1
    assert out == 42 and trace.recent_runs() == []
    monkeypatch.setattr(trace, "_flush", lambda buf: 1 / 0)
    with trace.span("root2"):
        pass                                        # even a crashing flush stays inside the span's own guard


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("FITNESS_TRACE", "0")
    with trace.span("root") as sp:
        sp.set(tokens_in=1)
    assert trace.recent_runs() == []


def test_retention_caps_spans_and_drops_old_runs(monkeypatch):
    monkeypatch.setenv("TRACE_MAX_SPANS", "10")
    monkeypatch.setattr(trace, "_flushes", 0)
    for i in range(8):
        with trace.span(f"r{i}", kind="event"):
            with trace.span("c", kind="agent"):
                pass
        time.sleep(0.002)
    trace.prune()
    rs = rows()
    assert len(rs) <= 10 and {r["name"] for r in rs if r["parent_id"] is None} >= {"r7"}      # newest survives, whole old runs go
    assert all(sum(1 for x in rs if x["run_id"] == r["run_id"]) == 2 for r in rs)            # never half a run
    old = time.time() - 40 * 86400
    c = sqlite3.connect(db.DB_PATH); c.execute("UPDATE trace_span SET t0 = ?", (old,)); c.commit(); c.close()
    trace.prune()
    assert rows() == []


def test_overhead_is_small():
    t = time.perf_counter()
    with trace.span("root"):
        for _ in range(200):
            with trace.span("child", kind="tool"):
                pass
    assert (time.perf_counter() - t) / 201 < 0.002   # well under 2 ms per span including the single batched write


def test_an_event_run_is_traced_end_to_end_offline():
    loop.run_event("cron.weekly", {})
    runs = trace.recent_runs()
    assert runs[0]["name"] == "cron.weekly" and runs[0]["kind"] == "event"
    names = {(n["kind"], n["name"]) for n in rows()}
    assert ("agent", "periodization") in names and ("critic", "critic") in names
    tree = trace.run_tree(runs[0]["run_id"])
    assert all(c["kind"] in ("agent", "critic", "policy") for c in tree["tree"][0]["children"])


def test_llm_agent_tool_critic_and_gate_spans_with_tokens(monkeypatch):
    from fitness_os import critic, runtime
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    from backend.agents import base

    def fake_raw(role, body, timeout=60.0, retries=1):
        tool = (body.get("tools") or [{}])[0].get("function", {}).get("name")
        args = {"reasoning": "ok", "proposals": []} if tool == "submit_proposals" else {"assessment": "", "reviews": []}
        return {"model": "m1", "usage": {"prompt_tokens": 50, "completion_tokens": 7},
                "choices": [{"message": {"tool_calls": [{"id": "1", "function": {"name": tool, "arguments": json.dumps(args)}}]}}]}
    monkeypatch.setattr(base, "_post_raw", fake_raw)
    loop.run_event("checkin.logged", {"sleep_hours": 7, "soreness": 2, "energy": 3})
    rs = rows()
    llm = [r for r in rs if r["kind"] == "llm"]
    assert llm and all(r["tokens_in"] == 50 and r["tokens_out"] == 7 for r in llm)
    assert any(r["kind"] == "tool" for r in rs) and any(r["kind"] == "agent" and r["name"] == "training" for r in rs)
    run_ids = {r["run_id"] for r in rs}
    assert len(run_ids) == 1                                   # worker threads joined the same run
    assert trace.recent_runs()[0]["tokens_in"] >= 50


def test_policy_gate_decisions_are_spans(monkeypatch):
    from fitness_os.schema import Proposal
    p = Proposal(agent="training", kind="volume_change", params={"fraction": -0.1}, rationale="because", evidence="data", confidence=0.8)
    with trace.span("root", kind="event"):
        loop._submit(p, "manual")
    g = [r for r in rows() if r["kind"] == "policy"]
    assert g and json.loads(g[0]["attrs"])["status"] and g[0]["outcome"]


def test_http_and_cli(capsys):
    from fastapi.testclient import TestClient
    from backend.main import app
    from fitness_os import cli
    loop.run_event("cron.daily", {})
    c = TestClient(app)
    body = c.get("/agents/trace").json()
    rid = body["runs"][0]["run_id"]
    assert body["enabled"] and body["runs"][0]["name"] == "cron.daily"
    assert c.get(f"/agents/trace/{rid}").json()["tree"][0]["id"] == body["runs"][0]["root_id"]
    assert c.get("/agents/trace", params={"run": rid}).json()["run_id"] == rid
    assert c.get("/agents/trace/zzz").status_code == 404
    cli.main(["trace"])
    assert rid in capsys.readouterr().out
    cli.main(["trace", rid])
    assert "event:cron.daily" in capsys.readouterr().out
    cli.main(["trace", rid, "--json"])
    assert json.loads(capsys.readouterr().out)["run_id"] == rid


# ---------------- scheduler: cron jobs without an LLM key ----------------
def test_weekly_runs_offline_without_a_key_and_is_observable(monkeypatch):
    monkeypatch.setenv("FITNESS_NOW", "2026-11-04T10:00:00+00:00")
    st0 = scheduler.status()
    assert st0["mode"] == "offline" and st0["offline"]["skipped_llm_agents"]["cron.weekly"] and "periodization" in st0["offline"]["runs_offline_agents"]
    r = scheduler.run_due(only="cron.weekly")["jobs"]["cron.weekly"]
    assert r["mode"] == "offline" and "training" in r["skipped_llm_agents"] and "periodization" not in r["skipped_llm_agents"] and "experiment" not in r
    assert "No LLM key" not in json.dumps(r.get("agent_errors", 0))
    st = scheduler.status()["jobs"]["cron.weekly"]
    assert st["mode"] == "offline" and st["last_run"] and not st["due"]
    assert scheduler.run_due(only="cron.weekly")["jobs"] == {}            # not run twice for the same moment
    tr = trace.recent_runs()[0]
    assert tr["name"] == "cron.weekly"                                   # and the offline run was traced


def test_scheduler_tick_checks_push_receipts_and_survives_failures(monkeypatch):
    from fitness_os import ledger, push
    monkeypatch.setenv("FITNESS_NOW", "2026-11-04T10:00:00+00:00")
    tok = "ExponentPushToken[abcdefghijklmnopqrstuv]"
    ledger.set_setting("push_token", {"token": tok, "platform": "ios", "ts": "2026-11-04T09:00:00+00:00"})
    ledger.set_setting("push_tickets", [{"id": "T1", "token": tok, "ts": "2026-11-04T09:00:00+00:00"}])
    calls = []
    monkeypatch.setattr(push, "_post", lambda url, payload, timeout=15.0: calls.append(payload) or {"data": {"T1": {"status": "error", "details": {"error": "DeviceNotRegistered"}}}})
    rep = scheduler.run_due(only="curator")
    assert calls and rep["push_receipts"]["checked"] == 1 and rep["push_receipts"]["pruned"] and push.token() is None
    monkeypatch.setattr(push, "check_receipts", lambda: 1 / 0)
    assert "error" in scheduler.run_due(only="curator")["push_receipts"]      # a broken push never breaks the tick
