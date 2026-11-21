"""Understand Hindi / Haryanvi speech (Devanagari) by turning it into the English-ish text the rest of the coach already
parses. Speech recognisers set to hi-IN return Devanagari, including English loan words ("बेंच प्रेस") and number words
("अस्सी"). This is deliberately small and predictable: numbers, gym vocabulary, commands, pain words, common foods.

normalize("बेंच प्रेस अस्सी किलो आठ बार") -> "bench press 80 kg for 8"
"""
from __future__ import annotations

import re

_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# Hindi number words 1-100 (the irregular tens/units are why this table is long), plus common spelling variants.
_NUM_WORDS = """
एक 1|दो 2|तीन 3|चार 4|पाँच 5|पांच 5|छह 6|छः 6|छे 6|छ 6|सात 7|आठ 8|नौ 9|दस 10|ग्यारह 11|बारह 12|तेरह 13|चौदह 14|पंद्रह 15|पन्द्रह 15
सोलह 16|सत्रह 17|अठारह 18|उन्नीस 19|बीस 20|इक्कीस 21|बाईस 22|तेईस 23|चौबीस 24|पच्चीस 25|छब्बीस 26|सत्ताईस 27|अट्ठाईस 28|उनतीस 29
तीस 30|इकतीस 31|बत्तीस 32|तैंतीस 33|चौंतीस 34|पैंतीस 35|छत्तीस 36|सैंतीस 37|अड़तीस 38|उनतालीस 39|चालीस 40|इकतालीस 41|बयालीस 42
तैंतालीस 43|चौवालीस 44|पैंतालीस 45|छियालीस 46|सैंतालीस 47|अड़तालीस 48|उनचास 49|पचास 50|इक्यावन 51|बावन 52|तिरपन 53|चौवन 54
पचपन 55|छप्पन 56|सत्तावन 57|अट्ठावन 58|उनसठ 59|साठ 60|इकसठ 61|बासठ 62|तिरसठ 63|चौंसठ 64|पैंसठ 65|छियासठ 66|सड़सठ 67|अड़सठ 68
उनहत्तर 69|सत्तर 70|इकहत्तर 71|बहत्तर 72|तिहत्तर 73|चौहत्तर 74|पचहत्तर 75|छिहत्तर 76|सतहत्तर 77|अठहत्तर 78|उनासी 79|अस्सी 80
इक्यासी 81|बयासी 82|तिरासी 83|चौरासी 84|पचासी 85|छियासी 86|सत्तासी 87|अट्ठासी 88|नवासी 89|नब्बे 90|इक्यानवे 91|बानवे 92|तिरानवे 93
चौरानवे 94|पचानवे 95|छियानवे 96|सत्तानवे 97|अट्ठानवे 98|निन्यानवे 99|सौ 100|डेढ़ 1.5|ढाई 2.5
"""
NUMBERS: dict[str, float] = {}
for _pair in re.split(r"[|\n]", _NUM_WORDS):
    _pair = _pair.strip()
    if _pair:
        _w, _n = _pair.rsplit(" ", 1)
        NUMBERS[_w] = float(_n)

# Multi-word phrases first (longest first), applied before the single-token pass.
PHRASES = [
    ("बेंच प्रेस", "bench press"), ("ओवरहेड प्रेस", "overhead press"), ("शोल्डर प्रेस", "overhead press"), ("इनक्लाइन प्रेस", "incline press"),
    ("लेग प्रेस", "leg press"), ("बारबेल रो", "barbell row"), ("लैट पुलडाउन", "lat pulldown"), ("पुल अप", "pull up"), ("डेड लिफ्ट", "deadlift"),
    ("रोमानियन डेडलिफ्ट", "romanian deadlift"), ("फ्रंट स्क्वाट", "front squat"), ("केबल रो", "cable row"), ("हैमर कर्ल", "hammer curls"),
    ("लेटरल रेज", "lateral raise"), ("काफ रेज", "calf raise"), ("फेस पुल", "face pull"), ("लेग कर्ल", "leg curl"), ("पुश अप", "push up"),
    ("काम खतम", "wrap up"), ("काम खत्म", "wrap up"), ("वाइंड अप", "wrap up"), ("रैप अप", "wrap up"), ("निपट गया", "wrap up"), ("छोड़ दे", "skip"),
    ("छोड़ दो", "skip"), ("अगली एक्सरसाइज", "next exercise"), ("आगे क्या", "what's next"), ("अगला क्या", "what's next"), ("कितने बाकी", "how many left"),
    ("समय कम", "short on time"), ("टाइम कम", "short on time"), ("जल्दी में", "in a rush"), ("बहुत भारी", "grinder"), ("घणा भारी", "grinder"),
    ("बहुत हल्का", "easy"), ("घणा हल्का", "easy"), ("के साथ", "with"), ("आर पी ई", "rpe"), ("प्रोटीन शेक", "protein shake"), ("पीनट बटर", "peanut butter"),
]

