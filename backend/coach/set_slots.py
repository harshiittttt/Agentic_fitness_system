"""Understand a half-described set: "1 set done", "8 reps", "eighty kilos", "80 for 8", "bench, 3 sets of 8 at 80".
Pure functions (no database, no model): the live coach uses them to decide what is still missing and to read the answer to its question."""
from __future__ import annotations

import re

from backend.tools import exercises as ex

_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
          "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_NUMW = set(_UNITS) | set(_TENS) | {"hundred"}
_AFTER_ONE = {"set", "sets", "rep", "reps", "time", "times", "kg", "kgs", "kilo", "kilos", "kilogram", "kilograms", "lb", "lbs", "pound", "pounds"}

BODYWEIGHT = {"pull up", "push up", "dips", "chin up", "burpee", "sit up", "bodyweight squat"}
CANCEL_RE = re.compile(r"\b(never ?mind|cancel( that)?|forget (it|that)|ignore (that|it)|scrap (that|it)|no set|didn'?t (do|finish) (it|that)|not (done|yet)|my bad)\b")
DONE_RE = re.compile(r"\b(done|finished|completed?|did|hit|knocked out|smashed|crushed|nailed)\b")
VERB_RE = re.compile(r"\b(squatted|benched|deadlifted|pressed|rowed|curled|lunged)\b")
SETWORD_RE = re.compile(r"\b(sets?|reps?)\b")
BARE_DONE_RE = re.compile(r"^\W*(set )?(done|finished|completed?|that'?s (one|a set)|done with (that|the) set)\W*$")
_UNIT_KG = r"(?:kgs?|kilos?|kilograms?)"
_UNIT_LB = r"(?:lbs?|pounds?)"


def _eval_run(run: list[str]) -> int | None:
    """['one','hundred','and','ten'] -> 110, ['eighty','five'] -> 85, ['one','oh','five'] -> 105. None when it is not a number."""
    toks = [t for t in run if t not in ("and",)]
    if "oh" in toks:
        i = toks.index("oh")
        if i == 1 and len(toks) == 3 and toks[0] in _UNITS and toks[2] in _UNITS and _UNITS[toks[0]] and 0 < _UNITS[toks[2]] < 10:
            return _UNITS[toks[0]] * 100 + _UNITS[toks[2]]
        return None
    total, cur = 0, 0
    for t in toks:
        if t in _UNITS:
            cur += _UNITS[t]
        elif t in _TENS:
            cur += _TENS[t]
        elif t == "hundred":
            cur = max(cur, 1) * 100
        else:
            return None
    return total + cur


def words_to_digits(text: str) -> str:
    """'eighty five kilos for eight' -> '85 kilos for 8'. A lone 'one' is only a number when a unit follows ('1 set'), so 'no one' stays words."""
    toks = re.findall(r"[A-Za-z']+|\d+(?:\.\d+)?|[^\sA-Za-z\d]", text)
    out: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i].lower()
        starts = t in _NUMW or (t == "a" and i + 1 < len(toks) and toks[i + 1].lower() == "hundred")
        if not starts:
            out.append(toks[i])
            i += 1
            continue
        j = i
        run: list[str] = []
        while j < len(toks) and (toks[j].lower() in _NUMW or toks[j].lower() in ("and", "oh") or (j == i and toks[j].lower() == "a")):
            run.append("one" if toks[j].lower() == "a" else toks[j].lower())
            j += 1
        while run and run[-1] in ("and", "oh"):   # a trailing "and" belongs to the sentence
            run.pop()
            j -= 1
        val = _eval_run(run)
        nxt = toks[j].lower() if j < len(toks) else ""
        if val is None or (run == ["one"] and nxt not in _AFTER_ONE and (not out or out[-1].lower() not in ("for", "x", "at", "of"))):
            out.append(toks[i])
            i += 1
            continue
        out.append(str(val))
        i = j
    return " ".join(out)


def find_exercise(text: str, plan_names: list[str]) -> str | None:
    """The plan exercise named in the text (exact, alias, or a word that belongs to exactly one plan exercise), else None."""
    low = text.lower()
    cands: dict[str, str] = {}
    for n in plan_names:
        cands[n.lower()] = n.lower()
    for n in ex.EXERCISES:                         # any exercise the app knows, even when it is not in today's plan
        cands.setdefault(n, n)
    for alias, canon in ex.ALIASES.items():
        cands[alias] = canon
    counts: dict[str, list[str]] = {}
    for n in plan_names:
        for w in {w.rstrip("s") for w in n.lower().split() if len(w) >= 4}:
            counts.setdefault(w, []).append(n.lower())
    for w, owners in counts.items():
        if len(owners) == 1:
            cands.setdefault(w, owners[0])
    best, size = None, 0
    for c, canon in cands.items():
        if re.search(rf"\b{re.escape(c)}\w*", low) and len(c) > size:
            best, size = canon, len(c)
    return ex.resolve_exercise(best, plan_names) if best else None


