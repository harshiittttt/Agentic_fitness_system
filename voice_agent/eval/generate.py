"""Generates the big scenario files (100+ positive set reports, 70+ negatives, 20 ambiguous, skip orders) into voice_agent/scenarios/generated_*.yaml.
    python -m voice_agent.eval generate
Deterministic (fixed seed). The expectations assume the throwaway backend's demo plan: the CURRENT exercise at the start is `squat` (see scenarios/README in eval/README.md).
The hand-written scenarios (ledger.yaml, conversations.yaml) are not touched."""
from __future__ import annotations

import random
from pathlib import Path

import yaml

OUT = Path(__file__).resolve().parent.parent / "scenarios"
CUR = "squat"

EN_ONES = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve", 15: "fifteen"}
EN_TENS = {20: "twenty", 30: "thirty", 40: "forty", 50: "fifty", 60: "sixty", 70: "seventy", 80: "eighty", 90: "ninety"}
HI_W = {3: "teen", 4: "chaar", 5: "paanch", 6: "chhe", 7: "saat", 8: "aath", 10: "das", 12: "barah", 40: "chalis", 50: "pachaas", 60: "saath", 70: "sattar", 80: "assi",
        90: "nabbe", 100: "sau"}


def en(n: float) -> str:
    """60 -> 'sixty', 62.5 -> 'sixty two point five', 100 -> 'a hundred', 120 -> 'a hundred and twenty'."""
    if n != int(n):
        whole, frac = str(n).split(".")
        return f"{en(int(whole))} point {' '.join(EN_ONES[int(c)] if int(c) else 'zero' for c in frac)}"
    n = int(n)
    if n in EN_ONES:
        return EN_ONES[n]
    if n in EN_TENS:
        return EN_TENS[n]
    if n < 100:
        return f"{EN_TENS[n // 10 * 10]} {EN_ONES[n % 10]}"
    if n == 100:
        return "a hundred"
    return f"a hundred and {en(n - 100)}"


def hi(n: float) -> str:
    return HI_W.get(int(n), str(n)) if n == int(n) else str(n)


WEIGHTS = [40, 50, 60, 62.5, 65, 67.5, 70, 75, 80, 90, 100, 120]
REPS = [3, 4, 5, 6, 8, 10, 12]
EN_T = ["{w} for {r}", "{wd} kilos, {rd} reps, done.", "{wd} for {rd}, felt heavy.", "I did {wd} kilos for {rd} reps.", "{rd} reps at {wd}.", "{wd} kg times {rd}.",
        "Just did {wd} for {rd}, felt easy.", "Set done, {wd} kilos, {rd} reps.", "{wd} by {rd}.", "{w} x {r} done", "{wd} kilos {rd} reps, rpe eight.", "Done. {w}, {r}."]
HI_T = ["{wh} kilo {rh} reps ho gaye.", "{wh} kilo pe {rh} reps.", "{wh} kilo {rh} baar kiya.", "{wh} kilo, {rh} reps, ho gaya.", "{rh} reps hue {wh} kilo pe.", "{wh} kilo {rh} reps hogaya bhai."]
HI_WEIGHTS = [40, 50, 60, 70, 80, 90, 100]
HI_REPS = [3, 4, 5, 6, 8, 10, 12]


def fmtn(n: float) -> str:
    return str(int(n)) if n == int(n) else str(n)


def sc(name: str, tags: list[str], say: str, expect: dict, lang: str = "en", turns: list | None = None) -> dict:
    return {"name": name, "tags": tags, "lang": lang, "turns": turns or [{"say": say, "expect": expect}]}


