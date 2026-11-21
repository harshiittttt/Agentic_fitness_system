"""Half-described sets: the coach asks for exactly what is missing, remembers the question, and logs the set when it is answered."""
import pytest
from fastapi.testclient import TestClient

from backend.coach import live, set_slots
from backend.main import app
from backend.memory import db
from backend.tools.demo import PROFILE

PLAN = ["bench press", "squat", "pull up", "romanian deadlift", "overhead press", "dips"]


# ---------------- parser ----------------
def test_spoken_numbers_become_digits_but_a_lone_one_stays_a_word():
    w = set_slots.words_to_digits
    assert w("eighty five kilos for eight") == "85 kilos for 8"
    assert w("one oh five for five") == "105 for 5"
    assert w("a hundred and ten for three") == "110 for 3"
    assert w("no one knows") == "no one knows"
    assert w("one set done") == "1 set done"


def test_extract_reads_weight_reps_sets_and_units():
    e = set_slots.extract
    assert e("8 reps")["r"] == 8 and e("8 reps")["w"] is None
    assert e("eighty kilos")["w"] == 80.0
    got = e("bench 3 sets of 8 at 80")
    assert (got["w"], got["r"], got["sets"], got["rpe"], got["invalid"], got["leftover"]) == (80.0, 8, 3, None, False, 0)
    assert e("135 lbs for 5")["w"] == 61.0                                   # pounds converted to kg
    assert e("1 set done")["sets"] == 1 and e("1 set done")["w"] is None    # the 1 is a set count, never a weight
    assert e("squat 100 for 5 then 3 more")["leftover"] == 1               # a number nothing explains: the caller must not guess


def test_a_bare_number_is_read_as_the_answer_to_what_was_asked():
    e = set_slots.extract
    assert e("80", ask={"w"})["w"] == 80.0
    assert e("eight", ask={"r"})["r"] == 8
    assert e("80 8", ask={"w", "r"}) | {} == {**e("80 8", ask={"w", "r"})} and e("80 8", ask={"w", "r"})["w"] == 80.0
    assert e("12", ask={"w"})["w"] == 12.0                                   # a 12 kg dumbbell is a weight when weight was asked


def test_absurd_numbers_are_rejected_not_logged():
    assert set_slots.extract("900 for 5")["invalid"] is True
    assert set_slots.extract("80 for 500")["invalid"] is True


def test_set_done_detection_ignores_questions_and_chatter():
    d = lambda t: set_slots.is_set_done(t, PLAN)
    assert d("1 set done") and d("finished my set") and d("I squatted") and d("done")
    assert not d("how many sets are left?") and not d("what should I do next") and not d("my phone is dead")


def test_find_exercise_uses_names_aliases_and_unique_words():
    f = lambda t: set_slots.find_exercise(t, PLAN)
    assert f("I did squats") == "squat" and f("bench felt good") == "bench press" and f("my rdl set") == "romanian deadlift"
    assert f("a press set") is None                                         # 'press' belongs to two exercises: do not guess


# ---------------- the live conversation ----------------
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    live._pending.clear()
    c = TestClient(app)
    c.post("/onboard", json={**PROFILE, "name": "Alex", "coach_persona": "straight"})
    c.post("/live/start")
    return c


def say(client, text):
    return client.post("/live/say", json={"transcript": text}).json()


def _current(client):
    return client.get("/live/state").json()["current"]["name"]


def test_set_done_with_nothing_else_asks_for_weight_and_reps(client):
    cur = _current(client)
    r = say(client, "1 set done")
    assert r["intent"] == "ask" and r["expects"] == "set_details" and r["saved"] == []
    assert live._pending["ex"] == cur and live._pending["missing"] == {"w", "r"}
    assert client.get("/live/state").json()["current"]["set_no"] == 1       # nothing logged yet


def test_answering_the_question_logs_the_set_and_moves_on(client):
    cur = _current(client)
    say(client, "1 set done")
    r = say(client, "80 for 8")
    assert r["intent"] == "set" and r["saved"][0]["exercise"] == cur and r["saved"][0]["weight"] == 80 and r["saved"][0]["reps"] == 8
    assert not live._pending
    assert client.get("/live/state").json()["current"]["set_no"] == 2 or client.get("/live/state").json()["current"]["name"] != cur


def test_it_asks_only_for_what_is_still_missing_across_several_turns(client):
    cur = _current(client)
    r = say(client, f"I did {cur} 8 reps")
    assert r["intent"] == "ask" and live._pending["missing"] == {"w"} and live._pending["r"] == 8
    r = say(client, "seventy kilos")
    assert r["intent"] == "set" and r["saved"][0]["weight"] == 70 and r["saved"][0]["reps"] == 8


def test_weight_first_then_reps(client):
    say(client, "set done, 60 kilos")
    assert live._pending["w"] == 60.0 and live._pending["missing"] == {"r"}
    r = say(client, "10")
    assert r["intent"] == "set" and r["saved"][0]["reps"] == 10 and r["saved"][0]["weight"] == 60


