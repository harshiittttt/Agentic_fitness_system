"""Adherence, Sleep & Stress and Injury & Rehab agents: scope, evidence, comms rules (quiet hours, frequency cap), undo. Offline, scripted model."""
import pytest

from backend.agents import orchestrator as orch
from backend.memory import db
from backend.tools import demo
from fitness_os import comms, context, control, ledger, loop, prompts, runtime, signals
from fitness_os.schema import SCOPES, Proposal


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("FITNESS_NOW", "2026-03-10T12:00:00+00:00")      # midday UTC: outside quiet hours
    monkeypatch.setenv("FITNESS_SCHEDULER", "0")
    orch.onboard({**demo.PROFILE, "name": "Alex"})
    monkeypatch.setenv("GEMINI_API_KEY", "fake")


def nudge(text="Haven't seen you in a few days. Want a lighter 20-minute session today?", agent="adherence"):
    return Proposal(agent=agent, kind="nudge", params={"text": text}, rationale="You have been quiet for days.", confidence=0.7)


def test_every_new_agent_has_a_role_scope_and_evidence_tool():
    for a in ("adherence", "sleep", "injury"):
        assert a in prompts.ROLES and a in SCOPES and a in runtime.EVIDENCE
        for t in runtime.EVIDENCE[a]:
            assert isinstance(context.tool(t), dict)


def test_scopes_keep_agents_in_their_lane():
    assert "swap_exercise" not in SCOPES["adherence"] and "calorie_change" not in SCOPES["adherence"]
    assert "calorie_change" not in SCOPES["sleep"] and "nudge" not in SCOPES["injury"]
    r = control.submit(Proposal(agent="adherence", kind="volume_change", params={"fraction": -0.1}, rationale="trim it", confidence=0.5))
    assert r["status"] == "blocked" and "agent_scope" in r["fired"]


def test_nudge_is_delivered_and_can_be_undone():
    r = control.submit(nudge())
    assert r["status"] == "applied" and r["tier"] == "auto"
    assert len(comms.unread()) == 1 and "lighter" in comms.unread()[0]["text"]
    control.revert(r["id"])
    assert comms.unread() == []


def test_quiet_hours_block_a_nudge(monkeypatch):
    monkeypatch.setenv("FITNESS_NOW", "2026-03-10T23:30:00+00:00")
    r = control.submit(nudge())
    assert r["status"] == "blocked" and "quiet_hours" in r["fired"] and comms.unread() == []


def test_frequency_cap_and_gap(monkeypatch):
    assert control.submit(nudge())["status"] == "applied"
    assert "nudge_gap" in control.submit(nudge("Another one"))["fired"]          # under 6 h later
    monkeypatch.setenv("FITNESS_NOW", "2026-03-10T19:00:00+00:00")
    assert control.submit(nudge("Evening reminder"))["status"] == "applied"
    monkeypatch.setenv("FITNESS_NOW", "2026-03-11T02:00:00+00:00")                # quiet hours anyway
    monkeypatch.setenv("FITNESS_NOW", "2026-03-10T21:30:00+00:00")
    assert "nudge_cap" in control.submit(nudge("A third"))["fired"]               # two in 24 h is the cap


def test_kill_switch_silences_outreach():
    ledger.set_setting("kill_switch", True)
    r = control.submit(nudge())
    assert r["status"] == "blocked" and comms.unread() == []


def test_empty_and_overlong_nudges():
    assert control.submit(nudge("   "))["status"] == "blocked"
    control.submit(nudge("x" * 900))
    assert len(comms.unread()[0]["text"]) <= comms.MAX_LEN


def test_adherence_evidence_sees_silence():
    ev = signals.adherence()
    assert ev["silent"] is False and ev["sessions_14d"] == 0         # a newcomer has not gone quiet, they have not started
    demo.seed(6)
    assert signals.adherence()["sessions_14d"] >= 1


def test_sleep_debt_evidence():
    for h in (5.0, 5.5, 5.0, 6.0, 7.5, 8.0):
        db.add_checkin(h, 3, 3, None, None)
    ev = signals.sleep_debt()
    assert ev["n"] == 6 and ev["nights_under_6h"] == 3 and ev["debt_h_per_night"] > 0.5


def test_injury_evidence_counts_pain_days():
    db.add_checkin(7, 3, 3, None, "left knee hurts on squats")
    ev = signals.injury_history()
    assert ev["pain_mention_days_60d"]


