"""Continuous, evidence-weighted adaptation. No fixed schedule, no hard caps.

Idea: every signal carries a strength (how far from baseline, -1..1) and a
confidence (how much data / how persistent, 0..1). The size of a recommended
change is strength x confidence, dampened if lots of change already happened
recently. Safety flags bypass all of that.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from backend.tools.calculators import ewma, linear_slope

# Ordered (first match is reported). Stems on purpose: "hurt" catches hurts/hurting, "tingl" tingling.
# Deliberately NOT included: "sore" (normal muscle soreness) and "pulled" (lifters say "pulled 180 for 3").
SAFETY_FLAGS = ("pain", "injury", "illness", "dizzy", "chest_pain", "hurt", "tweak", "strain", "numb", "tingl",
                "popped", "sharp", "pinch",
                # Hindi / Haryanvi: pain, injury, dizzy, sprain, strain, swelling, numb, "hurts"
                "दर्द", "चोट", "चक्कर", "मोच", "खिंचाव", "सूजन", "सुन्न", "दुखै", "दुखता", "दुखने")


@dataclass
class Signal:
    name: str
    strength: float  # -1 (strongly negative) .. +1 (strongly positive)
    confidence: float  # 0..1
    note: str = ""


def confidence_from_data(n_points: int, consistency: float = 1.0, full_at: int = 8) -> float:
    """More observations and more consistent direction -> more confidence."""
    return max(0.0, min(1.0, (n_points / full_at) * max(0.0, min(1.0, consistency))))


def trend_signal(name: str, values: Sequence[float], scale: float, alpha: float = 0.4) -> Signal:
    """Turn a series (e.g. weekly e1RM, RPE per session) into a Signal.

    scale = slope per observation considered 'strong' (strength saturates at +/-1).
    Smoothing (EWMA) means one noisy point barely moves it.
    """
    if len(values) < 3:
        return Signal(name, 0.0, 0.0, "not enough data")
    smooth = ewma(values, alpha)
    slope = linear_slope(smooth)
    strength = max(-1.0, min(1.0, slope / scale)) if scale else 0.0
    # consistency: fraction of steps moving the same way as the overall slope
    diffs = [b - a for a, b in zip(smooth, smooth[1:])]
    same = sum(1 for d in diffs if d * slope > 0)
    consistency = same / len(diffs) if diffs else 0.0
    if abs(strength) < 0.15:
        # A flat series has no direction to be 'consistent' about; confidence in
        # "it's flat" comes from how much data we have (needs ~5 points for 0.5).
        return Signal(name, strength, confidence_from_data(len(values), 1.0, full_at=10), f"flat, slope={slope:.3f}")
    return Signal(name, strength, confidence_from_data(len(values), consistency), f"slope={slope:.3f}")


def change_dampening(recent_changes: int, window_budget: int = 3) -> float:
    """1.0 when nothing changed lately; shrinks toward 0.3 as recent changes pile up.

    Soft: it dampens, it never forbids. Strong evidence still gets through.
    """
    return max(0.3, 1.0 - 0.7 * min(1.0, recent_changes / window_budget))


def recommended_change(signal: Signal, max_fraction: float, recent_changes: int = 0) -> float:
    """Fractional change to apply (e.g. +0.05 = +5%). max_fraction = ceiling for a 'full' signal."""
    magnitude = signal.strength * signal.confidence * change_dampening(recent_changes)
    # dead zone: ignore tiny nudges so the plan doesn't jitter
    if abs(magnitude) < 0.08:
        return 0.0
    return round(magnitude * max_fraction, 4)


import re as _re

# English flags match WHOLE words (with their usual endings): "number" is not "numb", "Spain" is not "pain", "sharpen" is not "sharp".
# The Hindi / Haryanvi flags are matched as substrings (Devanagari words carry their endings attached).
_EN_FLAG_RE = {
    "pain": r"pain(?:s|ful)?", "injury": r"injur(?:y|ies|ed)", "illness": r"illness", "dizzy": r"dizz(?:y|iness)", "chest_pain": r"chest pain",
    "hurt": r"hurt(?:s|ing)?", "tweak": r"tweak(?:ed|s|ing)?", "strain": r"strain(?:ed|s|ing)?", "numb": r"numb(?:ness)?", "tingl": r"tingl(?:e|es|ed|ing|y)",
    "popped": r"popped", "sharp": r"sharp", "pinch": r"pinch(?:ed|es|ing)?",
}
_EN_FLAGS = {f: _re.compile(r"(?<![a-z])" + p + r"(?![a-z])") for f, p in _EN_FLAG_RE.items()}


def has_safety_flag(text: str) -> str | None:
    t = text.lower()
    for flag in SAFETY_FLAGS:
        rx = _EN_FLAGS.get(flag)
        if rx is not None:
            if rx.search(t):
                return flag
        elif flag.replace("_", " ") in t:
            return flag
    return None


def readiness_score(sleep_h: float, soreness: int, energy: int, baseline_sleep: float = 7.5) -> dict:
    """Transparent rule-based readiness 0-100 (soreness/energy 1-5). Not a learned model."""
    sleep_part = max(0.0, min(1.0, sleep_h / baseline_sleep)) * 40
    soreness_part = (5 - soreness) / 4 * 25
    energy_part = (energy - 1) / 4 * 35
    score = round(sleep_part + soreness_part + energy_part)
    label = "low" if score < 50 else "ok" if score < 75 else "high"
    return {"score": score, "label": label}
