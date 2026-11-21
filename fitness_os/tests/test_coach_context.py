"""The shared interface the two coach workstreams build on: conversation memory + the single coach context."""
import pytest

from backend.agents import orchestrator as orch
from backend.memory import db
from backend.tools import demo
from fitness_os import coach_context, conversation, memory


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    orch.onboard({**demo.PROFILE, "name": "Alex"})


def test_conversation_is_ordered_merged_across_channels_and_limited():
    for i in range(5):
        conversation.record("chat" if i % 2 == 0 else "live", "user" if i % 2 == 0 else "assistant", f"message {i}")
    assert [m["content"] for m in conversation.history(limit=3)] == ["message 2", "message 3", "message 4"]
    assert {m["channel"] for m in conversation.history()} == {"chat", "live"}                  # chat and live share one memory
    assert [m["content"] for m in conversation.history(channel="live")] == ["message 1", "message 3"]


def test_old_turns_age_out_and_bad_input_is_refused(monkeypatch):
    conversation.record("chat", "user", "ancient")
    monkeypatch.setenv("FITNESS_NOW", "2099-01-01T00:00:00+00:00")
    assert conversation.history() == []
    for bad in (("tv", "user", "x"), ("chat", "robot", "x"), ("chat", "user", "   ")):
        with pytest.raises(ValueError):
            conversation.record(*bad)


def test_context_describes_the_split_today_and_what_was_learned():
    memory.add("procedural", "For this athlete, cutting volume 10% worked: readiness +9 over 7 days.", 0.7)
    ctx = coach_context.build(live_context="currently squat, set 2 of 3", query="should I cut volume")
    plan = db.get_plan()
    assert f"Current split ({len(plan['days'])} days" in ctx
    assert all(d["name"] in ctx for d in plan["days"])                       # every day of the split is visible to the coach
    assert "Next/today session:" in ctx and "Learned about this athlete" in ctx and "cutting volume 10% worked" in ctx
    assert "Right now: currently squat, set 2 of 3" in ctx
    assert len(ctx) < 4000                                                   # small on purpose


def test_context_reflects_logged_sets_and_injuries():
    from backend.tools import exercises as ex
    db.add_set(ex.today(), "squat", 100, 5, 8)
    db.add_memory("injury", "left knee pain")
    ctx = coach_context.build()
    assert "Logged today: squat 100x5" in ctx and "left knee pain" in ctx


def test_chat_and_live_turns_land_in_one_shared_conversation_memory(monkeypatch):
    for k in ("GEMINI_API_KEY", "LLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    orch.chat("how is my split looking?")                       # no LLM key: still answers, still remembered
    orch.chat("my shoulder feels sharp on the way up", live_context="squat set 2", channel="live")
    h = conversation.history()
    assert [(m["channel"], m["role"]) for m in h][:2] == [("chat", "user"), ("chat", "assistant")]
    assert {m["channel"] for m in h} == {"chat", "live"} and h[2]["content"] == "my shoulder feels sharp on the way up"
