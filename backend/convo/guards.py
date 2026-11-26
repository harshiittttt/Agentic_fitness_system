"""Guards: deterministic safety around what the model asks to DO. They can veto, or turn an action into a question; they never invent anything.

Action classes (docs/CONVERSATIONAL_AGENT_PLAN.md 4.4):
    0 read only                              get_status, recall_about_me
    1 reversible log                         log_set, correct_last_set, control_rest, set_next_weight, log_note   (numbers must be heard; plausibility)
    2 reversible but surprising              skip_exercise, unskip_exercise, swap_exercise, end_workout          (a clear, non-negated, non-question order)
The skip rule is the one that failed in the field (docs/ERROR_LEDGER.md E-001): it reuses `set_slots.skip_intent`, which reads one clause at a time.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from backend.convo import ground
from backend.coach import hindi, live, session_flow, set_slots, speech_fix
from backend.memory import db
from backend.tools import exercises as ex

LOW_LOGPROB = -1.05            # same limit the voice agent uses for "say that again"
MAX_REPS = 30
MAX_WEIGHT = 500.0
JUMP = 0.40                    # a weight more than 40% away from the last set of that lift is asked about, not saved
READ_ONLY = {"get_status", "recall_about_me"}
WRITE = {"log_set", "correct_last_set", "control_rest", "set_next_weight", "skip_exercise", "unskip_exercise", "swap_exercise", "end_workout", "log_note"}
CORRECTION_CUE = re.compile(r"^\W*(?:no|nope|nah|nahi|nahin|sorry|wait|actually|i mean|make (?:it|that)|it was|that was|not|change (?:it|that)|correction)\b")
RECENT_SET_SECS = 180.0
REPEAT_RE = re.compile(r"\b(same|again|repeat|ditto|as before|wahi|vahi|waisa|waise hi|phir se|fir se|dobara)\b")
SWAP_RE = re.compile(r"\b(swap|swapping|replace|replacing|substitut\w*|switch (?:it|this|that) (?:out|for|to)|change (?:it|this|that) (?:out|for|to)|badal\w*)\b")
SWAP_Q_RE = re.compile(r"^\W*(?:should|what|which|how|why|is|are|do|does|kya)\b|\?\s*$")
REST_RE = re.compile(r"\b(rest|resting|rested|timer|break|breather|aaram|ruk|ruko|wait)\b")
NEXT_RE = re.compile(r"\b(next|agla|agle|agli|upcoming|after this|ab ke|abhi ke)\b")
END_HI_RE = re.compile(r"\b(khatam|bas ho gaya|ho gaya aaj|wrap (?:it )?up|finish(?:ed)? (?:the |my )?(?:workout|session)|done for (?:today|the day)|end (?:the |my )?(?:workout|session))\b")
YESISH = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "haan", "han", "ha", "ji", "confirm", "confirmed", "correct", "pakka", "bilkul", "theek", "thik", "kardo", "yea", "ya"}
YES_PHRASE = re.compile(r"^(?:please )?(?:do it|go ahead|skip it|swap it|kar do|kar de|that one|this one|wrap it up|finish it|do that|sounds good|go for it)$")
NOISH = {"no", "nope", "nah", "not", "don't", "dont", "never", "nahi", "nahin", "nhi", "mat", "cancel", "stop", "wait", "ruko"}

PHRASES = {
    "say_again": {"en": "Sorry, say that again?", "hinglish": "Sorry, ek baar phir bolo?"},
    "numbers_q": {"en": "I want the numbers right. What weight, and how many reps?", "hinglish": "Numbers sahi chahiye. Kitna weight, aur kitne reps?"},
    "weight_q": {"en": "What weight was that?", "hinglish": "Kitna weight tha?"},
    "reps_q": {"en": "How many reps?", "hinglish": "Kitne reps?"},
    "many_reps": {"en": "{reps} reps? Say it again so I log it right.", "hinglish": "{reps} reps? Ek baar phir bolo, sahi log karun."},
    "plausible": {"en": "{w} kilos? Last time on this it was {prev}. Say yes and I'll log it.", "hinglish": "{w} kilo? Pichli baar {prev} tha. Haan bolo toh log kar deta hoon."},
    "heavy": {"en": "{w} kilos? Say it again so I log it right.", "hinglish": "{w} kilo? Ek baar phir bolo, sahi log karun."},
    "skip_q": {"en": "Skip {ex} for today? Yes or no?", "hinglish": "{ex} aaj skip karun? Haan ya na?"},
    "swap_q": {"en": "Swap {ex} for something else? Yes or no?", "hinglish": "{ex} ko kisi aur se badal doon? Haan ya na?"},
    "end_q": {"en": "Wrap up the workout now? Yes or no?", "hinglish": "Workout khatam karun abhi? Haan ya na?"},
    "nothing_skipped": {"en": "Nothing is skipped today, everything is still on.", "hinglish": "Aaj kuch skip nahi hua, sab abhi bhi hai."},
    "no_set": {"en": "I don't have a set to fix yet. What did you do?", "hinglish": "Abhi koi set nahi hai theek karne ko. Kya kiya tumne?"},
    "no_plan": {"en": "There is no plan on the screen yet. Finish the setup first.", "hinglish": "Abhi plan nahi hai. Pehle setup poora karo."},
    "which_ex": {"en": "Which exercise was that?", "hinglish": "Kaunsa exercise tha?"},
    "error": {"en": "Sorry, something went wrong on my side. Try that again.", "hinglish": "Sorry, meri taraf gadbad ho gayi. Phir se bolo."},
    "ok": {"en": "Okay.", "hinglish": "Theek hai."},
    "here": {"en": "I'm here. Go ahead.", "hinglish": "Main yahin hoon. Bolo."},
}


def T(lang: str, key: str, **kw) -> str:
    row = PHRASES[key]
    return (row.get(lang) or row["en"]).format(**kw)


@dataclass
class Ctx:
    """What a tool call may rely on: the athlete's words, the dialogue, the plan, and what already ran this turn."""
    text: str
    turn_id: str
    conn_id: str
    dlg: object
    lang: str = "en"
    asr: dict = field(default_factory=dict)
    pid: str = "hype"
    name: str = ""
    pain: bool = False
    executed: dict = field(default_factory=dict)      # (tool, args json) -> result (idempotency inside one turn)
    wrote: bool = False                                # a write tool ran this turn: the legacy brain must not run too
    plan: list[str] = field(default_factory=list)
    allowed: set = field(default_factory=set)          # numbers the coach may speak this turn (state block + athlete's words); tools add their own facts
    claim_dropped: bool = False                        # the model said 'logged'/'saved' with no write tool behind it: that sentence was removed
    dropped: int = 0                                   # model sentences / reactions removed before speaking (ungrounded number, code-like, wrong script)

    def plan_names(self) -> list[str]:
        if not self.plan:
            self.plan = live._plan_names()
        return self.plan


