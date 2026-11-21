"""Universal voice command layer: "Hey coach, <anything the app can do>" -> one reply + UI actions for the phone.

    handle(transcript, screen, day, ambient, followup) -> dict   (a superset of live.handle's result, see _out)

Pipeline (first match wins, everything before the LLM router is deterministic, offline and instant):
  0. Hindi normalisation, wake-word leftovers stripped, care / safety language ALWAYS first
  1. pending dialogues: a confirm (yes / no), a food amount question, live's own half-described set
  2. matchers: rest timer, settings, close, meal queries (left / eaten / undo / repeat), food logging, navigation, workout (live.handle, no chat)
  3. LLM tool router (needs a key): coach tools + voice tools; anything destructive waits for a spoken yes
  4. no key / model failure: a helpful offline reply that names things that DO work by voice

Actions are only the shapes of mobile/src/voiceBus.ts (navigate, open, close, refresh, nutrition_day, insights_segment, rest, rest_add,
rest_skip, end, setting). Never invent others.
"""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date, timedelta

from backend.agents.base import have_key, run_agent
from backend.coach import care, hindi, lines, live, persona as P, set_slots, speech_fix
from backend.memory import db
from backend.policy.adaptation import has_safety_flag
from backend.tools import plan_edit as pe

TABS = ("today", "coach", "nutrition", "insights", "setup")
SCREENS = ("live", "agenda", "intake", "research", "trace", "knows", "decisions", "roundtable", "scorecard", "review", "server", "push", "diet")
SETTING_KEYS = ("wake", "vibration", "voice_mode", "persona")
REFRESH_WHAT = ("today", "nutrition", "plan", "decisions", "profile", "all")
PEND_TTL = 120.0
LAST_MEAL_TTL = 1800.0

_pend: dict = {}
_last_logged: dict = {"ids": [], "day": None, "ts": 0.0}
_lock = threading.Lock()


_turns: list[tuple[float, str, str]] = []     # (time, what the athlete said, what the coach answered): the router's short-term memory
TURN_TTL = 240.0


def _remember_turn(said: str, answered: str) -> None:
    _turns.append((time.time(), said[:300], answered[:400]))
    del _turns[:-6]


def _history() -> list[dict]:
    """The last few voice turns as chat history, so 'sixty' after 'What weight did you use?' still means something to the router."""
    now = time.time()
    out: list[dict] = []
    for ts, said, ans in _turns:
        if now - ts < TURN_TTL:
            out += [{"role": "user", "content": said}, {"role": "assistant", "content": ans}]
    return out


def reset() -> None:
    """Forget every pending dialogue (tests, and a fresh app start)."""
    _pend.clear()
    _turns.clear()
    _last_logged.update(ids=[], day=None, ts=0.0)


SET_PEND_TTL = 90.0
_SET_WORDS = {"kg", "kgs", "kilo", "kilos", "kilogram", "kilograms", "lb", "lbs", "pound", "pounds", "rep", "reps", "for", "x", "at", "by", "and", "times", "same", "as",
              "last", "time", "set", "sets", "rpe", "a", "of", "it", "was", "i", "did", "got", "with", "on", "plus", "point", "half", "bodyweight", "body", "weight", "usual"}


def looks_like_set_answer(low: str) -> bool:
    """'80', '8 reps', '80 for 8', 'eighty kilos', 'same as last time': the shapes of an answer to 'how heavy / how many reps?'."""
    d = _num_words(low.replace("@", " at ").replace("×", " x "))
    toks = re.findall(r"[a-z]+|\d+(?:\.\d+)?", d)
    if not toks or len(toks) > 9:
        return False
    if not all(t in _SET_WORDS or re.fullmatch(r"\d+(?:\.\d+)?", t) for t in toks):
        return False
    return any(re.fullmatch(r"\d+(?:\.\d+)?", t) for t in toks) or "same" in toks or "usual" in toks or "bodyweight" in toks


def _pend_get(kind: str | None = None) -> dict | None:
    p = _pend
    if p and time.time() - p.get("ts", 0) < PEND_TTL and (kind is None or p.get("kind") == kind):
        return p
    if p and time.time() - p.get("ts", 0) >= PEND_TTL:
        _pend.clear()
    return None


def _set_pend(**kw) -> None:
    _pend.clear()
    _pend.update(kw, ts=time.time())


# ------------------------------------------------------------------ result
def _out(intent: str, reply: str, actions: list | None = None, expects: str | None = None, **extra) -> dict:
    base = {"reply": reply, "intent": intent, "actions": actions or [], "expects": expects, "moment": False, "pr": False, "safety_flag": None,
            "state": None, "saved": [], "next_set": None}
    base.update(extra)
    base["actions"] = [a for a in base["actions"] if valid_action(a)]
    return base


def valid_action(a) -> bool:
    """Mirror of parseAction() in mobile/src/voiceBus.ts."""
    if not isinstance(a, dict):
        return False
    t = a.get("type")
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v == v
    if t == "navigate":
        return a.get("tab") in TABS
    if t == "open":
        return a.get("screen") in SCREENS
    if t in ("close", "rest_skip", "end"):
        return True
    if t == "refresh":
        return isinstance(a.get("what", []), list) and all(x in REFRESH_WHAT for x in a.get("what", []))
    if t == "nutrition_day":
        return isinstance(a.get("day"), str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", a["day"]))
    if t == "insights_segment":
        return isinstance(a.get("seg"), str) and bool(a["seg"])
    if t == "rest":
        return num(a.get("seconds")) and 0 < a["seconds"] <= 900
    if t == "rest_add":
        return num(a.get("seconds")) and 0 < a["seconds"] <= 300
    if t == "setting":
        return a.get("key") in SETTING_KEYS and isinstance(a.get("value"), (str, bool))
    return False


def _from_live(res: dict, **over) -> dict:
    out = _out(res.get("intent", "fallback"), res.get("reply", ""), actions=list(res.get("actions") or []), expects=res.get("expects"))
    for k in ("moment", "pr", "safety_flag", "state", "saved", "next_set", "prs", "finished"):
        if k in res:
            out[k] = res[k]
    out.update(over)
    return out


def _ctx() -> tuple[str, str]:
    prof = db.get_profile() or {}
    return prof.get("coach_persona", P.DEFAULT), prof.get("name", "")


def _num_words(text: str) -> str:
    """'three fifty' -> '350' (a spoken amount), then the usual words-to-digits for the rest."""
    ones = {w: i for i, w in enumerate("one two three four five six seven eight nine".split(), 1)}
    tens = {w: i * 10 for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)}
    pat = re.compile(r"\b(" + "|".join(ones) + r")\s+(" + "|".join(tens) + r")(?:[\s-]+(" + "|".join(ones) + r"))?\b")
    text = pat.sub(lambda m: str(ones[m.group(1)] * 100 + tens[m.group(2)] + ones.get(m.group(3) or "", 0)), text.lower())
    return set_slots.words_to_digits(text)


# ------------------------------------------------------------------ wake leftovers and dates
def _wake_re() -> re.Pattern:
    names = {v["name"].lower() for v in P.PERSONAS.values()}
    return re.compile(r"^\W*(?:(?:hey|hi|hello|ok|okay|yo)\W+)?(?:coach|" + "|".join(map(re.escape, names)) + r")\b[\s,.:!-]*", re.I)


HI_WORDS = {"प्रोफाइल": "profile", "प्रोफ़ाइल": "profile", "प्रोग्रेस": "progress", "कैलेंडर": "calendar", "कैलेण्डर": "calendar", "न्यूट्रिशन": "nutrition",
            "खाना": "food", "सेटिंग": "settings", "सेटिंग्स": "settings", "वर्कआउट": "workout", "चैट": "chat", "कोच": "coach", "टुडे": "today"}


