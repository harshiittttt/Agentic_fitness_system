"""Scorers: turn one turn's evidence (what was said, what the brain answered, what changed in the database) into pass/fail checks.

Expectation keys (all optional) under `expect:` in a scenario turn:
  sets: [{weight, reps, exercise?}]   exactly these sets get logged this turn (exercise defaults to the current one)
  no_set: true                        no set is logged (this is also the DEFAULT when `sets` is not given)
  skipped: [names]                    exactly these exercises become skipped this turn (default: none)
  unskipped: [names]                  exactly these skips are undone this turn
  asks: true                          the coach asks a question instead of acting (clarification)
  intent_in / intent_not_in: [...]
  reply_max_words: N                  brevity
  reply_contains_any / reply_not_contains_any: [words]
  lang: en | hinglish | hi            the language the athlete spoke: the reply must not be in Devanagari for en/hinglish
  grounded: false                     switch the number-grounding check off for this turn (default: on)
  first_sound_ms_max / first_word_ms_max: N   latency limits (audio mode / any mode)
Vocabulary: 'wrong action' = something was done that the turn did not ask for (an unexpected set or skip, or a set with wrong values / exercise).
A 'miss' = something that should have happened did not."""
from __future__ import annotations

import re
from typing import Any

from .nums import digitize, numbers_in

DEVA = re.compile(r"[ऀ-ॿ]")
HINGLISH_MARKERS = {"kya", "hai", "haan", "nahi", "bhai", "chalo", "karo", "ho", "gaya", "ruko", "abhi", "aap", "tum", "accha", "achha", "theek", "kitna", "kitne", "wapas",
                    "bas", "yaar", "bilkul", "shabash", "chal", "mat", "karna", "hain", "tha", "thi", "toh", "aur", "lekin", "agla", "agle", "baar", "kilo"}


def _close(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) < 0.011


def _words(t: str) -> list[str]:
    return re.findall(r"[a-zA-Z']+", (t or "").lower())


def _numeric_leaves(x: Any, out: set[float]) -> None:
    if isinstance(x, bool):
        return
    if isinstance(x, (int, float)):
        out.add(float(x))
    elif isinstance(x, dict):
        for k, v in x.items():
            if k in ("date", "ts", "id"):
                continue
            _numeric_leaves(v, out)
    elif isinstance(x, (list, tuple)):
        for v in x:
            _numeric_leaves(v, out)


def allowed_numbers(said: str, before: dict, after: dict, actions: list) -> set[float]:
    """Numbers a reply may contain without inventing them: what the athlete said, the session state, logged sets, action values and the plain
    arithmetic around them (done+1, remaining-1, weight +/- 2.5 or 5, seconds -> minutes)."""
    ok: set[float] = set(numbers_in(said))
    for snap in (before, after):
        _numeric_leaves(snap.get("state"), ok)
        for s in snap.get("sets", []):
            for k in ("weight", "reps", "rpe"):
                if s.get(k) is not None:
                    ok.add(float(s[k]))
    acts: set[float] = set()
    _numeric_leaves(actions, acts)
    ok |= acts | {a / 60 for a in acts if a >= 60}
    for n in list(ok):
        for d in (1, 2.5, 5, -1, -2.5, -5):
            ok.add(n + d)
    return ok


def reply_numbers(reply: str) -> list[float]:
    return numbers_in(reply, include_ambiguous=False)


def _delta(before: dict, after: dict) -> tuple[list[dict], list[str], list[str]]:
    ids = {s["id"] for s in before["sets"]}
    new_sets = [s for s in after["sets"] if s["id"] not in ids]
    newly_skipped = [n for n in after["skipped"] if n not in before["skipped"]]
    unskipped = [n for n in before["skipped"] if n not in after["skipped"]]
    return new_sets, newly_skipped, unskipped