def test_model_proposals_for_new_agents_become_typed_and_gated(monkeypatch):
    def fake(system, user, tool, max_tokens=0):
        who = "adherence" if "ADHERENCE" in system else "sleep" if "SLEEP" in system else "injury" if "INJURY" in system else None
        if who == "adherence":
            return {"reasoning": "You have been away.", "proposals": [{"kind": "nudge", "rationale": "Check in.", "text": "Missed you this week. Quick 20 minutes?", "confidence": 0.8},
                                                                    {"kind": "calorie_change", "rationale": "Eat a bit less.", "fraction": -0.1}]}
        return {"reasoning": "", "proposals": []}
    monkeypatch.setattr(runtime, "extract_structured", fake)
    out = runtime.run_agent("adherence", "proactive.checkin")
    kinds = [p.kind for p in out["proposals"]]
    assert kinds == ["nudge", "calorie_change"]                        # a model can say anything...
    gated = [control.submit(p)["status"] for p in out["proposals"]]
    assert gated == ["applied", "blocked"]                             # ...but the scope gate stops adherence from touching calories
    rep = loop.run_event("proactive.checkin", {}, use_critic=False)
    assert list(rep["agents"]) == ["adherence"]


# ---------------------------------------------------------------- disruption mode and proactive check-ins
from fitness_os import disruption, scheduler