@dataclass
class Verdict:
    ok: bool
    status: str = "ok"                    # ok | needs_clarification | refused | invalid
    say: str | None = None
    reason: str = ""
    expects: str | None = None
    args: dict = field(default_factory=dict)
    pending: dict | None = None
    rewrite: str | None = None            # the model picked the wrong one of two near-identical tools: run this one instead (same args)


# ---------------------------------------------------------------- before any model call
def asr_problem(text: str, asr: dict | None) -> str | None:
    """'unclear' | 'foreign_script' | 'low_confidence' | None. Any of these means: act on nothing, ask again."""
    asr = asr or {}
    t = text or ""
    if asr.get("unclear") or "[[unclear" in t.lower():
        return "unclear"
    if ground.foreign_script(t):
        return "foreign_script"
    lp = asr.get("avg_logprob")
    if isinstance(lp, (int, float)) and lp < LOW_LOGPROB:
        return "low_confidence"
    return None


# ---------------------------------------------------------------- numbers the athlete really said
def heard(ctx: Ctx) -> set[float]:
    """Numbers in this utterance and the one or two just before it (a half-said set: 'sixty kilos' ... 'five'), parts of 'sixty five' included."""
    pool = [ctx.text, hindi.normalize(ctx.text), speech_fix.clean(ctx.text), ctx.dlg.recent_user_text(90.0, 2, exclude_turn=ctx.turn_id)]
    out: set[float] = set()
    for p in pool:
        out |= ground.numbers_in(p, split=True)
    return out


