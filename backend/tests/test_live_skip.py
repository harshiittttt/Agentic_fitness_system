"""Regression tests for docs/ERROR_LEDGER.md E-001 (exercises skipped without being asked), E-002 (sets saved under the wrong exercise)
and E-003 (reply that does not match what was logged), plus the un-skip action and the small mic-noise fixes."""
import pytest
from fastapi.testclient import TestClient

from backend.coach import live, set_slots
from backend.main import app
from backend.memory import db
from backend.tools.demo import PROFILE


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    live._pending_skip.clear()
    live._pending_clear()
    c = TestClient(app)
    c.post("/onboard", json={**PROFILE, "name": "Alex", "coach_persona": "hype"})
    return c


def say(client, text):
    return client.post("/live/say", json={"transcript": text}).json()


def state(client):
    return client.get("/live/state").json()


def skipped(client):
    return [e["name"] for e in state(client)["exercises"] if e["skipped"]]


def skip_rows():
    return [m for m in db.get_memory(100) if m["kind"] == "skip"]


# ---------------------------------------------------------------- E-001: nothing is skipped unless the lifter clearly says so
@pytest.mark.parametrize("sentence", [
    "We are not skipping anything, we are doing every exercise.",
    "I am not skipping the goblet squat.",
    "You're not skipping anything. We are doing every exercise.",
    "Skipping anything.",
    "Please don't skip it.",
    "no skip",
    "skip mat karna",
    "skip nahi karna hai",
    "I'm not going to skip bench.",
    "What is the next exercise?",
    "what's the next exercise",
    "Which exercise is next?",
    "agla exercise kaunsa hai",
    "Should I skip this one?",
    "How many sets are left?",
    "Okay, next set.",
])
def test_these_sentences_never_skip(client, sentence):
    before = state(client)["current"]["name"]
    r = say(client, sentence)
    assert r["intent"] not in ("skip", "skip_ask"), (sentence, r["intent"], r["reply"])
    assert skipped(client) == [] and skip_rows() == [], sentence
    assert state(client)["current"]["name"] == before


def test_what_is_the_next_exercise_answers_the_question(client):
    st = state(client)
    r = say(client, "What is the next exercise?")
    assert r["intent"] == "status"
    low = r["reply"].lower()
    assert st["exercises"][0]["name"] in low and st["exercises"][1]["name"] in low     # where you are, and what comes after it


@pytest.mark.parametrize("sentence", [
    "Skip this exercise, my shoulder hurts.",
    "skip this one",
    "please skip it",
    "let's skip this exercise",
    "I want to skip this",
    "okay skip",
    "yeh wala skip kardo",
])
def test_a_clear_order_still_skips(client, sentence):
    st = state(client)
    r = say(client, sentence)
    assert r["intent"] == "skip", (sentence, r["reply"])
    assert skipped(client) == [st["exercises"][0]["name"]]
    assert r["state"]["current"]["name"] == st["exercises"][1]["name"]
    assert len(skip_rows()) == 1


def test_skip_because_of_pain_also_writes_the_pain_down(client):
    r = say(client, "Skip this exercise, my shoulder hurts.")
    assert r["intent"] == "skip"
    assert any(m["kind"] == "injury" and "shoulder" in m["text"] for m in db.get_memory(20))


def test_skipping_the_same_exercise_twice_writes_one_record(client):
    say(client, "skip this one")
    first = skipped(client)[0]
    say(client, f"skip {first}")                      # already skipped: it moves on to skipping the next current one, never the same row twice
    assert len([m for m in skip_rows() if m["text"].endswith("|" + first)]) == 1


def test_unclear_phrases_ask_before_skipping(client):
    st = state(client)
    r = say(client, "next exercise please")
    assert r["intent"] == "skip_ask" and r["expects"] == "yes_no"
    assert st["exercises"][0]["name"] in r["reply"].lower()
    assert skipped(client) == []                      # nothing happened yet
    r2 = say(client, "yes")
    assert r2["intent"] == "skip" and skipped(client) == [st["exercises"][0]["name"]]


def test_answering_no_keeps_the_exercise(client):
    say(client, "the machine is busy")
    assert skipped(client) == []
    r = say(client, "no")
    assert r["intent"] == "ready" and skipped(client) == []
    assert say(client, "yes")["intent"] != "skip"     # the question was dropped: a late "yes" skips nothing
    assert skipped(client) == []


