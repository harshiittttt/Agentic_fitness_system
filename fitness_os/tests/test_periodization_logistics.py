"""Periodization and Logistics agents: registration, deterministic offline fallbacks (no LLM key), critic review path, policy gate, model path."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.agents import orchestrator as orch
from backend.memory import db
from backend.tools import demo
from fitness_os import agenda, context, control, critic, ledger, loop, periodization, logistics, prompts, roundtable, runtime
from fitness_os.schema import SCOPES, Proposal


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("FITNESS_SCHEDULER", "0")
    # Pin the wall clock to midday UTC today: otherwise these tests fail whenever the suite runs inside quiet hours (22:00-07:00).
    monkeypatch.setenv("FITNESS_NOW", datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0).isoformat())
    orch.onboard({**demo.PROFILE, "name": "Alex"})


def day(n=0):
    return (agenda.today() + timedelta(days=n)).isoformat()


def applied(agent):
    return [d for d in ledger.recent(50) if d["agent"] == agent and d["status"] == "applied"]


def test_registered_everywhere():
    for a in ("periodization", "logistics"):
        assert a in prompts.ROLES and a in SCOPES and a in runtime.EVIDENCE and a in roundtable.SPECIALISTS and a in roundtable.LABEL
        for t in runtime.EVIDENCE[a]:
            assert isinstance(context.tool(t), dict)
        assert a in context.ROUTES["cron.weekly"] and a in loop.OFFLINE
    assert "logistics" in context.ROUTES["cron.daily"] and context.ROUTES["calendar.changed"] == ["periodization", "logistics"]
    assert SCOPES["periodization"] == {"volume_change", "new_block", "note"} and "calorie_change" not in SCOPES["logistics"]
    assert "calendar" in context.pack("periodization") and "calendar" in context.pack("logistics")
    assert roundtable.triage("should I deload before my meet?")[0] == "periodization"
    assert "logistics" in roundtable.triage("I travel next week, no time for the gym")


def test_agenda_validates_and_orders():
    with pytest.raises(ValueError):
        agenda.add("party", day(1))
    with pytest.raises(ValueError):
        agenda.add("trip", day(5), day(2))
    with pytest.raises(ValueError):
        agenda.add("trip", "not a date")
    a = agenda.add("trip", day(5), day(8), "Goa")
    b = agenda.add("competition", day(30), label="Meet")
    assert [e["id"] for e in agenda.upcoming()] == [a["id"], b["id"]] and agenda.next_competition()["label"] == "Meet"
    assert agenda.remove(a["id"]) and not agenda.remove(99)


def test_no_history_means_no_proposals():
    r = periodization.propose()
    assert r["proposals"] == [] and "not enough training history" in r["reasoning"]


def test_deload_offline_through_the_whole_loop():
    demo.seed(6)                                                  # six loading weeks, no deload yet
    s = periodization.signals()
    assert s["weeks_since_deload_or_block_start"] >= 4
    before = db.get_plan()["volume_mult"]
    rep = loop.run_event("cron.weekly", {})
    assert rep["llm"] is False and "periodization" in rep["agents"] and rep["critic"]["critic_ok"] is False
    deloads = [r for r in rep["results"] if r["agent"] == "periodization" and r["proposal"].startswith("Volume")]
    assert deloads and deloads[0]["status"] == "applied"
    assert db.get_plan()["volume_mult"] < before
    again = loop.run_event("cron.weekly", {})                      # idempotent: no second deload while one is in force
    assert not [r for r in again["results"] if r["agent"] == "periodization" and r["proposal"].startswith("Volume")]


def test_taper_before_competition_and_block_plan_note():
    demo.seed(2)
    agenda.add("competition", day(10), label="Club meet")
    r = periodization.propose()
    vc = [p for p in r["proposals"] if p.kind == "volume_change"]
    assert vc and vc[0].params["fraction"] == periodization.TAPER_FAR and "taper" in vc[0].evidence
    far = agenda.add("competition", day(60), label="Nationals")
    assert far["id"] and any(p.kind == "note" and "Block plan" in p.params["text"] for p in periodization.propose()["proposals"]) is False  # nearest event wins
    ph = [w["phase"] for w in periodization.macro_plan(63)]
    assert ph[-1] == "taper" and ph[-2] == "taper" and "deload" in ph and ph[0] == "build"


def test_block_plan_note_for_a_far_event_is_stored_once():
    demo.seed(2)
    agenda.add("competition", day(70), label="Nationals")
    first = periodization.propose()
    assert [p.kind for p in first["proposals"]] == ["note"]
    rep = loop.run_event("cron.weekly", {})
    assert any(r["proposal"].startswith("note") and r["status"] == "applied" for r in rep["results"])
    assert not [p for p in periodization.propose()["proposals"] if p.kind == "note"]


def test_pain_freeze_and_disruption_silence_periodization():
    demo.seed(6)
    ledger.set_setting("pain_active", True)
    assert periodization.propose()["proposals"] == []
    ledger.set_setting("pain_active", False)
    ledger.set_setting("disruption", {"kind": "travel"})
    assert periodization.propose()["proposals"] == []


def test_rebound_after_deload():
    demo.seed(6)
    loop.run_event("cron.weekly", {})
    assert periodization.signals()["in_deload"]
    assert periodization.propose()["proposals"] == []              # no stacking while the deload runs
    # ten days later: deload settled, rebound is due
    row = next(d for d in ledger.recent(20) if d["agent"] == "periodization" and d["kind"] == "volume_change")
    ledger.update(row["id"], ts=(agenda.clock.now() - timedelta(days=10)).isoformat(timespec="seconds"))
    props = periodization.propose()["proposals"]
    assert props and props[0].params["fraction"] == periodization.REBOUND_STEP and "rebound" in props[0].evidence


def test_new_block_always_needs_approval_even_offline():
    p = Proposal(agent="periodization", kind="new_block", rationale="old block", confidence=0.7)
    assert control.submit(p)["status"] == "pending_approval"


def test_agent_scope_is_enforced():
    r = control.submit(Proposal(agent="periodization", kind="calorie_change", params={"fraction": -0.1}, rationale="sneaky"))
    assert r["status"] == "blocked" and "agent_scope" in r["fired"]
    r = control.submit(Proposal(agent="logistics", kind="new_block", rationale="sneaky"))
    assert r["status"] == "blocked" and "agent_scope" in r["fired"]


def test_offline_critic_holds_load_increases_under_pain():
    inc = Proposal(agent="periodization", kind="volume_change", params={"fraction": 0.1}, rationale="rebound")
    ok = critic.review_offline([inc], "cron.weekly", [])
    assert ok["proposals"] == [inc]
    db.add_memory("injury", "left knee pain")
    held = critic.review_offline([inc], "cron.weekly", [])
    assert held["proposals"] == [] and held["held"]
    cut = Proposal(agent="logistics", kind="volume_change", params={"fraction": -0.1}, rationale="trip")
    assert critic.review_offline([cut], "x", [])["proposals"] == [cut]


def test_time_box_preview_uses_the_app_rule():
    demo.seed(1)
    b = logistics.time_box(25)
    assert b["fitted_minutes"] <= max(25, b["fitted_minutes"]) and b["fitted_minutes"] <= b["full_minutes"] and b["exercises"]


def test_busy_day_gets_a_time_boxed_message_once():
    demo.seed(2)
    ev = agenda.add("busy", day(0), label="Client day", minutes=25)
    first = logistics.propose()
    assert [p.kind for p in first["proposals"]] == ["nudge"] and "25" in first["proposals"][0].evidence
    assert len(first["proposals"][0].params["text"]) <= 240
    rep = loop.run_event("cron.daily", {})
    sent = [r for r in rep["results"] if r["agent"] == "logistics"]
    assert sent and sent[0]["status"] == "applied" and ev["id"]
    assert not [p for p in logistics.propose()["proposals"] if p.kind == "nudge"]     # not repeated for the same event


def test_long_trip_message_and_small_cut():
    demo.seed(2)
    agenda.add("no_gym", day(1), day(6), "Family trip")
    kinds = sorted(p.kind for p in logistics.propose()["proposals"])
    assert kinds == ["nudge", "volume_change"]
    rep = loop.run_event("calendar.changed", {}, record=False)
    cut = [r for r in rep["results"] if r["agent"] == "logistics" and r["proposal"].startswith("Volume")]
    assert cut and cut[0]["status"] == "applied" and (cut[0]["action"] or {})["fraction"] == logistics.NO_GYM_CUT


def test_quiet_hours_still_block_logistics_messages(monkeypatch):
    demo.seed(2)
    monkeypatch.setenv("FITNESS_NOW", (agenda.clock.now().replace(hour=23, minute=30)).isoformat())
    agenda.add("busy", agenda.today().isoformat(), label="Late work", minutes=30)
    rep = loop.run_event("cron.daily", {})
    nud = [r for r in rep["results"] if r["agent"] == "logistics"]
    assert nud and nud[0]["status"] == "blocked" and "quiet_hours" in nud[0]["fired"]


def test_calendar_event_via_event_payload():
    demo.seed(2)
    rep = loop.run_event("calendar.changed", {"event": {"kind": "competition", "start": day(5), "label": "Meet"}})
    assert agenda.next_competition()["label"] == "Meet" and "periodization" in rep["agents"]
    with pytest.raises(ValueError):
        loop.run_event("calendar.changed", {"event": {"kind": "nonsense", "start": day(5)}})


def test_model_path_and_fallback_when_model_fails(monkeypatch):
    from backend.agents.base import LLMError
    demo.seed(6)
    monkeypatch.setenv("GEMINI_API_KEY", "fake")

    def fake(system, user, tool, max_tokens=0):
        if tool["name"] == "submit_reviews":
            n = user.count('"index"')
            return {"assessment": "ok", "reviews": [{"index": i, "verdict": "approve", "reason": "fine"} for i in range(n)]}
        if "PERIODIZATION agent" in system:
            raise LLMError("down")
        return {"reasoning": "nothing", "proposals": []}
    for mod in (runtime, critic):
        monkeypatch.setattr(mod, "extract_structured", fake)
    rep = loop.run_event("cron.weekly", {})
    assert any("rule-based periodization" in e for e in rep["errors"])           # model failed, the deterministic agent stood in
    assert any(r["agent"] == "periodization" and r["status"] == "applied" for r in rep["results"])
    assert rep["critic"]["critic_ok"] is True                                     # the model critic reviewed it


def test_model_proposals_validate_like_any_other_agent():
    p = runtime.to_proposal("periodization", {"kind": "new_block", "rationale": "twelve weeks in"})
    assert p.kind == "new_block"
    with pytest.raises(ValueError):
        runtime.to_proposal("logistics", {"kind": "nudge", "rationale": "x"})   # a message needs text
