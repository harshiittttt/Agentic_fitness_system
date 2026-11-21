"""Portion editing: look up a food's natural unit, resize a logged meal in place, log an exact-gram item."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.memory import db
from backend.tools.foods import FOODS, macros

router = APIRouter()


def _find_food(name: str | None) -> tuple[str, dict] | None:
    key = (name or "").strip().lower()
    if not key:
        return None
    if key in FOODS:
        return key, FOODS[key]
    for n, f in FOODS.items():
        if key in (a.lower() for a in f["aliases"]):
            return n, f
    return None


def _valid_day(day: str | None) -> str | None:
    if day is None:
        return None
    try:
        return date.fromisoformat(day).isoformat()
    except ValueError:
        raise HTTPException(400, "day must be YYYY-MM-DD")


def _check_grams(g) -> float:
    if g is None or isinstance(g, bool) or not isinstance(g, (int, float)) or g != g:
        raise HTTPException(422, "grams must be a number")
    if not 1 <= g <= 5000:
        raise HTTPException(422, "grams must be between 1 and 5000")
    return float(g)


# Foods you COUNT (4 eggs, 3 rotis) vs foods you MEASURE (350 g rice). Decided by the food's natural unit.
_COUNT_UNITS = {"egg", "white", "roti", "paratha", "banana", "apple", "orange", "slice", "bar", "idli", "dosa",
                "burger", "potato", "avocado", "breast", "fillet", "scoop"}
_LIQUIDS = {"milk", "buttermilk", "lassi"}


def portion_mode(name: str, unit: str | None) -> str:
    return "count" if (unit or "") in _COUNT_UNITS else "weight"


@router.get("/foods/portion")
def food_portion(name: str):
    hit = _find_food(name)
    if not hit:
        return {"name": name, "known": False, "unit": None, "unit_g": None, "per100": None, "mode": "weight", "measure": "g"}
    n, f = hit
    return {"name": n, "known": True, "unit": f["unit"], "unit_g": f["unit_g"],
            "mode": portion_mode(n, f["unit"]), "measure": "ml" if n in _LIQUIDS else "g",
            "per100": {"kcal": f["kcal"], "protein": f["p"], "carbs": f["c"], "fat": f["f"]}}


class PortionEdit(BaseModel):
    grams: float | None = None
    units: float | None = None


@router.patch("/meal/{meal_id}/portion")
def meal_portion_edit(meal_id: int, body: PortionEdit):
    row = db.get_meal(meal_id)
    if not row:
        raise HTTPException(404, "No such meal")
    hit = _find_food(row["name"])
    if body.grams is None and body.units is not None:
        if not hit:
            raise HTTPException(400, "units not supported for this food; send grams")
        grams = _check_grams(body.units * hit[1]["unit_g"])
    elif body.grams is not None:
        grams = _check_grams(body.grams)
    else:
        raise HTTPException(422, "send grams or units")

    if hit and not row["estimated"]:
        m = macros(hit[1], grams)
    else:
        old = row["grams"]
        if not old or old <= 0:
            raise HTTPException(400, "this meal has no recorded grams, so it cannot be resized")
        k = grams / old
        m = {"kcal": round((row["kcal"] or 0) * k), "protein": round((row["protein"] or 0) * k, 1),
             "carbs": round((row["carbs"] or 0) * k, 1), "fat": round((row["fat"] or 0) * k, 1)}
    with db.conn() as c:
        c.execute("UPDATE meals SET grams=?, kcal=?, protein=?, carbs=?, fat=? WHERE id=?",
                  (round(grams, 1), m["kcal"], m["protein"], m["carbs"], m["fat"], meal_id))
    return db.get_meal(meal_id)


class PortionLog(BaseModel):
    name: str
    grams: float | None = None
    day: str | None = None


@router.post("/meal/portion")
def meal_portion_log(body: PortionLog):
    day = _valid_day(body.day) or date.today().isoformat()
    hit = _find_food(body.name)
    if not hit:
        raise HTTPException(404, "unknown food")
    grams = _check_grams(body.grams)
    n, f = hit
    m = macros(f, grams)
    mid = db.add_meal(day, n, round(grams), m["kcal"], m["protein"], m["carbs"], m["fat"], False)
    return {"id": mid, "item": db.get_meal(mid)}
