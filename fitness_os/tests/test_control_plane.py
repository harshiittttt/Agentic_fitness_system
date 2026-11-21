"""Golden scenarios for the control plane: policy tiers, hard constraints, ledger, revert, watchdogs, kill switch."""
import json
import os
import shutil

import pytest

from backend.memory import db
from backend.tools import demo
from fitness_os import context, control, ledger, watchdogs
from fitness_os.schema import Proposal


@pytest.fixture(scope="module")
def template_db(tmp_path_factory):
    """Seed once (onboard + 8 weeks of synthetic history); every test gets a fresh copy."""
    path = tmp_path_factory.mktemp("tpl") / "tpl.db"
    old = db.DB_PATH
    db.DB_PATH = str(path)
    os.environ["FITNESS_NOW"] = "2026-11-17T08:00:00+00:00"
    try:
        demo.seed(8)
    finally:
        db.DB_PATH = old
        os.environ.pop("FITNESS_NOW", None)
    return path


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch, template_db):
    dst = tmp_path / "t.db"
    shutil.copy(template_db, dst)
    monkeypatch.setattr(db, "DB_PATH", str(dst))
    # a month after the seed, so the onboarding change is old and soft hysteresis isn't in play unless a test wants it
    monkeypatch.setenv("FITNESS_NOW", "2026-12-20T08:00:00+00:00")


def P(agent, kind, rationale="test rationale", **params):
    return Proposal(agent=agent, kind=kind, params=params, rationale=rationale, confidence=0.7)


def plan():
    return db.get_plan()


# ---------- tiers ----------
def test_small_volume_trim_is_auto_and_applied():
    r = control.submit(P("recovery", "volume_change", fraction=-0.10))
    assert r["status"] == "applied" and r["tier"] == "auto"
    assert plan()["volume_mult"] == 0.9


def test_cut_over_30_percent_needs_approval_then_applies():
    r = control.submit(P("recovery", "volume_change", fraction=-0.40))
    assert r["status"] == "pending_approval"
    assert plan()["volume_mult"] == 1.0  # nothing moved yet
    assert control.approve(r["id"])["status"] == "applied"
    assert plan()["volume_mult"] == 0.6


def test_calorie_tiers_by_size():
    cal = plan()["nutrition"]["calories"]
    a = control.submit(P("nutrition", "calorie_change", fraction=0.04))
    assert a["tier"] == "auto" and a["status"] == "applied"
    b = control.submit(P("nutrition", "calorie_change", fraction=0.08))
    assert b["tier"] == "notify_first" and b["status"] == "pending_veto"
    c = control.submit(P("nutrition", "calorie_change", fraction=0.15))
    assert c["tier"] == "approve" and c["status"] == "pending_approval"
    assert plan()["nutrition"]["calories"] == round(cal * 1.04)  # only the auto one moved


def test_veto_window_applies_after_it_closes_unless_vetoed():
    control.submit(P("nutrition", "calorie_change", fraction=0.08))
    before = plan()["nutrition"]["calories"]
    assert control.tick() == []  # window still open
    assert plan()["nutrition"]["calories"] == before
    os.environ["FITNESS_NOW"] = "2026-12-20T12:00:00+00:00"  # 4h later > 3h window
    out = control.tick()
    assert out and out[0]["status"] == "applied"
    assert plan()["nutrition"]["calories"] > before


def test_veto_cancels_pending_action():
    b = control.submit(P("nutrition", "calorie_change", fraction=0.08))
    assert control.reject(b["id"])["status"] == "vetoed"
    os.environ["FITNESS_NOW"] = "2026-12-21T08:00:00+00:00"
    assert control.tick() == []
    assert ledger.get(b["id"])["status"] == "vetoed"


# ---------- hard constraints ----------
def test_calorie_floor_is_enforced():
    p = plan()
    p["nutrition"]["calories"] = 1600
    db.save_plan(p)
    r = control.submit(P("nutrition", "calorie_change", fraction=-0.15))
    assert "min_calorie_floor" in r["fired"]
    assert plan()["nutrition"]["calories"] >= 1500 or r["status"] != "applied"


def test_calorie_floor_blocks_when_already_at_floor():
    p = plan()
    p["nutrition"]["calories"] = 1500
    db.save_plan(p)
    r = control.submit(P("nutrition", "calorie_change", fraction=-0.05))
    assert r["status"] == "blocked" and "min_calorie_floor" in r["fired"]