def _is_yes(text: str) -> bool:
    """A short, plain 'yes' to a question the coach just asked. Needs a real yes word (or a short 'do it' phrase) and no no-word: 'that's it for this one' is NOT a yes."""
    words = re.findall(r"[a-zA-Z']+", (text or "").lower())
    if not words or len(words) > 7 or any(w in NOISH for w in words):
        return False
    return any(w in YESISH for w in words) or bool(YES_PHRASE.match(" ".join(words)))


def _prev_weight(exercise: str) -> float | None:
    for s in db.get_sets(exercise, 40):
        if s.get("weight") and s["weight"] > 0:
            return float(s["weight"])
    return None


def _resolve_exercise(raw, ctx: Ctx) -> tuple[str | None, bool]:
    """-> (exercise name or None, was_named). A name that is not a real exercise (unknown, stretch, ...) is treated as 'not named': the current exercise is used."""
    names = ctx.plan_names()
    cur = (live.session_state() or {}).get("current")
    if raw:
        got = ex.resolve_exercise(str(raw), names)
        if got and live._known_exercise(got, names):
            return got, True
    return (cur["name"] if cur else None), False


def _fmt(x) -> str:
    return live.fmt(x)


def _set_count(args: dict, text: str) -> int:
    n = int(args.get("sets") or 1)
    if n > 1 and not re.search(r"(?:^|\W)(?:\d+|two|three|four|five|six)\s*(?:sets|times|x)(?:\W|$)|(?:^|\W)sets? of(?:\W|$)|(?:^|\W)(?:twice|thrice)(?:\W|$)|(?:^|\W)x\s*\d", (text or "").lower()):
        return 1
    return max(1, min(n, 10))


# ---------------------------------------------------------------- the check
def check(name: str, args: dict, ctx: Ctx) -> Verdict:
    args = dict(args or {})
    fn = _CHECKS.get(name)
    if name not in READ_ONLY and name not in WRITE:
        return Verdict(False, "invalid", reason=f"unknown tool {name}")
    if name in WRITE and name not in ("log_note",):
        why = asr_problem(ctx.text, ctx.asr)
        if why:
            return Verdict(False, "needs_clarification", T(ctx.lang, "say_again"), reason=why)
    return fn(args, ctx) if fn else Verdict(True, args=args)