def _hi_pre(text: str) -> str:
    """The few Devanagari command shapes the English matchers cannot see: '... खाए' (ate), '... खोलो' (open ...), wake words."""
    t = text
    t = re.sub(r"^(?:हे|ओके|अरे)\s+कोच\W*", "hey coach ", t)
    for hi, en in HI_WORDS.items():
        t = t.replace(hi, en)
    m = re.search(r"^(?:hey coach\s+)?(?:मैंने\s+|मैने\s+)?(.+?)\s*(?:खाए|खाया|खाई|खाये|खा लिया|खा ली|खाया है)\W*$", t)
    if m:
        return ("hey coach " if t.startswith("hey coach") else "") + "i ate " + m.group(1).strip()
    m = re.search(r"^(?:hey coach\s+)?(.+?)\s*(?:खोलो|खोल दो|खोलिए|दिखाओ|दिखा दो|ले चलो|खोल)\W*$", t)
    if m:
        return ("hey coach " if t.startswith("hey coach") else "") + "open " + m.group(1).strip()
    return t


def strip_wake(text: str) -> tuple[str, bool]:
    t, hit = text.strip(), False
    for _ in range(2):
        m = _wake_re().match(t)
        if not m or m.end() == 0:
            break
        t, hit = t[m.end():].strip(), True
    t = re.sub(r"[\s,.]*\bplease\W*$", "", t, flags=re.I).strip()
    return t, hit


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_day(text: str, today: date | None = None) -> tuple[str | None, str]:
    """-> (ISO day or None when none was said, text without the date words)."""
    today = today or date.today()
    low = text
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", low)
    if m:
        try:
            return date.fromisoformat(m.group(1)).isoformat(), (low[:m.start()] + low[m.end():]).strip()
        except ValueError:
            pass
    for pat, delta in ((r"\bday before yesterday\b|\b(?:two|2) days ago\b", 2), (r"\byesterday\b|\blast night\b", 1)):
        m = re.search(pat, low, re.I)
        if m:
            return (today - timedelta(days=delta)).isoformat(), (low[:m.start()] + low[m.end():]).strip()
    m = re.search(r"\b(?:on |last |this )?(" + "|".join(WEEKDAYS) + r")\b", low, re.I)
    if m:
        wd = WEEKDAYS.index(m.group(1).lower())
        back = (today.weekday() - wd) % 7
        if back == 0 and "last" in m.group(0).lower():
            back = 7
        return (today - timedelta(days=back)).isoformat(), (low[:m.start()] + low[m.end():]).strip()
    m = re.search(r"\b(today|tonight|this morning|this afternoon|this evening)\b", low, re.I)
    if m:
        return today.isoformat(), (low[:m.start()] + low[m.end():]).strip()
    return None, text


# ------------------------------------------------------------------ meals
FOOD_VERB_RE = re.compile(
    r"^(?:(?:and|also|so|um|uh)\s+)?(?:i\s+(?:just\s+|also\s+|only\s+)?(?:ate|had|eaten|consumed|drank|am eating|'?m eating|have (?:had|eaten))|i'?ve\s+(?:just\s+)?(?:eaten|had)|"
    r"(?:just )?(?:log|add|record|track|put|note down|enter)(?: (?:in|down))?(?: (?:a|an|my|the|some))?|we (?:ate|had)|had|ate)\s+(?P<rest>.+)$")
SLOT_TAIL_RE = re.compile(r"\s*\b(?:for|to|in|as)\s+(?:my\s+|the\s+)?(?:breakfast|lunch|dinner|snack|supper|brunch|meal)\b\s*", re.I)
SETTY_RE = re.compile(r"\b(sets?|reps?|rep|workout|session|exercise|kg|kgs|pounds|lbs|rpe)\b")
LEFT_RE = re.compile(r"\b(?:how (?:many|much) (calories?|kcal|protein|carbs?|fat)|what(?:'?s| is| are)? (?:my )?(calories?|protein|macros?)).{0,25}\b(left|remaining|remain|to go|do i have|have i got|can i eat)\b|"
                     r"\b(calories?|protein|macros?) (left|remaining)\b|\bhow'?s my (?:nutrition|food|diet|calories)\b|\bhow am i doing (?:on|with) (?:food|calories|protein|nutrition)\b")
ATE_Q_RE = re.compile(r"\b(?:what|which)\b.{0,12}\b(?:did i|have i)\b.{0,10}\b(?:eat|eaten|have|log|logged)\b|\bwhat have i (?:eaten|had)\b|\bwhat (?:i )?ate\b|\bwhat'?s (?:been )?logged\b")
UNDO_MEAL_RE = re.compile(r"\b(?:(?:undo|remove|delete|erase|scratch|cancel|take off)\b.{0,25}\b(?:last|latest|previous|that|the)\b.{0,12}\b(?:meal|food|entry|item|log)\b|"
                          r"(?:undo|remove|delete|scratch) (?:that|it|the last one)|that was wrong|my mistake remove)")
UNDO_BARE_RE = re.compile(r"^(?:undo|undo that|undo it|remove that|delete that|scratch that|take that back)\W*$")
REPEAT_RE = re.compile(r"\b(?:repeat|again|same as)\b.{0,20}\b(?:last|previous)\s+(?:meal|food|one|thing)|\b(?:repeat|log) (?:my )?(?:last|previous) (?:meal|food)\b|\bsame (?:meal )?again\b|\bi had the same\b")
USUAL_RE = re.compile(r"\b(usual|normal|regular|default|typical|same as (?:always|usual|before)|standard)\b")
UNIT_WORDS = {"cup", "cups", "bowl", "bowls", "plate", "plates", "glass", "glasses", "serving", "servings", "spoon", "spoons", "tbsp", "handful", "handfuls", "scoop", "scoops"}


def _find_food(name):
    from backend.routes_portion import _find_food as ff
    return ff(name)


def _count_mode(name: str) -> tuple[bool, str, float]:
    """-> (counted by pieces?, unit label, grams of one unit)"""
    from backend.routes_portion import portion_mode
    hit = _find_food(name)
    if not hit:
        return False, "g", 0.0
    n, f = hit
    return portion_mode(n, f["unit"]) == "count", f["unit"], float(f["unit_g"])


def _plural(w: str) -> str:
    return w if w.endswith("s") else (w + "es" if w.endswith(("sh", "ch", "x")) else w + "s")


def _describe(it: dict) -> str:
    counted, unit, unit_g = _count_mode(it["name"])
    g = it.get("grams") or 0
    if counted and unit_g and g:
        n = round(g / unit_g, 1)
        n = int(n) if n == int(n) else n
        return f"{n} {it['name'] if n == 1 else _plural(it['name'])}"
    if g:
        return f"{g} g {it['name']}"
    return it["name"]


def _question(it: dict) -> str:
    counted, unit, _ = _count_mode(it["name"])
    if counted:
        what = _plural(it["name"]) if (unit == it["name"] or it["name"].endswith(unit)) else f"{_plural(unit)} of {it['name']}"
        return f"How many {what}?"
    from backend.routes_portion import _LIQUIDS
    return f"How many {'ml' if it['name'] in _LIQUIDS else 'grams'} of {it['name']}?"


def _day_label(iso: str) -> str:
    d, t = date.fromisoformat(iso), date.today()
    if d == t:
        return "today"
    if d == t - timedelta(days=1):
        return "yesterday"
    return d.strftime("%A") if (t - d).days < 7 else d.strftime("%d %b")


def _left_phrase(day: str) -> str:
    from backend.tools.meal_log import today_summary
    s = today_summary(day)
    rem, when = s.get("remaining"), _day_label(day)
    if not rem:
        return f"That is {round(s['eaten']['kcal'])} calories and {round(s['eaten']['protein'])} grams of protein {when} so far; no target is set yet."
    kc, pr = round(rem["kcal"]), round(rem["protein"])
    k = f"{kc} calories left" if kc >= 0 else f"{-kc} calories over"
    p = f"{pr} grams of protein left" if pr >= 0 else f"{-pr} grams of protein over"
    return f"{k} and {p} {when}."


