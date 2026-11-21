"""The care layer: every persona stops being a character for disordered-eating and crisis language, and normal talk never trips it."""
import pytest
from fastapi.testclient import TestClient

from backend.agents import orchestrator as orch
from backend.coach import care, hindi, live
from backend.main import app
from backend.memory import db
from backend.tools.demo import PROFILE
from fitness_os import ledger


@pytest.mark.parametrize("text,kind", [
    ("I'm skipping meals so I can get thin", "eating"),
    ("i have been starving myself all week", "eating"),
    ("I make myself throw up after dinner", "eating"),
    ("I'm eating less to lose weight fast", "eating"),
    ("I'm barely eating, I want to be skinny", "eating"),
    ("मैं खाना कम खा रहा हूँ ताकि पतला हो जाऊँ", "eating"),
    ("मुझे भूखा रहना पड़ेगा", "eating"),
    ("I want to die", "crisis"),
    ("sometimes I think about hurting myself", "crisis"),
    ("मुझे जीने का मन नहीं है", "crisis"),
    ("मैं मरना चाहता हूँ और खाना भी नहीं खा रहा", "crisis"),   # crisis wins
])
def test_cues_are_detected(text, kind):
    assert care.check(text) == kind


@pytest.mark.parametrize("text", [
    "bench press 80 for 8", "I'm eating 3 roti with dal", "I only have 30 minutes today", "my knee hurts when I squat",
    "I ate less than planned yesterday, sorry", "I stopped eating dairy because it upsets my stomach", "can I eat more protein?",
    "बेंच प्रेस अस्सी किलो आठ बार", "आज मैं थक गया हूँ", "दूध और अंडे खाए आज", "कंधे में दर्द है",
])
def test_normal_talk_never_trips_it(text):
    assert care.check(text) is None


def test_replies_are_serious_gentle_and_in_the_right_language():
    for kind in ("eating", "crisis"):
        en, hi = care.reply(kind, False), care.reply(kind, True)
        assert not hindi.has_devanagari(en) and hindi.has_devanagari(hi)
        assert not any(w in en.lower() for w in ("lazy", "excuse", "idiot", "weak")) and not any(w in hi for w in ("निकम्मा", "बहाने", "आलस"))
    assert "14416" in care.reply("crisis", True) and "14416" in care.reply("crisis", False)
    assert "doctor" in care.reply("eating", False) and "डॉक्टर" in care.reply("eating", True)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "LLM_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    return TestClient(app)


@pytest.mark.parametrize("persona,hi", [("hype", False), ("mentor", False), ("straight", False), ("haryanvi", True), ("roast", True)])
def test_every_persona_drops_the_act_in_a_live_session(client, persona, hi):
    client.post("/onboard", json={**PROFILE, "name": "Rohit", "coach_persona": persona})
    r = client.post("/live/say", json={"transcript": "मैं खाना कम खा रहा हूँ ताकि पतला हो जाऊँ" if hi else "I'm skipping meals to get thin", "ambient": False}).json()
    assert r["intent"] == "care" and r["safety_flag"] == "eating" and r["moment"] is True
    assert hindi.has_devanagari(r["reply"]) == hi
    assert "nutrition" in ledger.frozen_domains()                          # calorie coaching is paused immediately, not at tomorrow's tick
    assert any(m["kind"] == "semantic" and "Care flag" in m["text"] for m in db.get_memory(10))


def test_the_coach_tab_chat_gets_the_same_protection(client):
    client.post("/onboard", json={**PROFILE, "name": "Rohit", "coach_persona": "roast"})
    out = client.post("/chat", json={"message": "मैं खाना कम खा रहा हूँ ताकि पतला हो जाऊँ"}).json()["reply"]
    assert hindi.has_devanagari(out) and "डॉक्टर" in out and "निकम्मा" not in out


def test_a_crisis_message_is_answered_not_logged_as_a_set_or_banter(client):
    client.post("/onboard", json={**PROFILE, "name": "Rohit", "coach_persona": "roast"})
    r = client.post("/live/say", json={"transcript": "I want to die", "ambient": True}).json()    # even with the mic in ambient mode
    assert r["intent"] == "care" and r["safety_flag"] == "crisis" and r["saved"] == [] and "14416" in r["reply"]
