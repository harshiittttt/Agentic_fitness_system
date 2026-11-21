"""Plateau experiments: detect, rule out confounders, run ONE controlled change, keep or revert, remember. Offline, simulated clock."""
from datetime import datetime, timedelta

import pytest

from backend.agents import orchestrator as orch
from backend.memory import db
from backend.tools import demo
from fitness_os import control, critic, experiments, ledger, memory, reflection, runtime, scheduler
from fitness_os.schema import Proposal

T0 = "2026-11-02T09:00:00+00:00"


def plus(days, hh=9):
    return (datetime.fromisoformat(T0) + timedelta(days=days)).replace(hour=hh).isoformat()


def at(mp, days, hh=9):
    mp.setenv("FITNESS_NOW", plus(days, hh))


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    at(monkeypatch, 0)
    orch.onboard({**demo.PROFILE, "name": "Alex"})


def plateau(mp, weight=100, days=range(0, 24, 3), sleep=7.5):
    """A squat that has not moved for 8 sessions, plus fresh daily check-ins. Leaves the clock on day 24."""
    for d in days:
        at(mp, d)
        db.add_set(plus(d)[:10], "squat", weight, 5, 8)
    for d in range(10, 24):
        at(mp, d)
        db.add_checkin(sleep, 2, 4, None, None)
    at(mp, 24)


def add_sessions(mp, days, weight):
    for d in days:
        at(mp, d)
        db.add_set(plus(d)[:10], "squat", weight, 5, 8)


def vol():
    return db.get_plan()["volume_mult"]


def test_a_flat_lift_with_good_sleep_starts_exactly_one_controlled_experiment(monkeypatch):
    plateau(monkeypatch)
    base = vol()
    r = experiments.start()
    assert r["started"] and r["lift"] == "squat" and r["hypothesis"] == "stimulus" and r["status"] == "applied"   # fresh athlete: test more stimulus
    assert vol() == pytest.approx(base * 1.05, abs=0.02)
    e = experiments.get(r["experiment"])
    assert e["status"] == "active" and e["metric"] == "e1rm:squat" and e["baseline"]["e1rm"] > 0
    assert (experiments.clock.parse(e["ends"]) - experiments.clock.parse(e["starts"])).days == 14
    again = experiments.start()
    assert not again["started"] and "only one at a time" in again["reason"]


def test_tired_athletes_test_fatigue_first(monkeypatch):
    plateau(monkeypatch)
    for d in range(18, 24):
        at(monkeypatch, d, 20)
        db.add_checkin(6.6, 5, 1, None, None)      # worn out lately (still above the sleep floor)
    at(monkeypatch, 24)
    r = experiments.start()
    assert r["started"] and r["hypothesis"] == "fatigue" and vol() < 1.0


def test_poor_sleep_is_fixed_first_not_experimented_on(monkeypatch):
    plateau(monkeypatch, sleep=5.0)
    base = vol()
    r = experiments.start()
    assert not r["started"] and "sleep" in r["reason"] and vol() == base and experiments.listing() == []


def test_too_few_recent_sessions_means_no_verdict_is_possible(monkeypatch):
    plateau(monkeypatch, days=range(0, 64, 8))      # 8 flat sessions, but only 3 in the last 3 weeks
    at(monkeypatch, 64)
    for d in range(50, 64):
        at(monkeypatch, d)
        db.add_checkin(7.5, 2, 4, None, None)
    at(monkeypatch, 64)
    r = experiments.start("squat")
    assert not r["started"] and "too few" in r["reason"]


def test_no_plateau_no_experiment(monkeypatch):
    for i, d in enumerate(range(0, 24, 3)):
        at(monkeypatch, d)
        db.add_set(plus(d)[:10], "squat", 100 + i * 5, 5, 8)    # steadily improving
    at(monkeypatch, 24)
    assert experiments.start() == {"started": False, "reason": "no plateau detected"}


def test_other_agents_cannot_change_training_while_it_runs_but_safety_can(monkeypatch):
    plateau(monkeypatch)
    experiments.start()
    P = lambda **k: control.reconcile([Proposal(**k)])
    kept, notes = P(agent="training", kind="volume_change", params={"fraction": 0.08}, rationale="felt strong")
    assert kept == [] and "plateau experiment is running" in notes[0]
    kept, notes = P(agent="training", kind="swap_exercise", params={"day_index": 0, "exercise": "x", "replacement": "y"}, rationale="stale")
    assert kept == [] and "confound" in notes[0]
    kept, _ = P(agent="recovery", kind="volume_change", params={"fraction": -0.20}, rationale="sleep debt")      # safety always passes
    assert [p.agent for p in kept] == ["recovery"]
    kept, _ = P(agent="nutrition", kind="calorie_change", params={"fraction": -0.05}, rationale="weight flat")    # a different domain
    assert [p.agent for p in kept] == ["nutrition"]
    kept, _ = P(agent="recovery", kind="escalate", params={"domain": "training", "reason": "sharp pain"}, rationale="pain")
    assert [p.kind for p in kept] == ["escalate"]