def _meal_actions(day: str, screen: str, shown: str | None) -> list[dict]:
    acts = []
    if day != (shown or date.today().isoformat()):
        acts.append({"type": "nutrition_day", "day": day})
    acts.append({"type": "refresh", "what": ["nutrition"]})
    return acts


def _commit(items: list[dict], day: str, screen: str, shown: str | None) -> dict:
    ids = [db.add_meal(day, it["name"], it.get("grams"), it["kcal"], it["protein"], it["carbs"], it["fat"], bool(it.get("estimated"))) for it in items]
    _last_logged.update(ids=ids, day=day, ts=time.time())
    _pend.clear()
    said = ", ".join(_describe(i) for i in items[:4]) + (f" and {len(items) - 4} more" if len(items) > 4 else "")
    est = " (my estimate)" if any(i.get("estimated") for i in items) else ""
    when = "" if day == date.today().isoformat() else f" for {_day_label(day)}"
    return _out("meal", f"Logged {said}{est}{when}. {_left_phrase(day)}", actions=_meal_actions(day, screen, shown), moment=True,
                saved=[{"meal_id": i} for i in ids])


def _ask_next(pend: dict) -> dict:
    it = pend["todo"][0]
    return _out("ask_food_amount", _question(it), expects="food_amount")


def _food(text: str, screen: str, shown: str | None, verb_required: bool = True) -> dict | None:
    m = FOOD_VERB_RE.match(text)
    if not m:
        return None
    rest = m.group("rest").strip()
    if SETTY_RE.search(rest.lower()) and not re.search(r"\b(kg|g|grams?)\b.*\b(rice|chicken|oats|dal|paneer)\b", rest.lower()):
        return None
    iso, rest = parse_day(rest)
    rest = SLOT_TAIL_RE.sub(" ", rest).strip(" ,.")
    rest = re.sub(r"^(?:today|just|some|my|the)\s+", "", rest).strip()
    if not rest:
        return None
    from backend.tools.meal_log import parse_for_log
    items, unknown = parse_for_log(rest)
    if not items:
        verb = text.lower().split()[0] if text else ""
        if verb in ("log", "add", "record", "track") and not unknown:
            return None
        if verb in ("log", "add", "record", "track", "put", "enter") and not re.match(r"^(?:i |we |had|ate)", text.lower()):
            return None   # "log bench 80 for 8": that is the workout side, not an unknown food
        if have_key():
            return None     # not a food we know: "I had a meeting" goes on to the model / general chat
        return _out("meal_unclear", "I didn't catch a food in that. Try: I ate two eggs.")
    day = iso or (shown if screen == "nutrition" and shown else date.today().isoformat())
    done, todo = [], []
    for it in items:
        known = _find_food(it["name"]) and not it.get("estimated")
        (done if (it.get("qty_given") or not known) else todo).append(it)
    unk = f" I didn't catch {', '.join(unknown[:2])}." if unknown else ""
    if not todo:
        res = _commit(done, day, screen, shown)
        res["reply"] += unk
        return res
    _set_pend(kind="food", day=day, done=done, todo=todo, screen=screen, shown=shown, tries=0)
    res = _ask_next(_pend)
    if unk:
        res["reply"] = unk.strip() + " " + res["reply"]
    return res


def _food_answer(text: str, low: str) -> dict | None:
    """The next utterance answers 'how many eggs?'. None = not an answer to this (the caller drops the question and carries on)."""
    pend = _pend_get("food")
    if not pend:
        return None
    if CANCEL_RE.search(low):
        _pend.clear()
        return _out("cancelled", "Okay, I won't log that.")
    if OPEN_VERB_RE.match(low) or FOOD_VERB_RE.match(low) or live.START_RE.match(low) or live.START_BARE_RE.match(low)             or re.match(r"^(?:turn|switch|change|set|close|go back|rebuild|swap|add)\b", low):
        return None         # a clearly different command: the amount question is dropped, not parsed as an amount
    it = pend["todo"][0]
    counted, unit, unit_g = _count_mode(it["name"])
    d = _num_words(low)
    grams = None
    if USUAL_RE.search(low) or re.match(r"^(?:same|the same|yes|yeah|ok|okay|sure)\W*$", low):
        grams = it["grams"]
    else:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(kg|kilos?|kilograms?|ml|g|grams?|gms?)?", d)
        if re.search(r"\b(?:a couple|couple)\b", low):
            n, u = 2.0, None
        elif re.search(r"^\W*(?:a|an|one)\W*$", low):
            n, u = 1.0, None
        elif re.search(r"\bhalf\b", low) and not m:
            n, u = 0.5, None
        elif m:
            n, u = float(m.group(1)), (m.group(2) or "")
        else:
            n = u = None
        if n is not None:
            if u.startswith("k") if u else False:
                grams = n * 1000
            elif u and (u.startswith("g") or u == "ml"):
                grams = n
            elif counted or any(w in low.split() for w in UNIT_WORDS) or n <= 10 and not counted and unit_g:
                grams = n * unit_g if (counted or n <= 10) and unit_g else n
            else:
                grams = n
            if counted and (n <= 0 or n > 60):
                grams = None
            if not counted and grams is not None and not 5 <= grams <= 3000:
                grams = None
    if grams is None:
        if len(low.split()) > 3 or OPEN_VERB_RE.match(low) or FOOD_VERB_RE.match(low):
            return None
        pend["tries"] = pend.get("tries", 0) + 1
        if pend["tries"] >= 3:
            _pend.clear()
            return _out("cancelled", "No problem, I'll leave that one out. Say it again with the amount when you're ready.")
        return _out("ask_food_amount", _question(it), expects="food_amount")
    from backend.tools.foods import macros
    hit = _find_food(it["name"])
    row = {"name": hit[0], "grams": round(grams), **macros(hit[1], grams), "estimated": False, "qty_given": True}
    pend["done"].append(row)
    pend["todo"].pop(0)
    pend["ts"] = time.time()
    if pend["todo"]:
        return _ask_next(pend)
    return _commit(pend["done"], pend["day"], pend["screen"], pend["shown"])


def _last_meal_ids() -> tuple[list[int], str | None]:
    if _last_logged["ids"] and time.time() - _last_logged["ts"] < LAST_MEAL_TTL:
        live_ids = [i for i in _last_logged["ids"] if db.get_meal(i)]
        if live_ids:
            return live_ids, _last_logged["day"]
    with db.conn() as c:
        r = c.execute("SELECT id, day FROM meals ORDER BY id DESC LIMIT 1").fetchone()
    return ([r[0]], r[1]) if r else ([], None)


def _undo_meal(screen: str, shown: str | None) -> dict:
    ids, day = _last_meal_ids()
    if not ids:
        return _out("meal_undo", "There is no meal to remove.")
    names = []
    for i in ids:
        m = db.get_meal(i)
        if m:
            names.append(_describe({"name": m["name"], "grams": round(m["grams"] or 0)}))
            db.delete_meal(i)
    _last_logged.update(ids=[], ts=0.0)
    return _out("meal_undo", f"Removed {', '.join(names)}. {_left_phrase(day)}", actions=_meal_actions(day, screen, shown), saved=[])


def _repeat_meal(screen: str, shown: str | None, day: str | None) -> dict:
    from backend.tools.meal_log import repeat_meal
    ids, _ = _last_meal_ids()
    if not ids:
        return _out("meal_repeat", "I don't have a previous meal to repeat yet.")
    target = day or date.today().isoformat()
    names, new = [], []
    for i in ids:
        m = repeat_meal(i, target)
        if m:
            names.append(_describe({"name": m["name"], "grams": round(m["grams"] or 0)}))
    if not names:
        return _out("meal_repeat", "I couldn't repeat that one.")
    with db.conn() as c:
        new = [r[0] for r in c.execute("SELECT id FROM meals ORDER BY id DESC LIMIT ?", (len(names),)).fetchall()][::-1]
    _last_logged.update(ids=new, day=target, ts=time.time())
    return _out("meal_repeat", f"Logged {', '.join(names)} again. {_left_phrase(target)}", actions=_meal_actions(target, screen, shown), moment=True)


