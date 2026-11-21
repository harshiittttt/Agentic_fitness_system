"""Deterministic Indian meal planner. The model never does portion maths: it asks this module, which sizes every food from the
athlete's real calorie / protein targets and the offline food database (backend/tools/foods.py), then reports exact totals.

    plan_day(targets, prefs, variation=0) -> {"meals": [...], "totals": {...}, "target": {...}, "delta": {...}, "notes": [...]}
    plan_week(targets, prefs)             -> {"days": [plan_day(...) x7], "grocery": [...]}
    swap_food(meal, food, prefs)           -> the same meal with `food` replaced by an equal-protein alternative

Sizing: every meal option is a few food groups. Each group is multiplied by one scale factor chosen so the meal lands on its share of the
day's calories and protein (closed form for 1 or 2 groups, clamped to sane portions). Portions are then rounded to real household units
(whole eggs, whole rotis, 5 g steps), and totals are recomputed from the rounded amounts, so the numbers shown are what you would eat."""
from __future__ import annotations

from collections import defaultdict

from backend.tools.foods import FOODS, lookup_food, macros

DIET_TYPES = ("veg", "egg", "nonveg")
_RANK = {"veg": 0, "egg": 1, "nonveg": 2}

# (slot name, share of the day's calories, share of the day's protein) by number of meals
SLOTS: dict[int, list[tuple[str, float, float]]] = {
    3: [("Breakfast", 0.27, 0.27), ("Lunch", 0.38, 0.38), ("Dinner", 0.35, 0.35)],
    4: [("Breakfast", 0.25, 0.25), ("Lunch", 0.30, 0.30), ("Evening snack", 0.15, 0.15), ("Dinner", 0.30, 0.30)],
    5: [("Breakfast", 0.22, 0.22), ("Lunch", 0.28, 0.28), ("Evening snack", 0.14, 0.14), ("Dinner", 0.26, 0.26), ("Before bed", 0.10, 0.10)],
}

# Whole-piece foods are rounded to these steps (grams are derived from the count).
_PIECE = {"egg": 1, "roti": 1, "banana": 1, "apple": 1, "orange": 1, "idli": 1, "dosa": 1, "paratha": 1, "bread": 1, "samosa": 1, "besan chilla": 1, "protein bar": 1}