def _log_set(args: dict, ctx: Ctx) -> Verdict:
    names = ctx.plan_names()
    if not names:
        return Verdict(False, "invalid", T(ctx.lang, "no_plan"), reason="no plan")
    try:
        w = float(args.get("weight")) if args.get("weight") is not None else None
        r = int(round(float(args.get("reps")))) if args.get("reps") is not None else None
    except (TypeError, ValueError):
        return Verdict(False, "needs_clarification", T(ctx.lang, "numbers_q"), expects="set_details", reason="numbers are not numbers")
    exn, named = _resolve_exercise(args.get("exercise"), ctx)
    if not exn:
        return Verdict(False, "needs_clarification", T(ctx.lang, "which_ex"), expects="set_details", reason="no exercise")
    body = exn in set_slots.BODYWEIGHT
    if r is None or r <= 0:
        return Verdict(False, "needs_clarification", T(ctx.lang, "reps_q"), expects="set_details", reason="no reps")
    if (w is None or w <= 0) and not body:
        return Verdict(False, "needs_clarification", T(ctx.lang, "weight_q"), expects="set_details", reason="no weight")
    w = 0.0 if (w is None or w < 0) else w
    if r > MAX_REPS:
        return Verdict(False, "needs_clarification", T(ctx.lang, "many_reps", reps=r), expects="set_details", reason="implausible reps")
    h = heard(ctx)
    repeat = bool(REPEAT_RE.search(ctx.text.lower()))
    last = next((s for s in db.get_sets(exn, 10) if s.get("reps")), None)
    if last and CORRECTION_CUE.match(ctx.text.lower().strip()) and _age_secs(last["ts"]) <= RECENT_SET_SECS and last["session"] == session_flow.current_label():
        return Verdict(True, args={"weight": w if w else None, "reps": r, "reaction": args.get("reaction")}, rewrite="correct_last_set")      # "no, sixty-five" is not a new set
    if repeat and last and ((w in (0.0, float(last["weight"] or 0))) and r in (int(last["reps"]),)):
        pass                                                                       # "same again": the numbers repeat the last set, nobody had to say them
    elif not ((float(r) in h) and (body or w in h or w == 0.0)):
        return Verdict(False, "needs_clarification", T(ctx.lang, "numbers_q"), expects="set_details", reason="numbers not heard")
    if w > MAX_WEIGHT:
        return Verdict(False, "needs_clarification", T(ctx.lang, "heavy", w=_fmt(w)), expects="set_details", reason="implausible weight")
    prev = _prev_weight(exn)
    if prev and w and abs(w - prev) / prev > JUMP:
        p = ctx.dlg.get_pending()
        if p and p.get("kind") == "plausible" and p.get("key") == (exn, w, r) and not any(t in NOISH for t in re.findall(r"[a-zA-Z']+", ctx.text.lower())):
            ctx.dlg.pending = None                                                 # asked, and the athlete confirmed: log it
        else:
            q = T(ctx.lang, "plausible", w=_fmt(w), prev=_fmt(prev))
            return Verdict(False, "needs_clarification", q, expects="yes_no", reason="weight jump",
                           pending={"kind": "plausible", "tool": "log_set", "key": (exn, w, r), "question": q})
    rpe = args.get("rpe")
    rpe = float(rpe) if isinstance(rpe, (int, float)) and 1 <= float(rpe) <= 10 and float(rpe) in h else None
    return Verdict(True, args={"exercise": exn, "weight": w, "reps": r, "rpe": rpe, "sets": _set_count(args, ctx.text), "reaction": args.get("reaction")})


def _age_secs(ts: str) -> float:
    from datetime import datetime, timezone
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
    except ValueError:
        return 1e9


def _last_today() -> dict | None:
    label = session_flow.current_label()
    return next((s for s in db.get_sets(limit=60) if s["session"] == label and s.get("reps")), None)


def _correct_last_set(args: dict, ctx: Ctx) -> Verdict:
    last = _last_today()
    if not last:
        if args.get("weight") is not None and args.get("reps") is not None:
            return Verdict(True, args={"weight": args["weight"], "reps": args["reps"], "reaction": args.get("reaction")}, rewrite="log_set")     # a self-correction in one sentence
        return Verdict(False, "needs_clarification", T(ctx.lang, "no_set"), expects="set_details", reason="nothing to correct")
    h = heard(ctx)
    out: dict = {"reaction": args.get("reaction")}
    for key, cast in (("weight", float), ("reps", lambda v: int(round(float(v))))):
        if args.get(key) is None:
            continue
        try:
            v = cast(args[key])
        except (TypeError, ValueError):
            return Verdict(False, "needs_clarification", T(ctx.lang, "numbers_q"), expects="set_details", reason="bad number")
        if float(v) not in h:
            return Verdict(False, "needs_clarification", T(ctx.lang, "numbers_q"), expects="set_details", reason="correction not heard")
        out[key] = v
    if "weight" not in out and "reps" not in out:
        return Verdict(False, "needs_clarification", T(ctx.lang, "numbers_q"), expects="set_details", reason="nothing to change")
    if out.get("reps", 0) > MAX_REPS or out.get("weight", 0) > MAX_WEIGHT:
        return Verdict(False, "needs_clarification", T(ctx.lang, "heavy", w=_fmt(out.get("weight", 0))), expects="set_details", reason="implausible")
    return Verdict(True, args=out)