# 'how much protein', 'tell me how much protein', 'my protein', 'how many calories do I need today' (no food named after 'in'): the numbers are in the app, no model needed
PROTEIN_Q_RE = re.compile(r"^(?:(?:then|and|so|okay|ok)\s+)*(?:tell me\s+)?(?:how much|how many|what(?:'s| is)(?: my)?|my)\s+(?:more\s+)?(?:protein|calories|carbs|fat)(?:\s+(?:do i (?:need|have|want)|should i (?:eat|have)|am i (?:at|on)|left|more|today|so far))*\s*\??$")


def _meal_queries(low: str, text: str, screen: str, shown: str | None) -> dict | None:
    iso, _ = parse_day(low)
    day = iso or (shown if screen == "nutrition" and shown else date.today().isoformat())
    if REPEAT_RE.search(low):
        return _repeat_meal(screen, shown, iso)
    if UNDO_MEAL_RE.search(low) or (UNDO_BARE_RE.match(low) and time.time() - _last_logged["ts"] < LAST_MEAL_TTL and _last_logged["ids"]):
        return _undo_meal(screen, shown)
    if LEFT_RE.search(low) or PROTEIN_Q_RE.match(low):
        return _out("meal_left", _left_phrase(day))
    if ATE_Q_RE.search(low):
        meals = db.get_meals(day)
        if not meals:
            return _out("meal_list", f"Nothing logged {_day_label(day)} yet.")
        agg: dict[str, float] = {}
        for m in meals:
            agg[m["name"]] = agg.get(m["name"], 0) + (m["grams"] or 0)
        said = ", ".join(_describe({"name": n, "grams": round(g)}) for n, g in list(agg.items())[:6])
        return _out("meal_list", f"{_day_label(day).capitalize()} you had {said}. {_left_phrase(day)}")
    return None


# ------------------------------------------------------------------ navigation
OPEN_VERB_RE = re.compile(r"^(?:can you |could you |would you |please )?(?:open|go to|go over to|show me|show|take me to|take me|bring up|pull up|switch to|navigate to|jump to|let me see|i want to see|i wanna see|launch|see)\s+(?:up\s+)?(?:(?:my|the|our|your|a)\s+)*(?P<what>.+)$")
Q_NAV_RE = re.compile(r"^(?:what|which)\b.{0,10}\b(?:do you know about me|did the coaches do|have the coaches done|did the agents do|is on my calendar|are my decisions)\b")

# (pattern on the target phrase, actions, reply). More specific entries first.
NAV: list[tuple[str, list[dict], str]] = [
    (r"server( settings)?|connection|backend|api address|ip address", [{"type": "navigate", "tab": "setup"}, {"type": "open", "screen": "server"}], "Opening the server settings."),
    (r"(push |notification|notifications)( settings)?|notifications?|alerts( settings)?", [{"type": "navigate", "tab": "setup"}, {"type": "open", "screen": "push"}], "Opening notification settings."),
    (r"calendar|agenda|trips?|travel plans?|schedule|events", [{"type": "navigate", "tab": "setup"}, {"type": "open", "screen": "agenda"}], "Opening your calendar."),
    (r"(update|redo|edit|change)?\s*(profile )?(interview|intake|onboarding)|update (my )?profile|profile interview", [{"type": "navigate", "tab": "setup"}, {"type": "open", "screen": "intake"}], "Opening the profile interview."),
    (r"evidence|research|studies|sources|science", [{"type": "navigate", "tab": "setup"}, {"type": "open", "screen": "research"}], "Opening the evidence library."),
    (r"trace|what (the )?(coaches|agents|team) did|agent (log|activity)|activity log", [{"type": "navigate", "tab": "setup"}, {"type": "open", "screen": "trace"}], "Opening the trace of what the coaches did."),
    (r"what (do )?you know about me|memory|what the coach knows|what you remember|your knowledge|coach knows", [{"type": "navigate", "tab": "setup"}, {"type": "open", "screen": "knows"}], "Opening what I know about you."),
    (r"round ?table|team meeting|coach(es)? meeting|team debate", [{"type": "open", "screen": "roundtable"}], "Opening the roundtable."),
    (r"score ?card|report card|team score", [{"type": "open", "screen": "scorecard"}], "Opening the scorecard."),
    (r"decisions?|approvals?|team|pending (changes|approvals)|proposals?", [{"type": "open", "screen": "decisions"}], "Opening the team's decisions."),
    (r"(weekly )?review", [{"type": "open", "screen": "review"}], "Opening your review."),
    (r"live( session)?|(the )?session screen", [{"type": "open", "screen": "live"}], "Opening your live session."),
    (r"profile|settings|setup|set up|preferences|my account|me", [{"type": "navigate", "tab": "setup"}], "Opening your profile and settings."),
    (r"(diet|nutrition) (coach|consultant|consulting|advisor|adviser|expert|agent|assistant)|dietitian|dietician|nutritionist|(diet|meal|eating|food) plans?|plan my (meals|diet|food)", [{"type": "navigate", "tab": "nutrition"}, {"type": "open", "screen": "diet"}], "Opening your diet coach."),
    (r"fuel|food|nutrition|meals?|diet|macros|calories|what i ate|eating|food log", [{"type": "navigate", "tab": "nutrition"}], "Opening Fuel."),
    (r"progress|insights?|stats|statistics|charts?|graphs?|trends?|numbers|lifts?|my gains", [{"type": "navigate", "tab": "insights"}, {"type": "insights_segment", "seg": "trends"}], "Opening your progress."),
    (r"coach( chat)?|chat|talk to (the )?coach|messages", [{"type": "navigate", "tab": "coach"}], "Opening the coach chat."),
    (r"today|workout( plan)?|training( plan)?|plan|home|exercises|my (split|program)|session", [{"type": "navigate", "tab": "today"}], "Opening today's plan."),
]
_NAV_RE = [(re.compile(r"^(?:" + p + r")(?: (?:tab|screen|page|section))?$"), a, r) for p, a, r in NAV]
CLOSE_RE = re.compile(r"^(?:close|close (?:this|that|it)|go back|back|dismiss|exit|go home back|leave this|close the screen|close this screen|close the window|never ?mind(?: close)?)\W*$")


def _nav(low: str, shown: str | None) -> dict | None:
    if CLOSE_RE.match(low):
        return _out("close", "Closed.", actions=[{"type": "close"}])
    if re.match(r"^(?:update|redo|edit|redo|refresh|re-?do) (?:my )?(?:profile|details|info|information)$", low):
        return _out("navigate", "Opening the profile interview.", actions=[{"type": "navigate", "tab": "setup"}, {"type": "open", "screen": "intake"}])
    m = OPEN_VERB_RE.match(low)
    what = m.group("what").strip(" .,!?") if m else None
    if what is None and Q_NAV_RE.search(low):
        what = "what you know about me" if "know about me" in low else "what the coaches did" if "coaches" in low or "agents" in low else "calendar"
    if what is None:
        return None
    iso, w2 = parse_day(what)
    if iso and re.search(r"\b(meals?|food|nutrition|fuel|eating|ate|diet)\b", what):
        return _out("navigate", f"Showing {_day_label(iso)}'s meals.", actions=[{"type": "navigate", "tab": "nutrition"}, {"type": "nutrition_day", "day": iso}])
    what = re.sub(r"^(?:(?:my|the|our|your|a)\s+)+", "", what)
    for rx, acts, reply in _NAV_RE:
        if rx.match(what):
            return _out("navigate", reply, actions=[dict(a) for a in acts])
    return None


