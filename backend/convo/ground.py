"""Numbers in spoken text: what was said, what may be spoken back, and the per-sentence check that keeps invented numbers from reaching the speaker.

`numbers_in(text)` understands digits and English / Hinglish number words ("sixty-five", "sixty two point five", "paanch", "ek sau bees").
The grounding rule (docs/CONVERSATIONAL_AGENT_PLAN.md 4.4): every number the coach SAYS must come from the state block, a tool result or the athlete's own
words (differences and sums of those are fine: "three fewer than last set"). A sentence that breaks the rule is dropped, never spoken.
"""
from __future__ import annotations

import re
from itertools import combinations

_ONES = {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
         "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
# Hinglish (Roman letters). "saath" is also the word for "with", so it counts as 60 only next to a unit (see _AMBIGUOUS).
_HI = {"ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5, "chhe": 6, "chhah": 6, "chah": 6, "cheh": 6, "saat": 7, "aath": 8, "nau": 9, "das": 10,
       "gyarah": 11, "barah": 12, "terah": 13, "chaudah": 14, "pandrah": 15, "solah": 16, "satrah": 17, "atharah": 18, "unnis": 19, "bees": 20, "tees": 30,
       "chalis": 40, "pachas": 50, "saath": 60, "sath": 60, "sattar": 70, "assi": 80, "nabbe": 90, "sau": 100}
_AMBIGUOUS = {"do", "saath", "sath", "char", "das", "sau", "teen", "ek", "nau", "saat"}
_UNIT = re.compile(r"^(kg|kgs|kilo|kilos|kilogram|kilograms|rep|reps|set|sets|lb|lbs|pounds?|minutes?|mins?|seconds?|secs?|baar|bar|times|x)$")
_TOKEN = re.compile(r"\d+(?:\.\d+)?|[a-zA-Z']+|[,;:!?]|\.")
_SENT_END = re.compile(r"(?<=[.!?।])\s+(?=[\"'(\[]?[A-Za-z0-9])|(?<=[.!?।])$")
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_NON_LATIN_SCRIPT = re.compile(r"[֐-ࣿঀ-෿฀-῿⺀-鿿ꀀ-￿]")   # not Latin, not Devanagari (Hebrew, Arabic, Thai, CJK, Hangul...)
_MARKDOWN = re.compile(r"[*_`#>~|]|^\s*[-•]\s+|^\s*\d+\.\s+", re.M)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
_CODEY = re.compile(r"\b[a-z_]{3,}\s*\(|\bfunction\b|\btool_?call|\{.*\}|<[^>]+>|\[\[|\]\]")


def _num_words(tokens: list[str], i: int, split: bool) -> tuple[list[float], int] | None:
    """Parse a number starting at tokens[i] -> (values, next index). `values` has the composed number first; with split=True also the parts."""
    t = tokens[i]
    if re.fullmatch(r"\d+(?:\.\d+)?", t):
        return [float(t)], i + 1
    w = t.lower()
    nxt = tokens[i + 1].lower() if i + 1 < len(tokens) else ""
    if w in _HI:
        if w in _AMBIGUOUS and not (_UNIT.match(nxt) or (i > 0 and _UNIT.match(tokens[i - 1].lower())) or re.fullmatch(r"\d+(?:\.\d+)?", nxt) or nxt in _ONES or nxt in _TENS):
            return None
        if w in _AMBIGUOUS and w in ("do", "ek", "teen", "char", "das", "nau", "saat") and not (_UNIT.match(nxt) or (i > 0 and _UNIT.match(tokens[i - 1].lower()))):
            return None                      # "do" (two) / "ek" / "teen"... are only numbers next to a unit: "do reps", "ek set"
        return [float(_HI[w])], i + 1
    if w in _TENS:
        v, j = _TENS[w], i + 1
        parts = [float(v)]
        if j < len(tokens) and tokens[j].lower() in _ONES and 1 <= _ONES[tokens[j].lower()] <= 9:
            ones = float(_ONES[tokens[j].lower()])
            v += _ONES[tokens[j].lower()]
            j += 1
            parts.append(ones)
        v, j = _decimal(tokens, j, float(v))
        return ([v] + parts if split and len(parts) > 1 else [v]), j
    if w in _ONES:
        v, j = float(_ONES[w]), i + 1
        if j < len(tokens) and tokens[j].lower() == "hundred":
            v, j = v * 100, j + 1
            if j < len(tokens) and tokens[j].lower() == "and":
                j += 1
            sub = _num_words(tokens, j, False) if j < len(tokens) else None
            if sub and sub[0][0] < 100:
                v, j = v + sub[0][0], sub[1]
        v, j = _decimal(tokens, j, v)
        return [v], j
    if w == "hundred" or (w == "a" and nxt == "hundred"):
        j = i + (2 if w == "a" else 1)
        v = 100.0
        if j < len(tokens) and tokens[j].lower() == "and":
            j += 1
        sub = _num_words(tokens, j, False) if j < len(tokens) else None
        if sub and 0 < sub[0][0] < 100:
            v, j = v + sub[0][0], sub[1]
        return [v], j
    return None


def _decimal(tokens: list[str], j: int, v: float) -> tuple[float, int]:
    """'... point five' -> +0.5, '... and a half' -> +0.5."""
    if j < len(tokens) and tokens[j].lower() == "point":
        k, digits = j + 1, ""
        while k < len(tokens) and (tokens[k].lower() in _ONES and _ONES[tokens[k].lower()] <= 9 or re.fullmatch(r"\d", tokens[k])):
            digits += tokens[k] if tokens[k].isdigit() else str(_ONES[tokens[k].lower()])
            k += 1
        if digits:
            return float(f"{v:g}.{digits}"), k
    if j + 2 < len(tokens) and tokens[j].lower() == "and" and tokens[j + 1].lower() == "a" and tokens[j + 2].lower() == "half":
        return v + 0.5, j + 3
    return v, j


def numbers_in(text: str, split: bool = False) -> set[float]:
    """Every number in the text. split=True also adds the parts of 'sixty five' (60 and 5), because a pause can split one into two ('sixty... five')."""
    if not text:
        return set()
    text = re.sub(r"(?<=[A-Za-z])-(?=[A-Za-z])", " ", text)
    tokens = _TOKEN.findall(text)
    out: set[float] = set()
    i = 0
    while i < len(tokens):
        r = _num_words(tokens, i, split)
        if r:
            vals, j = r
            out.update(vals)
            i = j
        else:
            i += 1
    return out


COMPARATIVE = re.compile(r"\b(fewer|more than|less than|up from|down from|lighter|heavier|than|over last|under last|jump|extra|added|plus|minus|difference|short of|ek aur|zyada|kam)\b", re.I)


def allowed_numbers(*sources, extra=()) -> set[float]:
    """Numbers the coach may say: those in the given texts (split-lenient) and `extra`, plus 0 and 1 (idioms: 'one more') and minutes <-> seconds.
    Differences and sums of two of them ('three fewer than last set') are allowed too, but only inside a sentence that compares (see `untraceable`)."""
    base: set[float] = set()
    for s in sources:
        if isinstance(s, str):
            base |= numbers_in(s, split=True)
        elif s is not None:
            base |= {float(x) for x in s}
    base |= {float(x) for x in extra}
    base |= {0.0, 1.0}
    return base


TIME_WORDS = re.compile(r"\b(minutes?|mins?|seconds?|secs?|second|minute)\b", re.I)


def _conversions(base: set[float]) -> set[float]:
    """120 s <-> two minutes: only inside a sentence that names a unit of time, so a rest of 120 s does not make 'two more sets' sayable."""
    out: set[float] = set()
    for a in base:
        if a >= 60 and a % 60 == 0:
            out.add(a / 60)
        if a <= 15:
            out.add(a * 60)
    return out


def _derived(base: set[float]) -> set[float]:
    pool = sorted(base)[:60]
    out: set[float] = set()
    for a, b in combinations(pool, 2):
        out.add(round(abs(a - b), 2))
        out.add(round(a + b, 2))
    return out


def untraceable(sentence: str, allowed: set[float]) -> list[float]:
    nums = numbers_in(sentence)
    if not nums:
        return []
    ok = {round(a, 2) for a in allowed}
    if TIME_WORDS.search(sentence):
        ok |= {round(a, 2) for a in _conversions(allowed)}
    if COMPARATIVE.search(sentence):
        ok |= _derived(allowed)
    return sorted(n for n in nums if round(n, 2) not in ok)


# ---------------------------------------------------------------- speech clean-up and sentence streaming
def clean_for_speech(text: str) -> str:
    t = _EMOJI.sub("", text or "")
    t = _MARKDOWN.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def looks_like_code(sentence: str) -> bool:
    """The model sometimes writes the tool call as text ('log_set(weight=60, reps=5)'): never speak that."""
    return bool(_CODEY.search(sentence))


def has_devanagari(text: str) -> bool:
    return bool(_DEVANAGARI.search(text or ""))


def foreign_script(text: str) -> bool:
    """True when the text contains a script that is neither Latin nor Devanagari: the 'noise heard as Korean' case. Nothing is acted on."""
    return bool(_NON_LATIN_SCRIPT.search(text or ""))


class SentenceStream:
    """Feed model text deltas, get complete sentences as soon as they end. Decimals ('62.5') and abbreviations do not end a sentence."""

    def __init__(self):
        self.buf = ""

    def feed(self, delta: str) -> list[str]:
        self.buf += delta
        out: list[str] = []
        while True:
            m = None
            for cand in re.finditer(r"[.!?।](?=\s|$)|\n", self.buf):
                end = cand.end()
                if cand.group() == "." and re.search(r"\b(?:Dr|Mr|Mrs|Ms|vs|e\.g|i\.e)\.$", self.buf[:end]):
                    continue
                if cand.group() != "\n" and end >= len(self.buf):
                    break                          # the sentence might continue ("62." + "5"): wait for the next delta
                m = cand
                break
            if not m:
                break
            s, self.buf = self.buf[: m.end()], self.buf[m.end():]
            if s.strip():
                out.append(s.strip())
        return out

    def flush(self) -> list[str]:
        s, self.buf = self.buf.strip(), ""
        return [s] if s else []


# ---------------------------------------------------------------- claims of action (never say 'logged' unless a tool really wrote it)
_CLAIM = re.compile(r"\b(?:logged|saved|recorded|noted down|put that down|written down|entered|added that|bumped|fixed that|corrected that)\b|\b(?:log|save|likh|note) (?:kar )?(?:diya|liya|li|di)\b", re.I)
_NEG_CLAIM = re.compile(r"\b(?:nothing|not|n't|never|no sets?|isn't|wasn't|haven't|hasn't|before i|so i can|so i'll|until|once|when|if|can log|will log|'ll log|to log|nahi|na)\b[^.?!]{0,40}\b(?:log|logged|logging|saved|save|recorded|record)\b|\b(?:log|save|record)\b(?: it| that| this)? (?:right|when|once|after)", re.I)


def claims_action(sentence: str) -> bool:
    """True when the sentence says an action was done (a set logged, a note saved). The Core drops such a sentence unless a write tool really succeeded this turn."""
    s = sentence or ""
    return bool(_CLAIM.search(s)) and not _NEG_CLAIM.search(s)