def test_several_sets_are_logged_once_each(client):
    cur = _current(client)
    r = say(client, f"{cur} 3 sets of 8 at 60")
    assert r["intent"] == "set" and len(r["saved"]) == 3 and all(s["reps"] == 8 and s["weight"] == 60 for s in r["saved"])


def test_numbers_without_an_exercise_use_the_current_one(client):
    cur = _current(client)
    r = say(client, "I did 70 for 6")
    assert r["intent"] == "set" and r["saved"][0]["exercise"] == cur and r["saved"][0]["weight"] == 70


def test_cancel_drops_the_half_set(client):
    say(client, "1 set done")
    r = say(client, "never mind")
    assert r["intent"] == "ask_cancel" and not live._pending and r["saved"] == []


def test_a_question_while_waiting_is_not_swallowed(client):
    say(client, "1 set done")
    r = say(client, "what's next")
    assert r["intent"] == "status" and live._pending          # answered, and the open question is still there


def test_gibberish_is_asked_again_then_dropped(client):
    say(client, "1 set done")
    assert say(client, "umm")["intent"] == "noop" and live._pending                # a filler is not an answer: silence, the question stays open
    assert say(client, "blorp")["intent"] == "ask_unclear"
    assert say(client, "blorp blorp")["intent"] == "ask_unclear"
    assert say(client, "blorp blorp blorp")["intent"] != "ask_unclear" and not live._pending     # never trapped in the question forever


def test_a_complete_new_log_replaces_the_half_finished_one(client):
    cur = _current(client)
    say(client, "1 set done")
    r = say(client, f"{cur} 50 for 5")
    assert r["intent"] == "set" and r["saved"][0]["weight"] == 50 and not live._pending


def test_bodyweight_exercise_only_asks_for_reps(client):
    r = say(client, "pull up set done")
    assert r["intent"] == "ask" and live._pending["missing"] == {"r"} and live._pending["w"] == 0.0
    r = say(client, "9")
    assert r["intent"] == "set" and r["saved"][0]["reps"] == 9 and r["saved"][0]["exercise"] == "pull up"


def test_pain_wording_never_enters_the_set_flow(client):
    r = say(client, "set done but my knee hurts")
    assert r["intent"] == "pain" and not live._pending


def test_ordinary_chatter_is_left_alone(client):
    r = say(client, "I have 8 more minutes today")
    assert r["intent"] != "ask" and not live._pending


def test_pending_expires_with_the_session(client):
    say(client, "1 set done")
    client.post("/live/end")
    assert not live._pending


def test_reps_alone_asks_for_the_weight_without_any_model(client):
    cur = _current(client)
    r = say(client, "8 reps")
    assert r["intent"] == "ask" and live._pending["ex"] == cur and live._pending["missing"] == {"w"} and r["saved"] == []
    r = say(client, "seventy")
    assert r["intent"] == "set" and r["saved"][0]["weight"] == 70 and r["saved"][0]["reps"] == 8


def test_weight_alone_asks_for_the_reps(client):
    say(client, "seventy kilos")
    assert live._pending["w"] == 70.0 and live._pending["missing"] == {"r"}
    r = say(client, "6")
    assert r["intent"] == "set" and r["saved"][0]["reps"] == 6 and r["saved"][0]["weight"] == 70


def test_a_question_about_weights_is_not_taken_as_a_set(client):
    assert say(client, "should I do 80 kilos?")["intent"] != "ask"
    assert not live._pending


def test_the_extractor_flags_what_was_said_with_a_unit():
    e = set_slots.extract
    assert e("8 reps")["r_explicit"] and not e("for 8")["r_explicit"]
    assert e("seventy kilos")["w_explicit"] and not e("70")["w_explicit"]


def test_a_weight_on_a_bodyweight_exercise_is_kept(client):
    say(client, "pull up set done")
    r = say(client, "20 kilos for 6")
    assert r["intent"] == "set" and r["saved"][0]["weight"] == 20 and r["saved"][0]["reps"] == 6


def test_a_hinglish_set_is_understood_and_asked_for_what_is_missing(client):
    """Real session: 'maine ek set maar diya' -> weight? -> '30 kilo' -> reps? -> 'aath bar uthaya aath bar' must log 30 x 8 (it used to say 'did not understand' twice)."""
    cur = _current(client)
    r = say(client, f"theek hai yaar maine Ek set Maar Diya {cur} ka")
    assert r["intent"] == "ask" and r["expects"] == "set_details"
    r = say(client, "bhajan tha 30 kilo aur Maine uthaya")
    assert r["intent"] == "ask" and live._pending["w"] == 30
    r = say(client, "aath bar uthaya aath bar")
    assert r["intent"] == "set" and r["saved"][0]["weight"] == 30 and r["saved"][0]["reps"] == 8


def test_repeating_the_weight_when_asked_for_reps_does_not_log_that_many_reps(client):
    cur = _current(client)
    say(client, f"{cur} 30 kilo set done")
    r = say(client, "30")
    assert r["intent"] != "set" and live._pending
    assert say(client, "8 reps")["saved"][0]["reps"] == 8
