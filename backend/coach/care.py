"""Care layer: the one place where EVERY persona stops being a character.

Deterministic and ahead of the model, so no prompt, persona or clever phrasing can switch it off. It catches two things the docs
treat as Tier-4 (stop and refer): signs of disordered eating (restricting food, purging, punishing the body) and crisis language
(thoughts of self-harm or not wanting to live). It answers seriously, in the persona's language, and records the cue so the
watchdogs freeze nutrition coaching. It never gives medical advice; it points to people who can help.
"""
from __future__ import annotations

EATING_EN = ("eating less to", "eat less to", "not eating to", "skip meals", "skipping meals", "starving", "starve myself", "purge", "throw up after",
             "throwing up after", "make myself throw up", "laxative", "punish myself", "hate my body", "don't deserve to eat", "guilty for eating",
             "stop eating", "barely eating", "only eating")
EATING_HI = ("खाना कम खा", "कम खाना खा", "कम खा रहा", "कम खा रही", "भूखा रह", "भूखे रह", "खाना छोड़", "खाना नहीं खा", "पतला हो जाऊँ", "पतला हो जाऊं",
             "पतला होने", "उल्टी कर", "खुद को सज़ा", "खुद को सजा", "अपने शरीर से नफरत", "खाने का गिल्ट")
# 'only eating' / 'stop eating' alone are too broad for non-restriction use, so they need a weight-loss cue next to them.
_NEEDS_CUE = {"stop eating", "barely eating", "only eating"}
_CUES = ("thin", "skinny", "lose weight", "weight loss", "fat", "slim", "पतला", "वजन घटा")

CRISIS_EN = ("kill myself", "killing myself", "want to die", "wanna die", "end my life", "end it all", "hurt myself", "hurting myself", "harm myself",
             "harming myself", "self harm", "self-harm", "suicide",
             "suicidal", "no reason to live", "don't want to live", "dont want to live", "better off dead")
CRISIS_HI = ("मरना चाहता", "मरना चाहती", "जान दे दूं", "जान दे दूँ", "खुद को खत्म", "खुद को नुकसान", "आत्महत्या", "जीने का मन नहीं", "जीना नहीं चाहता",
             "जीना नहीं चाहती", "मर जाऊं", "मर जाऊँ")

REPLIES = {
    ("eating", False): ("I'm dropping the jokes. Eating far less to get thinner isn't something I'll coach, and it can really hurt you. "
                        "Please talk to a doctor or a dietitian, and tell someone you trust how you're feeling. I'll keep training with you, but not on an empty tank, "
                        "and I'm pausing calorie changes."),
    ("eating", True): ("मज़ाक बंद। खाना कम करके पतला होना मैं नहीं सिखाऊँगा, इससे सच में नुकसान हो सकता है। किसी डॉक्टर या डाइटीशियन से बात कर, "
                       "और जिस पर भरोसा हो उसे बता कि तू कैसा महसूस कर रहा है। ट्रेनिंग मैं तेरे साथ करूँगा, पर भूखे पेट नहीं, और कैलोरी में बदलाव अभी रोक रहा हूँ।"),
    ("crisis", False): ("I'm stopping everything else, because what you just said matters more than any workout. You don't have to go through this alone: "
                        "please talk to someone you trust right now, or call your local emergency number or a crisis line. In India you can call Tele-MANAS on 14416. "
                        "If you might act on it, please get help in person right away."),
    ("crisis", True): ("मैं बाकी सब रोक रहा हूँ, क्योंकि तूने जो कहा वो किसी भी वर्कआउट से ज़्यादा ज़रूरी है। तुझे ये अकेले नहीं झेलना है: "
                       "अभी किसी भरोसे वाले इंसान से बात कर, या अपने इलाके का इमरजेंसी नंबर या हेल्पलाइन मिला। भारत में तू टेली-मानस पर 14416 पर कॉल कर सकता है। "
                       "अगर तुझे लगे कि तू खुद को नुकसान पहुँचा सकता है, तो अभी किसी के पास जा या मदद बुला।"),
}


def check(text: str) -> str | None:
    """'crisis' | 'eating' | None. Crisis wins when both appear."""
    t = " ".join(text.lower().split())
    if any(p in t for p in CRISIS_EN + CRISIS_HI):
        return "crisis"
    for p in EATING_EN:
        if p in t and (p not in _NEEDS_CUE or any(c in t for c in _CUES)):
            return "eating"
    if any(p in t for p in EATING_HI):
        return "eating"
    return None


def reply(kind: str, hindi: bool) -> str:
    return REPLIES[(kind, hindi)]


def handle(text: str, hindi: bool) -> str | None:
    """If the message needs care: record it, trigger the watchdogs, and return the serious reply. Otherwise None."""
    from backend.memory import db

    kind = check(text)
    if not kind:
        return None
    db.add_checkin(note=text)                                                     # the watchdogs read check-in notes
    db.add_memory("semantic", f"Care flag ({kind}): the athlete said something that needs a gentle, serious reply, not banter.")
    try:                                                                          # freeze nutrition coaching right now, not at tomorrow's tick
        from fitness_os import control, ledger, watchdogs
        from fitness_os.schema import Proposal
        frozen = ledger.frozen_domains()
        if kind == "eating" and "nutrition" not in frozen and "all" not in frozen:   # direct, so it never depends on a second phrase list
            control.submit(Proposal(agent="watchdog", kind="escalate", params={"domain": "nutrition", "reason": "disordered-eating language in chat"},
                                    rationale="Language that can signal disordered eating. Calorie coaching is paused; please talk to a qualified professional.",
                                    evidence=text[:160], confidence=1.0), trigger="watchdog:care")
        for w in watchdogs.run_all():
            if not watchdogs.already_open(w):
                control.submit(w["proposal"], trigger="watchdog:care")
    except Exception:
        pass
    return reply(kind, hindi)