# ------------------------------------------------------------------ rest timer, settings
REST_ADD_RE = re.compile(r"\b(?:rest\s*\+\s*|add |plus |another |give me |need |extend (?:the )?rest by |more rest\W+)(\d{1,3})\s*(?:more\s*)?(sec|secs|seconds?|s|min|mins|minutes?)?\b.*\b(?:rest|seconds?|more)?|\b(\d{1,3})\s*(sec|secs|seconds?)\s*more\b")
REST_SKIP_RE = re.compile(r"\b(?:skip|end|stop|cancel|done with|finish|no more)\b.{0,10}\brest\b|\brest(?: is)? over\b|\bskip the (?:timer|rest)\b|\bready to go\b.*\brest\b|\bi'?m rested\b")
REST_START_RE = re.compile(r"^(?:start |set |begin )?(?:a )?(?:rest|timer)(?: timer)?(?: (?:for|of))? (\d{1,3})\s*(sec|secs|seconds?|min|mins|minutes?)$")


def _secs(n: str, unit: str | None) -> int:
    v = int(n)
    return v * 60 if unit and unit.startswith("min") else v


def _rest(low: str) -> dict | None:
    d = _num_words(low)
    if REST_SKIP_RE.search(d):
        return _out("rest_skip", "Rest skipped. Go when you're ready.", actions=[{"type": "rest_skip"}])
    m = re.search(r"\b(?:add|plus|another|extra|more|give me|need|\+)\s*(\d{1,3})\s*(min|mins|minutes?|sec|secs|seconds?)?\b", d)
    if m and re.search(r"\b(rest|seconds?|secs?|timer|more|minutes?)\b", d) and ("rest" in d or "timer" in d or "more" in d or "+" in d or "add" in d):
        s = _secs(m.group(1), m.group(2) or "sec")
        if 0 < s <= 300:
            return _out("rest_add", f"Added {s} seconds of rest.", actions=[{"type": "rest_add", "seconds": s}])
    m = REST_START_RE.match(d.strip(" .,"))
    if m:
        s = _secs(m.group(1), m.group(2))
        if 0 < s <= 900:
            return _out("rest", f"Resting for {s} seconds.", actions=[{"type": "rest", "seconds": s}])
    return None


ON_RE, OFF_RE = r"\b(?:on|enable|enabled|start)\b", r"\b(?:off|disable|disabled|stop|mute|quiet)\b"


def _persona_from(text: str) -> str | None:
    t = text.lower()
    for pid, v in P.PERSONAS.items():
        if re.search(r"\b" + re.escape(pid) + r"\b", t) or re.search(r"\b" + re.escape(v["name"].lower()) + r"\b", t):
            return pid
    for alias, pid in (("hype", "hype"), ("calm", "mentor"), ("mentor", "mentor"), ("strict", "straight"), ("direct", "straight"), ("harsh", "roast"), ("desi", "haryanvi")):
        if re.search(r"\b" + alias + r"\b", t):
            return pid
    return None


def _settings(low: str) -> dict | None:
    m = re.search(r"\b(?:change|switch|set|make|use|swap)\b.{0,25}\b(?:coach|persona|trainer|voice|style|personality)\b.{0,12}\b(?:to|into)\b(?P<p>.+)$", low) \
        or re.search(r"\b(?:switch to|use|give me|i want|i'?d like|let me talk to|talk to)\b\s+(?P<p>.+?)\s*(?:coach|persona|mode|style)?$", low) if re.search(r"\b(coach|persona|trainer|personality|mode)\b", low) else None
    if m and re.search(r"\b(coach|persona|trainer|personality|mode|voice)\b", low):
        pid = _persona_from(m.group("p"))
        if pid:
            prof = db.get_profile()
            if not prof:
                return _out("setting", "Finish setup first, then I can change your coach.")
            prof["coach_persona"] = pid
            db.save_profile(prof)
            return _out("setting", f"Done. {P.get(pid)['name']} is your coach now.", actions=[{"type": "setting", "key": "persona", "value": pid}], moment=True)
        names = ", ".join(v["name"] for v in P.PERSONAS.values())
        return _out("setting", f"I can switch to {names}. Which one?")
    if re.search(r"\b(?:hey coach|wake word|wake up word|wake-word|always listening|hands[- ]?free|hey coach mode|voice wake)\b", low) and re.search(ON_RE + "|" + OFF_RE, low):
        on = not re.search(OFF_RE, low)
        return _out("setting", "Hey coach is on." if on else "Okay, Hey coach is off. You can switch it back on in Settings.", actions=[{"type": "setting", "key": "wake", "value": on}])
    if re.search(r"\b(?:vibration|vibrate|haptics?|buzz)\b", low) and re.search(ON_RE + "|" + OFF_RE, low):
        on = not re.search(OFF_RE, low)
        return _out("setting", "Vibration is on." if on else "Vibration is off.", actions=[{"type": "setting", "key": "vibration", "value": on}])
    return None


# ------------------------------------------------------------------ confirmations
YES_RE = re.compile(r"^\W*(?:yes|yeah|yep|yup|sure|confirm(?:ed)?|do it|go ahead|go for it|ok|okay|please do|approve|that'?s right|correct|affirmative|haan|han|ha|theek hai|kar do)\b[\w\s,.!']{0,20}$")
CANCEL_RE = re.compile(r"^\W*(?:no|nope|nah|cancel|never ?mind|don'?t|do not|stop|forget it|leave it|not now|abort|nahi|mat karo|rehne do)\b[\w\s,.!']{0,20}$")


def _ask_confirm(op: str, args: dict, say: str, kind: str = "tool") -> dict:
    _set_pend(kind="confirm", op=op, args=args, say=say, what=kind)
    return {"status": "needs_confirm", "say": say}


def _confirmed() -> dict:
    p = dict(_pend_get("confirm") or {})
    _pend.clear()
    try:
        res = _EXEC[p["op"]](**p["args"])
    except Exception as e:  # never let a stored action crash the turn
        return _out("confirm_failed", f"I couldn't do that: {str(e)[:120]}")
    return _result_from_tool(p["op"], res)


def _result_from_tool(op: str, res: dict) -> dict:
    ok = (res or {}).get("status") in ("applied", "reverted", "ok", "done")
    msg = (res or {}).get("summary") or (res or {}).get("message") or ((res or {}).get("error") or "")
    if not ok:
        reasons = " ".join((res or {}).get("reasons") or []) or msg or "That didn't go through."
        return _out("confirm_failed", reasons)
    return _out(op, f"Done. {msg}".strip(), actions=[{"type": "refresh", "what": ["plan", "today", "profile", "decisions"]}], moment=True)


# ------------------------------------------------------------------ voice tools (used by the LLM router; also directly testable)
def _t(name, desc, props, required=None):
    return {"name": name, "description": desc, "input_schema": {"type": "object", "properties": props, **({"required": required} if required else {})}}


S, I, N = {"type": "string"}, {"type": "integer"}, {"type": "number"}
VOICE_TOOLS = [
    _t("swap_exercise", "Swap an exercise for a safe like-for-like alternative. scope today = only today's session, plan = permanently. Omit replacement to pick the best safe one.",
       {"exercise": S, "replacement": S, "scope": {"type": "string", "enum": ["today", "plan"]}}, ["exercise"]),
    _t("add_agenda_event", "Add a calendar event. kind: competition | busy | travel | equipment | no_gym. Dates YYYY-MM-DD resolved from today's date in the prompt. end defaults to start.",
       {"kind": {"type": "string", "enum": ["competition", "busy", "travel", "equipment", "no_gym"]}, "start": S, "end": S, "label": S, "minutes": I, "equipment": {"type": "array", "items": S}}, ["kind", "start"]),
    _t("log_checkin", "Log how the athlete feels today: sleep_hours, soreness 1-5, energy 1-5, stress 1-5 (stored in the note), a note.",
       {"sleep_hours": N, "soreness": I, "energy": I, "stress": I, "note": S}),
    _t("change_profile_field", "Change a profile field: goal (cut|bulk|maintain), days_per_week (3-5), injuries (text to add), weight (kg), experience, activity. Goal, days and injuries rebuild the plan and need a spoken confirm first.",
       {"field": {"type": "string", "enum": ["goal", "days_per_week", "injuries", "weight", "experience", "activity"]}, "value": S}, ["field", "value"]),
    _t("list_pending_decisions", "Decisions from the team that wait for the athlete's approval, with ids and plain summaries.", {}),
    _t("approve_decision", "Approve one pending decision (the summary is read aloud and the athlete confirms first).", {"decision_id": I}, ["decision_id"]),
    _t("decline_decision", "Decline one pending decision.", {"decision_id": I}, ["decision_id"]),
    _t("ask_evidence", "Answer an evidence question from the cited research library.", {"question": S}, ["question"]),
]
GATED = {"restructure_split", "rebuild_plan", "set_days_per_week", "set_goal", "forget_this", "undo_decision", "undo_last_change", "clear_restriction"}