def test_other_talk_drops_the_question(client):
    say(client, "let's move on")
    say(client, "how many sets are left")
    assert say(client, "yes")["intent"] != "skip" and skipped(client) == []


def test_a_yes_with_no_question_asked_skips_nothing(client):
    say(client, "yes")
    assert skipped(client) == []


# ---------------------------------------------------------------- un-skip
def test_put_it_back_restores_the_exercise(client):
    st = state(client)
    first = st["exercises"][0]["name"]
    say(client, "skip this exercise")
    assert skipped(client) == [first]
    r = say(client, "No wait, put it back, I will do it.")
    assert r["intent"] == "unskip" and r["restored"] == [first]
    assert first in r["reply"].lower()
    assert skipped(client) == [] and skip_rows() == []
    assert state(client)["current"]["name"] == first


def test_negation_after_a_wrong_skip_puts_everything_back(client):
    st = state(client)
    say(client, "skip this one")
    say(client, "skip this one")
    assert len(skipped(client)) == 2
    r = say(client, "We are not skipping anything, we are doing every exercise.")
    assert r["intent"] == "unskip" and len(r["restored"]) == 2
    assert skipped(client) == [] and state(client)["current"]["name"] == st["exercises"][0]["name"]


def test_not_skipping_a_named_exercise_restores_only_that_one(client):
    st = state(client)
    a, b = st["exercises"][0]["name"], st["exercises"][1]["name"]
    say(client, "skip this one")
    say(client, "skip this one")
    r = say(client, f"I am not skipping {b}.")
    assert r["restored"] == [b] and skipped(client) == [a]


def test_unskip_by_name(client):
    st = state(client)
    a, b = st["exercises"][0]["name"], st["exercises"][1]["name"]
    say(client, "skip this one")
    say(client, "skip this one")
    r = say(client, f"put {a} back")
    assert r["restored"] == [a] and skipped(client) == [b]


def test_put_it_back_with_nothing_skipped_says_so(client):
    r = say(client, "put it back")
    assert r["intent"] == "unskip" or r["intent"] != "skip"
    assert skipped(client) == []


def test_i_will_do_it_with_nothing_skipped_is_not_an_unskip(client):
    assert say(client, "I will do it")["intent"] != "unskip"


def test_unskip_endpoint_and_nothing_to_restore(client):
    first = state(client)["exercises"][0]["name"]
    say(client, "skip this one")
    r = client.post("/live/unskip", json={}).json()
    assert r["intent"] == "unskip" and r["restored"] == [first] and skipped(client) == []
    again = client.post("/live/unskip", json={}).json()
    assert again["restored"] == [] and again["reply"]
    say(client, "skip this one")
    say(client, "skip this one")
    assert len(client.post("/live/unskip", json={"everything": True}).json()["restored"]) == 2
    say(client, "skip this one")
    named = state(client)["exercises"][0]["name"]
    assert client.post("/live/unskip", json={"exercise": named}).json()["restored"] == [named]


def test_unskip_retracts_the_memory_layer_belief(client):
    from fitness_os.mem import store
    first = state(client)["exercises"][0]["name"]

    def live_skip_facts():
        return [f for f in store.facts(predicate="happened", status=("active",), limit=200) if "skipped" in f["object_text"] and first in f["object_text"]]

    say(client, "skip this one")
    assert live_skip_facts()
    client.post("/live/unskip", json={})
    assert not live_skip_facts()
    say(client, "skip this one")                      # skipping it for real afterwards is still recorded
    assert live_skip_facts()