def test_weekly_load_increase_cap_across_proposals():
    r1 = control.submit(P("training", "volume_change", fraction=0.08))
    assert r1["status"] in ("applied", "pending_veto")
    r2 = control.submit(P("training", "volume_change", fraction=0.08))
    # only ~2% of headroom left in the 7-day window, so limited rather than a full 8%
    assert "max_weekly_load_increase" in r2["fired"] or r2["status"] == "blocked"


def test_no_volume_increase_while_injury_restriction_active():
    control.submit(P("watchdog", "restriction", text="left shoulder pain, avoid overhead"))
    r = control.submit(P("training", "volume_change", fraction=0.05))
    assert r["status"] == "blocked" and "no_increase_under_pain" in r["fired"]
    # but cutting volume is always allowed
    assert control.submit(P("recovery", "volume_change", fraction=-0.10))["status"] == "applied"


def test_volume_never_exceeds_125_percent_of_baseline():
    p = plan()
    p["volume_mult"] = 1.24
    db.save_plan(p)
    r = control.submit(P("training", "volume_change", fraction=0.10))
    assert r["status"] == "blocked" or plan()["volume_mult"] <= 1.25


# ---------- agent scope (capability tokens) ----------
def test_nutrition_cannot_move_a_workout_and_training_cannot_change_calories():
    assert control.submit(P("nutrition", "volume_change", fraction=-0.1))["fired"] == ["agent_scope"]
    assert control.submit(P("training", "calorie_change", fraction=-0.05))["fired"] == ["agent_scope"]
    assert plan()["volume_mult"] == 1.0


# ---------- ledger, why, revert ----------
def test_every_decision_is_in_the_ledger_and_explainable():
    r = control.submit(P("recovery", "volume_change", "Poor sleep three nights running", fraction=-0.1))
    text = control.why(r["id"])
    assert "Poor sleep" in text and "recovery" in text and "auto" in text
    assert ledger.recent(5)[0]["id"] == r["id"]


def test_revert_restores_state_for_each_action_type():
    v = control.submit(P("recovery", "volume_change", fraction=-0.10))
    c = control.submit(P("nutrition", "calorie_change", fraction=0.04))
    cal_after = plan()["nutrition"]["calories"]
    assert control.revert(v["id"])["status"] == "reverted" and plan()["volume_mult"] == 1.0
    assert control.revert(c["id"])["status"] == "reverted"
    assert abs(plan()["nutrition"]["calories"] - round(cal_after / 1.04)) <= 1
    assert ledger.get(v["id"])["status"] == "reverted"
    assert "error" in control.revert(v["id"])  # can't revert twice


def test_revert_restriction_removes_it():
    r = control.submit(P("watchdog", "restriction", text="avoid deep squats"))
    assert any("deep squats" in m["text"] for m in db.get_memory(20))
    control.revert(r["id"])
    assert not any("deep squats" in m["text"] for m in db.get_memory(20))


# ---------- reconcile ----------
def test_recovery_cut_beats_training_increase():
    kept, notes = control.reconcile([P("training", "volume_change", fraction=0.08), P("recovery", "volume_change", fraction=-0.15)])
    assert [p.agent for p in kept] == ["recovery"] and notes


def test_small_nudge_waits_for_last_change_to_settle():
    control.submit(P("recovery", "volume_change", fraction=-0.10))  # logs a change "today"
    kept, notes = control.reconcile([P("training", "volume_change", fraction=0.05)])
    assert kept == [] and "settling" in notes[0]


# ---------- watchdogs & escalation ----------
def test_red_flag_freezes_domain_and_blocks_further_training_changes():
    db.add_checkin(7, 2, 3, 80, "felt chest pain and dizzy on the last set")
    ws = watchdogs.run_all()
    assert ws and ws[0]["severity"] == "escalate"
    r = control.submit(ws[0]["proposal"])
    assert r["status"] == "escalated" and "training" in ledger.frozen_domains()
    blocked = control.submit(P("training", "volume_change", fraction=-0.1))
    assert blocked["status"] == "blocked" and "domain_frozen" in blocked["fired"]
    assert "error" in control.revert(r["id"])  # escalations are not one-tap revertible
    ledger.unfreeze("training")
    assert ledger.frozen_domains() == []