def _day_index_today() -> int:
    v = pe.plan_view()
    if not v:
        raise ValueError("No plan yet")
    ov = v.get("today_override")
    return ov["day_index"] if ov else v["today_index"]


def t_swap_exercise(exercise: str, replacement: str = "", scope: str = "today", **_) -> dict:
    from backend.tools import exercises as ex
    scope = scope if scope in ("today", "plan") else "today"
    try:
        idx = _day_index_today()
        names = live._plan_names()
        exercise = ex.resolve_exercise(exercise, names) or exercise
        alts = [a["name"] for a in pe.get_alternatives(exercise)]
        if not alts:
            return {"status": "invalid", "error": f"I have no safe swap for {exercise}."}
        rep = next((a for a in alts if a.lower() == (replacement or "").strip().lower()), None) or (alts[0] if not replacement else None)
        if rep is None:
            return {"status": "invalid", "error": f"{replacement} isn't a safe swap for {exercise}. Safe ones: {', '.join(alts[:4])}.", "alternatives": alts[:4]}
        pe.swap(idx, exercise, rep, scope)
    except ValueError as e:
        return {"status": "invalid", "error": str(e)}
    return {"status": "applied", "summary": f"Swapped {exercise} for {rep} {'in the plan' if scope == 'plan' else 'for today'}.", "structural": scope == "plan"}


def t_add_agenda_event(kind: str, start: str, end: str | None = None, label: str = "", minutes: int | None = None, equipment: list | None = None, **_) -> dict:
    from fitness_os import agenda
    kind = {"trip": "travel"}.get(kind, kind)
    kind = "trip" if kind == "travel" else kind
    try:
        ev = agenda.add(kind, start, end, label, minutes, equipment)
    except ValueError as e:
        return {"status": "invalid", "error": str(e)}
    _fire("calendar.changed", {})
    return {"status": "applied", "summary": f"Added {ev['kind'].replace('_', ' ')} {ev['label']} from {ev['start']} to {ev['end']}.", "event": ev}


def _fire(event: str, payload: dict) -> None:
    def run():
        try:
            from fitness_os import loop
            loop.run_event(event, payload, record=False)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


def t_log_checkin(sleep_hours=None, soreness=None, energy=None, stress=None, note: str = "", **_) -> dict:
    def clamp(v, lo, hi):
        try:
            return max(lo, min(hi, type(lo)(v)))
        except (TypeError, ValueError):
            return None
    sl, so, en, st = clamp(sleep_hours, 0.0, 16.0), clamp(soreness, 1, 5), clamp(energy, 1, 5), clamp(stress, 1, 5)
    if all(v is None for v in (sl, so, en, st)) and not note:
        return {"status": "invalid", "error": "Tell me at least sleep, soreness or energy."}
    text = " ".join(x for x in (f"stress {st}/5" if st else "", (note or "").strip()) if x) or None
    db.add_checkin(sl, so, en, None, text)
    _fire("checkin.logged", {"sleep_h": sl, "soreness": so, "energy": en, "note": text})
    bits = [f"sleep {sl:g} h" if sl is not None else "", f"soreness {so}/5" if so else "", f"energy {en}/5" if en else "", f"stress {st}/5" if st else ""]
    return {"status": "applied", "summary": "Check-in saved: " + ", ".join(b for b in bits if b) + "."}


def _profile_apply(patch: dict, rebuild: bool) -> dict:
    from backend.tools.profile_edit import update_profile
    try:
        r = update_profile(patch, rebuild=rebuild)
    except ValueError as e:
        return {"status": "invalid", "error": str(e)}
    if not r.get("changed"):
        return {"status": "applied", "summary": "That was already set."}
    bits = ", ".join(r["changed"])
    return {"status": "applied", "summary": f"Updated {bits}." + (" Your plan was rebuilt." if r.get("rebuilt") else "")}


def t_change_profile_field(field: str, value, **_) -> dict:
    prof = db.get_profile() or {}
    if not prof:
        return {"status": "invalid", "error": "Finish setup first."}
    v = str(value).strip()
    if field == "goal":
        if v.lower() not in ("cut", "bulk", "maintain"):
            return {"status": "invalid", "error": "Goal must be cut, bulk or maintain."}
        patch, rebuild, say = {"goal": v.lower()}, True, f"Change your goal to {v.lower()} and rebuild your plan?"
    elif field == "days_per_week":
        try:
            n = int(float(_num_words(v).split()[0]))
        except (ValueError, IndexError):
            return {"status": "invalid", "error": "Tell me a number of days, 3 to 5."}
        if not 3 <= n <= 5:
            return {"status": "invalid", "error": "I can plan 3 to 5 training days a week."}
        patch, rebuild, say = {"days_per_week": n}, True, f"Switch to {n} days a week and rebuild your plan?"
    elif field == "injuries":
        if not v:
            return {"status": "invalid", "error": "Which injury?"}
        patch, rebuild, say = {"injuries": sorted(set(prof.get("injuries", [])) | {v.lower()[:60]})}, True, f"Add {v.lower()} as an injury and rebuild your plan around it?"
    elif field == "weight":
        try:
            kg = float(re.search(r"\d+(?:\.\d+)?", _num_words(v)).group(0))
        except (AttributeError, ValueError):
            return {"status": "invalid", "error": "Tell me your weight in kilos."}
        if not 30 <= kg <= 300:
            return {"status": "invalid", "error": "That weight looks off. Say it in kilos."}
        return _profile_apply({"weight_kg": kg}, False)      # no rebuild: nutrition targets update, the plan stays
    elif field in ("experience", "activity"):
        ok = {"experience": ("beginner", "intermediate", "advanced"), "activity": ("sedentary", "light", "moderate", "high")}[field]
        if v.lower() not in ok:
            return {"status": "invalid", "error": f"{field} must be one of {', '.join(ok)}."}
        return _profile_apply({field: v.lower()}, False)
    else:
        return {"status": "invalid", "error": "I can't change that by voice. Equipment limits go in the calendar as an equipment event."}
    return _ask_confirm("profile_apply", {"patch": patch, "rebuild": rebuild}, say, "profile")


def t_list_pending_decisions(**_) -> dict:
    from fitness_os import ledger
    from fitness_os.api import _decision
    rows = [_decision(d) for d in ledger.recent(30, "pending_approval")]
    return {"pending": [{"id": r["id"], "summary": r["summary"], "why": r["why"]} for r in rows][:5]}


def t_approve_decision(decision_id: int, **_) -> dict:
    from fitness_os import ledger
    from fitness_os.api import _decision
    d = ledger.get(int(decision_id))
    if not d or d["status"] not in ("pending_approval", "pending_veto"):
        return {"status": "invalid", "error": f"Decision {decision_id} isn't waiting for approval."}
    return _ask_confirm("approve", {"decision_id": int(decision_id)}, f"The team proposes: {_decision(d)['summary']}. Approve it?", "decision")


def _do_approve(decision_id: int) -> dict:
    from fitness_os import control
    r = control.approve(int(decision_id))
    if r.get("status") == "applied":
        return {"status": "applied", "summary": "Approved and applied. You can undo it any time."}
    return {"status": "invalid", "error": r.get("error") or " ".join(r.get("explain") or []) if isinstance(r.get("explain"), list) else (r.get("error") or str(r.get("explain") or "The safety rules blocked that one."))}