# An option is (title, min diet type, [component...]); component = (food, group, base_grams) where group is
# "a" / "b" (scaled to hit the meal) or "fix" (a fixed side).
# Only foods in the database are used, so every number is traceable.
_OPTIONS: dict[str, list[tuple[str, str, list[tuple[str, str, float]]]]] = {
    "Breakfast": [
        ("Paneer bhurji with roti", "veg", [("paneer", "a", 80), ("roti", "b", 80), ("salad", "fix", 80)]),
        ("Besan chilla with curd", "veg", [("besan chilla", "a", 160), ("curd", "fix", 150), ("salad", "fix", 60)]),
        ("Oats with milk, banana and peanuts", "veg", [("milk", "a", 250), ("oats", "b", 50), ("banana", "fix", 118), ("peanuts", "fix", 15)]),
        ("Poha with sprouts and milk", "veg", [("poha", "b", 200), ("moong sprouts", "a", 100), ("milk", "fix", 200)]),
        ("Egg bhurji with roti", "egg", [("egg", "a", 150), ("roti", "b", 80), ("salad", "fix", 80)]),
        ("Boiled eggs with oats and banana", "egg", [("egg", "a", 150), ("oats", "b", 50), ("banana", "fix", 118)]),
        ("Omelette with bread", "egg", [("egg", "a", 150), ("bread", "b", 60), ("salad", "fix", 80)]),
    ],
    "Lunch": [
        ("Dal, rice, curd and salad", "veg", [("dal", "a", 250), ("white rice", "b", 200), ("curd", "fix", 100), ("salad", "fix", 100)]),
        ("Rajma with rice and salad", "veg", [("rajma", "a", 200), ("white rice", "b", 200), ("salad", "fix", 100), ("curd", "fix", 100)]),
        ("Chole with roti and curd", "veg", [("chickpeas", "a", 200), ("roti", "b", 120), ("curd", "fix", 100), ("salad", "fix", 80)]),
        ("Paneer sabzi, roti and dal", "veg", [("paneer", "a", 100), ("roti", "b", 120), ("dal", "fix", 120), ("salad", "fix", 80)]),
        ("Soya chunks curry with roti", "veg", [("soya chunks", "a", 40), ("roti", "b", 120), ("curd", "fix", 100), ("salad", "fix", 80)]),
        ("Chicken curry, rice and salad", "nonveg", [("chicken breast", "a", 150), ("white rice", "b", 200), ("salad", "fix", 100), ("olive oil", "fix", 7)]),
        ("Chicken with roti and dal", "nonveg", [("chicken breast", "a", 150), ("roti", "b", 120), ("dal", "fix", 120), ("salad", "fix", 80)]),
        ("Egg curry with rice", "egg", [("egg", "a", 200), ("white rice", "b", 200), ("salad", "fix", 100), ("olive oil", "fix", 7)]),
    ],
    "Evening snack": [
        ("Curd with peanuts and a fruit", "veg", [("curd", "a", 200), ("peanuts", "b", 25), ("apple", "fix", 182)]),
        ("Whey shake and a banana", "veg", [("whey protein", "a", 30), ("banana", "b", 118), ("milk", "fix", 150)]),
        ("Sprouts chaat with curd", "veg", [("moong sprouts", "a", 150), ("curd", "b", 100), ("peanuts", "fix", 10)]),
        ("Paneer cubes and roasted peanuts", "veg", [("paneer", "a", 60), ("peanuts", "b", 20), ("orange", "fix", 130)]),
        ("Boiled eggs and a banana", "egg", [("egg", "a", 100), ("banana", "b", 118)]),
    ],
    "Dinner": [
        ("Paneer, roti and salad", "veg", [("paneer", "a", 100), ("roti", "b", 120), ("salad", "fix", 120), ("curd", "fix", 100)]),
        ("Dal khichdi with curd and paneer", "veg", [("khichdi", "b", 250), ("paneer", "a", 60), ("curd", "fix", 100)]),
        ("Moong dal, roti and sabzi", "veg", [("dal", "a", 250), ("roti", "b", 100), ("salad", "fix", 150)]),
        ("Soya chunks pulao with curd", "veg", [("soya chunks", "a", 35), ("white rice", "b", 200), ("curd", "fix", 100), ("salad", "fix", 80)]),
        ("Chicken, roti and sabzi", "nonveg", [("chicken breast", "a", 160), ("roti", "b", 100), ("salad", "fix", 150), ("olive oil", "fix", 7)]),
        ("Chicken with rice and dal", "nonveg", [("chicken breast", "a", 150), ("white rice", "b", 180), ("dal", "fix", 100), ("salad", "fix", 100)]),
        ("Egg bhurji, roti and sabzi", "egg", [("egg", "a", 150), ("roti", "b", 100), ("salad", "fix", 150)]),
    ],
    "Before bed": [
        ("Warm milk with almonds", "veg", [("milk", "a", 250), ("almonds", "b", 20)]),
        ("Curd with a handful of peanuts", "veg", [("curd", "a", 200), ("peanuts", "b", 20)]),
        ("Milk with a boiled egg", "egg", [("milk", "a", 250), ("egg", "fix", 50)]),
    ],
}


def default_prefs() -> dict:
    return {"diet_type": "veg", "meals_per_day": 3, "dislikes": [], "allergies": [], "budget": "medium", "notes": ""}


