"""Offline tests for the LLM agent plane. A scripted fake model stands in for Gemini, so these prove the CONTRACT:
whatever the model says, only validated, scoped, gated proposals can change state."""
import copy

import pytest

from backend.agents import orchestrator as orch
from backend.memory import db
from backend.tools import demo
from fitness_os import critic, ledger, loop, planning, runtime


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    orch.onboard({**demo.PROFILE, "name": "Alex"})  # offline: starter plan, no model call
    demo.seed(6)
    monkeypatch.setenv("GEMINI_API_KEY", "fake")  # have_key() true from here; every model call is faked per test


def fake_model(monkeypatch, script):
    """script(system, user, tool) -> dict. Patches every module that calls the LLM."""
    for mod in (runtime, critic, planning):
        monkeypatch.setattr(mod, "extract_structured", lambda s, u, t, max_tokens=0, _f=script: _f(s, u, t))


def by_tool(**handlers):
    def run(system, user, tool):
        return handlers[tool["name"]](system, user, tool)
    return run


def test_malformed_and_out_of_scope_proposals_never_apply(monkeypatch):
    fake_model(monkeypatch, by_tool(
        submit_proposals=lambda s, u, t: {"reasoning": "x", "proposals": [
            {"kind": "volume_change", "rationale": "no fraction given"},                      # malformed
            {"kind": "nope", "rationale": "unknown kind"},                                    # unknown kind
        ]},
        submit_reviews=lambda s, u, t: {"assessment": "", "reviews": []}))
    r = loop.run_event("checkin.logged", {"sleep_h": 7, "soreness": 2, "energy": 4})
    assert r["results"] == []
    assert any(a["dropped"] for a in r["agents"].values())


def test_nutrition_cannot_move_a_workout_even_if_model_tries(monkeypatch):
    # the tool enum for nutrition has no volume_change, but even a forged one is rejected by the scope gate
    from fitness_os import control
    from fitness_os.schema import Proposal
    p = Proposal(agent="nutrition", kind="volume_change", params={"fraction": 0.1}, rationale="sneaky")
    res = control.submit(p)
    assert res["status"] == "blocked" and "agent_scope" in res["fired"]


def test_critic_block_stops_a_proposal_and_ledger_has_no_apply(monkeypatch):
    fake_model(monkeypatch, by_tool(
        submit_proposals=lambda s, u, t: {"reasoning": "r", "proposals": [
            {"kind": "volume_change", "fraction": -0.2, "rationale": "tired", "evidence": "readiness 30", "confidence": 0.8}]},
        submit_reviews=lambda s, u, t: {"assessment": "no", "reviews": [{"index": 0, "verdict": "block", "reason": "thin evidence"}]}))
    before = db.get_plan()["volume_mult"]
    r = loop.run_event("checkin.logged", {"sleep_h": 5, "soreness": 4, "energy": 2})
    assert r["results"] == [] and r["critic"]["held"]
    assert db.get_plan()["volume_mult"] == before


def test_critic_failure_fails_closed(monkeypatch):
    from backend.agents.base import LLMError

    def boom(s, u, t):
        raise LLMError("down")
    props = [runtime.to_proposal("training", {"kind": "volume_change", "fraction": 0.1, "rationale": "stronger"}),
             runtime.to_proposal("training", {"kind": "volume_change", "fraction": -0.1, "rationale": "tired"})]
    fake_model(monkeypatch, boom)
    out = critic.review(props, "cron.daily", [])
    assert [p.params["fraction"] for p in out["proposals"]] == [-0.1]  # the increase is held, the cut goes through
    assert out["critic_ok"] is False and out["held"]


def test_critic_revision_can_only_shrink_the_move(monkeypatch):
    p = runtime.to_proposal("training", {"kind": "volume_change", "fraction": -0.1, "rationale": "tired"})
    for bad in (-0.3, 0.05):  # enlarging or flipping direction is refused
        fake_model(monkeypatch, by_tool(submit_reviews=lambda s, u, t, b=bad: {"assessment": "", "reviews": [
            {"index": 0, "verdict": "revise", "reason": "x", "fraction": b}]}))
        assert critic.review([p], "e", [])["proposals"] == []
    fake_model(monkeypatch, by_tool(submit_reviews=lambda s, u, t: {"assessment": "", "reviews": [
        {"index": 0, "verdict": "revise", "reason": "x", "fraction": -0.05}]}))
    assert critic.review([p], "e", [])["proposals"][0].params["fraction"] == -0.05


def test_unreviewed_proposal_does_not_pass_by_default(monkeypatch):
    p = runtime.to_proposal("training", {"kind": "volume_change", "fraction": -0.1, "rationale": "tired"})
    fake_model(monkeypatch, by_tool(submit_reviews=lambda s, u, t: {"assessment": "", "reviews": []}))
    assert critic.review([p], "e", [])["proposals"] == []


def test_missed_flag_escalates_once(monkeypatch):
    p = runtime.to_proposal("training", {"kind": "note", "text": "x", "rationale": "obs"})
    fake_model(monkeypatch, by_tool(submit_reviews=lambda s, u, t: {"assessment": "", "reviews": [
        {"index": 0, "verdict": "approve", "reason": "ok"}], "missed_flags": ["numbness down the arm", "chest tightness"]}))
    out = critic.review([p], "e", [])
    assert sum(q.kind == "escalate" for q in out["proposals"]) == 1