def t_decline_decision(decision_id: int, **_) -> dict:
    from fitness_os import control
    r = control.reject(int(decision_id))
    if "error" in r:
        return {"status": "invalid", "error": r["error"]}
    return {"status": "applied", "summary": "Declined. Nothing changes."}


def t_ask_evidence(question: str, **_) -> dict:
    from fitness_os import research
    r = research.ask(question)
    return {"status": "ok", "answer": r["answer"], "sources": [s.get("title") for s in r.get("sources", [])][:2], "message": r["answer"]}


_EXEC = {"approve": _do_approve, "profile_apply": _profile_apply}


def _gate(name: str, fn):
    """A tool that changes a lot or deletes: the first call only asks, the spoken yes runs it."""
    def wrapped(**kw):
        say = {"forget_this": "Forget that for good?", "undo_decision": "Undo that change?", "undo_last_change": "Undo your last change?",
               "restructure_split": "Rebuild your split like that?", "rebuild_plan": "Rebuild your whole plan?", "set_days_per_week": f"Change to {kw.get('days')} days a week?",
               "set_goal": f"Change your goal to {kw.get('goal')} and recompute your targets?", "clear_restriction": "Clear that restriction?"}.get(name, "Go ahead with that?")
        _EXEC[f"tool:{name}"] = fn
        return _ask_confirm(f"tool:{name}", kw, say, "tool")
    return wrapped


def build_tools(words: str) -> tuple[list[dict], dict]:
    from fitness_os import coach_tools
    tools, handlers = coach_tools.build(words=words, channel="live")
    handlers = dict(handlers)
    for g in GATED:
        if g in handlers:
            handlers[g] = _gate(g, handlers[g])
    handlers.update({"swap_exercise": t_swap_exercise, "add_agenda_event": t_add_agenda_event, "log_checkin": t_log_checkin,
                     "change_profile_field": t_change_profile_field, "list_pending_decisions": t_list_pending_decisions,
                     "approve_decision": t_approve_decision, "decline_decision": t_decline_decision, "ask_evidence": t_ask_evidence})
    return list(tools) + VOICE_TOOLS, handlers


_TOOL_REFRESH = {"swap_exercise": ["plan", "today"], "add_agenda_event": ["plan", "today"], "log_checkin": ["today"], "change_profile_field": ["profile", "plan", "today"],
                 "approve_decision": ["decisions", "plan", "today"], "decline_decision": ["decisions"], "log_meal": ["nutrition"], "log_bodyweight": ["profile", "today"],
                 "set_calories": ["nutrition", "plan"], "set_macros": ["nutrition", "plan"], "set_goal": ["nutrition", "plan", "profile"], "log_set": ["today"]}


def _actions_from_trace(trace: list[dict]) -> list[dict]:
    from fitness_os import coach_tools
    what: list[str] = []
    for t in trace or []:
        r = t.get("result") or {}
        if r.get("status") in ("applied", "reverted"):
            for w in _TOOL_REFRESH.get(t["tool"], ["plan", "today", "decisions"] if t["tool"] in coach_tools.WRITE_NAMES or "undo" in t["tool"] else []):
                if w not in what:
                    what.append(w)
    return [{"type": "refresh", "what": what}] if what else []


def _system(pid: str, name: str) -> str:
    today = date.today()
    return (P.system_prompt(pid, name, "live") + f" TODAY is {today.isoformat()} ({today.strftime('%A')}). You are the voice control of a fitness app: the athlete spoke a command and you do it with your tools. "
            "You are spoken aloud while the athlete is mid-workout: answer in ONE short sentence of about 15 words, two only when they ask you to explain or go slowly; never more than 35 words. Plain words, no lists, no markdown, no filler. Never invent numbers: use tools. Resolve dates yourself (next Friday, in two weeks) to YYYY-MM-DD from TODAY. "
            "Anything that deletes or rebuilds is gated by code: when a tool answers needs_confirm, just ask that question and wait; never call it again. "
            "Read a pending decision's summary aloud before approving. Pain or medical talk: tell them to stop and see a professional; never advise treatment. "
            "If the command is not something you can do, say what you CAN do by voice.")


OFFLINE_HINT = ("I can log food, like 'I ate two eggs', open any screen, like 'open my calendar', or run your workout, like 'start my session'. "
                "For anything fancier I need my AI connection, and it isn't available right now.")


ROUTER_DEADLINE_S = 12.0

# Which tools a sentence can need. The full catalogue is ~45 tools; sending all of them on every model turn made replies take 8-20 s.
_TOOL_GROUPS: list[tuple[re.Pattern, set[str]]] = [
    (re.compile(r"\b(split|day|days|exercise|exercises|add|remove|swap|replace|volume|sets?|reps?|plan|program|workout|session|skip|time|minutes?|rest day|squat|bench|deadlift|press|row|curl)\b"),
     {"get_split", "get_today", "add_exercise", "remove_exercise", "change_sets_reps_rpe", "move_exercise", "rename_day", "change_volume", "rebuild_plan", "fit_session",
      "skip_session", "restore_session", "swap_exercise", "find_alternatives", "restructure_split", "log_set", "recent_sets", "get_progress", "set_days_per_week"}),
    (re.compile(r"\b(eat|ate|food|meal|meals|calorie|calories|protein|carb|carbs|fat|macro|macros|diet|weigh|weight|bodyweight|kg|kilos?|cut|bulk|maintain|goal)\b"),
     {"get_nutrition_today", "log_meal", "set_calories", "set_macros", "set_goal", "log_bodyweight", "change_profile_field"}),
    (re.compile(r"\b(sleep|slept|sore|soreness|tired|energy|stress|stressed|pain|hurt|hurts|injur\w*|ache|knee|shoulder|back|elbow|wrist|ready|recover\w*)\b"),
     {"log_checkin", "get_readiness", "add_restriction", "clear_restriction"}),
    (re.compile(r"\b(trip|trips|travel\w*|calendar|busy|competition|meet|vacation|holiday|gym|away|schedule|flight|out of town|no gym|equipment)\b"), {"add_agenda_event"}),
    (re.compile(r"\b(profile|goal|days a week|days per week|days|switch|change my|update my|experience|activity|height|age|train \d|four days|three days|five days)\b"),
     {"change_profile_field", "set_days_per_week", "set_goal", "rebuild_plan", "restructure_split"}),
    (re.compile(r"\b(decision|decisions|approve|decline|pending|team|proposal)\b"), {"list_pending_decisions", "approve_decision", "decline_decision"}),
    (re.compile(r"\b(evidence|study|studies|research|science|true that|myth)\b"), {"ask_evidence"}),
    (re.compile(r"\b(remember|forget|know about me|prefer|preference|dislike|like to)\b"), {"recall_about_me", "remember_this", "correct_memory", "forget_this", "add_preference"}),
]
_CORE_TOOLS = {"get_today", "get_nutrition_today", "get_readiness", "recall_about_me", "undo_last_change", "ask_evidence"}


def tools_for(text: str, tools: list[dict]) -> list[dict]:
    low = text.lower()
    want = set(_CORE_TOOLS)
    for rx, names in _TOOL_GROUPS:
        if rx.search(low):
            want |= names
    out = [t for t in tools if t["name"] in want]
    return out or tools


