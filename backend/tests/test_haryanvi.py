import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.coach import hindi, lines, live, persona as P, voice
from backend.main import app
from backend.memory import db
from backend.policy.adaptation import has_safety_flag
from backend.tools.demo import PROFILE
from backend.tools.foods import parse_meal_text
from backend.tools.voice_parser import _fallback


# ---------------- understanding Hindi / Haryanvi speech ----------------
@pytest.mark.parametrize("spoken,expected", [
    ("बेंच प्रेस अस्सी किलो आठ बार", "bench press 80 kg for 8"),
    ("स्क्वाट एक सौ दस किलो पाँच बार आरपीई नौ", "squat 110 kg for 5 rpe 9"),
    ("डेडलिफ्ट एक सौ बीस किलो तीन बार", "deadlift 120 kg for 3"),
    ("बेंच प्रेस ८० किलो ८ बार", "bench press 80 kg for 8"),                # Devanagari digits
    ("ओवरहेड प्रेस साठ किलो दस बार बहुत भारी", "overhead press 60 kg for 10 grinder"),
    ("काम खतम", "wrap up"),
    ("मेरे पास सिर्फ तीस मिनट हैं", "मेरे पास only 30 minutes हैं"),
])
def test_normalize_hindi_to_parseable_english(spoken, expected):
    assert hindi.normalize(spoken) == expected


def test_normalize_does_not_corrupt_look_alike_words():
    assert hindi.normalize("बारह रेप्स") == "for 12"                         # 'बारह' (twelve) must not be eaten by 'बार' (times); count moves after "for"
    assert hindi.normalize("बारबेल रो सत्तर किलो") == "barbell row 70 kg"
    assert hindi.normalize("अस्सी आठ") == "80 8"                              # adjacent numbers without 'सौ' stay separate
    assert hindi.normalize("डेढ़ किलो") == "1.5 kg"


def test_english_and_empty_pass_through_untouched():
    assert hindi.normalize("bench press 80 for 8") == "bench press 80 for 8"
    assert hindi.normalize("") == "" and hindi.normalize("   ") == "   "


def test_spoken_hindi_becomes_a_real_logged_set():
    s = _fallback(hindi.normalize("बेंच प्रेस अस्सी किलो आठ बार, हल्का"))["sets"][0]
    assert (s["exercise"], s["weight"], s["reps"], s["rpe"]) == ("bench press", 80.0, 8, 6.0)


def test_hindi_pain_words_are_safety_flags():
    for phrase in ("कंधे में दर्द सै", "घुटना दुखै सै", "पीठ में चोट लग गई", "आंखों के आगे चक्कर आ रे सैं", "हाथ सुन्न हो गया"):
        assert has_safety_flag(phrase), phrase
    assert has_safety_flag("आज घणा बढ़िया लाग रहा सै") is None


def test_hindi_meals_are_understood():
    items = {i["name"]: i for i in parse_meal_text(hindi.normalize("तीन रोटी और दाल और एक गिलास छाछ"))["items"]}
    assert items["roti"]["grams"] == 120 and "dal" in items and items["buttermilk"]["grams"] == 250


# ---------------- the coach persona ----------------
def test_haryanvi_persona_is_registered_with_hindi_voice_hints():
    p = P.get("haryanvi")
    assert p["name"] == "Pehalwan" and p["tts"]["lang"] == "hi-IN"
    assert "Haryanvi" in P.system_prompt("haryanvi") and "Devanagari" in P.system_prompt("haryanvi")
    assert {x["id"] for x in P.listing()} == {"hype", "mentor", "straight", "haryanvi", "roast"}
    assert all(x["tts"]["lang"] == "en-US" for x in P.listing() if x["id"] not in ("haryanvi", "roast"))


def test_localisation_swaps_english_for_devanagari():
    done = lines.pick("haryanvi", "ex_done", ex="bench press", nxt="barbell row", nsets=4, nreps=8)
    assert "बेंच प्रेस" in done and "बारबेल रो" in done and "bench" not in done
    assert "दो मिनट" in lines.pick("haryanvi", "rest_cue", rest=lines.rest_text(120)) or "दो मिनट" in lines.pick("haryanvi", "rest_cue", rest="two minutes")
    assert "किलो पे 5 बार" in lines.pick("haryanvi", "last_time", ex="squat", last="100 for 5")
    assert lines.prs_phrase("haryanvi", ["deadlift"]) == "डेडलिफ्ट में एक पीआर" and lines.prs_phrase("hype", ["deadlift"]) == "a PR on deadlift"
    greetings = [lines.pick("haryanvi", "open_ok", "Alex") for _ in range(30)]
    assert any(g.split()[0] in ("सुबह", "दोपहर", "शाम") for g in greetings)           # time of day is spoken in Hindi...
    assert not any(w in g for g in greetings for w in ("Morning", "Afternoon", "Evening"))   # ...never in English


def test_haryanvi_lines_actually_use_haryanvi_not_plain_hindi():
    text = " ".join(l for opts in lines.BANKS["haryanvi"].values() for l in opts)
    for marker in ("सै", "कोनी", "घणा", "तनै", "धोरे", "कित"):
        assert marker in text, marker


