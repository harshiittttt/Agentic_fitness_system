"""Ustaad, the rude Hindi roaster: complete, in Hindi, and safe. The act stops at pain; it never attacks the person."""
import re

import pytest
from fastapi.testclient import TestClient

from backend.coach import hindi, lines, persona as P, voice
from backend.main import app
from backend.memory import db
from backend.tools.demo import PROFILE

BANK = lines.BANKS["roast"]

# Things a roaster must never joke about: the body, weight, looks. (Devanagari stems; matched as substrings.)
BODY_SHAMING = ["मोटा", "मोटू", "मोटे", "मोटापा", "दुबला", "पतला", "सूखा", "काला", "गोरा", "बदसूरत", "लंगड़", "अंधा", "बहरा", "पागल"]
ABUSE = ["माँ ", "बहन ", "मां ", "बाप", "हरामी", "कमीना"]
IN_CHARACTER = ["निकम्मा", "बहाने", "आलस", "परवाह", "ड्रामा", "डर"]


def test_every_moment_of_a_session_has_a_roast_line():
    assert set(BANK) == set(lines.SLOTS)
    assert all(len(v) >= 1 and all(isinstance(x, str) and x for x in v) for v in BANK.values())


def test_all_lines_are_devanagari_with_no_roman_letters():
    for key, options in BANK.items():
        for o in options:
            assert not re.search(r"[A-Za-z]", re.sub(r"\{[a-z_]+\}", "", o)), (key, o)


def test_every_line_fills_its_slots_without_error():
    ctx = dict(day="Upper A", cnt=5, ex="bench press", w=80, r=8, nw=82.5, nr=8, rest="two minutes", nxt="barbell row", nsets=4, nreps=8, n=2, tot=4,
               sets=18, exn=6, tonnes=7.2, prs="a PR on squat", prot=40, min=30, rem=2, pr_ex="squat", last="100 for 5", done=6)
    for key in lines.SLOTS:
        for _ in range(4):
            out = lines.pick("roast", key, "Rohit", **{k: v for k, v in ctx.items() if k in lines.SLOTS[key]})
            assert out and "{" not in out, (key, out)


def test_it_roasts_effort_but_never_the_body_or_the_person_s_family():
    text = " ".join(l for opts in BANK.values() for l in opts)
    assert not [w for w in BODY_SHAMING if w in text]
    assert not [w for w in ABUSE if w in text]
    assert sum(w in text for w in IN_CHARACTER) >= 4                 # it really is rude and dismissive, not a nice coach in disguise


def test_the_act_stops_at_pain_no_insults_just_stop_and_see_a_professional():
    for line in BANK["pain"]:
        assert "रुक" in line and "दर्द" in line
        assert not any(w in line for w in ("निकम्मा", "बहाने", "आलस", "ड्रामा", "डर"))
    assert any("डॉक्टर" in l for l in BANK["pain"])


def test_system_prompt_carries_the_guardrails():
    s = P.system_prompt("roast", "Rohit")
    assert "Devanagari" in s and "DROP THE ACT" in s and "never invent numbers" in s.lower()
    assert "no slurs" in s and "weight, body, looks" in s


def test_persona_is_hindi_with_a_deep_hindi_voice_hint():
    p = P.get("roast")
    assert p["name"] == "Ustaad" and p["lang"] == "hi" and p["tts"]["lang"] == "hi-IN" and p["tts"]["pitch"] < 1.0
    assert "roast" in {x["id"] for x in P.listing()}


def test_elevenlabs_voice_choice_is_hindi_enforced_and_resolvable():
    assert voice.PREFERRED["roast"][0] == "Callum" and voice.LANG["roast"] == "hi"
    assert all(n in voice.FALLBACK_IDS for n in voice.PREFERRED["roast"])                   # works even with a restricted key
    assert voice.SETTINGS["roast"]["style"] > voice.SETTINGS["haryanvi"]["style"]            # more attitude than the akhada coach
    assert hindi.has_devanagari(voice.SAMPLES["roast"])


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "LLM_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    c = TestClient(app)
    assert c.post("/onboard", json={**PROFILE, "name": "Rohit", "coach_persona": "roast"}).status_code == 200
    return c


def say(c, text):
    return c.post("/live/say", json={"transcript": text, "ambient": False}).json()


def test_a_whole_session_with_ustaad_is_in_hindi(client):
    assert client.get("/coach").json()["persona"] == "roast" and client.get("/coach").json()["tts"]["lang"] == "hi-IN"
    opening = client.post("/live/start").json()["reply"]
    assert hindi.has_devanagari(opening)
    r = say(client, "बेंच प्रेस अस्सी किलो आठ बार")
    assert r["intent"] == "set" and r["saved"][0]["weight"] == 80 and hindi.has_devanagari(r["reply"])
    assert hindi.has_devanagari(say(client, "काम खतम")["reply"])


def test_pain_in_a_session_gets_the_serious_answer_and_is_remembered(client):
    r = say(client, "कंधे में दर्द हो रहा है")
    assert r["safety_flag"] and "रुक" in r["reply"] and r["actions"] == []
    assert any(m["kind"] == "injury" for m in db.get_memory(5))


def test_other_personas_are_unchanged():
    assert P.get("haryanvi")["name"] == "Pehalwan" and P.get("hype")["name"] == "Max"
    assert lines.HI_PERSONAS == {"haryanvi", "roast"}