def test_repeated_pain_across_days_creates_restriction_not_escalation():
    db.add_checkin(7, 2, 3, 80, "left knee hurts on squats")
    with db.conn() as c:  # make that first mention a different day
        c.execute("UPDATE checkins SET ts = '2026-12-18T08:00:00+00:00' WHERE id = (SELECT MAX(id) FROM checkins)")
    db.add_checkin(7, 2, 3, 80, "knee tweak again")
    ws = watchdogs.injury_watchdog()
    assert ws and ws[0]["severity"] == "warn" and ws[0]["proposal"].kind == "restriction"


def test_single_pain_mention_does_not_fire():
    db.add_checkin(7, 2, 3, 80, "my shoulder hurts a bit")
    assert watchdogs.injury_watchdog() == []


def test_disordered_eating_language_pauses_nutrition_only():
    db.add_checkin(7, 2, 3, 80, "i feel guilty for eating, going to skip meals and starve tomorrow")
    ws = watchdogs.eating_watchdog()
    assert ws and ws[0]["proposal"].params["domain"] == "nutrition"
    assert control.submit(ws[0]["proposal"])["status"] == "escalated"
    assert control.submit(P("nutrition", "calorie_change", fraction=-0.05))["fired"] == ["domain_frozen"]
    assert control.submit(P("recovery", "volume_change", fraction=-0.1))["status"] == "applied"  # training still works


def test_watchdogs_do_not_reraise_active_flag():
    db.add_checkin(7, 2, 3, 80, "fainted after deadlift")
    w = watchdogs.run_all()[0]
    control.submit(w["proposal"])
    assert watchdogs.already_open(watchdogs.run_all()[0])


# ---------- kill switch & dial ----------
def test_kill_switch_pauses_all_writes_but_not_escalation():
    ledger.set_setting("kill_switch", True)
    r = control.submit(P("recovery", "volume_change", fraction=-0.1))
    assert r["status"] == "blocked" and r["fired"] == ["kill_switch"]
    assert control.submit(P("watchdog", "escalate", "red flag symptom reported", domain="training"))["status"] == "escalated"


def test_dial_hands_on_makes_auto_ask_first_and_autopilot_loosens_notify_only():
    ledger.set_setting("dial", "hands_on")
    assert control.submit(P("recovery", "volume_change", fraction=-0.1))["tier"] == "notify_first"
    ledger.set_setting("dial", "autopilot")
    assert control.submit(P("nutrition", "calorie_change", fraction=0.08))["tier"] == "auto"
    assert control.submit(P("nutrition", "calorie_change", fraction=0.15))["tier"] == "approve"  # never loosened


def test_new_block_always_needs_approval():
    r = control.submit(P("training", "new_block", "new hypertrophy block"))
    assert r["status"] == "pending_approval"


# ---------- recall ----------
def test_context_pack_is_small_and_scoped():
    nut = context.pack("nutrition")
    assert "bodyweight_series" in nut and "checkins_14d" not in nut
    rec = context.pack("recovery")
    assert "checkins_14d" in rec and len(json.dumps(rec)) < 8000


def test_events_route_to_the_right_agents():
    assert context.ROUTES["pain.report"] == ["recovery", "training", "injury"]   # Injury & Rehab joins pain triage
    assert "nutrition" in context.ROUTES["session.logged"]


def test_repeat_escalation_for_frozen_domain_is_dropped():
    db.add_checkin(7, 2, 3, 80, "chest pain on squats")
    control.submit(watchdogs.run_all()[0]["proposal"])
    kept, notes = control.reconcile([P("recovery", "escalate", "chest pain reported by athlete", domain="training")])
    assert kept == [] and "already frozen" in notes[0]


def test_invalid_swaps_are_blocked_cleanly_never_crash():
    from fitness_os import control
    from fitness_os.schema import Proposal
    plan = db.get_plan()
    day0 = plan["days"][0]["exercises"][0]["name"]
    for params in ({"day_index": 99, "exercise": day0, "replacement": "x"},                       # no such day
                   {"day_index": 0, "exercise": "bench press!!", "replacement": "dumbbell press"},  # not in that day
                   {"day_index": 0, "exercise": day0, "replacement": "made up lift"},               # not a safe alternative
                   {"day_index": "zero", "exercise": day0, "replacement": "x"}):                    # garbage
        res = control.submit(Proposal(agent="training", kind="swap_exercise", params=params, rationale="try it", evidence="x"))
        assert res["status"] == "blocked" and "invalid_swap" in res["fired"], params
