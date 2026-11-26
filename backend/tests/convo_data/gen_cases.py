"""Generates the replay case files next to this script:  python backend/tests/convo_data/gen_cases.py
negatives.json (nothing may be done), positives.json (a set must be logged), actions.json (corrections, skips, clarifications), chat.json (talk, no actions)."""
import json
from pathlib import Path

HERE = Path(__file__).parent
ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety"}


def words(n: float) -> str:
    if n != int(n):
        i = int(n)
        return f"{words(i)} point {ONES[int(round((n - i) * 10))]}"
    n = int(n)
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else "-" + ONES[n % 10])
    return "one hundred" if n == 100 else str(n)


def num(n: float, as_words: bool) -> str:
    if as_words:
        return words(n)
    return f"{n:g}"


NEG = {"no_write_tools": True, "skipped": [], "sets_logged_delta": 0}


def neg(i, text, tags, lang=None, **extra):
    t = {"say": text, "expect": {**NEG, **extra}}
    if lang:
        t["lang"] = lang
    return {"id": f"neg-{i:02d}", "tags": ["negative", *tags], "turns": [t]}


negatives: list[dict] = []
n = 0


def add(text, tags, lang=None, **extra):
    global n
    n += 1
    negatives.append(neg(n, text, tags, lang, **extra))


for s in ["We are not skipping anything, we are doing every exercise.", "I am not skipping the goblet squat.", "You're not skipping anything. We are doing every exercise.",
          "Skipping anything.", "Please don't skip it.", "no skip", "I'm not going to skip squats.", "We are not skipping the squat.", "Nobody is skipping today.",
          "I would never skip leg day.", "Don't skip anything.", "No skipping please.", "I said we're not skipping.", "Not skipping, keep going.", "I'm not skipping, let's go."]:
    add(s, ["skip", "en"])
for s in ["What is the next exercise?", "what's next", "Which exercise is next?", "Should I skip this one?", "How many sets are left?", "Okay, next set.", "What's the plan for today?",
          "Can I skip deadlifts?", "Do I have to do leg extension?", "What weight should I use?", "how am I doing", "what did I do last time", "Is it okay if I skip?",
          "how many sets left on squats", "what comes after this"]:
    add(s, ["question", "en"])
for s in ["Let's move on.", "Next exercise please.", "I want to move on.", "machine's busy", "I can't do this one right now."]:
    add(s, ["vague", "en"])
for s in ["skip mat karna", "skip nahi karna hai", "sab karenge, kuch skip nahi karna", "agla exercise kaunsa hai", "kitne set bache hain", "aaj ka plan kya hai", "abhi skip mat karo",
          "main skip nahi kar raha", "next set kab hai", "mujhe skip nahi karna", "kaunsa exercise next hai", "kitna weight lagaun"]:
    add(s, ["hinglish", "skip"], lang="hinglish")
# a sentence the athlete clearly said, but the ASR says it is unreliable: never act
for text, asr in [("sixty kilos five reps done", {"unclear": True}), ("sixty kilos five reps done", {"avg_logprob": -1.6}), ("[[unclear]]", None),
                  ("안녕하세요 운동 시작합니다", None), ("skip this exercise", {"unclear": True}), ("what is the next exercise", {"avg_logprob": -2.0})]:
    n += 1
    negatives.append({"id": f"neg-{n:02d}", "tags": ["negative", "asr"], "turns": [{"say": text, "asr": asr, "expect": {**NEG}}]})
for s in ["Boxing, wipes hands, adjust straps.", "Copy.", "hmm", "Right.", "Hello."]:
    add(s, ["noise"])
# half-said sets: never saved
for s in ["sixty kilos", "five reps", "I did some squats"]:
    add(s, ["incomplete"])
# implausible numbers: asked about, never saved
for s in ["six hundred kilos five reps", "sixty kilos fifty reps", "sixty kilos zero reps"]:
    add(s, ["implausible"])
n += 1
negatives.append({"id": f"neg-{n:02d}", "tags": ["negative", "implausible", "jump"], "setup": {"sets_done": {"squat": 1}, "weights": {"squat": 20}},
                  "turns": [{"say": "sixty kilos five reps", "expect": {"no_write_tools": True, "skipped": [], "sets_logged_delta": 0}}]})

FORMS_EN = ["{w} kilos {r} reps done", "{w} for {r}", "I did {r} reps at {w}", "{w} kg times {r}", "just finished {r} at {w} kilos", "that was {w} for {r}, felt heavy",
            "{w} kilos... {r}", "set done, {w} kilos, {r} reps", "{r} reps with {w}"]
FORMS_HI = ["{w} kilo {r} reps ho gaye", "{r} reps maare {w} kilo pe", "{w} kilo ka set ho gaya, {r} reps"]
COMBOS = [(60, 5), (80, 8), (45, 10), (100, 3), (62.5, 6), (70, 8), (55, 12), (90, 5), (75, 6)]
positives: list[dict] = []
k = 0
for fi, form in enumerate(FORMS_EN + FORMS_HI):
    for ci, (w, r) in enumerate(COMBOS):
        k += 1
        hi = form in FORMS_HI
        as_words = (fi + ci) % 2 == 0 and not (hi and w != int(w))
        text = form.format(w=num(w, as_words), r=num(r, as_words))
        t = {"say": text, "expect": {"tools": ["log_set"], "last_set": {"weight": w, "reps": r}, "sets_logged_delta": 1, "skipped": []}}
        if hi:
            t["lang"] = "hinglish"
        positives.append({"id": f"pos-{k:03d}", "tags": ["positive", "hinglish" if hi else "en"], "turns": [t]})

actions: list[dict] = []


