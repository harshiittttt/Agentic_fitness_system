"""Offline food database + meal-text parser. Values are per 100 g (typical cooked/ready-to-eat)."""
from __future__ import annotations

import re

# name: (aliases, kcal, protein, carbs, fat, unit_g (one natural serving/piece), unit label)
_RAW = {
    "chicken breast": (["chicken", "grilled chicken"], 165, 31, 0, 3.6, 120, "breast"),
    "egg": (["eggs", "boiled egg", "omelette"], 143, 12.6, 0.7, 9.5, 50, "egg"),
    "egg white": (["egg whites"], 52, 11, 0.7, 0.2, 33, "white"),
    "white rice": (["rice", "cooked rice", "basmati"], 130, 2.7, 28, 0.3, 150, "cup"),
    "brown rice": ([], 112, 2.6, 23, 0.9, 150, "cup"),
    "roti": (["chapati", "chapatti", "phulka"], 297, 9, 50, 7, 40, "roti"),
    "paratha": ([], 326, 7, 45, 13, 80, "paratha"),
    "dal": (["lentils", "daal", "lentil soup"], 116, 9, 20, 0.4, 200, "bowl"),
    "chickpeas": (["chole", "chana", "garbanzo"], 164, 8.9, 27, 2.6, 150, "cup"),
    "rajma": (["kidney beans"], 127, 8.7, 23, 0.5, 150, "cup"),
    "paneer": ([], 296, 18, 1.2, 25, 80, "serving"),
    "tofu": ([], 76, 8, 1.9, 4.8, 100, "serving"),
    "curd": (["yogurt", "yoghurt", "dahi"], 61, 3.5, 4.7, 3.3, 150, "bowl"),
    "greek yogurt": (["greek yoghurt"], 59, 10, 3.6, 0.4, 170, "tub"),
    "milk": ([], 61, 3.2, 4.8, 3.3, 240, "glass"),
    "whey protein": (["whey", "protein shake", "protein powder"], 400, 80, 8, 6, 30, "scoop"),
    "oats": (["oatmeal", "porridge"], 389, 16.9, 66, 6.9, 40, "serving"),
    "banana": ([], 89, 1.1, 23, 0.3, 118, "banana"),
    "apple": ([], 52, 0.3, 14, 0.2, 182, "apple"),
    "orange": ([], 47, 0.9, 12, 0.1, 130, "orange"),
    "bread": (["toast", "brown bread"], 265, 9, 49, 3.2, 30, "slice"),
    "peanut butter": ([], 588, 25, 20, 50, 16, "tbsp"),
    "almonds": (["almond", "nuts"], 579, 21, 22, 50, 28, "handful"),
    "olive oil": (["oil", "ghee", "butter"], 884, 0, 0, 100, 14, "tbsp"),
    "pasta": (["noodles", "spaghetti"], 158, 5.8, 31, 0.9, 180, "plate"),
    "potato": (["potatoes", "aloo"], 77, 2, 17, 0.1, 150, "potato"),
    "sweet potato": ([], 86, 1.6, 20, 0.1, 130, "potato"),
    "broccoli": ([], 34, 2.8, 7, 0.4, 90, "cup"),
    "salad": (["vegetables", "veggies", "sabzi"], 25, 1.5, 4, 0.3, 100, "bowl"),
    "salmon": ([], 208, 20, 0, 13, 150, "fillet"),
    "fish": (["tuna", "tilapia"], 116, 26, 0, 1, 150, "fillet"),
    "beef": (["steak", "mutton"], 250, 26, 0, 15, 150, "serving"),
    "cottage cheese": ([], 98, 11, 3.4, 4.3, 100, "serving"),
    "cheese": ([], 402, 25, 1.3, 33, 20, "slice"),
    "avocado": ([], 160, 2, 9, 15, 150, "avocado"),
    "protein bar": ([], 350, 33, 35, 10, 60, "bar"),
    "pizza": ([], 266, 11, 33, 10, 107, "slice"),
    "burger": ([], 295, 17, 24, 14, 200, "burger"),
    "dosa": ([], 168, 3.9, 29, 3.7, 100, "dosa"),
    "idli": ([], 130, 4, 27, 0.4, 40, "idli"),
    "buttermilk": (["chaach", "chach", "mattha"], 40, 3.3, 4.8, 0.9, 250, "glass"),
    "lassi": ([], 90, 3, 14, 2.5, 250, "glass"),
    "bajra roti": (["bajre ki roti", "bajra"], 360, 11.6, 67, 5, 50, "roti"),
    "kheer": ([], 150, 3.5, 22, 5, 150, "bowl"),
    "saag": (["sarson ka saag", "palak"], 40, 2.5, 4, 1.8, 150, "bowl"),
}

