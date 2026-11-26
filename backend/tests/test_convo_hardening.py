"""Stream F hardening: failures the 259-scenario eval found in the first converse run (2026-11-26), each frozen as a test.
1. 'Can we do something else?' swapped squat for leg press on its own (a permanent plan change that then polluted 260 turns).
2. 'Maybe skip this.' skipped squat while the reply asked 'Do you want to skip?'.
3. "I'm done here" / "that's it for this one" ended the workout.
4. A model reply that says 'logged' with no tool behind it.
5. A hedged / non-exercise skip ('I will skip dessert tonight')."""
from backend.coach import set_slots
from backend.convo import ground, tools
from backend.tests.test_convo_core import CUR, env, run, script, skipped, text, today_sets, tool, names  # noqa: F401  (env is a fixture)

PLAN = ["squat", "romanian deadlift", "leg press", "leg curl", "calf raise"]


def plan_names():
    from backend.coach import live
    return [e["name"] for e in live.session_state()["exercises"]]


def test_something_else_is_a_question_not_a_swap(env, monkeypatch):
    before = plan_names()
    script(monkeypatch, lambda u, m, t: [tool("swap_exercise", reaction="Okay, swapping.")] if "something else" in u else [text("Hmm.")])
    ev = run("Can we do something else?")
    assert plan_names() == before
    said = " ".join(e["text"] for e in names(ev, "say"))
    assert "?" in said                                                    # the guard asks "swap X for something else? yes or no" and changes nothing
    assert names(ev, "done")[0]["expects"] == "yes_no"


def test_swap_needs_an_explicit_swap_word_and_is_not_a_should_i_question(env, monkeypatch):
    before = plan_names()
    script(monkeypatch, lambda u, m, t: [tool("swap_exercise", replacement="goblet squat")])
    run("Should I swap squat for something easier?")
    assert plan_names() == before
    run("Machine's taken.", tid="t9")
    assert plan_names() == before
    ev = run("Swap squat for goblet squat.", tid="t10")
    st = [e for e in names(ev, "tool") if e["name"] == "swap_exercise"][0]["status"]
    assert st in ("ok", "invalid")                                        # the guard let an explicit swap through (the executor may still refuse an unsafe replacement)


def test_hedged_skip_asks_instead_of_skipping(env, monkeypatch):
    script(monkeypatch, lambda u, m, t: [tool("skip_exercise", reaction="Do you want to skip squats today?")])
    for i, sentence in enumerate(["Maybe skip this.", "Skip... or maybe not.", "Perhaps skip this one.", "I might skip this."]):
        ev = run(sentence, tid=f"h{i}")
        assert skipped() == [], sentence
        assert names(ev, "done")[0]["expects"] == "yes_no", sentence


def test_skip_intent_hedges_and_non_exercise_objects():
    k = lambda t: set_slots.skip_intent(t, PLAN)["kind"]          # noqa: E731
    assert k("Maybe skip this.") == "ask" and k("Skip... or maybe not.") == "ask"
    assert k("I will skip dessert tonight.") is None and k("I skipped breakfast") is None
    assert k("Skip squat.") == "skip" and k("Skip this exercise, my shoulder hurts.") == "skip" and k("okay skip") == "skip"
    assert k("Yeh wala skip kar do.") == "skip" and k("We are not skipping anything") == "negated"


def test_a_done_skip_is_stated_never_asked(env, monkeypatch):
    script(monkeypatch, lambda u, m, t: [tool("skip_exercise", reaction="Do you want to skip squats today?")])
    cur = CUR()
    ev = run(f"Skip {cur}.")
    said = " ".join(e["text"] for e in names(ev, "say"))
    assert cur in skipped() and "?" not in said and "skip" in said.lower()


def test_vague_done_phrases_ask_before_ending(env, monkeypatch):
    script(monkeypatch, lambda u, m, t: [tool("end_workout", reaction="Great effort today.")])
    for i, sentence in enumerate(["I'm done here.", "That's it for this one."]):
        ev = run(sentence, tid=f"d{i}")
        assert not [e for e in names(ev, "action") if e["action"].get("type") == "end"], sentence
        assert names(ev, "done")[0]["expects"] == "yes_no", sentence
    ev = run("Okay coach, let's wrap up.", tid="d9")
    assert [e for e in names(ev, "tool") if e["name"] == "end_workout"][0]["status"] == "ok"


def test_claims_of_action_are_recognised():
    for s in ["Logged, nice set.", "Seventy for five, logged.", "Saved.", "Set log kar diya."]:
        assert ground.claims_action(s), s
    for s in ["I haven't logged anything yet.", "Tell me the reps so I can log it.", "Nothing was saved.", "Say it again so I log it right.", "Clean set, take a breather."]:
        assert not ground.claims_action(s), s


def test_model_cannot_say_logged_without_a_tool(env, monkeypatch):
    script(monkeypatch, lambda u, m, t: [text("Seventy for five, logged. Good start.")])        # the model answers in words and calls no tool
    ev = run("squat seventy for five")
    said = " ".join(e["text"] for e in names(ev, "say"))
    assert "logged" not in said.lower() and today_sets() == []
    assert "reps" in said.lower() or "numbers" in said.lower()                                  # it asks for the numbers instead of claiming


def test_a_real_tool_may_say_logged(env, monkeypatch):
    script(monkeypatch, lambda u, m, t: [tool("log_set", weight=60, reps=5, reaction="Clean. Same again.")])
    ev = run("sixty kilos five reps")
    assert len(today_sets()) == 1 and any(e["t"] == "say" for e in ev)


def test_reaction_question_is_kept_after_a_logged_set_but_not_after_class_two(env, monkeypatch):
    script(monkeypatch, lambda u, m, t: [tool("log_set", weight=60, reps=5, reaction="Heavy today?")])
    ev = run("sixty kilos five reps")
    assert any(e["text"].endswith("?") for e in names(ev, "say"))