def test_one_agent_failing_does_not_stop_the_loop(monkeypatch):
    from backend.agents.base import LLMError

    def script(system, user, tool):
        if tool["name"] == "submit_reviews":
            return {"assessment": "", "reviews": [{"index": 0, "verdict": "approve", "reason": "ok"}]}
        if "NUTRITION" in system:
            raise LLMError("429")
        return {"reasoning": "r", "proposals": [{"kind": "note", "text": "responds well to frequency", "rationale": "pattern seen"}]
                if "PROGRESS" in system else []}
    fake_model(monkeypatch, script)
    r = loop.run_event("cron.weekly", {})
    assert any("nutrition" in e for e in r["errors"])
    assert any(x["status"] == "applied" for x in r["results"])


def test_no_key_means_no_agents_but_watchdogs_still_run(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    r = loop.run_event("pain.report", {"text": "sudden chest pain and I felt faint"})
    assert r["llm"] is False and r["errors"]
    assert "training" in ledger.frozen_domains() or "all" in ledger.frozen_domains()  # watchdog escalated without any model


def test_run_agents_scoped_tool_enum_matches_scope():
    from fitness_os.schema import SCOPES
    for a in ("training", "nutrition", "recovery", "progress"):
        assert set(runtime._proposals_tool(a)["input_schema"]["properties"]["proposals"]["items"]["properties"]["kind"]["enum"]) == SCOPES[a]


# ---- adaptive plan design: the model proposes, code validates ----
PROFILE = dict(age=30, sex="f", height_cm=165, weight_kg=70, goal="cut", days_per_week=3, experience="beginner", injuries=["left knee pain"],
               equipment=["dumbbell"])
GOOD = {"rationale": "full body", "days": [
    {"name": "A", "exercises": [{"name": "hip thrust", "sets": 3, "reps": 10}, {"name": "dumbbell press", "sets": 3, "reps": 10},
                                {"name": "dumbbell row", "sets": 3, "reps": 10}, {"name": "plank", "sets": 2, "reps": 45}]},
    {"name": "B", "exercises": [{"name": "romanian deadlift", "sets": 3, "reps": 10}, {"name": "dumbbell shoulder press", "sets": 3, "reps": 10},
                                {"name": "pull up", "sets": 3, "reps": 5}, {"name": "push up", "sets": 3, "reps": 8}]},
    {"name": "C", "exercises": [{"name": "back extension", "sets": 3, "reps": 12}, {"name": "incline press", "sets": 3, "reps": 8},
                                {"name": "chest supported row", "sets": 3, "reps": 10}, {"name": "curls", "sets": 2, "reps": 12}]},
]}


def test_valid_model_plan_is_accepted_and_normalised(monkeypatch):
    fake_model(monkeypatch, by_tool(submit_block=lambda s, u, t: copy.deepcopy(GOOD)))
    plan = planning.design_block(PROFILE)
    assert plan and len(plan["days"]) == 3 and plan["designed_by"] == "training-agent"
    assert all(e["target_rpe"] == 8.0 for d in plan["days"] for e in d["exercises"])


def test_plan_with_banned_or_missing_equipment_exercises_is_rejected_then_fixed(monkeypatch):
    bad = copy.deepcopy(GOOD)
    bad["days"][0]["exercises"][0] = {"name": "squat", "sets": 3, "reps": 5}  # knee injury + no barbell
    calls = []

    def script(s, u, t):
        calls.append(u)
        return bad if len(calls) == 1 else copy.deepcopy(GOOD)
    fake_model(monkeypatch, by_tool(submit_block=script))
    plan = planning.design_block(PROFILE)
    assert plan is not None and len(calls) == 2 and "Fix ALL of these" in calls[1]
    assert all(e["name"] != "squat" for d in plan["days"] for e in d["exercises"])


def test_plan_that_never_validates_returns_none_so_caller_falls_back(monkeypatch):
    bad = {"rationale": "x", "days": [{"name": "A", "exercises": [{"name": "made up lift", "sets": 3, "reps": 10}]}]}
    fake_model(monkeypatch, by_tool(submit_block=lambda s, u, t: bad))
    assert planning.design_block(PROFILE) is None


@pytest.mark.parametrize("mutate,expect", [
    (lambda p: p["days"][0]["exercises"][0].update(sets=9), "sets must be"),
    (lambda p: p["days"][0]["exercises"][0].update(reps=60), "reps must be"),
    (lambda p: p["days"].pop(), "exactly 3 training days"),
    (lambda p: p["days"][0]["exercises"].append(dict(p["days"][0]["exercises"][0])), "appears twice"),
])
def test_validator_rules(mutate, expect):
    plan = copy.deepcopy(GOOD)
    mutate(plan)
    assert any(expect in e for e in planning.validate(plan, PROFILE, planning._avoid(PROFILE)))


def test_template_fallback_still_works_without_a_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    from backend.tools.profile_edit import training_plan
    plan = training_plan({**demo.PROFILE})
    assert plan["days"] and "designed_by" not in plan