def _num(x: str) -> float:
    return float(x)


def _kg(v: float, lb: bool) -> float:
    return round(v * 0.45359237 * 2) / 2 if lb else v


def extract(text: str, ask: set[str] | None = None) -> dict:
    """Pull weight (kg), reps, sets and rpe out of free speech. `ask` = what the coach just asked for ({'w'}, {'r'}, {'w','r'}), so a bare
    number can be read as the answer. Anything that does not fit is reported in `leftover` (the caller then lets the model read the sentence)."""
    t = " " + words_to_digits(text.lower()) + " "
    w = r = sets = rpe = None
    invalid = False
    w_explicit = r_explicit = False        # said with a unit or the word "reps": clearly gym talk even without "done"

    def take(pattern: str):
        nonlocal t
        m = re.search(pattern, t)
        if m:
            t = t[:m.start()] + " " + t[m.end():]
        return m

    m = take(rf"(\d+(?:\.\d+)?)\s*{_UNIT_KG}\b")
    if m:
        w, w_explicit = _num(m.group(1)), True
    else:
        m = take(rf"(\d+(?:\.\d+)?)\s*{_UNIT_LB}\b")
        if m:
            w, w_explicit = _kg(_num(m.group(1)), True), True
    m = take(r"\brpe\s*(\d+(?:\.\d+)?)")
    if m:
        rpe = _num(m.group(1))
    m = take(r"(\d+)\s*sets?\s+of\s+(\d+)\b")
    if m:
        sets, r, r_explicit = int(m.group(1)), int(m.group(2)), True
    else:
        m = take(r"\bsets?\s+of\s+(\d+)\b")
        if m:
            r, r_explicit = int(m.group(1)), True
    m = sets is None and take(r"(\d+)\s*sets?\b")
    if m:
        sets = int(m.group(1))
    if r is None:
        m = take(r"(\d+)\s*(?:reps?|repetitions?|times)\b")
        if m:
            r, r_explicit = int(m.group(1)), True
        else:
            m = take(r"\b(?:for|x)\s*(\d+)\b(?!\s*(?:min|minutes|mins|seconds|secs))")
            if m:
                r = int(m.group(1))
    if w is None:
        m = take(r"\bat\s+(\d+(?:\.\d+)?)\b")
        if m:
            w = _num(m.group(1))
    bare = [float(x) for x in re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", t)]
    need_w, need_r = w is None, r is None
    if bare:
        if need_w and need_r:
            if len(bare) >= 2:
                a, b = bare[0], bare[1]
                w, r = (a, int(b)) if a >= b else (b, int(a))
                bare = bare[2:]
            elif ask == {"w"}:
                w, bare = bare[0], []
            elif ask == {"r"}:
                r, bare = int(bare[0]), []
            else:                                   # one number, both unknown: kilos are usually above 20, reps below
                v = bare[0]
                if v > 20:
                    w = v
                else:
                    r = int(v)
                bare = []
        elif need_w:
            w, bare = bare[0], bare[1:]
        elif need_r:
            r, bare = int(bare[0]), bare[1:]
    if w is not None and not (0 < w <= 500):
        w, invalid = None, True
    if r is not None and not (1 <= r <= 100):
        r, invalid = None, True
    if sets is not None and not (1 <= sets <= 12):
        sets = None
    return {"w": w, "r": r, "sets": sets, "rpe": rpe, "invalid": invalid, "leftover": len(bare),
            "w_explicit": w_explicit and w is not None, "r_explicit": r_explicit and r is not None}


def is_set_done(text: str, plan_names: list[str]) -> bool:
    """'1 set done', 'finished my set', 'I squatted', 'did squats' (not a question)."""
    low = words_to_digits(text.lower()).strip()
    if "?" in low or re.match(r"^(what|how|should|can|could|why|is|are|do|does|when|where|which)\b", low):
        return False
    if BARE_DONE_RE.match(low):
        return True
    if not (DONE_RE.search(low) or VERB_RE.search(low)):
        return False
    return bool(SETWORD_RE.search(low) or find_exercise(low, plan_names) or re.search(r"\d", low))
