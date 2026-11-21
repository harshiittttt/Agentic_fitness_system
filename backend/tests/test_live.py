import pytest
from fastapi.testclient import TestClient

from backend.coach import live, lines, persona as P
from backend.main import app
from backend.memory import db
from backend.tools import exercises as ex
from backend.tools.demo import PROFILE

DUMMY = {k: "x" for k in ("day", "cnt", "ex", "w", "r", "pr_ex", "nw", "nr", "rest", "nxt", "nsets", "nreps", "n", "tot",
                          "sets", "exn", "tonnes", "prs", "prot", "min", "rem", "last", "done")}


# ---------------- personality banks ----------------
def test_every_persona_covers_every_moment():
    for pid in P.PERSONAS:
        assert set(lines.BANKS[pid]) == set(lines.SLOTS), pid


def test_every_line_only_uses_allowed_slots():
    for pid, bank in lines.BANKS.items():
        for key, options in bank.items():
            assert options, (pid, key)
            for o in options:
                used = {f for _, f, _, _ in __import__("string").Formatter().parse(o) if f}
                assert used <= lines.SLOTS[key] | lines.COMMON, (pid, key, o)
                o.format(**DUMMY, nm="", tod="Morning")   # must not raise


def test_never_repeats_the_same_line_back_to_back():
    for key, options in lines.BANKS["hype"].items():
        if len(options) < 2:
            continue
        prev = None
        for _ in range(40):
            cur = lines.pick("hype", key, **{k: "x" for k in lines.SLOTS[key]})
            assert cur != prev, key
            prev = cur


def test_personas_sound_different_and_use_the_name():
    samples = {pid: lines.BANKS[pid]["sample"][0] for pid in P.PERSONAS}
    assert len(set(samples.values())) == len(P.PERSONAS) == 5
    assert any("Alex" in lines.pick("hype", "open_ok", "Alex") for _ in range(30))


def test_unknown_persona_falls_back_to_default():
    assert "Max" in P.get("nonsense")["name"]
    assert "Sage" in P.system_prompt("mentor") and "NEVER invent numbers" in P.system_prompt("mentor")


# ---------------- name resolution ----------------
def test_resolve_exercise():
    plan = ["bench press", "overhead press", "romanian deadlift"]
    assert ex.resolve_exercise("bench", plan) == "bench press"
    assert ex.resolve_exercise("ohp", plan) == "overhead press"
    assert ex.resolve_exercise("rdl", plan) == "romanian deadlift"
    assert ex.resolve_exercise("something odd", plan) == "something odd"   # unknown stays as spoken


# ---------------- polish guard ----------------
def test_polish_only_accepts_rewrites_that_keep_the_numbers():
    assert live.numbers_preserved("Go 82.5 for 8", "Alright, 82.5 for 8 it is")
    assert not live.numbers_preserved("Go 82.5 for 8", "Alright, 85 for 8 it is")
    assert live.polish("Go 82.5 for 8", "hype", "") == "Go 82.5 for 8"   # no API key -> untouched


# ---------------- live session ----------------
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = TestClient(app)
    c.post("/onboard", json={**PROFILE, "name": "Alex", "coach_persona": "hype"})
    return c


def say(client, text):
    return client.post("/live/say", json={"transcript": text}).json()


def test_start_names_the_day_and_first_exercise(client):
    r = client.post("/live/start").json()
    first = r["state"]["current"]["name"]
    assert r["reply"] and first in r["reply"].lower() and r["state"]["day"].lower() in r["reply"].lower()


def test_coming_back_mid_session_resumes_instead_of_greeting_like_a_new_morning(client):
    first = client.post("/live/start").json()
    assert "back already" not in first["reply"].lower()
    name = first["state"]["current"]["name"]
    say(client, f"{'bench' if name == 'bench press' else name} 80 for 8 rpe 7")
    again = client.post("/live/start").json()
    low = again["reply"].lower()
    assert again["state"]["current"]["name"] in low and "1" in low                       # names where you are and the sets done
    assert not any(w in low for w in ("morning", "afternoon", "evening", "check-in", "let's go on"))   # no fresh-session greeting
    assert any(w in low for w in ("back", "welcome"))


def test_logging_a_set_resolves_names_advances_state_and_starts_rest(client):
    first = client.get("/live/state").json()["current"]["name"]
    spoken = "bench" if first == "bench press" else first
    r = say(client, f"{spoken} 80 for 8 rpe 7")
    assert r["intent"] == "set" and r["saved"][0]["exercise"] == first
    assert r["state"]["current"]["set_no"] == 2
    assert r["actions"] and r["actions"][0]["type"] == "rest" and r["actions"][0]["seconds"] >= 75
    assert r["next_set"] and r["reply"]


def test_finishing_an_exercise_moves_on_and_names_the_next_one(client):
    st = client.get("/live/state").json()
    first, sets = st["current"]["name"], st["current"]["sets"]
    nxt = st["exercises"][1]["name"]
    for _ in range(sets):
        r = say(client, f"{first} 60 for 6 rpe 8")
    assert r["state"]["current"]["name"] == nxt
    assert nxt in r["reply"].lower()