FOODS = {n: {"kcal": v[1], "p": v[2], "c": v[3], "f": v[4], "unit_g": v[5], "unit": v[6], "aliases": [n] + v[0]} for n, v in _RAW.items()}

# Hindi/Hinglish names and compound dishes. Extra aliases are added to the DB entries above.
_EXTRA_ALIASES = {
    "white rice": ["chawal", "chaval", "plain rice", "steamed rice", "jeera rice"],
    "roti": ["tawa roti", "missi roti", "phulke"],
    "egg": ["ande", "anda", "boiled eggs", "fried egg", "scrambled eggs"],
    "dal": ["dhal", "toor dal", "moong dal", "masoor dal", "dal tadka", "arhar dal"],
    "chickpeas": ["chhole", "kala chana", "chana masala"],
    "rajma": ["rajmah"],
    "milk": ["doodh", "dudh"],
    "curd": ["raita"],
    "salad": ["sabji", "subzi", "sabzee", "mixed veg", "bhindi", "gobi", "cucumber"],
    "potato": ["aloo sabzi"],
    "chicken breast": ["chicken curry", "tandoori chicken", "chicken tikka"],
    "bread": ["pav"],
    "oats": ["dalia", "daliya"],
}
_EXTRA_FOODS = {
    "poha": ([], 130, 2.5, 25, 2.5, 200, "plate"),
    "upma": ([], 150, 3.5, 22, 5, 200, "plate"),
    "khichdi": (["khichri"], 130, 4.5, 22, 2.5, 250, "bowl"),
    "biryani": (["chicken biryani", "veg biryani", "mutton biryani", "egg biryani"], 165, 6.5, 22, 5.5, 250, "plate"),
    "samosa": (["samose"], 262, 4, 30, 14, 60, "samosa"),
}
for _n, _v in _EXTRA_FOODS.items():
    FOODS[_n] = {"kcal": _v[1], "p": _v[2], "c": _v[3], "f": _v[4], "unit_g": _v[5], "unit": _v[6], "aliases": [_n] + _v[0]}
for _n, _al in _EXTRA_ALIASES.items():
    FOODS[_n]["aliases"] += [a for a in _al if a not in FOODS[_n]["aliases"]]

_ALIAS = sorted(((a, n) for n, f in FOODS.items() for a in f["aliases"]), key=lambda x: -len(x[0]))
_ALIAS_RE = re.compile(r"\b(" + "|".join(re.escape(a) for a, _ in _ALIAS) + r")(?:es|s)?\b")
_ALIAS_MAP = dict(_ALIAS)


def lookup_food(name: str) -> str | None:
    """Exact (plural-tolerant) alias or canonical-name lookup -> canonical DB food name, else None."""
    n = re.sub(r"\s+", " ", (name or "").lower().strip())
    for cand in (n, n[:-1] if n.endswith("s") else n, n[:-2] if n.endswith("es") else n):
        if cand in _ALIAS_MAP:
            return _ALIAS_MAP[cand]
    return None


# ---------------------------------------------------------------- number words -> digits
_NUMWORD = {
    **{k: ("u", v) for k, v in {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                                "ek": 1, "teen": 3, "char": 4, "paanch": 5, "panch": 5, "chhe": 6, "saat": 7, "aath": 8, "nau": 9}.items()},
    **{k: ("t", v) for k, v in {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
                                "seventeen": 17, "eighteen": 18, "nineteen": 19, "das": 10}.items()},
    **{k: ("T", v) for k, v in {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}.items()},
    **{k: ("f", v) for k, v in {"half": 0.5, "quarter": 0.25, "dedh": 1.5, "dhai": 2.5, "sawa": 1.25, "adha": 0.5, "aadha": 0.5, "couple": 2}.items()},
    "hundred": ("h", 100),
}
# 'do' (Hindi two) is also an English verb, so it only counts right before a unit/food word
_W = "|".join(sorted(_NUMWORD, key=len, reverse=True)) + r"|do(?=\s+(?:roti|rotis|chapati|egg|eggs|ande|glass|glasses|cup|cups|bowl|bowls|kg|litre|piece|pieces|slice|slices)\b)"
_RUN = re.compile(rf"\b(?:{_W})(?:[\s-]+(?:{_W}))*\b")


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def _eval_run(run: str) -> str:
    res, cur, last = [], None, None
    for tok in re.split(r"[\s-]+", run):
        k, v = _NUMWORD.get(tok, ("u", 2))
        if k == "f":
            if cur is not None:
                res.append(cur)
            res.append(v); cur = last = None
            continue
        if k == "h" and cur is not None and last == "u":
            cur, last = cur * 100, "h"
        elif k == "h" and cur is None:
            cur, last = 100, "h"
        elif k == "T" and last == "h":
            cur, last = cur + v, "T"
        elif k == "u" and last in ("h", "T"):
            cur, last = cur + v, "u2"
        elif k == "t" and last == "h":
            cur, last = cur + v, "t2"
        else:  # cannot extend the current number: start a new one
            if cur is not None:
                res.append(cur)
            cur, last = v, ("u" if k == "u" else k)
            if k == "h":
                cur = 100
    if cur is not None:
        res.append(cur)
    return " ".join(_fmt(x) for x in res)


