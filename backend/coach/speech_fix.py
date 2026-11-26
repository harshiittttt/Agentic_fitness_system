"""Cleaning what a speech recognizer returns before anything tries to understand it.

Real phone transcripts of a lifter mid-set look like: "I did like uhh one set of overhead press 15 wraps 20 kg", "10 raps", "okay I am done with Bomma bikers".
This module removes the filler, repairs the mishearings that are systematic (reps -> wraps / raps), and recognises a dangling half-sentence ("I did like")
so the coach waits for the rest instead of answering it.

    clean(text)        -> the same words, fillers removed, 'wraps/raps' -> 'reps'
    is_fragment(text)  -> True when the utterance is an unfinished sentence that should be continued, not answered
"""
from __future__ import annotations

import re

_FILLER = re.compile(r"(?<![A-Za-z])(?:u+m+|u+h+|e+r+m*|h+m+|m{2,}|a+h+|uh+h*|eh+)(?![A-Za-z])[,.]?\s*", re.I)
_NUM = r"(?:\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty)"
_REPS_AFTER_NUM = re.compile(rf"\b({_NUM})\s+(?:wraps?|raps?|rapes?|reaps?|reps?)\b", re.I)
_REPS_PLURAL = re.compile(r"\b(?:wraps|raps|rapes|reaps)\b", re.I)

# a sentence that stops on one of these is not finished
_DANGLING = {"like", "and", "so", "then", "the", "a", "an", "my", "of", "with", "for", "at", "to", "i", "i'm", "im", "just", "about", "was", "is", "have", "had",
             "did", "do", "that", "which", "also", "plus", "but", "because", "when", "if", "or", "on", "in", "it", "its", "it's", "some", "around", "kind", "sort",
             "basically", "actually", "well", "okay", "ok", "yeah", "need", "needs", "want", "wanna", "gonna", "supposed", "why", "whether", "while", "between", "after", "before"}
# verbs that cannot end a sentence: "do I need", "should I want" are always the start of a longer one, whatever word they begin with
_NEVER_END = {"need", "needs", "wanna", "gonna", "supposed", "whether"}
# endings that mean "and the rest is coming": "can you just tell me", "should I go till", "what is the"
_OPEN_END = re.compile(r"(?:\b(?:tell|give|show|let|help|ask|get|take|teach|remind)\s+me|\b(?:can|could|would|should|will|do|does|did|am|is|are|was|were)\s+(?:you|i|we|it|that|this)(?:\s+just)?|"
                       r"\b(?:till|until|between|towards?|against|instead of|because of|according to|how do i|what is the|what's the|where is the))$", re.I)
_STARTERS = re.compile(r"^(?:i|i'?ve|i'?m|we|my|the|so|and|then|okay|ok|well|yeah|just|like)\b", re.I)


# ---- Hinglish: Hindi spoken in Roman letters ("aath bar uthaya", "30 kilo", "bench press shuru kar"). The phone transcribes it that way (en-IN), and the
# parsers only knew English numbers and Devanagari. Numbers that are not English words are converted everywhere; ambiguous ones (ek, do) only next to a unit.
_HI_NUM = {"teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5, "chhe": 6, "chhah": 6, "cheh": 6, "saat": 7, "aath": 8, "ath": 8, "nau": 9, "das": 10,
           "gyarah": 11, "barah": 12, "terah": 13, "chaudah": 14, "pandrah": 15, "solah": 16, "satrah": 17, "atharah": 18, "unnis": 19, "bees": 20, "pachees": 25,
           "tees": 30, "chalis": 40, "pachaas": 50, "pachas": 50, "sattar": 70, "assi": 80, "nabbe": 90, "sau": 100}