def score_turn(turn: dict, expect: dict, before: dict, after: dict, res: dict, lang: str | None, said: str) -> dict:
    """-> {checks: [{name, ok, detail}], wrong_action: bool, miss: bool, new_sets, newly_skipped, unskipped, ungrounded: [numbers]}"""
    checks: list[dict] = []
    new_sets, newly_skipped, unskipped = _delta(before, after)
    cur = ((before.get("state") or {}).get("current") or {}).get("name")
    exp_sets = expect.get("sets")
    exp_skip = [s.lower() for s in (expect.get("skipped") or [])]
    exp_unskip = [s.lower() for s in (expect.get("unskipped") or [])]
    wrong = miss = False

    def add(name: str, ok: bool, detail: str = "", info: bool = False) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail, **({"info": True} if info else {})})

    # --- sets
    if exp_sets:
        got = [(s["exercise"].lower(), float(s["weight"]), int(s["reps"])) for s in new_sets]
        want = [((e.get("exercise") or cur or "").lower(), float(e["weight"]), int(e["reps"])) for e in exp_sets]
        pool = list(got)
        matched = 0
        for w in want:
            hit = next((g for g in pool if g[0] == w[0] and _close(g[1], w[1]) and g[2] == w[2]), None)
            if hit:
                pool.remove(hit)
                matched += 1
        ok = matched == len(want) and not pool
        add("set_logged", ok, f"want {want} got {got}")
        if not ok:
            miss = miss or matched < len(want)
            wrong = wrong or bool(pool) or (bool(got) and matched < len(want))        # a set with the wrong values / exercise is a wrong action, not just a miss
    else:
        add("no_set", not new_sets, f"unexpected sets: {[(s['exercise'], s['weight'], s['reps']) for s in new_sets]}" if new_sets else "")
        wrong = wrong or bool(new_sets)
    # --- skips
    got_skip = [n.lower() for n in newly_skipped]
    if exp_skip:
        ok = sorted(got_skip) == sorted(exp_skip)
        add("skipped", ok, f"want {exp_skip} got {got_skip}")
        if not ok:
            miss = miss or not set(exp_skip) <= set(got_skip)
            wrong = wrong or bool(set(got_skip) - set(exp_skip))
    else:
        add("no_skip", not got_skip, f"skipped {got_skip}" if got_skip else "")
        wrong = wrong or bool(got_skip)
    if exp_unskip:
        got_un = [n.lower() for n in unskipped]
        ok = sorted(got_un) == sorted(exp_unskip)
        add("unskipped", ok, f"want {exp_unskip} got {got_un}")
        miss = miss or not ok
    # --- reply shape
    reply = res.get("reply") or ""
    intent = res.get("intent")
    if expect.get("asks"):
        asks = "?" in reply or (intent or "").startswith("ask") or intent == "confirm" or bool(res.get("expects"))
        add("asks", asks, f"reply={reply!r} intent={intent}")
        miss = miss or not asks
    if expect.get("intent_in"):
        add("intent_in", intent in expect["intent_in"], f"intent={intent} want {expect['intent_in']}")
    if expect.get("intent_not_in"):
        add("intent_not_in", intent not in expect["intent_not_in"], f"intent={intent}")
    if expect.get("reply_max_words") is not None:
        n = len(reply.split())
        add("reply_max_words", n <= expect["reply_max_words"], f"{n} words")
    if expect.get("reply_contains_any"):
        low = reply.lower()
        add("reply_contains_any", any(w.lower() in low for w in expect["reply_contains_any"]), f"reply={reply!r}")
    if expect.get("reply_not_contains_any"):
        low = reply.lower()
        bad = [w for w in expect["reply_not_contains_any"] if w.lower() in low]
        add("reply_not_contains_any", not bad, f"found {bad}")
    # --- grounding: no number in the reply that cannot be traced
    ungrounded: list[float] = []
    if expect.get("grounded", True) and reply:
        allowed = allowed_numbers(said, before, after, res.get("actions") or [])
        ungrounded = [n for n in reply_numbers(reply) if not any(_close(n, a) for a in allowed)]
        add("grounded", not ungrounded, f"untraceable numbers {ungrounded} in {reply!r}" if ungrounded else "")
    # --- language
    lg = expect.get("lang") or lang
    if lg in ("en", "hinglish") and reply:
        add("script", not DEVA.search(reply), "reply is in Devanagari although the athlete spoke Roman letters" if DEVA.search(reply) else "")
        w = _words(reply)
        hits = sum(1 for x in w if x in HINGLISH_MARKERS)
        if lg == "en":
            said_hits = sum(1 for x in _words(said) if x in HINGLISH_MARKERS)               # the caller itself mixed Hinglish words in: mirroring them is right, not a failure
            add("lang_mirror", hits < 3 or said_hits >= 1, f"{hits} Hinglish words in an English reply")
        else:
            add("lang_mirror", hits >= 1 or len(w) <= 3, "athlete spoke Hinglish, the reply has no Hinglish word (informational)", info=True)
    # --- latency limits
    for key, field in (("first_sound_ms_max", "first_sound_ms"), ("first_word_ms_max", "first_word_ms")):
        if expect.get(key) is not None and res.get(field) is not None:
            add(key, res[field] <= expect[key], f"{res[field]:.0f} ms > {expect[key]}" if res[field] > expect[key] else f"{res[field]:.0f} ms")
    fc = false_log_claim(reply, new_sets)
    add("no_false_log_claim", not fc, f"reply claims a set was logged but none was: {reply!r}" if fc else "")
    return {"checks": checks, "wrong_action": wrong, "miss": miss, "new_sets": [(s["exercise"], s["weight"], s["reps"]) for s in new_sets],
            "newly_skipped": newly_skipped, "unskipped": unskipped, "ungrounded": ungrounded}


_CLAIM = re.compile(r"\b(?:logged|saved|recorded|noted down|put that down)\b", re.I)
_NEG_CLAIM = re.compile(r"\b(?:nothing|not|n't|never|no sets?|isn't|wasn't|haven't|hasn't)\b[^.?!]{0,30}\b(?:logged|saved|recorded)\b", re.I)


def false_log_claim(reply: str, new_sets: list) -> bool:
    """The reply says a set was logged/saved but nothing was written to the database this turn (the coach claims an action it did not take)."""
    return bool(reply) and not new_sets and bool(_CLAIM.search(reply)) and not _NEG_CLAIM.search(reply)


def blocking(checks: list[dict]) -> list[dict]:
    """The checks that decide pass/fail: informational ones (flagged `info`, or the Hinglish-mirror note in older run files) never fail a turn."""
    return [c for c in checks if not c["ok"] and not c.get("info") and not (c["name"] == "lang_mirror" and "informational" in c["detail"])]


def percentile(vals: list[float], p: float) -> float | None:
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


def wer(ref: str, hyp: str) -> float:
    """Word error rate between what was said and what the agent heard (lowercase, punctuation and number format normalised)."""
    def norm(t: str) -> list[str]:
        t = digitize(t)
        t = re.sub(r"\bok(?:ay)?\b", "okay", t)
        return re.findall(r"[a-z0-9.']+", t)
    r, h = norm(ref), norm(hyp)
    if not r:
        return 0.0 if not h else 1.0
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return d[len(h)] / len(r)
