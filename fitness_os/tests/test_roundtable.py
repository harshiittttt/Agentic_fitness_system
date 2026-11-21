"""Roundtable call mode, offline: triage is deterministic, disagreement triggers at most two rebuttal rounds, the verdict cannot invent numbers,
plan changes still go through critic + policy, and care/red-flag text never reaches an agent."""
import pytest
from fastapi.testclient import TestClient

from backend.agents import orchestrator as orch
from backend.memory import db
from backend.tools import demo
from fitness_os import critic, ledger, memory, roundtable, runtime


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("FITNESS_NOW", "2026-03-10T12:00:00+00:00")
    orch.onboard({**demo.PROFILE, "name": "Alex"})
    demo.seed(6)
    monkeypatch.setenv("GEMINI_API_KEY", "fake")


def script(monkeypatch, by_agent, calls=None, moderator=None):
    """by_agent[name] -> callable(round) returning the model's tool output. The agent is detected from the role line in the system prompt."""
    def fake(system, user, tool, max_tokens=0):
        agent = next(a for a, tag in (("training", "TRAINING agent"), ("nutrition", "NUTRITION agent"), ("recovery", "RECOVERY agent"),
                                     ("sleep", "SLEEP & STRESS"), ("injury", "INJURY & REHAB"), ("progress", "PROGRESS ANALYST"),
                                     ("adherence", "ADHERENCE agent")) if tag in system)
        rnd = 2 if "ROUNDTABLE REBUTTAL" in system else 1
        if calls is not None:
            calls.append((agent, rnd))
        return by_agent[agent](rnd)
    monkeypatch.setattr(runtime, "extract_structured", fake)
    monkeypatch.setattr(critic, "extract_structured", lambda s, u, t, max_tokens=0: {"assessment": "ok", "reviews": [
        {"index": i, "verdict": "approve", "reason": "fine"} for i in range(8)], "missed_flags": []})
    monkeypatch.setattr(roundtable, "quick_text", moderator or (lambda s, u, **k: "Here is the call: hold your volume steady this week."))


def say(text, props=None):
    return {"reasoning": text, "proposals": props or []}


UP = {"kind": "volume_change", "fraction": 0.08, "rationale": "Lifts are rising, add some volume.", "confidence": 0.7}
DOWN = {"kind": "volume_change", "fraction": -0.10, "rationale": "Readiness is dropping, trim volume.", "confidence": 0.7}


def test_triage_picks_two_or_three_relevant_specialists():
    assert roundtable.triage("should I eat more protein and cut calories")[0] == "nutrition"
    assert set(roundtable.triage("my knee hurts when I squat")) >= {"injury", "training"}
    assert 2 <= len(roundtable.triage("hello there")) <= 3                              # ambiguous still gets two voices
    assert len(roundtable.triage("squat knee pain sleep calories protein plateau")) == 3  # never more than three


def test_addressing_a_specialist_by_name_wins():
    assert roundtable.triage("@nutrition how much should I eat") == ["nutrition"]
    assert roundtable.triage("ask the recovery agent about my legs") == ["recovery"]
    assert roundtable.triage("whatever", addressed=["sleep"]) == ["sleep"]


def test_agreeing_agents_answer_once_and_no_debate(monkeypatch):
    calls = []
    script(monkeypatch, {"training": lambda r: say("Your lifts are steady, keep going."), "recovery": lambda r: say("Readiness looks fine.")}, calls)
    out = roundtable.run("how is my training going @training @recovery")
    assert out["conflict"] is False and out["rebuttal_rounds"] == 0
    assert sorted(calls) == [("recovery", 1), ("training", 1)]
    assert out["verdict"].startswith("Here is the call")