def _control_rest(args: dict, ctx: Ctx) -> Verdict:
    action = args.get("action")
    if action not in ("start", "add", "skip"):
        return Verdict(False, "invalid", reason="bad rest action")
    if not REST_RE.search(ctx.text.lower()) and not (action == "skip" and re.search(r"(ready|let'?s go|go)", ctx.text.lower()) and ctx.dlg.rest_until > time.time()):
        return Verdict(False, "refused", None, reason="Nothing was changed: the athlete did not talk about the rest timer.")
    secs = args.get("seconds")
    try:
        secs = int(round(float(secs))) if secs is not None else None
    except (TypeError, ValueError):
        secs = None
    if action == "start":
        secs = secs if secs and 10 <= secs <= 900 else None
    elif action == "add":
        if not secs or not (5 <= secs <= 300):
            return Verdict(False, "invalid", reason="rest add needs 5-300 seconds")
    return Verdict(True, args={"action": action, "seconds": secs, "reaction": args.get("reaction")})


def _set_next_weight(args: dict, ctx: Ctx) -> Verdict:
    try:
        w = float(args.get("weight"))
    except (TypeError, ValueError):
        return Verdict(False, "needs_clarification", T(ctx.lang, "weight_q"), reason="no weight")
    exn, _ = _resolve_exercise(args.get("exercise"), ctx)
    if not exn:
        return Verdict(False, "needs_clarification", T(ctx.lang, "which_ex"), reason="no exercise")
    if not (0 < w <= MAX_WEIGHT) or w not in heard(ctx):
        return Verdict(False, "needs_clarification", T(ctx.lang, "weight_q"), reason="weight not heard")
    if not NEXT_RE.search(ctx.text.lower()):
        return Verdict(False, "refused", None, reason="The athlete did not say this is for the NEXT set. If they gave a weight without reps it is probably the start of a set: ask for the reps.")
    return Verdict(True, args={"weight": w, "exercise": exn, "reaction": args.get("reaction")})


def _skip_target(args: dict, ctx: Ctx) -> tuple[str | None, str | None]:
    """-> (exercise, problem). The one named in the sentence, else the model's, else the current one; it must still be skippable."""
    st = live.session_state()
    if not st:
        return None, "no plan"
    names = [r["name"] for r in st["exercises"]]
    said = set_slots.find_exercise(ctx.text.lower(), names)
    raw = said or (ex.resolve_exercise(str(args["exercise"]), names) if args.get("exercise") else None)
    row = next((r for r in st["exercises"] if r["name"] == raw), None)
    if row is None:
        cur = st.get("current")
        row = next((r for r in st["exercises"] if cur and r["name"] == cur["name"]), None)
    if row is None:
        return None, "no current exercise"
    if row["skipped"]:
        return row["name"], "already skipped"
    if row["done"] >= row["sets"]:
        return row["name"], "already done"
    return row["name"], None


def _skip_exercise(args: dict, ctx: Ctx) -> Verdict:
    target, problem = _skip_target(args, ctx)
    if target is None:
        return Verdict(False, "invalid", T(ctx.lang, "no_plan"), reason=problem or "")
    if problem:
        return Verdict(False, "invalid", reason=f"{target}: {problem}")      # no say: the model explains in a second round, with this reason
    intent = set_slots.skip_intent(ctx.text, ctx.plan_names())
    kind = intent["kind"]
    if kind == "skip":
        ctx.dlg.pending = None
        return Verdict(True, args={"exercise": target, "reason": args.get("reason") or "", "reaction": args.get("reaction")})
    p = ctx.dlg.get_pending()
    if p and p.get("kind") == "skip" and p.get("exercise") == target and bool(args.get("confirmed")) and kind not in ("negated", "question", "unskip") and _is_yes(ctx.text):
        ctx.dlg.pending = None
        return Verdict(True, args={"exercise": target, "reason": args.get("reason") or "", "reaction": args.get("reaction")})
    if kind in ("negated", "question", "unskip"):
        why = {"negated": "the athlete said they are NOT skipping", "question": "the athlete asked a question, it is not an order",
               "unskip": "the athlete wants something put back"}[kind]
        return Verdict(False, "refused", None, reason=f"Nothing was skipped: {why}.")
    q = T(ctx.lang, "skip_q", ex=target)
    return Verdict(False, "needs_clarification", q, expects="yes_no", reason="skip is not a clear order",
                   pending={"kind": "skip", "tool": "skip_exercise", "exercise": target, "question": q})