def _router(text: str, screen: str, ambient: bool, addressed: bool) -> dict:
    pid, name = _ctx()
    if not have_key():
        return _out("fallback", OFFLINE_HINT)
    if ambient and not addressed and not live.QUESTION_RE.match(text.lower()):
        return _out("ignored", "")
    try:
        tools, handlers = build_tools(text)
        tools = tools_for(text, tools)                        # only the tools this sentence can need: each model turn is several times faster
        trace: list[dict] = []
        user = f"[Screen: {screen}. {live.live_context()}]\n{text}" if live.live_context() else f"[Screen: {screen}]\n{text}"
        pool = ThreadPoolExecutor(max_workers=1)
        fut = pool.submit(run_agent, _system(pid, name), user, tools, handlers, 3, 400, _history(), trace, True)
        try:
            reply = fut.result(timeout=ROUTER_DEADLINE_S)
        except FutureTimeout:
            pool.shutdown(wait=False)
            return _out("fallback", "That is taking longer than it should. Say it again in a few words, or tap the mic and try once more.")
        finally:
            pool.shutdown(wait=False)
    except Exception:
        # the model is there (we have a key) but this call failed or timed out: say so in a human way, not the offline feature list
        return _out("fallback", "Sorry, I lost my train of thought on that one. Say it again?")
    p = _pend_get("confirm")
    if p:
        say = p["say"]
        reply = reply if (reply and "?" in reply and not reply.startswith("(agent stopped")) else say
        return _out("confirm", reply, expects="confirm")
    acts = _actions_from_trace(trace)
    if not reply or reply.startswith("(agent stopped"):
        from fitness_os import coach_tools
        reply = coach_tools.fallback_reply(trace, pid in lines.HI_PERSONAS) or next((str(t["result"].get("summary") or t["result"].get("message") or t["result"].get("error")) for t in reversed(trace) if isinstance(t.get("result"), dict) and (t["result"].get("summary") or t["result"].get("message") or t["result"].get("error"))), "") or OFFLINE_HINT
    expects = "answer" if reply.rstrip().endswith("?") else None
    if reply:
        _remember_turn(text, reply)
    return _out("router", reply, actions=acts, expects=expects, cards=_cards(trace))


def _cards(trace):
    try:
        from fitness_os import coach_tools
        return coach_tools.actions_from_trace(trace)
    except Exception:
        return []


# ------------------------------------------------------------------ diet questions go to the dietitian, not the general router
DIET_Q_RE = re.compile(
    r"\b(?:meal plan|diet plan|diet chart|food plan|grocery list|dietitian|dietician|nutritionist)\b"
    r"|^(?:what|which|how much|how many|should i|can i|is it ok(?:ay)? to|give me|suggest|tell me|any|plan)\b.{0,60}\b(?:eat|eating|food|foods|protein|carbs?|calories|meals?|snacks?|breakfast|lunch|dinner|diet|paneer|chicken|eggs?|dal|rice|roti|whey|sabzi|veg|non.?veg)\b"
    r"|\bwhat (?:should|can|do) i (?:eat|have)\b|\b(?:high|more|good) protein (?:foods?|snacks?|meals?|options?|ideas?)\b")


def _diet(text: str, low: str) -> dict | None:
    """'what should I eat after the gym', 'give me a meal plan', 'how much paneer for 40 g protein': the diet consultant answers (it has the targets,
    the saved preferences and the planner), and plans open on the Diet screen."""
    if not DIET_Q_RE.search(low) or not have_key():
        return None
    if re.search(r"do i need|evidence|stud(?:y|ies)|research|science|myth|is it true|safe to", low):
        return None                      # an evidence question: the router has the ask_evidence tool for those
    try:
        from backend.diet import agent as diet_agent
        r = diet_agent.chat(text, fast=True)
    except Exception:
        return None
    reply = (r.get("reply") or "").strip()
    if not reply:
        return None
    acts = [{"type": "navigate", "tab": "nutrition"}, {"type": "open", "screen": "diet"}] if (r.get("plan") or r.get("week")) else []
    return _out("diet", reply, actions=acts, expects="answer" if reply.rstrip().endswith("?") else None, cards=r.get("actions") or [])


# ------------------------------------------------------------------ entry point
def handle(transcript: str, screen: str = "unknown", day: str | None = None, ambient: bool = False, followup: bool = False) -> dict:
    screen = screen if screen in ("today", "coach", "nutrition", "insights", "setup", "live") else "unknown"
    try:
        shown = date.fromisoformat(day).isoformat() if day else None
    except ValueError:
        shown = None
    text = speech_fix.clean((transcript or "").strip())      # fillers out, 'wraps/raps' -> 'reps'
    if not text:                                             # only "uhh...": say nothing, and keep any open question open so the next words answer it
        return _out("noop", "", expects="set_details" if live._pending_get() else "food_amount" if _pend_get("food") else None)
    pid, name = _ctx()
    cared = care.handle(text, pid in lines.HI_PERSONAS)
    if cared:
        _pend.clear()
        return _out("care", cared, safety_flag=care.check(text), moment=True)
    norm = _hi_pre(hindi.normalize(text))
    body, woke = strip_wake(norm)
    low = " ".join(body.lower().replace("’", "'").split())
    low = low.strip(" .!?,") if len(low) > 1 else low
    if not low:
        return _out("noop", "Yes?" if woke else "")
    addressed = woke or P.get(pid)["name"].lower() in low
    clear_cmd = addressed or not ambient or bool(re.match(r"^(?:log|add)\b", low))

    # pain / medical: the existing pain path (live logs a set that carries it, otherwise we record the injury and answer)
    if has_safety_flag(low) and not _pend_get("food"):
        res = live.handle(body, ambient=ambient, allow_chat=False)
        if res.get("intent") in ("set", "pain"):
            return _from_live(res)
        db.add_memory("injury", text)
        db.add_checkin(note=text)
        return _out("pain", lines.pick(pid, "pain", name), safety_flag=has_safety_flag(low), moment=True)

    # an unfinished sentence ("I did like"): say nothing, the app keeps it and joins it to what comes next
    if speech_fix.is_fragment(low) and not _pend_get() and not live._pending_get():
        return _out("fragment", "", expects="answer")

    # 1. pending dialogues
    if _pend_get("confirm"):
        if YES_RE.match(low):
            return _confirmed()
        if CANCEL_RE.match(low):
            _pend.clear()
            return _out("cancelled", "Okay, leaving it as it is.")
        _pend.clear()      # something else entirely: the question is dropped, never answered by accident later
    if _pend_get("food"):
        r = _food_answer(body, low)
        if r:
            return r
        _pend.clear()
    elif CANCEL_RE.match(low) and (followup or len(low.split()) <= 2) and not live._pending_get():
        return _out("noop", "Okay.")
    lp = live._pending_get()
    if lp and time.time() - lp["ts"] > SET_PEND_TTL:
        live._pending_clear()
        lp = None
    if lp and not followup and not looks_like_set_answer(low):
        live._pending_clear()       # a new command, not the answer: forget the half-described set and handle the sentence normally
        lp = None
    if lp:
        res = live.handle(body, ambient=ambient, allow_chat=False)
        if res.get("intent") not in ("fallback", "noop", "ignored"):
            return _from_live(res)

    # 2. deterministic matchers
    if not clear_cmd and not live._pending_get():
        # ambient gym chatter: only workout talk (sets, skip, status...) is ever acted on without addressing the coach
        res = live.handle(body, ambient=True, allow_chat=False)
        if res.get("intent") in ("fallback", "ignored", "noop"):
            return _out("ignored", "")
        return _from_live(res)
    for fn in (lambda: _rest(low), lambda: _settings(low), lambda: _nav(low, shown), lambda: _meal_queries(low, text, screen, shown), lambda: _food(low, screen, shown)):
        r = fn()
        if r:
            return r
    res = live.handle(body, ambient=ambient, allow_chat=False)
    if res.get("intent") not in ("fallback", "noop", "ignored") or (res.get("intent") == "noop" and res.get("reply") and live.START_RE.match(low)):
        out = _from_live(res)
        if out["intent"] == "start":
            out["actions"] = [{"type": "open", "screen": "live"}] + [a for a in out["actions"] if a.get("type") != "open"]
        return out
    if res.get("intent") == "ignored":
        return _out("ignored", "")
    # 3. diet questions: the diet consultant
    if clear_cmd:
        d = _diet(body, low)
        if d:
            return d
    # 4. the LLM router, or an honest offline answer
    return _router(body, screen, ambient, addressed)