def test_disagreement_triggers_a_bounded_rebuttal_and_safety_wins(monkeypatch):
    calls = []
    script(monkeypatch, {"training": lambda r: say("Add a little volume.", [UP]), "recovery": lambda r: say("Trim instead.", [DOWN])}, calls)
    out = roundtable.run("should I add volume or take it easy @training @recovery")
    assert out["conflict"] is True and out["rebuttal_rounds"] == roundtable.MAX_ROUNDS      # nobody conceded: exactly two rounds, then it rules
    assert calls.count(("training", 2)) == 2 and calls.count(("recovery", 2)) == 2
    assert [r["proposal"] for r in out["results"]] == ["Volume -10%"]                         # the cut beats the increase (safety first)
    assert any("did not fully agree" in n for n in out["notes"])


def test_an_agent_that_concedes_ends_the_debate_early(monkeypatch):
    calls = []
    script(monkeypatch, {"training": lambda r: say("Add volume.", [UP]) if r == 1 else say("Fair, I will hold.", []),
                         "recovery": lambda r: say("Trim.", [DOWN])}, calls)
    out = roundtable.run("add volume? @training @recovery")
    assert out["rebuttal_rounds"] == 1 and out["conflict"] is False
    assert calls.count(("training", 2)) == 1


def test_plan_changes_still_go_through_the_gate(monkeypatch):
    before = db.get_plan()["volume_mult"]
    script(monkeypatch, {"training": lambda r: say("ok"), "recovery": lambda r: say("Trim.", [DOWN])})
    out = roundtable.run("tired lately @training @recovery")
    assert out["results"][0]["status"] == "applied" and db.get_plan()["volume_mult"] < before
    ledger.set_setting("kill_switch", True)
    again = roundtable.run("tired lately @training @recovery")
    assert again["results"][0]["status"] == "blocked"                                         # the kill switch still silences everything


def test_a_moderator_that_invents_numbers_is_replaced_by_the_plain_verdict(monkeypatch):
    script(monkeypatch, {"training": lambda r: say("Keep going."), "recovery": lambda r: say("All good.")},
           moderator=lambda s, u, **k: "Do 14 extra sets this week, trust me.")
    out = roundtable.run("how am I doing @training @recovery")
    assert "14" not in out["verdict"] and "Keep going" in out["verdict"]


def test_commit_false_does_not_touch_state(monkeypatch):
    before = db.get_plan()["volume_mult"]
    script(monkeypatch, {"training": lambda r: say("ok"), "recovery": lambda r: say("Trim.", [DOWN])})
    out = roundtable.run("tired @training @recovery", commit=False)
    assert out["results"] == [] and db.get_plan()["volume_mult"] == before


def test_the_call_is_summarised_into_episodic_memory(monkeypatch):
    script(monkeypatch, {"training": lambda r: say("Keep going."), "recovery": lambda r: say("All good.")})
    roundtable.run("how am I doing @training @recovery")
    assert any("Roundtable" in m["body"] for m in memory.recall("roundtable how am I doing", k=6))


def test_care_and_red_flags_never_reach_an_agent(monkeypatch):
    calls = []
    script(monkeypatch, {"training": lambda r: say("x")}, calls)
    care = roundtable.run("I want to starve myself to get thin")
    assert care["care"] and calls == [] and care["results"] == []
    flag = roundtable.run("I get chest pain when I squat")
    assert flag["care"] and calls == [] and "medical" in flag["verdict"].lower()
    assert "training" in ledger.frozen_domains()


def test_no_key_still_triages_and_says_why(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    out = roundtable.run("what should I eat")
    assert out["llm"] is False and out["speakers"] and "LLM key" in out["verdict"]


def test_endpoint(monkeypatch):
    from backend.main import app
    script(monkeypatch, {"training": lambda r: say("Keep going."), "recovery": lambda r: say("All good.")})
    with TestClient(app) as c:
        r = c.post("/agents/roundtable", json={"question": "how am I doing", "addressed": ["training", "recovery"]})
        assert r.status_code == 200 and r.json()["verdict"] and len(r.json()["turns"]) == 2
        assert c.post("/agents/roundtable", json={"question": "  "}).status_code == 400