@pytest.fixture()
def offline(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def _train(day: str, n: int = 4):
    for i in range(n):
        with db.conn() as c:
            c.execute("INSERT INTO sets (ts, session, exercise, weight, reps, rpe, note) VALUES (?,?,?,?,?,?,?)",
                      (f"{day}T10:0{i}:00+00:00", day, "bench press", 60, 8, 7, None))


def test_newcomer_with_no_sessions_is_not_silent_and_not_disrupted(offline):
    assert signals.adherence()["silent"] is False
    assert disruption.detect() is None and disruption.proactive()["sent"] is False


def test_missed_days_enter_degraded_mode_once_and_cut_volume_offline(offline):
    _train("2026-03-01"); _train("2026-03-03")
    assert disruption.detect()["kind"] == "missed_days"
    before = db.get_plan()["volume_mult"]
    r = disruption.tick()
    assert r["entered"] == "missed_days" and r["fallback"] == "applied"
    assert db.get_plan()["volume_mult"] < before
    again = disruption.tick()                                  # already in the mode: never enters or cuts twice
    assert again["active"] is True and "entered" not in again and db.get_plan()["volume_mult"] < before


def test_sickness_in_a_checkin_note_is_detected_and_beats_a_plain_gap(offline):
    _train("2026-03-01"); _train("2026-03-02")
    db.add_checkin(6, 3, 2, None, "I think I have the flu")
    assert disruption.detect()["kind"] == "sick"


def test_return_ramp_is_gradual_and_the_mode_ends(offline, monkeypatch):
    _train("2026-03-01"); _train("2026-03-03")
    disruption.tick()
    cut = db.get_plan()["volume_mult"]
    monkeypatch.setenv("FITNESS_NOW", "2026-03-11T12:00:00+00:00")
    db.add_set("2026-03-11", "bench press", 60, 8, 7)           # trains again
    r = disruption.tick()
    assert r["ramp"]["result"] == "applied" and cut < db.get_plan()["volume_mult"] < 1.0 + 1e-9
    monkeypatch.setenv("FITNESS_NOW", "2026-03-12T12:00:00+00:00")
    assert "ramp" not in disruption.tick()                      # one step a week at most
    monkeypatch.setenv("FITNESS_NOW", "2026-03-19T12:00:00+00:00")
    db.add_set("2026-03-19", "bench press", 60, 8, 7)
    assert disruption.tick()["ramp"]["step"] == 2 and disruption.state() is not None
    monkeypatch.setenv("FITNESS_NOW", "2026-03-26T12:00:00+00:00")
    db.add_set("2026-03-26", "bench press", 60, 8, 7)
    end = disruption.tick()
    assert end["ramp"]["step"] == 3 and end.get("ended") and disruption.state() is None


def test_ongoing_sickness_blocks_the_ramp(offline, monkeypatch):
    _train("2026-03-01"); _train("2026-03-03")
    disruption.tick()
    monkeypatch.setenv("FITNESS_NOW", "2026-03-11T12:00:00+00:00")
    db.add_set("2026-03-11", "bench press", 60, 8, 7)
    db.add_checkin(6, 3, 2, None, "still sick, fever again")
    assert "ramp" not in disruption.tick()


def test_proactive_sends_one_template_nudge_when_silent_then_not_again(offline):
    _train("2026-03-01"); _train("2026-03-02")
    r = disruption.proactive()
    assert r["sent"] is True and r["via"] == "template" and len(comms.unread()) == 1
    assert disruption.proactive()["sent"] is False             # already messaged within a day


def test_proactive_respects_quiet_hours(offline, monkeypatch):
    _train("2026-03-01"); _train("2026-03-02")
    monkeypatch.setenv("FITNESS_NOW", "2026-03-10T23:00:00+00:00")
    r = disruption.proactive()
    assert r["sent"] is False and "quiet_hours" in r["fired"] and comms.unread() == []


def test_scheduler_runs_the_new_jobs_offline(offline, monkeypatch):
    _train("2026-03-01"); _train("2026-03-02")
    assert "proactive" in scheduler.SCHEDULE
    rep = scheduler.run_due()
    assert rep["disruption"]["entered"] == "missed_days"
    assert rep["jobs"]["proactive"]["sent"] is True
    assert scheduler.run_due()["jobs"].get("proactive") is None   # a job never runs twice for the same moment


def test_backdated_session_labels_use_the_label_date_not_the_insert_time():
    """Regression: demo/imported data has old session labels but every row was inserted 'now'. The label is the date; counts must not explode."""
    for day in ("2026-01-05", "2026-01-12", "2026-02-02"):             # all months before the test clock (2026-03-10)
        db.add_set(day, "bench press", 60, 8, 7)                          # ts = now, label = the old date
    ev = signals.adherence()
    assert ev["sessions_14d"] == 0 and ev["sessions_28d"] == 0
    assert ev["days_since_last_session"] > 30
    for d in [f"2026-03-0{i}" for i in range(1, 10)]:                  # nine sessions in 14 days against eight planned
        db.add_set(d, "bench press", 60, 8, 7)
    assert signals.adherence()["rate_14d"] == 1.0                        # more sessions than planned is capped at 100%


def test_a_lapsed_athlete_is_messaged_a_few_times_then_left_alone(offline, monkeypatch):
    """Absence of a reply is the answer: three unanswered nudges in two weeks pause proactive outreach."""
    db.add_set("2026-01-20", "bench press", 60, 8, 7)                    # last trained 7 weeks ago
    assert signals.adherence()["silent"] is True
    sent = 0
    for day in (10, 11, 12, 13, 14):
        monkeypatch.setenv("FITNESS_NOW", f"2026-03-{day}T12:00:00+00:00")
        sent += bool(disruption.proactive()["sent"])
    assert sent == 3
    monkeypatch.setenv("FITNESS_NOW", "2026-03-15T12:00:00+00:00")
    r = disruption.proactive()
    assert r["sent"] is False and "backing off" in r["why"]


# ---------------------------------------------------------------- regression: the decision feed after an Undo
def test_the_decision_feed_survives_an_undo_including_old_format_rows():
    """Bug: control.revert wrote a ledger row whose proposal was {'revert_of': id}; every reader then crashed (500 on /agents/decisions)
    and the whole 'Your team' screen went blank after ANY Undo. New rows are valid; old rows already in a database are read tolerantly."""
    from fastapi.testclient import TestClient
    from backend.main import app
    first = control.submit(Proposal(agent="recovery", kind="volume_change", params={"fraction": -0.1}, rationale="Trim for recovery.", confidence=0.7))
    control.revert(first["id"])
    old = ledger.append(agent="you", kind="note", proposal={"revert_of": 99}, action={"reverted": 99}, rationale="Reverted decision 99",
                        evidence="Volume restored", tier="auto", fired=[], status="applied")          # the OLD malformed shape
    with TestClient(app) as c:
        r = c.get("/agents/decisions")
        assert r.status_code == 200
        summaries = [d["summary"] for d in r.json()]
        assert any("Undid decision" in s for s in summaries) and any("Reverted decision 99" in s for s in summaries)
        assert c.get(f"/agents/decisions/{old}/why").status_code == 200
        assert c.get("/agents/status").status_code == 200
    assert "Reverted decision 99" in control.why(old)
