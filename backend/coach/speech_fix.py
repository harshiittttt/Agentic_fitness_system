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
             "basically", "actually", "well", "okay", "ok", "yeah"}
# endings that mean "and the rest is coming": "can you just tell me", "should I go till", "what is the"
_OPEN_END = re.compile(r"(?:\b(?:tell|give|show|let|help|ask|get|take|teach|remind)\s+me|\b(?:can|could|would|should|will|do|does|did|am|is|are|was|were)\s+(?:you|i|we|it|that|this)(?:\s+just)?|"
                       r"\b(?:till|until|between|towards?|against|instead of|because of|according to|how do i|what is the|what's the|where is the))$", re.I)
_STARTERS = re.compile(r"^(?:i|i'?ve|i'?m|we|my|the|so|and|then|okay|ok|well|yeah|just|like)\b", re.I)


def clean(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return t
    t = _FILLER.sub(" ", t)
    t = _REPS_AFTER_NUM.sub(lambda m: f"{m.group(1)} reps", t)
    t = _REPS_PLURAL.sub("reps", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def is_fragment(text: str) -> bool:
    """'I did like', 'and then', 'so I', 'I just': too little to act on and ending on a word that needs a continuation."""
    t = clean(text)
    words = re.findall(r"[A-Za-z']+|\d+(?:\.\d+)?", t.lower())
    if not words:
        return False
    if any(w.replace(".", "").isdigit() for w in words):        # a number means there is content ('bench 60'): let the parsers see it
        return False
    if len(words) <= 5 and words[-1] in _DANGLING and _STARTERS.match(t):     # 'undo that', 'what is that' are commands, not half-sentences
        return True
    if len(words) <= 8 and _OPEN_END.search(t.lower()):
        return True
    return False