def test_pr_needs_history_and_a_real_improvement(client):
    assert say(client, "bench press 80 for 8 rpe 8")["pr"] is False          # first ever: no fake PR
    for i in range(3):
        db.add_set(f"2020-01-0{i + 1}", "squat", 100, 8, 8)
    r = say(client, "squat 130 for 8 rpe 8")
    assert r["pr"] is True and any(w in r["reply"].lower() for w in ("pr", "personal", "best", "record"))
    assert say(client, "squat 100 for 8 rpe 8")["pr"] is False


def test_skip_moves_to_the_next_exercise(client):
    st = client.get("/live/state").json()
    r = say(client, "skip this one")
    assert r["intent"] == "skip"
    assert r["state"]["current"]["name"] == st["exercises"][1]["name"]
    assert r["state"]["exercises"][0]["skipped"] is True


def test_short_on_time_refits_today_not_the_plan(client):
    before = client.get("/plan").json()
    r = say(client, "I only have 25 minutes")
    assert r["intent"] == "fit"
    after = client.get("/plan").json()
    assert after["today_override"]["day"]["est_minutes"] <= 25
    assert after["days"] == before["days"]


def test_short_on_time_without_a_number_asks_how_long(client):
    assert say(client, "I'm short on time today")["intent"] == "ask_time"


def test_status_and_ready(client):
    s = say(client, "what's next")
    assert s["intent"] == "status" and str(s["state"]["remaining_sets"]) in s["reply"]
    assert say(client, "ready")["intent"] == "ready"


def test_wrap_up_gives_a_recap_with_real_numbers(client):
    say(client, "bench press 80 for 10 rpe 8")
    say(client, "bench press 80 for 10 rpe 8")
    r = say(client, "okay wrap it up")
    assert r["intent"] == "end" and r["actions"] == [{"type": "end"}]
    assert "2 sets" in r["reply"]
    assert "1.6" in r["reply"]    # 2 x 80kg x 10 = 1.6 tonnes


def test_wrap_up_with_nothing_logged(client):
    assert "Nothing logged" in say(client, "I'm done")["reply"]


def test_pain_stops_everything_and_is_remembered(client):
    r = say(client, "bench 60 for 8, sharp pain in my shoulder")
    assert r["safety_flag"] and r["actions"] == []
    assert any(m["kind"] == "injury" for m in db.get_memory(5))
    r2 = say(client, "my elbow hurts")
    assert r2["intent"] == "pain" and r2["reply"]


def test_unrecognised_speech_gets_a_helpful_nudge_offline(client):
    r = say(client, "what's the weather like")
    assert r["intent"] == "fallback" and r["reply"]


def test_rest_over_cue_names_the_next_set(client):
    first = client.get("/live/state").json()["current"]["name"]
    say(client, f"{first} 60 for 6 rpe 8")
    r = client.post("/live/cue", json={"event": "rest_over"}).json()
    assert first in r["reply"].lower() and "2" in r["reply"]


# ---------------- persona settings ----------------
def test_persona_is_persisted_and_used(client):
    assert client.post("/coach", json={"persona": "mentor"}).json()["coach_name"] == "Sage"
    lines._last.clear()
    say(client, "bench press 80 for 8 rpe 8")
    used = {k[0] for k in lines._last}
    assert used == {"mentor"}


def test_coach_settings_validation(client):
    assert client.post("/coach", json={"persona": "robot"}).status_code == 422
    assert client.post("/coach", json={"name": "Sam"}).json()["user_name"] == "Sam"


def test_coach_needs_setup_first(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "empty.db"))
    assert TestClient(app).post("/coach", json={"persona": "mentor"}).status_code == 400


# ---------------- hands-free (ambient) mode ----------------
def say_ambient(client, text):
    return client.post("/live/say", json={"transcript": text, "ambient": True}).json()


def test_ambient_ignores_gym_chatter(client):
    for chatter in ("yeah I'll text you later", "this song is fire", "dude pass me that towel"):
        r = say_ambient(client, chatter)
        assert r["intent"] == "ignored" and r["reply"] == "", chatter


def test_ambient_still_hears_sets_commands_and_pain(client):
    assert say_ambient(client, "bench press 80 for 8 rpe 8")["intent"] == "set"
    assert say_ambient(client, "what's next")["intent"] == "status"
    assert say_ambient(client, "my elbow hurts")["intent"] == "pain"
    assert say_ambient(client, "wrap it up")["intent"] == "end"


def test_ambient_answers_when_addressed_by_name_or_question(client):
    assert say_ambient(client, "hey Max how am I doing")["intent"] != "ignored"
    assert say_ambient(client, "should I go heavier")["intent"] != "ignored"


# ---------------- spoken-text hygiene ----------------
def test_tidy_fixes_plurals_and_sentence_starts():
    assert lines.tidy("I trimmed it to 1 exercises and 1 sets.") == "I trimmed it to 1 exercise and 1 set."
    assert lines.tidy("Breathe. two minutes, then back at it.") == "Breathe. Two minutes, then back at it."
    assert lines.tidy("Skipping barbell row. overhead press next.") == "Skipping barbell row. Overhead press next."
    assert lines.tidy("10 exercises and 11 sets stay as they are.") == "10 exercises and 11 sets stay as they are."