def clean_prefs(p: dict | None) -> dict:
    out = default_prefs()
    p = p or {}
    if p.get("diet_type") in DIET_TYPES:
        out["diet_type"] = p["diet_type"]
    try:
        n = int(p.get("meals_per_day"))
        if n in SLOTS:
            out["meals_per_day"] = n
    except (TypeError, ValueError):
        pass
    for k in ("dislikes", "allergies"):
        v = p.get(k) or []
        if isinstance(v, str):
            v = [x for x in v.replace(";", ",").split(",")]
        out[k] = [str(x).strip().lower()[:40] for x in v if str(x).strip()][:20]
    if p.get("budget") in ("low", "medium", "high"):
        out["budget"] = p["budget"]
    out["notes"] = str(p.get("notes") or "")[:300]
    return out


def _blocked(foods: list[str], prefs: dict) -> bool:
    bad = set(prefs.get("dislikes", [])) | set(prefs.get("allergies", []))
    for f in foods:
        names = {f, *FOODS[f]["aliases"]}
        if any(b and (b in n or n in b) for b in bad for n in names):
            return True
        if "milk" in bad and f in ("milk", "curd", "paneer", "whey protein"):      # a milk allergy rules out the whole dairy group
            return True
    return False


def options_for(slot: str, prefs: dict) -> list[tuple[str, str, list[tuple[str, str, float]]]]:
    rank = _RANK[prefs["diet_type"]]
    out = []
    for title, kind, comps in _OPTIONS.get(slot, []):
        if _RANK[kind] > rank:
            continue
        if any(c[0] not in FOODS for c in comps):
            continue
        if _blocked([c[0] for c in comps], prefs):
            continue
        out.append((title, kind, comps))
    return out


# ---------------------------------------------------------------- sizing
def _vec(food: str, grams: float) -> tuple[float, float]:
    m = macros(FOODS[food], grams)
    return m["kcal"], m["protein"]


def _solve(comps: list[tuple[str, str, float]], kcal: float, prot: float) -> tuple[float, float]:
    """Scale factors (a, b) for the two scalable groups so the meal is as close as possible to (kcal, prot)."""
    fixed = [0.0, 0.0]
    g = {"a": [0.0, 0.0], "b": [0.0, 0.0]}
    for f, grp, base in comps:
        k, p = _vec(f, base)
        tgt = fixed if grp == "fix" else g[grp]
        tgt[0] += k
        tgt[1] += p
    rk, rp = max(kcal - fixed[0], 1.0), prot - fixed[1]
    ka, pa = g["a"]
    kb, pb = g["b"]
    alo, ahi, blo, bhi = 0.4, 1.5, 0.25, 1.8             # keep portions believable: nobody eats 175 g of paneer for breakfast

    def clamp(x: float) -> float:
        return max(alo, min(ahi, x))

    if kb == 0 and pb == 0:                              # one scalable group: balance relative kcal and protein error
        num = (ka / max(kcal, 1)) * (rk / max(kcal, 1)) + (pa / max(prot, 1)) * (rp / max(prot, 1))
        den = (ka / max(kcal, 1)) ** 2 + (pa / max(prot, 1)) ** 2
        return clamp(num / den) if den else 1.0, 1.0
    det = ka * pb - kb * pa
    if abs(det) > 1e-6:
        a = (rk * pb - kb * rp) / det
        b = (ka * rp - rk * pa) / det
        if alo <= a <= ahi and blo <= b <= bhi:
            return a, b
    best = (1.0, 1.0, 1e18)                              # no exact solution inside sane portions: coarse search, protein weighted a bit higher
    for a in [alo + i * (ahi - alo) / 40 for i in range(41)]:
        for b in [blo + i * (bhi - blo) / 40 for i in range(41)]:
            e = ((a * ka + b * kb - rk) / max(kcal, 1)) ** 2 + 1.4 * ((a * pa + b * pb - rp) / max(prot, 1)) ** 2
            if e < best[2]:
                best = (a, b, e)
    return best[0], best[1]


def _round_grams(food: str, grams: float) -> float:
    unit = FOODS[food]["unit_g"]
    if food in _PIECE:
        n = max(1, round(grams / unit)) if food != "banana" else max(1, round(grams / unit))
        return n * unit
    if food == "whey protein":
        return max(1, round(grams / 30)) * 30
    if food == "milk":
        return max(50, round(grams / 50) * 50)
    return max(5, round(grams / 5) * 5)