# Single tokens. NB: matched as whole tokens, so "बार" (times) never fires inside "बारह" (twelve) or "बारबेल".
WORDS = {
    # exercises
    "बेंच": "bench", "स्क्वाट": "squat", "स्क्वॉट": "squat", "डेडलिफ्ट": "deadlift", "कर्ल": "curls", "लंजेस": "lunges", "डिप्स": "dips", "प्लैंक": "plank",
    "पुलअप": "pull up", "पहलवान": "pehalwan",
    # units and rep words
    "किलो": "kg", "किलोग्राम": "kg", "केजी": "kg", "बार": "§", "रेप्स": "§", "रेप": "§", "दफा": "§", "आरपीई": "rpe", "ग्राम": "g", "गिलास": "glass",
    "मिनट": "minutes", "सिर्फ": "only", "सिर्फ़": "only", "केवल": "only",
    # effort
    "हल्का": "easy", "आसान": "easy", "सौखा": "easy", "भारी": "heavy",
    # commands
    "खतम": "wrap up", "खत्म": "wrap up", "स्किप": "skip", "तैयार": "ready", "रेडी": "ready", "चलो": "let's go", "चल": "go",
    # pain / injury (safety first)
    "दर्द": "pain", "चोट": "injury", "चक्कर": "dizzy", "मोच": "sprain", "खिंचाव": "strain", "सूजन": "swelling", "सुन्न": "numb",
    # questions
    "क्या": "what", "कैसे": "how", "कितना": "how", "कितने": "how", "कितनी": "how", "कब": "when", "क्यों": "why", "कौन": "which", "कित": "where",
    "कहां": "where", "कहाँ": "where",
    # joiners
    "और": "and",
    # foods (Haryanvi kitchen included)
    "रोटी": "roti", "चपाती": "roti", "दाल": "dal", "पनीर": "paneer", "दही": "curd", "दूध": "milk", "चावल": "rice", "अंडा": "egg", "अंडे": "eggs",
    "केला": "banana", "केले": "banana", "घी": "ghee", "मक्खन": "butter", "छाछ": "buttermilk", "लस्सी": "lassi", "चिकन": "chicken", "मछली": "fish",
    "सेब": "apple", "बादाम": "almonds", "ओट्स": "oats", "दलिया": "oats", "आलू": "potato", "सब्जी": "sabzi", "सब्ज़ी": "sabzi", "व्हे": "whey",
}

_SPLIT = re.compile(r"([\s,।.!?]+)")


def has_devanagari(text: str) -> bool:
    return any("ऀ" <= c <= "ॿ" for c in text)


def _fmt(n: float) -> str:
    return str(int(n)) if float(n) == int(n) else f"{n:g}"


def _merge_numbers(run: list[float]) -> list[float]:
    """Hindi compounds: [1, 100, 10] -> [110]. Without 'सौ' (hundred) adjacent numbers stay separate ('अस्सी आठ' = 80, 8)."""
    if 100.0 in run:
        i = run.index(100.0)
        value = (sum(run[:i]) or 1) * 100 + (run[i + 1] if i + 1 < len(run) else 0)
        return [value] + run[i + 2:]
    return run


def normalize(text: str) -> str:
    """Devanagari speech -> English-ish text for the existing parsers. Text with no Devanagari is returned untouched."""
    if not text or not has_devanagari(text):
        return text
    t = text.translate(_DIGITS)
    for hi, en in PHRASES:
        t = t.replace(hi, en)
    out: list[str] = []
    run: list[float] = []
    gap = False   # a space was swallowed while a number run was open; put it back when the run closes

    def flush():
        nonlocal run, gap
        if run:
            out.append(" ".join(_fmt(n) for n in _merge_numbers(run)))
            if gap:
                out.append(" ")
            run, gap = [], False

    parts = _SPLIT.split(t)
    for part in parts:
        if not part:
            continue
        if _SPLIT.fullmatch(part):
            if run and part.strip() == "":          # plain spaces keep a number run together
                gap = True
                continue
            flush()
            out.append(part)
        elif part in NUMBERS:
            run.append(NUMBERS[part])
        else:
            flush()
            out.append(WORDS.get(part, part))
    flush()
    result = "".join(out)
    # Hindi puts the count first ("8 बार" = "8 times"); the set parser wants "for 8". Only Hindi's own rep words use the § marker.
    result = re.sub(r"(\d+(?:\.\d+)?)\s*§", lambda m: "for " + m.group(1), result).replace("§", "for")
    return re.sub(r"\s+", " ", result).strip()