def _unskip_exercise(args: dict, ctx: Ctx) -> Verdict:
    if not live._skip_rows(session_flow.current_label()):
        return Verdict(False, "invalid", T(ctx.lang, "nothing_skipped"), reason="nothing skipped today")
    return Verdict(True, args={"exercise": args.get("exercise"), "everything": bool(args.get("everything")), "reaction": args.get("reaction")})


def _swap_exercise(args: dict, ctx: Ctx) -> Verdict:
    st = live.session_state()
    cur = (st or {}).get("current")
    names = ctx.plan_names()
    exn = (ex.resolve_exercise(str(args["exercise"]), names) if args.get("exercise") else None) or set_slots.find_exercise(ctx.text.lower(), names) or (cur["name"] if cur else None)
    if not exn:
        return Verdict(False, "invalid", T(ctx.lang, "which_ex"), reason="no exercise to swap")
    if SWAP_RE.search(ctx.text.lower()) and not SWAP_Q_RE.search(ctx.text.lower()) and not re.search(r"\bnot\b.{0,12}\b(swap|replace|switch|change)", ctx.text.lower()):
        return Verdict(True, args={"exercise": exn, "replacement": args.get("replacement") or "", "reaction": args.get("reaction")})
    p = ctx.dlg.get_pending()
    if p and p.get("kind") == "swap" and p.get("exercise") == exn and _is_yes(ctx.text):
        ctx.dlg.pending = None
        return Verdict(True, args={"exercise": exn, "replacement": args.get("replacement") or "", "reaction": args.get("reaction")})
    q = T(ctx.lang, "swap_q", ex=exn)
    return Verdict(False, "needs_clarification", q, expects="yes_no", reason="swap was not asked for", pending={"kind": "swap", "tool": "swap_exercise", "exercise": exn, "question": q})


def _end_workout(args: dict, ctx: Ctx) -> Verdict:
    low = ctx.text.lower()
    if END_HI_RE.search(low) and not re.search(r"\b(not|don'?t|nahi|mat)\b.{0,15}\b(wrap|done|finish|end|khatam)", low):
        return Verdict(True, args={"reaction": args.get("reaction")})
    p = ctx.dlg.get_pending()
    if p and p.get("kind") == "end" and _is_yes(ctx.text):
        ctx.dlg.pending = None
        return Verdict(True, args={"reaction": args.get("reaction")})
    q = T(ctx.lang, "end_q")
    return Verdict(False, "needs_clarification", q, expects="yes_no", reason="end was not asked for", pending={"kind": "end", "tool": "end_workout", "question": q})


def _log_note(args: dict, ctx: Ctx) -> Verdict:
    t = re.sub(r"\s+", " ", str(args.get("text") or ctx.text)).strip()[:300]
    kind = args.get("kind") if args.get("kind") in ("pain", "feeling", "preference", "other") else "other"
    if ctx.pain:
        kind = "pain"
    if kind == "other":
        return Verdict(False, "refused", None, reason="Nothing was saved: a note is only for pain, how they feel, or a preference. Just answer them.")
    return Verdict(True, args={"text": t, "kind": kind, "reaction": args.get("reaction")})


_CHECKS = {"log_set": _log_set, "correct_last_set": _correct_last_set, "control_rest": _control_rest, "set_next_weight": _set_next_weight, "skip_exercise": _skip_exercise,
           "unskip_exercise": _unskip_exercise, "swap_exercise": _swap_exercise, "end_workout": _end_workout, "log_note": _log_note}