def test_a_lift_that_breaks_through_keeps_the_change_and_is_remembered(monkeypatch):
    plateau(monkeypatch)
    r = experiments.start()
    kept_vol = vol()
    add_sessions(monkeypatch, [25, 28, 31, 34], 105)
    at(monkeypatch, 38, 10)
    out = experiments.evaluate_due()
    assert out[0]["verdict"] == "worked" and out[0]["reverted"] is False and out[0]["change"] >= 0.04
    assert vol() == kept_vol
    lesson = memory.items("procedural")[0]["body"]
    assert "broke through" in lesson and "5% more volume" in lesson and lesson.endswith("Kept.")
    assert experiments.get(r["experiment"])["status"] == "concluded" and not experiments.running()


def test_a_lift_that_gets_worse_is_reverted_and_that_idea_is_not_retried(monkeypatch):
    plateau(monkeypatch)
    base = vol()
    r = experiments.start()
    add_sessions(monkeypatch, [25, 28, 31, 34], 95)
    at(monkeypatch, 38, 10)
    out = experiments.evaluate_due()
    assert out[0]["verdict"] == "failed" and out[0]["reverted"] is True
    assert vol() == pytest.approx(base, abs=0.011)
    assert ledger.get(r["decision"])["status"] == "reverted"
    assert "did not respond" in memory.items("procedural")[0]["body"]
    assert "stimulus" not in experiments.ranked("squat")             # do not repeat what just failed


def test_too_few_sessions_waits_a_week_then_reverts_as_inconclusive(monkeypatch):
    plateau(monkeypatch)
    base = vol()
    experiments.start()
    at(monkeypatch, 38, 10)                                           # window over, but the athlete did not train
    assert experiments.evaluate_due() == []
    at(monkeypatch, 46, 10)
    out = experiments.evaluate_due()
    assert out[0]["verdict"] == "inconclusive" and out[0]["reverted"] is True and vol() == pytest.approx(base, abs=0.011)


def test_a_change_that_needs_approval_only_starts_the_clock_once_approved(monkeypatch):
    plateau(monkeypatch)
    ledger.set_setting("dial", "hands_on")
    r = experiments.start()
    assert r["status"] == "pending_veto" and experiments.get(r["experiment"])["status"] == "pending"
    assert experiments.activate_pending() == []                       # nothing happens until the athlete decides
    control.approve(r["decision"])
    at(monkeypatch, 25)
    assert experiments.activate_pending() == [{"experiment": r["experiment"], "status": "active"}]
    assert experiments.get(r["experiment"])["starts"].startswith(plus(25)[:10])


def test_declining_the_change_aborts_the_experiment(monkeypatch):
    plateau(monkeypatch)
    ledger.set_setting("dial", "hands_on")
    r = experiments.start()
    control.reject(r["decision"])
    assert experiments.activate_pending() == [{"experiment": r["experiment"], "status": "aborted"}] and not experiments.running()


def test_a_blocked_change_falls_through_to_the_next_hypothesis(monkeypatch):
    plateau(monkeypatch)
    db.add_memory("injury", "left knee pain")           # no volume increase while an injury is active
    r = experiments.start()
    assert r["started"] and r["hypothesis"] == "fatigue"


def test_experiment_decisions_are_not_double_scored_by_reflection(monkeypatch):
    plateau(monkeypatch)
    r = experiments.start()
    at(monkeypatch, 33)
    assert reflection.run() == [] and "reflected" not in (ledger.get(r["decision"]).get("outcome") or {})


def test_scheduler_ticks_conclude_experiments_and_the_weekly_review_starts_one(monkeypatch):
    plateau(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    quiet = lambda s, u, t, max_tokens=0: ({"reasoning": "fine", "proposals": []} if t["name"] == "submit_proposals" else {"assessment": "", "reviews": []})
    for mod in (runtime, critic):
        monkeypatch.setattr(mod, "extract_structured", quiet)
    monkeypatch.setenv("FITNESS_NOW", "2026-11-29T19:00:00+00:00")    # Sunday evening: the weekly review is due
    r = scheduler.run_due(only="cron.weekly")
    assert r["jobs"]["cron.weekly"]["experiment"]["started"] is True and experiments.running()
    monkeypatch.setenv("FITNESS_NOW", "2026-12-14T10:00:00+00:00")    # window over, but the athlete did not train: wait a grace week
    assert scheduler.run_due(only="curator")["experiments"]["concluded"] == []
    monkeypatch.setenv("FITNESS_NOW", "2026-12-21T10:00:00+00:00")
    tick = scheduler.run_due(only="curator")
    assert [c["verdict"] for c in tick["experiments"]["concluded"]] == ["inconclusive"] and not experiments.running()


def test_http_and_cli_expose_the_registry(monkeypatch, capsys):
    from fastapi.testclient import TestClient
    from backend.main import app
    from fitness_os import cli
    plateau(monkeypatch)
    r = experiments.start()
    row = TestClient(app).get("/agents/experiments").json()[0]
    assert row["id"] == r["experiment"] and row["lift"] == "squat" and row["status"] == "active"
    cli.main(["experiments", "list"])
    assert '"hypothesis": "stimulus"' in capsys.readouterr().out
