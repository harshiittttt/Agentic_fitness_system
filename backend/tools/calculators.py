"""Deterministic fitness math. The LLM never invents numbers; it calls these."""
from __future__ import annotations

from typing import Sequence


def epley_e1rm(weight: float, reps: int) -> float:
    """Estimated one-rep max (Epley). Reps beyond ~12 are unreliable but still returned."""
    if reps <= 0 or weight <= 0:
        return 0.0
    if reps == 1:
        return float(weight)
    return round(weight * (1 + reps / 30), 1)


def rpe_adjusted_e1rm(weight: float, reps: int, rpe: float | None) -> float:
    """Count reps-in-reserve as extra reps so easy sets don't understate strength."""
    rir = max(0.0, 10.0 - rpe) if rpe is not None else 0.0
    return epley_e1rm(weight, int(round(reps + rir)))


def round_to_plate(weight: float, increment: float = 2.5) -> float:
    return round(round(weight / increment) * increment, 2)


def next_set_suggestion(
    weight: float,
    reps: int,
    rpe: float | None,
    target_reps: int,
    target_rpe: float = 8.0,
    increment: float = 2.5,
) -> dict:
    """Autoregulate the NEXT SET only. Small, continuous nudges; no program rewrite."""
    if rpe is None:
        return {"weight": weight, "reps": target_reps, "reason": "No RPE given; keep load."}
    rpe_gap = rpe - target_rpe  # >0 means harder than planned
    rep_gap = reps - target_reps  # >0 means beat the target
    score = rpe_gap - 0.5 * rep_gap
    if score >= 1.5:
        new = weight * 0.95
        reason = "Harder than planned; drop ~5%."
    elif score >= 0.5:
        new = weight * 0.975
        reason = "Slightly hard; trim load a touch."
    elif score <= -1.5:
        new = weight * 1.05
        reason = "Easier than planned; add ~5%."
    elif score <= -0.5:
        new = weight * 1.025
        reason = "Slightly easy; small bump."
    else:
        new = weight
        reason = "Right on target; keep load."
    return {"weight": round_to_plate(new, increment), "reps": target_reps, "reason": reason}


def mifflin_st_jeor(weight_kg: float, height_cm: float, age: int, sex: str) -> float:
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return round(base + (5 if sex.lower().startswith("m") else -161), 0)


ACTIVITY = {"sedentary": 1.2, "light": 1.375, "moderate": 1.55, "high": 1.725}


def tdee(bmr: float, activity: str = "moderate") -> float:
    return round(bmr * ACTIVITY.get(activity, 1.55), 0)


def macro_targets(calories: float, weight_kg: float, goal: str = "maintain") -> dict:
    """Protein by bodyweight, fat floor, carbs fill the rest."""
    protein = weight_kg * (2.2 if goal == "cut" else 1.8)
    fat = max(weight_kg * 0.8, calories * 0.22 / 9)
    carbs = max(0.0, (calories - protein * 4 - fat * 9) / 4)
    return {"calories": round(calories), "protein_g": round(protein), "fat_g": round(fat), "carbs_g": round(carbs)}


def goal_calories(tdee_kcal: float, goal: str) -> float:
    return {"cut": tdee_kcal * 0.85, "bulk": tdee_kcal * 1.10}.get(goal, tdee_kcal)


def ewma(values: Sequence[float], alpha: float = 0.3) -> list[float]:
    out: list[float] = []
    for v in values:
        out.append(v if not out else alpha * v + (1 - alpha) * out[-1])
    return out


def linear_slope(values: Sequence[float]) -> float:
    """Least-squares slope per observation. 0 if fewer than 2 points."""
    n = len(values)
    if n < 2:
        return 0.0
    xm = (n - 1) / 2
    ym = sum(values) / n
    den = sum((i - xm) ** 2 for i in range(n))
    return sum((i - xm) * (v - ym) for i, v in enumerate(values)) / den
