"""The coach's tools and the one-round debate, offline. A scripted fake model stands in for Gemini (it replaces the HTTP call under run_agent),
so these prove the CONTRACT: hard limits are never negotiable, a concern is voiced once and then the athlete decides, every change can be undone,
conversation history is replayed, and the care layer runs before any model."""
import json

import pytest

from backend.agents import base, orchestrator as orch
from backend.memory import db
from backend.tools import demo
from fitness_os import coach_tools, control, debate, ledger


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    orch.onboard({**demo.PROFILE, "name": "Alex"})
    demo.seed(6)
    monkeypatch.setenv("GEMINI_API_KEY", "fake")


def cal() -> int:
    return db.get_plan()["nutrition"]["calories"]


def call(name, **args):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": f"c_{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def say(text):
    return {"role": "assistant", "content": text}


def script_model(monkeypatch, replies):
    """Each model call pops the next scripted message; every request body is recorded for inspection."""
    seen, queue = [], list(replies)

    def fake_post(role, body, **kw):
        seen.append(body)
        return {"choices": [{"message": queue.pop(0) if queue else say("(done)")}]}

    monkeypatch.setattr(base, "_post", fake_post)
    return seen


# ---------------------------------------------------------------- hard limits
def test_calorie_floor_is_refused_and_changes_nothing():
    before = cal()
    r = control.submit_user_change("set_calories", {"calories": 900}, words="eat 900")
    assert r["status"] == "refused" and r["rule"] == "calorie_floor"
    assert cal() == before


def test_unknown_change_is_invalid():
    assert control.submit_user_change("make_me_huge", {})["status"] == "invalid"


def test_kill_switch_blocks_the_coach_but_not_a_missing_switch(monkeypatch):
    monkeypatch.setattr(ledger, "kill_switch", lambda: True)
    before = cal()
    r = control.submit_user_change("set_calories", {"calories": before + 100}, words="more food")
    assert r["status"] == "refused" and r["rule"] == "kill_switch" and cal() == before


# ---------------------------------------------------------------- the one-round debate
def test_steep_cut_pushback_then_wait_then_comply_then_undo():
    start = cal()
    target = round(start * 0.7)
    debate.clear()

    debate.begin_turn()
    first = control.submit_user_change("set_calories", {"calories": target}, words="cut hard")
    assert first["status"] == "pushback" and first["reasons"] and first["alternative"]
    assert cal() == start                                          # nothing applied by an objection

    again_same_turn = control.submit_user_change("set_calories", {"calories": target}, words="cut hard")
    assert again_same_turn["status"] == "wait" and cal() == start  # the model cannot skip the debate inside one reply

    debate.begin_turn()                                            # the athlete answers on a LATER turn
    done = control.submit_user_change("set_calories", {"calories": target}, words="do it anyway")
    assert done["status"] == "applied" and done["user_override"] is True
    assert cal() == target

    undone = coach_tools.undo_decision(done["decision_id"])
    assert undone["status"] == "reverted" and cal() == start


def test_a_different_request_resets_the_pending_pushback():
    start = cal()
    debate.clear()
    debate.begin_turn()
    assert control.submit_user_change("set_calories", {"calories": round(start * 0.7)}, words="x")["status"] == "pushback"
    debate.begin_turn()
    other = control.submit_user_change("set_calories", {"calories": start + 50}, words="a small bump instead")
    assert other["status"] == "applied" and other["user_override"] is False
    assert any(h["status"] == "reset" for h in debate.history())


def test_undo_with_nothing_to_undo_is_invalid():
    assert coach_tools.undo_last_change()["status"] == "invalid"


def test_actions_and_truthful_fallback_come_from_the_trace():
    trace = [{"tool": "set_calories", "args": {}, "result": {"status": "applied", "summary": "Calories 2400 -> 2500", "decision_id": 3, "changes": []}}]
    acts = coach_tools.actions_from_trace(trace)
    assert acts[0]["kind"] == "changed" and acts[0]["can_undo"] and acts[0]["decision_id"] == 3
    assert "2400 -> 2500" in coach_tools.fallback_reply(trace)
    assert "Undo" in coach_tools.fallback_reply(trace, hindi=True)            # Hindi persona gets Hindi
    assert coach_tools.actions_from_trace([{"tool": "get_split", "args": {}, "result": {"status": "applied"}}]) == []   # reads are never actions


# ---------------------------------------------------------------- the chat loop with a scripted model
def test_chat_applies_a_small_change_and_reports_an_undoable_action(monkeypatch):
    start = cal()
    script_model(monkeypatch, [call("set_calories", calories=start + 100), say("Done. Calories are up.")])
    out = orch.chat_with_actions("bump my calories a little")
    assert out["reply"] == "Done. Calories are up."
    assert out["actions"][0]["kind"] == "changed" and out["actions"][0]["can_undo"]
    assert cal() == start + 100


def test_chat_pushback_changes_nothing_and_second_message_insists(monkeypatch):
    start = cal()
    target = round(start * 0.7)
    debate.clear()
    script_model(monkeypatch, [call("set_calories", calories=target), say("That is steep. Want the gentler option?")])
    first = orch.chat_with_actions("cut my calories hard")
    assert first["actions"][0]["kind"] == "pushback" and cal() == start

    script_model(monkeypatch, [call("set_calories", calories=target), say("Okay, your call. Done.")])
    second = orch.chat_with_actions("no, do it my way")
    assert second["actions"][0]["kind"] == "changed" and second["actions"][0]["user_override"] is True and cal() == target


def test_model_that_says_nothing_still_gets_a_truthful_reply(monkeypatch):
    start = cal()
    script_model(monkeypatch, [call("set_calories", calories=start + 100), say("")])
    out = orch.chat_with_actions("more food please")
    assert out["reply"].startswith("Done:") and out["actions"][0]["kind"] == "changed"


def test_history_is_replayed_to_the_model_on_the_next_turn(monkeypatch):
    script_model(monkeypatch, [say("Hello Alex.")])
    orch.chat_with_actions("hi coach, my name is Alex")
    seen = script_model(monkeypatch, [say("Still here.")])
    orch.chat_with_actions("what did I just say?")
    contents = [m.get("content") for m in seen[0]["messages"] if m["role"] in ("user", "assistant")]
    assert any("my name is Alex" in (c or "") for c in contents) and any("Hello Alex." in (c or "") for c in contents)


def test_care_layer_answers_before_any_model_or_tool(monkeypatch):
    start = cal()
    seen = script_model(monkeypatch, [call("set_calories", calories=900)])
    out = orch.chat_with_actions("I want to kill myself")
    assert seen == [] and out["actions"] == [] and out["reply"] and cal() == start       # model never called, nothing changed
    assert "14416" in out["reply"] or "help" in out["reply"].lower()


def test_hindi_persona_chat_still_answers_in_code_for_care(monkeypatch):
    db.save_profile({**db.get_profile(), "coach_persona": "roast"})
    assert db.get_profile()["coach_persona"] == "roast"
    seen = script_model(monkeypatch, [say("x")])
    out = orch.chat_with_actions("मैं मरना चाहता हूँ")
    assert seen == [] and out["actions"] == [] and any("ऀ" <= ch <= "ॿ" for ch in out["reply"])   # answered in Hindi, by code, no roast


def test_hindi_persona_gets_a_hindi_truthful_reply_after_a_change(monkeypatch):
    db.save_profile({**db.get_profile(), "coach_persona": "roast"})
    script_model(monkeypatch, [call("set_calories", calories=cal() + 100), say("")])
    out = orch.chat_with_actions("खाना थोड़ा बढ़ा दे")
    assert "हो गया" in out["reply"] and out["actions"][0]["kind"] == "changed"


# ---------------------------------------------------------------- persona, language and channel
def test_switching_to_an_english_coach_ignores_old_hindi_history_and_pins_english(monkeypatch):
    from fitness_os import conversation
    db.save_profile({**db.get_profile(), "coach_persona": "mentor"})                # Sage
    conversation.record("chat", "user", "hi", learn=False)
    conversation.record("chat", "assistant", "अबे, आ गया? बोल, क्या पूछना है।", meta=None)         # untagged older Ustaad turn
    conversation.record("chat", "assistant", "राम राम भाई!", meta={"persona": "haryanvi"})
    seen = script_model(monkeypatch, [say("Hello, how can I help?")])
    orch.chat_with_actions("how is my week going?")
    msgs = seen[0]["messages"]
    assert not any(any("ऀ" <= ch <= "ॿ" for ch in (m.get("content") or "")) for m in msgs if m["role"] == "assistant")
    system = msgs[0]["content"]
    assert "always reply in English" in system


def test_hindi_coach_keeps_its_own_hindi_history(monkeypatch):
    from fitness_os import conversation
    db.save_profile({**db.get_profile(), "coach_persona": "roast"})
    conversation.record("chat", "user", "hi", learn=False)
    conversation.record("chat", "assistant", "अबे, आ गया?", meta={"persona": "roast"})
    seen = script_model(monkeypatch, [say("बोल।")])
    orch.chat_with_actions("आज क्या करना है")
    assert any("अबे" in (m.get("content") or "") for m in seen[0]["messages"] if m["role"] == "assistant")
    assert "Devanagari" in seen[0]["messages"][0]["content"]


def test_chat_prompt_is_not_a_gym_prompt_but_live_is(monkeypatch):
    seen = script_model(monkeypatch, [say("ok")])
    orch.chat_with_actions("hello there")
    chat_system = seen[0]["messages"][0]["content"].lower()
    assert "earbuds" not in chat_system and "do not assume they are in the gym" in chat_system
    seen = script_model(monkeypatch, [say("ok")])
    orch.chat_with_actions("hello there", channel="live")
    assert "earbuds" in seen[0]["messages"][0]["content"].lower()