def test_old_skips_are_not_undone_by_a_vague_put_it_back(client):
    from datetime import datetime, timedelta, timezone
    first = state(client)["exercises"][0]["name"]
    say(client, "skip this one")
    with db.conn() as c:                              # the skip is an hour old
        c.execute("UPDATE memory SET ts = ? WHERE kind = 'skip'", ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds"),))
    say(client, "put it back")
    assert skipped(client) == [first]
    say(client, f"put {first} back")                  # naming it always works
    assert skipped(client) == []


# ---------------------------------------------------------------- E-002 / E-003: a spoken set belongs to a plan exercise, with the numbers said
def test_a_set_with_no_exercise_goes_to_the_current_one(client):
    cur = state(client)["current"]["name"]
    r = say(client, "Sixty kilos, five reps, done.")
    assert r["intent"] == "set"
    assert [(s["exercise"], s["weight"], s["reps"]) for s in r["saved"]] == [(cur, 60.0, 5)]
    rows = [s for s in db.get_sets(limit=20)]
    assert [(s["exercise"], s["weight"], s["reps"]) for s in rows] == [(cur, 60.0, 5)]
    assert r["state"]["exercises"][0]["done"] == 1 and r["state"]["current"]["set_no"] == 2


@pytest.mark.parametrize("junk", ["unknown", "stretch", "cable exercise", "exercise", "my special thing", ""])
def test_made_up_exercise_names_are_saved_as_the_current_exercise(client, monkeypatch, junk):
    cur = state(client)["current"]["name"]
    monkeypatch.setattr(live, "parse_voice_log", lambda t, fast=False: {"sets": [{"exercise": junk, "weight": 60.0, "reps": 5, "rpe": None}], "safety_note": ""})
    r = live._log_sets("60 kilos 5 reps", "hype", "Alex")
    assert r["intent"] == "set" and r["saved"][0]["exercise"] == cur
    assert {s["exercise"] for s in db.get_sets(limit=20)} == {cur}


def test_a_name_that_fits_a_plan_exercise_is_used(client, monkeypatch):
    st = state(client)
    target = st["exercises"][1]["name"]
    word = target.split()[0]
    monkeypatch.setattr(live, "parse_voice_log", lambda t, fast=False: {"sets": [{"exercise": "unknown", "weight": 40.0, "reps": 10, "rpe": None}], "safety_note": ""})
    r = live._log_sets(f"{word} 40 kilos 10 reps", "hype", "Alex")
    assert r["saved"][0]["exercise"] == target


def test_a_real_exercise_that_is_not_in_the_plan_is_kept_as_said(client, monkeypatch):
    monkeypatch.setattr(live, "parse_voice_log", lambda t, fast=False: {"sets": [{"exercise": "lat pulldown", "weight": 40.0, "reps": 10, "rpe": None}], "safety_note": ""})
    r = live._log_sets("lat pulldown 40 kilos 10 reps", "hype", "Alex")
    assert r["saved"][0]["exercise"] == "lat pulldown"


def test_with_nothing_left_to_do_an_unplaceable_set_is_asked_about(client, monkeypatch):
    st = state(client)
    for e in st["exercises"]:
        for _ in range(e["sets"]):
            say(client, f"{e['name']} 50 for 8")
    assert state(client)["finished"]
    n = len(db.get_sets(limit=500))
    monkeypatch.setattr(live, "parse_voice_log", lambda t, fast=False: {"sets": [{"exercise": "unknown", "weight": 60.0, "reps": 5, "rpe": None}], "safety_note": ""})
    r = live._log_sets("60 kilos 5 reps", "hype", "Alex")
    assert r["intent"] == "ask" and r["expects"] == "set_details"
    assert len(db.get_sets(limit=500)) == n          # nothing was saved under a made-up name


def test_the_same_set_returned_twice_by_the_parser_is_saved_once(client, monkeypatch):
    one = {"exercise": "unknown", "weight": 30.0, "reps": 12, "rpe": 6.0}
    monkeypatch.setattr(live, "parse_voice_log", lambda t, fast=False: {"sets": [dict(one), dict(one)], "safety_note": ""})
    live._log_sets("30 kilos 12 reps slow negative", "hype", "Alex")
    assert len(db.get_sets(limit=20)) == 1


def test_but_two_sets_asked_for_are_saved_twice(client, monkeypatch):
    one = {"exercise": "unknown", "weight": 30.0, "reps": 12, "rpe": None}
    monkeypatch.setattr(live, "parse_voice_log", lambda t, fast=False: {"sets": [dict(one), dict(one)], "safety_note": ""})
    live._log_sets("2 sets of 12 at 30 kilos", "hype", "Alex")
    assert len(db.get_sets(limit=20)) == 2


def test_the_numbers_said_win_over_the_models_guess(client, monkeypatch):
    cur = state(client)["current"]["name"]
    monkeypatch.setattr(live, "parse_voice_log", lambda t, fast=False: {"sets": [{"exercise": cur, "weight": 60.0, "reps": 8, "rpe": None}], "safety_note": ""})
    r = live._log_sets("same weight as before, sixty kilos, five reps, my last warmup was eight", "hype", "Alex")
    assert [(s["weight"], s["reps"]) for s in r["saved"]] == [(60.0, 5)]
    assert "8" not in r["reply"].replace(str(state(client)["remaining_sets"]), "")     # the reply speaks the logged reps, not the guessed ones


def test_reply_matches_the_log(client):
    st = state(client)
    cur = st["current"]
    r = say(client, "Sixty kilos, five reps, done.")
    low = r["reply"]
    assert "8 reps" not in low
    after = r["state"]
    assert after["remaining_sets"] == st["remaining_sets"] - 1
    assert after["exercises"][0]["done"] == len([s for s in db.get_sets(limit=50) if s["exercise"] == cur["name"]]) == 1


def test_the_model_is_told_exactly_what_is_logged(client):
    assert "none yet" in live.live_context()
    say(client, "Sixty kilos, five reps, done.")
    ctx = live.live_context()
    assert "sets logged today (exact, from the log): 1" in ctx and "none yet" not in ctx


# ---------------------------------------------------------------- mic noise
@pytest.mark.parametrize("noise", ["Boxing, wipes hands, adjust straps.", "*wipes hands*", "(takes a deep breath)", "Hmm.", "Thanks.", "Copy."])
def test_stage_directions_and_filler_are_ignored(client, noise):
    r = say(client, noise)
    assert r["intent"] in ("ignored", "noop") and r["reply"] == ""          # noop: the cleaner already dropped a lone "hmm"
    assert skipped(client) == [] and db.get_sets(limit=5) == []


def test_a_command_inside_stage_directions_is_still_heard(client):
    r = say(client, "Wipes hands. Sixty kilos, five reps, done.")
    assert r["intent"] == "set" and r["saved"][0]["reps"] == 5


def test_hello_gets_one_short_line(client):
    r = say(client, "Hello.")
    assert r["intent"] == "ready" and 0 < len(r["reply"]) < 120


# ---------------------------------------------------------------- the pure reader
PLAN = ["deadlift", "goblet squat", "leg extension", "leg curl", "hanging leg raise"]


@pytest.mark.parametrize("text, kind", [
    ("We are not skipping anything, we are doing every exercise.", "negated"),
    ("I am not skipping the goblet squat.", "negated"),
    ("What is the next exercise?", "question"),
    ("Skip this exercise, my shoulder hurts.", "skip"),
    ("No wait, put it back, I will do it.", "unskip"),
    ("No, skip this one", "skip"),
    ("don't skip it", "negated"),
    ("Skipping anything.", "negated"),
    ("should I skip bench?", "question"),
    ("next exercise please", "ask"),
    ("let's move on", "ask"),
    ("the machine is taken", "ask"),
    ("I can't do this", "ask"),
    ("Okay, next set.", None),
    ("Sixty kilos, five reps, done.", None),
    ("I skipped breakfast", None),
    ("skip everything", None),
    ("nahi, wapas laga do, main yeh karunga", "unskip"),
])
def test_skip_intent_reader(text, kind):
    assert set_slots.skip_intent(text, PLAN)["kind"] == kind, text


def test_skip_intent_names_the_exercise():
    assert set_slots.skip_intent("I am not skipping the goblet squat.", PLAN)["named"] == "goblet squat"
    assert set_slots.skip_intent("skip leg curl", PLAN)["named"] == "leg curl"


# ---------------------------------------------------------------- Hindi / Hinglish through the same reader
@pytest.mark.parametrize("hindi, kind", [
    ("यह वाला स्किप कर दो कंधा दुख रहा है", "skip"),   # "yeh wala skip kar do, kandha dukh raha hai"
    ("स्किप मत करो", "negated"),                                                           # "skip mat karo"
    ("अगला एक्सरसाइज कौन सा है", "question"),   # "agla exercise kaun sa hai"
])
def test_hindi_skip_phrases(client, hindi, kind):
    from backend.coach import hindi as H
    assert set_slots.skip_intent(H.normalize(hindi), PLAN)["kind"] == kind


def test_hindi_not_skip_does_not_skip(client):
    r = say(client, "स्किप मत करो")
    assert r["intent"] != "skip" and skipped(client) == []