def describe(food: str, grams: float) -> str:
    """A quantity a person can actually measure: '3 eggs', '2 rotis', '1 katori (150 g) rajma', '30 g soya chunks'."""
    f = FOODS[food]
    unit, ug = f["unit"], f["unit_g"]
    if food in _PIECE:
        n = max(1, round(grams / ug))
        return f"{n} {food}{'s' if n > 1 and not food.endswith('s') else ''}" if food in ("egg", "roti", "banana", "idli", "dosa", "paratha") else f"{n} {unit}{'s' if n > 1 else ''} of {food}"
    if food == "whey protein":
        n = max(1, round(grams / 30))
        return f"{n} scoop{'s' if n > 1 else ''} whey"
    if unit in ("bowl", "cup", "serving", "plate") and ug >= 100:
        n = grams / ug
        if 0.35 <= n:
            half = round(n * 2) / 2
            return f"{half:g} {'katori' if unit in ('bowl', 'cup') else unit} ({round(grams)} g) {food}"
    if unit == "glass":
        return f"{round(grams)} ml {food}"
    if food == "olive oil":
        return f"{round(grams / 14) or 1} tsp ghee/oil" if grams < 14 else f"{round(grams / 14)} tbsp ghee/oil"
    return f"{round(grams)} g {food}"


def _meal(slot: str, title: str, comps: list[tuple[str, str, float]], kcal: float, prot: float) -> dict:
    a, b = _solve(comps, kcal, prot)
    items = []
    for food, grp, base in comps:
        grams = base * (a if grp == "a" else b if grp == "b" else 1.0)
        grams = _round_grams(food, grams) if grp != "fix" else _round_grams(food, base)
        m = macros(FOODS[food], grams)
        items.append({"food": food, "grams": round(grams), "label": describe(food, grams), **m})
    tot = {k: round(sum(i[k] for i in items), 1) for k in ("kcal", "protein", "carbs", "fat")}
    tot["kcal"] = round(tot["kcal"])
    return {"slot": slot, "title": title, "items": items, "totals": tot, "target": {"kcal": round(kcal), "protein": round(prot)}}


def _energy_shake(short_kcal: float, prefs: dict) -> dict | None:
    """Milk + banana + peanut butter, sized to the missing calories (a classic bulking add-on)."""
    if _blocked(["milk", "banana", "peanut butter"], prefs):
        return None
    k_milk, k_ban, k_pb = (macros(FOODS[f], FOODS[f]["unit_g"])["kcal"] for f in ("milk", "banana", "peanut butter"))
    unit = k_milk + k_ban + k_pb
    n = max(1, min(2, round(min(short_kcal, 800) / unit)))
    items = []
    for f, mult in (("milk", n), ("banana", 1), ("peanut butter", n)):
        g = FOODS[f]["unit_g"] * mult
        items.append({"food": f, "grams": round(g), "label": describe(f, g), **macros(FOODS[f], g)})
    tot = {k: round(sum(i[k] for i in items), 1) for k in ("kcal", "protein", "carbs", "fat")}
    tot["kcal"] = round(tot["kcal"])
    return {"slot": "Energy add-on", "title": "Banana peanut-butter milkshake", "items": items, "totals": tot, "target": {"kcal": tot["kcal"], "protein": round(tot["protein"])}}