# ---------------- a whole session, spoken in Haryanvi ----------------
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    c = TestClient(app)
    c.post("/onboard", json={**PROFILE, "name": "Rohit", "coach_persona": "haryanvi"})
    return c


def say(c, text, ambient=False):
    return c.post("/live/say", json={"transcript": text, "ambient": ambient}).json()


def test_session_opens_in_haryanvi_with_the_first_exercise_in_devanagari(client):
    r = client.post("/live/start").json()
    assert hindi.has_devanagari(r["reply"]) and "राम राम" in r["reply"] or "सै" in r["reply"]
    assert any(name in r["reply"] for name in lines.HI_EX.values())            # e.g. बेंच प्रेस, not "bench press"


def test_a_set_spoken_in_hindi_is_logged_and_answered_in_haryanvi(client):
    first = client.get("/live/state").json()["current"]["name"]
    r = say(client, "बेंच प्रेस अस्सी किलो आठ बार")
    assert r["intent"] == "set" and r["saved"][0]["weight"] == 80 and r["saved"][0]["reps"] == 8
    assert r["saved"][0]["exercise"] == ("bench press" if first == "bench press" else r["saved"][0]["exercise"])
    assert hindi.has_devanagari(r["reply"]) and r["actions"][0]["type"] == "rest"


def test_commands_work_in_hindi(client):
    assert say(client, "सिर्फ तीस मिनट हैं")["intent"] == "fit"
    assert say(client, "ये छोड़ दे")["intent"] == "skip"
    assert say(client, "चल")["intent"] == "ready"
    end = say(client, "काम खतम")
    assert end["intent"] == "end" and hindi.has_devanagari(end["reply"])


def test_hindi_pain_is_caught_and_answered_with_care(client):
    r = say(client, "कंधे में दर्द हो रह्या सै")
    assert r["safety_flag"] and hindi.has_devanagari(r["reply"]) and r["actions"] == []
    assert any(m["kind"] == "injury" for m in db.get_memory(5))


def test_every_reply_type_stays_in_haryanvi_no_english_leaks(client):
    replies = [
        client.post("/live/start").json()["reply"], say(client, "बेंच प्रेस साठ किलो आठ बार")["reply"], say(client, "आगे क्या")["reply"],
        say(client, "ready")["reply"] if False else say(client, "तैयार")["reply"], client.post("/live/cue", json={"event": "rest_over"}).json()["reply"],
        say(client, "काम खतम")["reply"], say(client, "बकवास कुछ भी")["reply"],
    ]
    for r in replies:
        assert hindi.has_devanagari(r), r


def test_ambient_listening_ignores_hindi_chatter_but_hears_the_coach_name_and_questions(client):
    assert say(client, "अरे भाई चाय पीले यार", ambient=True)["intent"] == "ignored"
    assert say(client, "पहलवान आज कितना बाकी सै", ambient=True)["intent"] != "ignored"
    assert say(client, "बेंच प्रेस अस्सी किलो आठ बार", ambient=True)["intent"] == "set"
    assert say(client, "कंधे में दर्द", ambient=True)["intent"] == "pain"


def test_pehalwan_can_be_chosen_through_the_api(client):
    assert client.post("/coach", json={"persona": "mentor"}).json()["coach_name"] == "Sage"
    r = client.post("/coach", json={"persona": "haryanvi"}).json()
    assert r["coach_name"] == "Pehalwan" and r["tts"]["lang"] == "hi-IN"
    assert client.post("/profile", json={"coach_persona": "haryanvi"}).status_code == 200


def test_hindi_meal_logging_through_the_api(client):
    client.post("/meal", json={"transcript": "तीन रोटी और दाल"})
    names = {m["name"] for m in client.get("/nutrition/today").json()["meals"]}
    assert {"roti", "dal"} <= names


# ---------------- ElevenLabs: speak it as Hindi ----------------
@pytest.fixture()
def eleven(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "e.db"))
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    seen = []

    def handler(request):
        if request.url.path.endswith("/voices"):
            return httpx.Response(200, json={"voices": [{"voice_id": "v_dan", "name": "Daniel", "category": "premade"}]})
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=b"ID3x")
    monkeypatch.setattr(voice, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    voice.reset()
    yield seen
    voice.reset()


def test_haryanvi_asks_elevenlabs_to_enforce_hindi_and_other_personas_do_not(eleven):
    voice.synthesize("राम राम भाई!", "haryanvi")
    voice.synthesize("Nice set.", "hype")
    assert eleven[0]["language_code"] == "hi" and "language_code" not in eleven[1]
    assert eleven[0]["model_id"] == "eleven_flash_v2_5"


def test_language_is_part_of_the_cache_key(eleven):
    a = voice.synthesize("Same text.", "haryanvi")
    b = voice.synthesize("Same text.", "straight")      # same voice (Daniel) and text, different language setting
    assert a.path != b.path and len(eleven) == 2


def test_test_endpoint_works_for_the_new_persona(eleven):
    r = TestClient(app).get("/tts/test", params={"persona": "haryanvi"}).json()
    assert r["ok"] and r["voice"] == "Daniel" and hindi.has_devanagari(r["sample"])