def act(id_, tags, turns, setup=None):
    sc = {"id": id_, "tags": ["action", *tags], "turns": turns}
    if setup:
        sc["setup"] = setup
    actions.append(sc)


L = lambda w, r, **kw: {"tools": ["log_set"], "last_set": {"weight": w, "reps": r}, "sets_logged_delta": 1, **kw}
C = lambda w, r: {"tools": ["correct_last_set"], "last_set": {"weight": w, "reps": r}, "sets_logged_delta": 0}
act("cor-1", ["correction"], [{"say": "sixty kilos five reps done", "expect": L(60, 5)}, {"say": "no, sixty-five", "expect": C(65, 5)}])
act("cor-2", ["correction"], [{"say": "80 for 8", "expect": L(80, 8)}, {"say": "it was six reps", "expect": C(80, 6)}])
act("cor-3", ["correction", "single"], [{"say": "sixty, no, sixty-five, five reps", "expect": L(65, 5)}])
act("cor-4", ["correction", "single"], [{"say": "seventy kilos for eight... sorry, ten reps", "expect": L(70, 10)}])
act("cor-5", ["correction", "hinglish"], [{"say": "saath kilo paanch reps", "lang": "hinglish", "expect": L(60, 5)}, {"say": "nahi nahi, 65 kilo", "lang": "hinglish", "expect": C(65, 5)}])
act("cor-6", ["correction"], [{"say": "fifty for ten", "expect": L(50, 10)}, {"say": "make that fifty five", "expect": C(55, 10)}])
act("slot-1", ["slots"], [{"say": "sixty kilos", "expect": {"no_write_tools": True, "sets_logged_delta": 0}}, {"say": "five", "expect": L(60, 5)}])
act("slot-2", ["slots"], [{"say": "five reps", "expect": {"no_write_tools": True, "sets_logged_delta": 0}}, {"say": "sixty", "expect": L(60, 5)}])
act("jump-1", ["plausible"], [{"say": "sixty kilos five reps", "expect": {"no_write_tools": True, "sets_logged_delta": 0}}, {"say": "yes, sixty", "expect": L(60, 5)}],
    setup={"sets_done": {"squat": 1}, "weights": {"squat": 20}})
act("skip-1", ["skip"], [{"say": "Skip this exercise, my shoulder hurts.", "expect": {"tools": ["skip_exercise"], "skipped": ["squat"], "sets_logged_delta": 0}}])
act("skip-2", ["skip"], [{"say": "skip squats", "expect": {"tools": ["skip_exercise"], "skipped": ["squat"], "sets_logged_delta": 0}}])
act("skip-3", ["skip"], [{"say": "the machine is taken, skip this one", "expect": {"tools": ["skip_exercise"], "skipped": ["squat"], "sets_logged_delta": 0}}])
act("skip-4", ["skip"], [{"say": "skip romanian deadlift", "expect": {"tools": ["skip_exercise"], "skipped": ["romanian deadlift"], "sets_logged_delta": 0}}])
act("skip-5", ["skip", "hinglish"], [{"say": "ye wala skip kar do", "lang": "hinglish", "expect": {"skipped": ["squat"], "sets_logged_delta": 0}}])
act("skip-6", ["skip", "confirm"], [{"say": "let's skip the squat, the machine is busy... or should I?", "expect": {"no_write_tools": True, "skipped": []}},
                                    {"say": "yes skip it", "expect": {"skipped": ["squat"]}}])
act("unskip-1", ["unskip"], [{"say": "put it back, I'll do it", "expect": {"tools": ["unskip_exercise"], "skipped": []}}], setup={"skipped": ["squat"]})
act("unskip-2", ["unskip"], [{"say": "we are not skipping anything", "expect": {"no_write_tools": False, "skipped": []}}], setup={"skipped": ["squat"]})
act("rest-1", ["rest"], [{"say": "start a two minute rest", "expect": {"tools": ["control_rest"], "sets_logged_delta": 0}}])
act("rest-2", ["rest"], [{"say": "skip the rest, I'm ready", "expect": {"tools": ["control_rest"], "sets_logged_delta": 0, "skipped": []}}])
act("next-w", ["nextweight"], [{"say": "sixty-five next set", "expect": {"sets_logged_delta": 0, "skipped": [], "no_write_tools": False}}])
act("pain-1", ["pain"], [{"say": "my knee feels off", "expect": {"sets_logged_delta": 0, "skipped": [], "say_matches": ["stop|rok|ruk"]}}])
act("end-1", ["end"], [{"say": "okay coach, let's wrap up", "expect": {"tools": ["end_workout"]}}], setup={"sets_done": {"squat": 1}})

chat: list[dict] = []
for i, (text, lang) in enumerate([("that was heavy", None), ("I'm tired today", None), ("what weight did I do last time on squats", None), ("how many sets are left", None),
                                  ("is it okay to go heavier", None), ("my back feels a bit tight", None), ("this music is terrible", None), ("how long is the rest", None),
                                  ("bhai aaj bahut thaka hua hoon", "hinglish"), ("kya main weight badhaun", "hinglish"), ("what's the plan after squats", None),
                                  ("give me a tip for squats", None)], 1):
    t = {"say": text, "expect": {"no_write_tools": text != "my back feels a bit tight", "skipped": [], "sets_logged_delta": 0}}
    if lang:
        t["lang"] = lang
    chat.append({"id": f"chat-{i:02d}", "tags": ["chat"], "turns": [t]})

for name, data in (("negatives", negatives), ("positives", positives), ("actions", actions), ("chat", chat)):
    (HERE / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(name, len(data), "scenarios,", sum(len(s["turns"]) for s in data), "turns")