_HI_NUM_AMBIG = {"ek": 1, "do": 2, "saath": 60, "sath": 60, "saadh": 60}   # 'do' is English ("do it"), 'saath' is "with": only when a unit follows
_UNIT = r"(?:set|sets|bar|baar|bari|dafa|times|rep|reps|kilo|kilos|kg|kgs)"
_HI_NUM_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(sorted(_HI_NUM, key=len, reverse=True)) + r")(?![A-Za-z])", re.I)
_HI_AMBIG_RE = re.compile(r"(?<![A-Za-z])(ek|do|saath|sath|saadh)(?=\s+" + _UNIT + r"(?![A-Za-z]))", re.I)
_HI_REPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:bar|baar|bari|dafa|times|rep)\b", re.I)
_DUP_REPS_RE = re.compile(r"(\b\d+(?:\.\d+)? reps\b)(.*?)\s*\b\1\b", re.I)               # "8 reps ... 8 reps": said twice, counted once
_HI_PHRASES = [                                                # (pattern, English the parsers know) - kept small and safe
    (r"\b(?:maar|kar|ho)\s*(?:diya|liya|di|li|gaya|gayi|gaye)\b|\bkhatam\b|\bpura\s+(?:kar|ho)\b", "done"),
    (r"\bshuru\s+(?:kar|karo|kariye|karte|karein|kardo)\b(?:\s+hain)?|\bstart\s+karo\b|\bchalu\s+kar(?:o)?\b", "start"),
    (r"\b(?:wajan|vajan|wazan|bhajan|wajn|vajn)\b", "weight"),
    (r"\bagla\b|\bagli\s+(?:wali|wala)\b", "next"),
    (r"\bchhod\s+(?:do|de)\b|\bskip\s+karo\b", "skip"),
    (r"\bkitn[aei]\b", "how much"),
]
_HI_PHRASES_RE = [(re.compile(p, re.I), r) for p, r in _HI_PHRASES]


_AGLA = re.compile(r"\ba{1,2}gl[ae]\b", re.I)
_ASKING = re.compile(r"\b(?:batao|bitao|bataiye|bata\s+do|dikhao|kya|kaun\s*sa|kaunsa|kaun)\b", re.I)
# the phone/Whisper hear these systematically wrong
_MISHEARD = [(re.compile(r"\bsquads\b", re.I), "squats"), (re.compile(r"\bsquad\b", re.I), "squat"), (re.compile(r"\brapes\b", re.I), "reps")]


def hinglish(t: str) -> str:
    """Roman-letter Hindi numbers and a few set-logging phrases -> what the English parsers understand. Plain English text passes through unchanged."""
    if _AGLA.search(t) and _ASKING.search(t):
        return "what is next"                        # "agla exercise batao / kaun sa hai" is a QUESTION about what is next, never an order to skip it
    for rx, rep in _MISHEARD:
        t = rx.sub(rep, t)
    t = _HI_AMBIG_RE.sub(lambda m: str(_HI_NUM_AMBIG[m.group(1).lower()]), t)
    t = _HI_NUM_RE.sub(lambda m: str(_HI_NUM[m.group(1).lower()]), t)
    t = _HI_REPS_RE.sub(lambda m: f"{m.group(1)} reps", t)
    for rx, rep in _HI_PHRASES_RE:
        t = rx.sub(rep, t)
    return t


def clean(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return t
    t = _FILLER.sub(" ", t)
    t = hinglish(t)
    t = _REPS_AFTER_NUM.sub(lambda m: f"{m.group(1)} reps", t)
    t = _REPS_PLURAL.sub("reps", t)
    t = _DUP_REPS_RE.sub(lambda m: m.group(1) + m.group(2), t)
    return re.sub(r"\s{2,}", " ", t).strip()


def is_fragment(text: str) -> bool:
    """'I did like', 'and then', 'so I', 'I just': too little to act on and ending on a word that needs a continuation."""
    t = clean(text)
    words = re.findall(r"[A-Za-z']+|\d+(?:\.\d+)?", t.lower())
    if not words:
        return False
    if any(w.replace(".", "").isdigit() for w in words):        # a number means there is content ('bench 60'): let the parsers see it
        return False
    if len(words) <= 8 and words[-1] in _NEVER_END:
        return True
    if len(words) <= 5 and words[-1] in _DANGLING and _STARTERS.match(t):     # 'undo that', 'what is that' are commands, not half-sentences
        return True
    if len(words) <= 8 and _OPEN_END.search(t.lower()):
        return True
    return False