def _top_up(gap: float, prefs: dict) -> dict | None:
    """One extra small meal that closes a protein gap, from what this person eats."""
    rank = _RANK[prefs["diet_type"]]
    cands = []
    if not _blocked(["whey protein"], prefs):
        cands.append(("Whey shake", "whey protein"))
    if rank >= 1 and not _blocked(["egg"], prefs):
        cands.append(("Boiled eggs", "egg"))
    if rank >= 2 and not _blocked(["chicken breast"], prefs):
        cands.append(("Chicken salad", "chicken breast"))
    if not _blocked(["soya chunks"], prefs):
        cands.append(("Soya chunks stir-fry", "soya chunks"))
    if not _blocked(["curd"], prefs):
        cands.append(("Bowl of curd", "curd"))
    if not cands:
        return None
    title, food = cands[0]
    grams = _round_grams(food, min(gap, 45) / FOODS[food]["p"] * 100)
    m = macros(FOODS[food], grams)
    return {"slot": "Protein top-up", "title": title, "items": [{"food": food, "grams": round(grams), "label": describe(food, grams), **m}],
            "totals": {"kcal": m["kcal"], "protein": m["protein"], "carbs": m["carbs"], "fat": m["fat"]}, "target": {"kcal": m["kcal"], "protein": round(min(gap, 45))}}


def plan_day(targets: dict, prefs: dict | None = None, variation: int = 0) -> dict:
    """targets = {"kcal", "protein", "carbs", "fat"} (grams). Returns the day with exact totals and how far they are from the targets."""
    prefs = clean_prefs(prefs)
    slots = SLOTS[prefs["meals_per_day"]]
    meals, notes = [], []
    for i, (slot, ks, ps) in enumerate(slots):
        opts = options_for(slot, prefs)
        if not opts:
            opts = options_for(slot, {**prefs, "dislikes": [], "allergies": prefs["allergies"]})   # honour allergies even when dislikes empty the list
        if not opts:
            notes.append(f"No {slot.lower()} option fits your allergies; skipped.")
            continue
        k_t, p_t = targets["kcal"] * ks, targets["protein"] * ps
        built = [(_RANK[kind], _meal(slot, t, c, k_t, p_t)) for t, kind, c in opts]
        err = lambda m: abs(m["totals"]["kcal"] - k_t) / max(k_t, 1) + 0.8 * abs(m["totals"]["protein"] - p_t) / max(p_t, 1)
        good = [(r, m) for r, m in built if err(m) <= 0.16] or sorted(built, key=lambda x: err(x[1]))[:2]     # variety only among options that actually fit this slot
        mine = _RANK[prefs["diet_type"]]
        if mine > 0 and variation % 3 != 2:                 # someone who eats eggs / chicken gets them most days, not a paneer plan with the odd egg
            top = [(r, m) for r, m in good if r == mine] or [(r, m) for r, m in good if r > 0]
            good = top or good
        good = [m for _, m in good]
        meals.append(good[(variation + i) % len(good)])
    short = targets["kcal"] - sum(m["totals"]["kcal"] for m in meals)
    if short > 0.08 * targets["kcal"] and short > 200:      # big appetites: believable portions fall short on energy, so add a real shake
        shake = _energy_shake(short, prefs)
        if shake:
            meals.append(shake)
            notes.append(f"Added {shake['title'].lower()} for the extra ~{round(shake['totals']['kcal'])} kcal your target needs.")
    gap = targets["protein"] - sum(m["totals"]["protein"] for m in meals)
    if gap > 12:                                          # believable portions leave protein short: add a real top-up instead of giant servings
        top = _top_up(gap, prefs)
        if top:
            meals.append(top)
            notes.append(f"Added a protein top-up ({top['title'].lower()}) because normal portions of these meals stop about {gap:.0f} g short.")
    tot = {k: round(sum(m["totals"][k] for m in meals), 1) for k in ("kcal", "protein", "carbs", "fat")}
    tot["kcal"] = round(tot["kcal"])
    delta = {"kcal": round(tot["kcal"] - targets["kcal"]), "protein": round(tot["protein"] - targets["protein"], 1)}
    if delta["protein"] < -max(8, 0.1 * targets["protein"]):
        notes.append(f"Protein lands {abs(delta['protein']):.0f} g under the target; add a bowl of curd or a scoop of whey.")
    return {"meals": meals, "totals": tot, "target": {k: targets.get(k) for k in ("kcal", "protein", "carbs", "fat")}, "delta": delta, "notes": notes,
            "diet_type": prefs["diet_type"], "meals_per_day": len(meals)}