def test_every_line_in_every_bank_is_clean_after_filling():
    import re
    for pid, bank in lines.BANKS.items():
        for key, options in bank.items():
            for o in options:
                out = lines.tidy(o.format(**{k: "1" for k in ("cnt", "sets")}, **{k: "bench press" for k in lines.SLOTS[key] - {"cnt", "sets"}}, nm="", tod="Morning"))
                assert not re.search(r"(^|[.!?]\s+)[a-z]", out), (pid, key, out)
                assert "1 exercises" not in out and "1 sets" not in out, (pid, key, out)


def test_short_session_keeps_more_than_one_movement():
    from backend.tools.exercises import fit_to_time
    day = {"name": "Upper A", "exercises": [
        {"name": "bench press", "sets": 4, "reps": 6, "target_rpe": 8}, {"name": "barbell row", "sets": 4, "reps": 8, "target_rpe": 8},
        {"name": "overhead press", "sets": 3, "reps": 8, "target_rpe": 8}, {"name": "curls", "sets": 2, "reps": 12, "target_rpe": 8}]}
    f = fit_to_time(day, 20)
    assert f["est_minutes"] <= 20 and len(f["exercises"]) >= 2
    assert f["exercises"][0]["name"] == "bench press"


def test_plain_set_log_is_parsed_without_the_model(monkeypatch):
    from backend.tools import voice_parser as vp
    monkeypatch.setattr(vp, "have_key", lambda: True)
    monkeypatch.setattr(vp, "extract_structured", lambda *a, **k: (_ for _ in ()).throw(AssertionError("model must not be called")))
    r = vp.parse_voice_log("bench press 60 kilos for 8")
    assert r["sets"] == [{"exercise": "bench press", "weight": 60.0, "reps": 8}]
    r = vp.parse_voice_log("squat 100 for 5 rpe 8")
    assert r["sets"][0]["reps"] == 5 and r["sets"][0]["rpe"] == 8.0


def test_anything_the_rules_cannot_fully_explain_still_goes_to_the_model(monkeypatch):
    from backend.tools import voice_parser as vp
    calls = []
    monkeypatch.setattr(vp, "have_key", lambda: True)
    monkeypatch.setattr(vp, "extract_structured", lambda *a, **k: calls.append(1) or {"sets": [{"exercise": "squat", "weight": 100, "reps": 5}]})
    for t in ["squat 100 for 5 then 3 more", "bench 135 lbs for 8", "squat 100 for 5 my knee hurts", "did squats, one oh five for five"]:
        vp.parse_voice_log(t)
    assert len(calls) == 4


# ---------------- the workout screen's dials: POST /live/log ----------------
def test_live_log_saves_the_set_and_answers_at_once(client, monkeypatch):
    from backend.coach import compose
    monkeypatch.setattr(compose, "compose", lambda *a, **k: pytest.fail("a button tap must not wait for the composed line"))
    st = client.get("/live/state").json()
    first = st["current"]["name"]
    r = client.post("/live/log", json={"weight": 60, "reps": 8, "rpe": 8})
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "set" and body["reply"] and body["saved"][0]["exercise"] == first
    assert body["state"]["exercises"][0]["done"] == 1
    assert any(a["type"] == "rest" for a in body["actions"]) or body["state"]["finished"]
    assert any(s["exercise"] == first and s["weight"] == 60 and s["reps"] == 8 for s in db.get_sets(first, 5))


def test_live_log_for_a_chosen_exercise_and_validation(client):
    st = client.get("/live/state").json()
    other = st["exercises"][1]["name"]
    r = client.post("/live/log", json={"exercise": other, "weight": 40, "reps": 10})
    assert r.json()["saved"][0]["exercise"] == other
    assert client.post("/live/log", json={"weight": 40, "reps": 0}).status_code == 422
    assert client.post("/live/log", json={"weight": -5, "reps": 5}).status_code == 422


def test_live_say_fast_skips_the_composer(client, monkeypatch):
    from backend.coach import compose
    monkeypatch.setattr(compose, "compose", lambda *a, **k: pytest.fail("fast must not compose"))
    r = client.post("/live/say", json={"transcript": "skip this exercise", "fast": True}).json()
    assert r["intent"] == "skip" and r["reply"]


def test_skip_by_name_skips_that_exercise_not_the_current_one(client):
    st = client.get("/live/state").json()
    cur, other = st["exercises"][0]["name"], st["exercises"][2]["name"]
    r = client.post("/live/say", json={"transcript": f"skip {other}", "fast": True}).json()
    assert r["intent"] == "skip"
    after = client.get("/live/state").json()
    assert next(e for e in after["exercises"] if e["name"] == other)["skipped"] is True
    assert next(e for e in after["exercises"] if e["name"] == cur)["skipped"] is False
    assert after["current"]["name"] == cur
