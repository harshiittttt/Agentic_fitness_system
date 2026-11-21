"""A set may only be logged from numbers the athlete actually said, and a spoken reply never waits on a slow model."""
import time

import pytest

from backend.agents import orchestrator as orch
from backend.coach import compose
from backend.memory import db
from backend.tools import demo
from fitness_os import coach_tools


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    orch.onboard({**demo.PROFILE, "name": "Alex"})


def log(words, **kw):
    return coach_tools.build(words=words)[1]["log_set"](**kw)


def test_an_invented_set_is_refused_and_nothing_is_saved(env):
    r = log("three", exercise="squat", weight=13, reps=5)          # the model made up 13 kg x 5 from the word "three"
    assert r["status"] == "invalid" and "weight" in r["error"] and "reps" in r["error"] and "ask" in r["error"].lower()
    assert db.get_sets("squat", 5) == []


def test_a_set_the_athlete_actually_described_is_logged(env):
    r = log("squat sixty for eight", exercise="squat", weight=60, reps=8)    # number words count
    assert r.get("status") != "invalid", r
    assert any(s["weight"] == 60 and s["reps"] == 8 for s in db.get_sets("squat", 5))


def test_weight_missing_for_a_barbell_lift_is_asked_for_but_bodyweight_lifts_need_only_reps(env):
    assert log("did 8", exercise="squat", reps=8)["status"] == "invalid"
    assert log("did 8", exercise="pull up", reps=8).get("status") != "invalid"


def test_answers_to_an_earlier_question_count(env):
    from fitness_os import conversation
    conversation.record("live", "user", "bench press sixty", learn=False)
    r = log("eight", exercise="bench press", weight=60, reps=8)             # 60 was said one turn ago
    assert r.get("status") != "invalid", r


def test_compose_gives_up_at_its_deadline_and_the_template_line_is_used(monkeypatch):
    monkeypatch.setenv("COACH_COMPOSE_DEADLINE", "1")
    monkeypatch.setattr(compose, "enabled", lambda: True)
    monkeypatch.setattr(compose, "quick_text", lambda *a, **k: (time.sleep(4), "late")[1])
    t = time.time()
    out = compose.compose("hype", "Alex", "set", "bench 60 for 8", [{"key": "react_good", "ctx": {"w": "60", "r": 8}, "text": "Good set."}])
    assert out is None and time.time() - t < 2.5


def test_a_hung_first_model_is_hedged_by_a_second_one(monkeypatch):
    monkeypatch.setenv("COACH_COMPOSE_DEADLINE", "4")
    monkeypatch.setenv("COACH_COMPOSE_HEDGE", "0.3")
    monkeypatch.setattr(compose, "enabled", lambda: True)
    seen = []

    def fake(system, user, max_tokens=160, timeout=6.0, role="fast", models=None):
        seen.append(models)
        if models and models[0].endswith("primary"):
            time.sleep(3)                                  # the primary model hangs
            return "late"
        return "Good set. Sixty for eight, same again."
    monkeypatch.setattr(compose, "quick_text", fake)
    import backend.agents.base as base
    monkeypatch.setattr(base, "role_models", lambda role: ["x-primary", "x-backup"])
    beats = [{"key": "react_good", "ctx": {"w": "60", "r": 8}, "text": "Good set."}]
    t = time.time()
    out = compose.compose("hype", "Alex", "set", "bench 60 for 8", beats)
    assert time.time() - t < 2.0 and len(seen) == 2 and seen[1] == ["x-backup"]
    assert out is None or "late" not in out
