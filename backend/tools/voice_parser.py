"""Turn a spoken transcript ("bench 80 for 8, felt easy") into structured sets.

Uses Claude when an API key is present; falls back to a regex parser so the app
and tests work offline.
"""
from __future__ import annotations

import re

from backend.agents.base import extract_structured, have_key
from backend.policy.adaptation import has_safety_flag

LOG_TOOL = {
    "name": "log_sets",
    "description": "Record the sets the lifter described. Weights in kg unless unit says lb.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "exercise": {"type": "string"},
                        "weight": {"type": "number"},
                        "reps": {"type": "integer"},
                        "rpe": {"type": "number", "description": "1-10; infer from words like 'easy'(6), 'hard'(9), 'grinder'(10)"},
                        "note": {"type": "string"},
                    },
                    "required": ["exercise", "reps"],
                },
            },
            "safety_note": {"type": "string", "description": "Any pain/injury mentioned, else empty"},
        },
        "required": ["sets"],
    },
}

_WORD_RPE = {"easy": 6.0, "light": 6.0, "moderate": 7.5, "hard": 9.0, "heavy": 9.0, "grinder": 10.0, "brutal": 10.0}
_PATTERN = re.compile(
    r"(?P<ex>[a-z][a-z \-]*?)\s+(?:at\s+)?(?P<w>\d+(?:\.\d+)?)\s*(?:kgs?|kilos?|kilograms?|lbs?|pounds?)?\s*(?:for|x|times)\s*(?P<r>\d+)",
    re.I,
)


def _fallback(transcript: str) -> dict:
    text = transcript.lower()
    sets = []
    for m in _PATTERN.finditer(text):
        ex = re.sub(r"^(and|then|next|did|i did)\s+", "", m.group("ex").strip())
        s = {"exercise": ex, "weight": float(m.group("w")), "reps": int(m.group("r"))}
        rpe = re.search(r"rpe\s*(\d+(?:\.\d+)?)", text)
        if rpe:
            s["rpe"] = float(rpe.group(1))
        else:
            for word, val in _WORD_RPE.items():
                if word in text:
                    s["rpe"] = val
                    break
        sets.append(s)
    return {"sets": sets, "safety_note": ""}


def _post_process(out: dict, transcript: str) -> dict:
    """Deterministic safety net: weaker models sometimes drop RPE or the pain flag. Code fills both in."""
    for st in out.get("sets") or []:
        if st.get("rpe") is None:
            blob = f"{st.get('note') or ''} {transcript if len(out['sets']) == 1 else ''}".lower()
            for word, val in _WORD_RPE.items():
                if word in blob:
                    st["rpe"] = val
                    break
    if not out.get("safety_note") and has_safety_flag(transcript):
        out["safety_note"] = transcript
    return out


_LB_RE = re.compile(r"\b(lbs?|pounds?)\b")


def _clean_rule_parse(transcript: str) -> dict | None:
    """Fast path: a plain "bench press 60 for 8" needs no model. Only when the regex explains EVERY number in the sentence
    (nothing left over), there is no pain wording and no pounds (the model converts those): otherwise return None and let the model read it."""
    text = transcript.lower()
    out = _fallback(text)
    if not out["sets"] or has_safety_flag(transcript) or _LB_RE.search(text):
        return None
    leftover = re.sub(r"rpe\s*\d+(?:\.\d+)?", " ", _PATTERN.sub(" ", text))
    if re.search(r"\d", leftover):
        return None
    from backend.tools import exercises as _ex       # the "exercise" must really be one ("i did 70 for 6" has none): else let the model or the coach decide
    known = set(_ex.EXERCISES) | set(_ex.ALIASES)
    for st in out["sets"]:
        name = st["exercise"].strip()
        if name not in known and name.rstrip("s") not in known:
            return None
    return _post_process(out, transcript)


def parse_voice_log(transcript: str, fast: bool = False) -> dict:
    fast = _clean_rule_parse(transcript)       # no network round-trip for the common in-gym phrase (it used to cost 3+ s per set)
    if fast is not None:
        return fast
    if have_key():
        try:
            out = extract_structured(
                "You parse gym voice logs into structured sets. Be literal; never invent sets. "
                "Put effort words ('easy', 'hard', 'grinder') into that set's rpe field, not only the note. "
                "If any pain or injury is mentioned, put it in safety_note.",
                transcript, LOG_TOOL, fast=fast,
            )
        except Exception:  # provider down / rate-limited: the regex parser still works offline
            out = {}
        if out.get("sets") or has_safety_flag(transcript):
            return _post_process(out if out.get("sets") else {"sets": [], **out}, transcript)
    return _post_process(_fallback(transcript), transcript)