def _words_to_digits(text: str) -> str:
    """'two hundred fifty grams' -> '250 grams'; 'half kg' -> '0.5 kg'; 'ek glass' -> '1 glass'; 'half dozen' -> '6'."""
    text = re.sub(r"\bhalf\s+(?:a\s+)?dozen\b", "6", text)
    text = re.sub(r"\b(?:a\s+)?dozen\b", "12", text)
    text = text.replace("½", " 0.5 ").replace("¼", " 0.25 ").replace("¾", " 0.75 ")
    text = re.sub(r"(\d+)\s*/\s*(\d+)", lambda m: _fmt(int(m.group(1)) / int(m.group(2))) if int(m.group(2)) else m.group(0), text)
    # "one and a half cups", "2 and a quarter litres" (before the splitter can cut at 'and')
    unit_words = "|".join(k for k, (kind, _) in _NUMWORD.items() if kind == "u")
    text = re.sub(rf"\b(\d+(?:\.\d+)?|{unit_words})\s+and\s+(?:a\s+)?(half|quarter)\b",
                  lambda m: _fmt((float(m.group(1)) if m.group(1)[0].isdigit() else _NUMWORD[m.group(1)][1]) + (0.5 if m.group(2) == "half" else 0.25)), text)
    text = _RUN.sub(lambda m: _eval_run(m.group(0)), text)
    return text


# ---------------------------------------------------------------- quantities
_WEIGHT = {"kg": 1000, "kgs": 1000, "kilo": 1000, "kilos": 1000, "kilogram": 1000, "kilograms": 1000, "l": 1000, "litre": 1000, "litres": 1000,
           "liter": 1000, "liters": 1000, "g": 1, "gm": 1, "gms": 1, "gram": 1, "grams": 1, "gr": 1, "ml": 1}
_GENERIC = {"cup": 150, "cups": 150, "bowl": 200, "bowls": 200, "katori": 120, "katoris": 120, "glass": 250, "glasses": 250,
            "plate": 250, "plates": 250, "tbsp": 15, "tbsps": 15, "tablespoon": 15, "tablespoons": 15, "tsp": 5, "teaspoon": 5, "teaspoons": 5}
_COUNT = {"piece", "pieces", "slice", "slices", "scoop", "scoops", "serving", "servings", "handful", "handfuls", "pc", "pcs"}
_UNITS = "|".join(sorted(set(_WEIGHT) | set(_GENERIC) | _COUNT, key=len, reverse=True))
# "<number> [unit]" or "a/an <unit>" or a bare unit ("glass of lassi")
_QTY = re.compile(rf"(?:(?P<n>\d+(?:\.\d+)?)(?![\d.])\s*(?P<u>{_UNITS})?\b(?!\s*(?:am|pm)\b)|\b(?:a|an)?\s*(?P<u2>{_UNITS})\b)")
_LIQUID = {"milk", "lassi", "buttermilk"}


def _find_qty(seg: str, base: int, consumed: set, last: bool = True):
    best = None
    for m in _QTY.finditer(seg):
        if m.group("n") is None and m.group("u2") in ("l", "g", "gr", "gm"):
            continue  # a stray 'l'/'g' without a number is not a unit
        if (base + m.start()) in consumed:
            continue
        best = m
        if not last:
            break
    return best


def _qty_grams(m, food: dict, name: str | None):
    """(grams, explicit, ambiguous) from a quantity match (None -> one natural serving, not explicit)."""
    if m is None:
        return float(food["unit_g"]), False, False
    n = float(m.group("n")) if m.group("n") is not None else 1.0
    u = m.group("u") or m.group("u2")
    if u in _WEIGHT:
        return n * _WEIGHT[u], True, False
    if u in _GENERIC:
        same = u.rstrip("s") == food["unit"] or (food["unit"] == "bowl" and u.startswith("bowl"))
        if same or food["kcal"] >= 200:  # dense foods (oats, nuts, oil): trust the food's own serving
            return n * food["unit_g"], True, False
        base = 250 if name in _LIQUID and u in ("cup", "cups", "glass", "glasses") else _GENERIC[u]
        return n * base, True, False
    if u in _COUNT:
        return n * food["unit_g"], True, False
    if m.group("n") is not None and n >= 25:  # bare large number ("400 rice"): grams, but ambiguous
        return n, True, True
    return n * food["unit_g"], True, False