def positives(rng: random.Random) -> list[dict]:
    out = []
    n = 0
    for i in range(84):                                              # English phrasings x weights x reps
        w, r = rng.choice(WEIGHTS), rng.choice(REPS)
        t = EN_T[i % len(EN_T)]
        say = t.format(w=fmtn(w), r=r, wd=en(w), rd=en(r))
        n += 1
        out.append(sc(f"pos-en-{n:03d}", ["positive", "set", "en"], say, {"sets": [{"weight": w, "reps": r}]}))
    for i in range(30):                                              # Hinglish Roman letters, spoken number words
        w, r = rng.choice(HI_WEIGHTS), rng.choice(HI_REPS)
        say = HI_T[i % len(HI_T)].format(wh=hi(w), rh=hi(r))
        n += 1
        out.append(sc(f"pos-hi-{n:03d}", ["positive", "set", "hinglish"], say, {"sets": [{"weight": w, "reps": r}]}, lang="hinglish"))
    for ex, w, r in [("leg press", 120, 10), ("leg press", 140, 8), ("leg curl", 40, 12), ("leg curl", 45, 10), ("calf raise", 60, 15), ("romanian deadlift", 80, 8),
                     ("romanian deadlift", 90, 6), ("squat", 70, 5), ("leg press", 100, 12), ("calf raise", 50, 15)]:
        n += 1
        out.append(sc(f"pos-named-{n:03d}", ["positive", "set", "named", "en"], f"{ex} {en(w)} for {en(r)}", {"sets": [{"exercise": ex, "weight": w, "reps": r}]}))
    for i, (w1, w2, r) in enumerate([(60, 65, 5), (70, 75, 5), (80, 85, 3), (50, 55, 8), (60, 62.5, 6), (90, 95, 4), (100, 105, 3), (40, 45, 10), (70, 72.5, 6), (65, 60, 5),
                                      (80, 75, 5), (50, 60, 8)], 1):      # self-corrections: the LAST number wins
        n += 1
        say = [f"{en(w1)}... no, {en(w2)} for {en(r)}", f"{en(w1)} kilos. No wait, {en(w2)}. {en(r)} reps.", f"I did {en(w1)}, sorry, {en(w2)}, for {en(r)}"][i % 3]
        out.append(sc(f"pos-corr-{n:03d}", ["positive", "set", "correction", "en"], say, {"sets": [{"weight": w2, "reps": r}]}))
    for w, r, say in [(60, 5, "साठ किलो पाँच बार हो गया"), (50, 8, "पचास किलो आठ रेप्स"), (70, 6, "सत्तर किलो छह बार"), (40, 10, "चालीस किलो दस बार हो गया")]:
        n += 1
        out.append(sc(f"pos-deva-{n:03d}", ["positive", "set", "hindi"], say, {"sets": [{"weight": w, "reps": r}], "grounded": False}, lang="hi"))
    return out


NEG_SKIP_EN = ["We are not skipping anything, we are doing every exercise.", "I am not skipping the goblet squat.", "Don't skip squats.", "Don't skip anything.",
               "Nobody is skipping today.", "I'm not going to skip leg press.", "No skipping, I will do all of them.", "Never skip leg day.", "I won't skip it.",
               "Not skipping the squat, I feel great.", "We're not skipping, let's go.", "I did not skip anything yesterday."]
NEG_SKIP_HI = ["Skip mat karo, sab karenge.", "Kuch skip nahi karna hai.", "Main squat skip nahi karunga.", "Skip nahi, hum sab exercise karenge.", "Aaj kuch bhi skip nahi karna.",
               "Skip mat karna please.", "Nahi nahi, skip nahi karna.", "Leg press skip nahi karte."]
NEG_Q = ["What is the next exercise?", "What's next?", "Should I skip this one?", "How many sets are left?", "What is the plan for today?", "What weight should I use for the next set?",
         "How many reps today?", "Which exercise is after this?", "Can you tell me the plan?", "Agla exercise kaunsa hai?", "Kitne set bache hain?", "Aaj ka plan kya hai?",
         "Next set mein kitna weight lagaun?", "What exercise am I on?"]
