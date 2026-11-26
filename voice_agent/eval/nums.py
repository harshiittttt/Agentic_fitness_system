"""Numbers in text: digits and spoken numbers (English + Hinglish in Roman letters). Used by the scorers (is a number in the reply traceable?) and the generator."""
from __future__ import annotations

import re

ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
        "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
# Hinglish (Roman letters): the words people actually say at the gym
HI_ONES = {"ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5, "chhe": 6, "chhah": 6, "cheh": 6, "saat": 7, "aath": 8, "nau": 9, "das": 10,
           "gyarah": 11, "barah": 12, "terah": 13, "chaudah": 14, "pandrah": 15, "solah": 16}
HI_TENS = {"bees": 20, "tees": 30, "chalis": 40, "pachaas": 50, "pachas": 50, "saath": 60, "sath": 60, "sattar": 70, "assi": 80, "nabbe": 90}
HUNDRED = {"hundred", "sau"}
AMBIGUOUS = {"one", "do", "ek"}          # mostly NOT quantities in a sentence ("no one", "do it")

_TOKEN = re.compile(r"\d+(?:\.\d+)?|[a-z]+|,")
_DIGITS = re.compile(r"\d+(?:\.\d+)?")


def _unit(tok: str) -> int | None:
    return ONES.get(tok, HI_ONES.get(tok))


def _tens(tok: str) -> int | None:
    return TENS.get(tok, HI_TENS.get(tok))


def _small(toks: list[str], i: int) -> tuple[int | None, int]:
    """Parse a number below 100 starting at toks[i]: 'sixty two', 'sixteen', 'five', 'saath'. -> (value|None, next index)."""
    if i >= len(toks):
        return None, i
    t = _tens(toks[i])
    if t is not None:
        if i + 1 < len(toks):
            u = _unit(toks[i + 1])
            if u is not None and 1 <= u <= 9 and toks[i + 1] not in AMBIGUOUS:
                return t + u, i + 2
        return t, i + 1
    u = _unit(toks[i])
    return (u, i + 1) if u is not None else (None, i)


def numbers_in(text: str, include_ambiguous: bool = True) -> list[float]:
    """All numbers in a sentence, digits or words. 'sixty-two point five' -> 62.5, 'a hundred and twenty' -> 120, 'saath kilo' -> 60.
    include_ambiguous=False drops a bare 'one'/'do'/'ek' (usually not a quantity: 'no one', 'do it')."""
    toks = _TOKEN.findall((text or "").lower().replace("-", " "))
    return [v for _, _, v in _scan(toks, include_ambiguous)]


def digitize(text: str) -> str:
    """'Sixty kilos, five reps' -> '60 kilos , 5 reps': number words become digits (used to compare what was said with what was heard)."""
    toks = _TOKEN.findall((text or "").lower().replace("-", " "))
    out, i = [], 0
    for a, b, v in _scan(toks, True):
        out += toks[i:a] + [fmt(v)]
        i = b
    return " ".join(out + toks[i:])


def _scan(toks: list[str], include_ambiguous: bool) -> list[tuple[int, int, float]]:
    out: list[tuple[int, int, float]] = []
    i = 0
    while i < len(toks):
        tok = toks[i]
        if _DIGITS.fullmatch(tok):
            out.append((i, i + 1, float(tok)))
            i += 1
            continue
        start = i
        if tok in HUNDRED:
            total, i = 100.0, i + 1
        else:
            v, j = _small(toks, i)
            if v is None:
                i += 1
                continue
            total, i = float(v), j
            if i < len(toks) and toks[i] in HUNDRED:      # "two hundred"
                total, i = total * 100, i + 1
            else:
                if toks[start] in AMBIGUOUS and not include_ambiguous and total == v:
                    pass
        if total >= 100 and total % 100 == 0:               # "a hundred (and) twenty"
            j = i + 1 if i < len(toks) and toks[i] == "and" else i
            v, k = _small(toks, j)
            if v is not None:
                total, i = total + v, k
        if i + 1 < len(toks) and toks[i] == "point":        # decimals: "point five", "point two five"
            digits = ""
            k = i + 1
            while k < len(toks) and (_unit(toks[k]) is not None and _unit(toks[k]) <= 9 or _DIGITS.fullmatch(toks[k])):
                digits += str(int(_unit(toks[k]))) if _unit(toks[k]) is not None else toks[k]
                k += 1
            if digits:
                total += float("0." + digits)
                i = k
        if not include_ambiguous and toks[start] in AMBIGUOUS and i == start + 1:
            continue
        out.append((start, i, total))
    return out


def fmt(x: float) -> str:
    return str(int(x)) if float(x) == int(x) else f"{x:g}"
