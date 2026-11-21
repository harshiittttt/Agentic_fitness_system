"""Meal logging + daily nutrition summary. Offline food DB first; LLM only estimates unknown foods."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from backend.agents.base import extract_structured, have_key
from backend.coach.hindi import normalize
from backend.memory import db
from backend.tools.foods import FOODS, lookup_food, macros, parse_meal_text, suggest_for_protein

LLM_TIMEOUT_S = 15.0

ESTIMATE_TOOL = {
    "name": "estimate_foods",
    "description": "Parse the whole meal description into one item per food actually eaten, with the amount eaten in grams and "
                   "its nutrition. Use stated quantities exactly (half kg = 500 g, 1.5 cups, 3 rotis); otherwise typical Indian "
                   "home portions. Split compound dishes (rajma chawal = rajma + rice). Set matches_db_name when the food is one of the listed names.",
    "input_schema": {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "grams": {"type": "number", "description": "grams eaten (liquids: ml)"},
                           "kcal": {"type": "number"}, "protein": {"type": "number"}, "carbs": {"type": "number"}, "fat": {"type": "number"},
                           "qty_given": {"type": "boolean", "description": "true only if the user stated an amount for this item (a number, 'a/one', a unit); false when you guessed a typical portion"},
                           "matches_db_name": {"type": "string", "description": "exact name from the known-foods list, if it is that food"}},
            "required": ["name", "grams", "kcal", "protein", "carbs", "fat"]}}},
        "required": ["items"],
    },
}


def _num(v, lo: float, hi: float, default: float | None = None) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if x != x:  # NaN
        return default
    return max(lo, min(hi, x))


def sanitize_item(raw: dict) -> dict | None:
    """One LLM item -> a safe item. Known DB foods are recomputed from the DB at the LLM's grams (DB is truth);
    others keep the LLM estimate (estimated=True) after clamping grams and fixing kcal that disagrees with 4/4/9."""
    if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
        return None
    name = str(raw["name"]).strip().lower()[:80]
    db_name = None
    for cand in (raw.get("matches_db_name"), name):
        if cand and (hit := lookup_food(str(cand))):
            db_name = hit
            break
    grams = _num(raw.get("grams"), 1, 3000)
    given = raw.get("qty_given") is True and grams is not None
    if db_name:
        food = FOODS[db_name]
        g = grams if grams is not None else float(food["unit_g"])
        return {"name": db_name, "grams": round(g), **macros(food, g), "estimated": False, "qty_given": given}
    p, c, f = (_num(raw.get(k), 0, 500, 0.0) for k in ("protein", "carbs", "fat"))
    kcal = _num(raw.get("kcal"), 0, 6000)
    calc = 4 * p + 4 * c + 9 * f
    if kcal is None or kcal <= 0:
        kcal = calc
    elif calc > 0 and abs(kcal - calc) / calc > 0.25:
        kcal = calc
    if kcal <= 0 and calc <= 0:
        return None
    return {"name": name, "grams": round(grams) if grams is not None else 0, "kcal": round(kcal), "protein": round(p, 1),
            "carbs": round(c, 1), "fat": round(f, 1), "estimated": True, "qty_given": given}


def _llm_parse(text: str) -> list[dict]:
    """Whole-text LLM parse. [] on no key, error, timeout or nothing usable (caller falls back to the offline parse)."""
    if not have_key():
        return []
    system = ("You turn a lifter's meal description into structured nutrition. Be realistic; never refuse. Known foods "
              "(use these exact names in matches_db_name when the food is one of them): " + ", ".join(sorted(FOODS)) + ".")
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        out = ex.submit(extract_structured, system, text, ESTIMATE_TOOL).result(timeout=LLM_TIMEOUT_S)
        items = [i for i in (sanitize_item(r) for r in (out or {}).get("items") or []) if i]
        return items[:25]
    except Exception:  # timeout, HTTP error, bad JSON: fail soft
        return []
    finally:
        ex.shutdown(wait=False)


def parse_for_log(text: str) -> tuple[list[dict], list]:
    """Shared by log_meal and /meal/preview: offline parse first, hybrid LLM when unsure. Inserts nothing."""
    parsed = parse_meal_text(normalize(text))
    items, unknown = parsed["items"], parsed["unknown"]
    if parsed.get("low_confidence") or unknown:
        llm = _llm_parse(normalize(text))
        if llm:
            items, unknown = llm, []
    return items, unknown


def log_meal(text: str, day: str | None = None) -> dict:
    day = day or date.today().isoformat()
    items, unknown = parse_for_log(text)
    for it in items:
        db.add_meal(day, it["name"], it.get("grams"), it["kcal"], it["protein"], it["carbs"], it["fat"], it.get("estimated", False))
    return {"items": items, "unknown": unknown}


def week_summary(days: int = 7, end: str | None = None) -> list[dict]:
    """Per-day totals for a date strip, oldest first (the last entry is `end`, default today)."""
    end_d = date.fromisoformat(end) if end else date.today()
    plan = db.get_plan() or {}
    target = (plan.get("nutrition") or {}).get("calories")
    out = []
    for i in range(days - 1, -1, -1):
        d = (end_d - timedelta(days=i)).isoformat()
        meals = db.get_meals(d)
        out.append({"day": d, "kcal": round(sum((m["kcal"] or 0) for m in meals)), "protein": round(sum((m["protein"] or 0) for m in meals)),
                    "meals": len(meals), "target_kcal": target})
    return out


def repeat_meal(meal_id: int, day: str | None = None) -> dict | None:
    """Log the same food and portion again (exact copy, including estimated foods)."""
    m = db.get_meal(meal_id)
    if not m:
        return None
    db.add_meal(day or date.today().isoformat(), m["name"], m["grams"], m["kcal"], m["protein"], m["carbs"], m["fat"], bool(m["estimated"]))
    return m


def today_summary(day: str | None = None) -> dict:
    day = day or date.today().isoformat()
    plan = db.get_plan() or {}
    target = plan.get("nutrition")
    meals = db.get_meals(day)
    eaten = {k: round(sum((m[k] or 0) for m in meals), 1) for k in ("kcal", "protein", "carbs", "fat")}
    out = {"day": day, "meals": meals, "eaten": eaten, "target": None, "remaining": None, "suggestions": []}
    if target:
        out["target"] = {"kcal": target["calories"], "protein": target["protein_g"], "carbs": target["carbs_g"], "fat": target["fat_g"]}
        out["remaining"] = {k: round(out["target"][k] - eaten[k], 1) for k in eaten}
        out["suggestions"] = suggest_for_protein(out["remaining"]["protein"], out["remaining"]["kcal"])
    return out