def macros(food: dict, grams: float) -> dict:
    k = grams / 100.0
    return {"kcal": round(food["kcal"] * k), "protein": round(food["p"] * k, 1),
            "carbs": round(food["c"] * k, 1), "fat": round(food["f"] * k, 1)}


_STOP = set("""i had ate just for breakfast lunch dinner snack snacks some of a an the my in on at today also then about around approx
approximately like little bit small big large medium full me was it meal eaten have having took drank drink drinking eat eating finished
good tasty its is and or to from after before post pre workout whole half grilled boiled fried cooked fresh raw plain homemade hot cold
roasted baked steamed more extra lots lot few heavy light normal regular kha liya li khaya piya pi maine hum ho gaya gayi ki ka ke ko se
mein""".split()) | set(_WEIGHT) | set(_GENERIC) | _COUNT
_SPLIT = re.compile(r",|;|\+|&|\n|\band\b|\bwith\b|\bplus\b|\baur\b|\bke saath\b|(?<!\d)\.|\.(?!\d)")


def _leftover(chunk: str, spans: list[tuple[int, int]]) -> str:
    """Words in a chunk that are not a food, number, unit or filler (i.e. unexplained)."""
    rest, prev = [], 0
    for a, b in spans:
        rest.append(chunk[prev:a])
        prev = b
    rest.append(chunk[prev:])
    return " ".join(w for w in re.findall(r"[a-z']+", " ".join(rest)) if w not in _STOP and len(w) >= 3)


def parse_meal_text(text: str) -> dict:
    """'2 eggs, 150g chicken and a cup of rice' -> items with macros. Unknown chunks are returned, not guessed.

    Also returns `low_confidence` (+ `reasons`): True when something is unexplained, a quantity is ambiguous, a compound dish
    ('rajma chawal') was split, or a multi-item text has foods with no stated amount. Callers may use an LLM then."""
    text = _words_to_digits(text.lower())
    chunks = [c.strip() for c in _SPLIT.split(text) if c and c.strip()]
    items, unknown, reasons = [], [], set()
    for ch in chunks:
        spans, last_end = [], -1
        for m in _ALIAS_RE.finditer(ch):
            if m.start() >= last_end:
                spans.append(m)
                last_end = m.end()
        if not spans:
            if _leftover(ch, []):
                unknown.append(ch)
            continue
        consumed: set = set()
        prev_end = 0
        for idx, m in enumerate(spans):
            name = _ALIAS_MAP[m.group(1)]
            food = FOODS[name]
            nxt = spans[idx + 1].start() if idx + 1 < len(spans) else len(ch)
            q, base = _find_qty(ch[prev_end:m.start()], prev_end, consumed), prev_end
            if q is None:  # amount written after the food: "rice 200 g", "paneer 2 pieces"
                q2 = _find_qty(ch[m.end():nxt], m.end(), consumed, last=False)
                if q2 is not None and (q2.group("u") or q2.group("u2")):
                    q, base = q2, m.end()
            if q is not None:
                consumed.add(base + q.start())
            g, explicit, ambiguous = _qty_grams(q, food, name)
            if q is None and re.search(r"\b(?:a|an)\s*$", ch[prev_end:m.start()]):
                explicit = True  # "an egg", "a banana": one, stated
            items.append({"name": name, "grams": round(g), **macros(food, g), "estimated": False, "_explicit": explicit})
            if ambiguous:
                reasons.add("ambiguous quantity")
            prev_end = m.end()
        if len(spans) > 1:
            reasons.add("compound dish")
        lo = _leftover(ch, [(m.start(), m.end()) for m in spans])
        if lo:
            unknown.append(lo)
    if unknown:
        reasons.add("unknown food")
    if len(items) > 1 and any(not i["_explicit"] for i in items):
        reasons.add("amount missing in multi-item meal")
    for i in items:
        i["qty_given"] = bool(i.pop("_explicit", False))
    return {"items": items, "unknown": unknown, "low_confidence": bool(reasons), "reasons": sorted(reasons)}


_HIGH_PROTEIN = ["chicken breast", "whey protein", "greek yogurt", "egg white", "tofu", "paneer", "cottage cheese", "fish"]


def suggest_for_protein(gap_g: float, kcal_left: float) -> list[dict]:
    """Foods that close (part of) the protein gap without blowing the calorie budget."""
    if gap_g < 8 or kcal_left < 80:
        return []
    out = []
    target = min(gap_g, 40)
    for n in _HIGH_PROTEIN:
        f = FOODS[n]
        grams = max(f["unit_g"] * 0.5, round(target / f["p"] * 100 / 10) * 10)
        m = macros(f, grams)
        if m["kcal"] <= kcal_left:
            out.append({"food": n, "grams": round(grams), "protein": m["protein"], "kcal": m["kcal"]})
    return sorted(out, key=lambda x: x["kcal"] / max(x["protein"], 1))[:3]