NEG_CHAT = ["Hello.", "Okay.", "Copy.", "Right.", "Nice music in the gym today.", "This gym is so crowded.", "Hmm.", "Yeah.", "Thanks coach.", "Haan theek hai.", "Cool."]
NEG_STAGE = ["Boxing, wipes hands, adjust straps.", "Takes a deep breath and chalks up.", "Walks to the rack.", "Puts on the belt.", "Sips water.", "He grabs the bar."]
NEG_NOISE = ["Umm, haan, woh, kya, hmm.", "Uh... uh...", "Kike mech kya?", "Samadhi bhiya, phir kabhi continue karenge.", "안녕하세요", "बस तो ना लेस चाही."]
NEG_WORD = ["I skipped breakfast this morning.", "Skipping rope is great cardio.", "I will skip dessert tonight.", "My phone keeps skipping songs.", "I skipped lunch today.", "I skipped the gym on Sunday."]
NEG_NUM = ["I have been training for five years.", "My phone is at sixty percent.", "It is two hours since I ate.", "The gym closes at nine.", "My locker number is forty two.",
           "I am twenty eight years old.", "Five minutes left on my parking.", "My bench is at seventy five percent of my max."]


def negatives() -> list[dict]:
    out = []
    n = 0
    for fam, items, lang in (("neg-skip", NEG_SKIP_EN, "en"), ("neg-skip-hi", NEG_SKIP_HI, "hinglish"), ("neg-question", NEG_Q, "en"), ("neg-chat", NEG_CHAT, "en"),
                             ("neg-stage", NEG_STAGE, "en"), ("neg-noise", NEG_NOISE, "en"), ("neg-word", NEG_WORD, "en"), ("neg-number", NEG_NUM, "en")):
        for say in items:
            n += 1
            lg = "hinglish" if fam == "neg-question" and any(w in say.lower() for w in ("kaunsa", "bache", "kya", "mein")) else lang
            exp = {"grounded": fam not in ("neg-noise",)}
            if fam in ("neg-stage", "neg-chat", "neg-noise"):
                exp["reply_max_words"] = 25                                  # chatter must not trigger a long speech
            out.append(sc(f"{fam}-{n:03d}", ["negative", fam, lg], say, exp, lang=lg))
    return out


AMBIG = ["Next exercise please.", "Let's move on.", "I don't know about this one.", "Maybe skip this.", "Machine's taken.", "Can't do this one today.", "Move on.",
         "I'm not sure about squats today.", "Let's see what's next.", "This one feels wrong today.", "Skip... or maybe not.", "Can we do something else?",
         "I did sixty.", "Five reps.", "Did it.", "Same as before.", "Heavy one.", "Done with this.", "I'm done here.", "That's it for this one."]


def ambiguous() -> list[dict]:
    return [sc(f"amb-{i:03d}", ["ambiguous", "en"], say, {"asks": True, "grounded": True}) for i, say in enumerate(AMBIG, 1)]


def skips() -> list[dict]:
    out = []
    for i, (say, names, lang) in enumerate([("Skip this exercise, my shoulder hurts.", ["squat"], "en"), ("Skip squat.", ["squat"], "en"), ("Skip the leg curl.", ["leg curl"], "en"),
                                            ("I want to skip romanian deadlift today.", ["romanian deadlift"], "en"), ("Skip leg press please.", ["leg press"], "en"),
                                            ("Squat skip kar do.", ["squat"], "hinglish"), ("Yeh wala skip kar do.", ["squat"], "hinglish"), ("Skip the calf raise.", ["calf raise"], "en")], 1):
        out.append(sc(f"skip-real-{i:03d}", ["positive", "skip_real", lang], say, {"skipped": names}, lang=lang))
    return out


def write(name: str, scenarios: list[dict]) -> Path:
    OUT.mkdir(exist_ok=True)
    p = OUT / f"generated_{name}.yaml"
    p.write_text("# GENERATED by `python -m voice_agent.eval generate` (voice_agent/eval/generate.py). Do not edit by hand.\n"
                 + yaml.safe_dump({"scenarios": scenarios}, allow_unicode=True, sort_keys=False, width=140), encoding="utf-8")
    return p


def main() -> None:
    rng = random.Random(7)
    files = [("positive", positives(rng)), ("negative", negatives()), ("ambiguous", ambiguous()), ("skip", skips())]
    for name, sc_list in files:
        p = write(name, sc_list)
        print(f"{p.name}: {len(sc_list)} scenarios")


if __name__ == "__main__":
    main()