def plan_week(targets: dict, prefs: dict | None = None) -> dict:
    prefs = clean_prefs(prefs)
    days = [plan_day(targets, prefs, variation=d) for d in range(7)]
    return {"days": days, "grocery": grocery(days)}


def grocery(days: list[dict]) -> list[dict]:
    """Total grams per food across the given days, as a shopping list (raw weights are approximate for cooked foods)."""
    tot: dict[str, float] = defaultdict(float)
    for d in days:
        for m in d["meals"]:
            for i in m["items"]:
                tot[i["food"]] += i["grams"]
    out = []
    for food, g in sorted(tot.items(), key=lambda kv: -kv[1]):
        unit, ug = FOODS[food]["unit"], FOODS[food]["unit_g"]
        pieces = f"about {round(g / ug)} {unit}{'s' if g / ug >= 1.5 else ''}" if food in _PIECE else (f"{g / 1000:.1f} kg" if g >= 1000 else f"{round(g)} g")
        out.append({"food": food, "grams": round(g), "buy": pieces})
    return out


# ---------------------------------------------------------------- swaps
_GROUP = {
    "protein": ["paneer", "chicken breast", "egg", "tofu", "soya chunks", "dal", "rajma", "chickpeas", "greek yogurt", "cottage cheese", "curd", "fish", "whey protein", "moong sprouts"],
    "carb": ["roti", "white rice", "brown rice", "oats", "bread", "poha", "khichdi", "potato", "sweet potato", "pasta", "bajra roti"],
}


def swap_food(meal: dict, food: str, prefs: dict | None = None, replacement: str | None = None) -> dict | None:
    """Replace one food in a meal by another of the same kind, resized so protein (protein foods) or kcal (carbs) stays the same."""
    prefs = clean_prefs(prefs)
    food = lookup_food(food) or food
    item = next((i for i in meal["items"] if i["food"] == food), None)
    if not item:
        return None
    kind = "protein" if food in _GROUP["protein"] else "carb" if food in _GROUP["carb"] else None
    if not kind:
        return None
    present = {i["food"] for i in meal["items"]}
    rank = _RANK[prefs["diet_type"]]
    ok_nonveg = {"chicken breast", "fish"}
    cands = [c for c in _GROUP[kind] if c != food and c in FOODS and c not in present and not _blocked([c], prefs)
             and not (c in ok_nonveg and rank < 2) and not (c == "egg" and rank < 1)]
    if replacement:
        r = lookup_food(replacement)
        if not r or r in present or _blocked([r], prefs) or (r in ok_nonveg and rank < 2) or (r == "egg" and rank < 1):
            return None
        cands = [r]
    if not cands:
        return None
    new = cands[0]
    key = "protein" if kind == "protein" else "kcal"
    per100 = FOODS[new]["p"] if kind == "protein" else FOODS[new]["kcal"]
    if per100 <= 0:
        return None
    grams = _round_grams(new, item[key] / per100 * 100)
    items = []
    for i in meal["items"]:
        if i["food"] == food:
            m = macros(FOODS[new], grams)
            items.append({"food": new, "grams": round(grams), "label": describe(new, grams), **m})
        else:
            items.append(i)
    tot = {k: round(sum(i[k] for i in items), 1) for k in ("kcal", "protein", "carbs", "fat")}
    tot["kcal"] = round(tot["kcal"])
    return {**meal, "items": items, "totals": tot, "title": meal["title"] + f" (with {new})", "swapped": {"from": food, "to": new}}


def week_calories_ok(week: dict, tol: float = 0.12) -> bool:
    return all(abs(d["delta"]["kcal"]) <= tol * (d["target"]["kcal"] or 1) for d in week["days"])


__all__ = ["plan_day", "plan_week", "swap_food", "grocery", "describe", "clean_prefs", "default_prefs", "options_for", "SLOTS", "DIET_TYPES"]
