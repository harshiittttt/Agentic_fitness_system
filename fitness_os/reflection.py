"""Reflection closes the learning loop (docs section 05): after a horizon passes, compare what a change was meant to do with
what happened, store the verdict on the decision, and write a lesson into procedural memory. Over months the system becomes
specific to this athlete. Scoring is deterministic and honest about thin data; the LLM is not involved.
"""
from __future__ import annotations

from datetime import timedelta
from statistics import mean

from backend.memory import db
from backend.policy.adaptation import readiness_score
from backend.tools import calculators as calc

from . import clock, ledger, memory

HORIZON_DAYS = {"volume_change": 7, "calorie_change": 14}
MAX_WAIT_FACTOR = 3          # give thin data up to 3x the horizon, then record "insufficient_data" and stop waiting
BEFORE_DAYS = 14
MIN_READINESS_POINTS = 3
MIN_WEIGHT_POINTS, MIN_WEIGHT_SPAN_DAYS = 3, 6


def _t(ts: str):
    return clock.parse(ts)


def _readiness(checks: list[dict]) -> list[float]:
    return [readiness_score(c["sleep_h"] or 7.5, c["soreness"] or 3, c["energy"] or 3)["score"] for c in checks]


def _window(rows: list[dict], start, end) -> list[dict]:
    return [r for r in rows if start <= _t(r["ts"]) < end]


def _best_e1rm(sets: list[dict]) -> dict[str, float]:
    best: dict[str, float] = {}
    for s in sets:
        if s["weight"] and s["reps"]:
            e = calc.rpe_adjusted_e1rm(float(s["weight"]), int(s["reps"]), s.get("rpe"))
            best[s["exercise"]] = max(best.get(s["exercise"], 0.0), e)
    return best


def _strength_delta(before: list[dict], after: list[dict]) -> float | None:
    b, a = _best_e1rm(before), _best_e1rm(after)
    ratios = [a[x] / b[x] - 1 for x in b if x in a and b[x] > 0]
    return mean(ratios) if ratios else None


def _slope_kg_per_week(checks: list[dict]) -> float | None:
    w = sorted([(_t(c["ts"]), c["bodyweight"]) for c in checks if c.get("bodyweight")], key=lambda x: x[0])
    if len(w) < MIN_WEIGHT_POINTS or (w[-1][0] - w[0][0]).days < MIN_WEIGHT_SPAN_DAYS:
        return None
    days = [(t - w[0][0]).total_seconds() / 86400 for t, _ in w]
    return _ols(days, [v for _, v in w]) * 7


def _ols(x: list[float], y: list[float]) -> float:
    mx, my = mean(x), mean(y)
    den = sum((a - mx) ** 2 for a in x)
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / den if den else 0.0


def score(d: dict) -> dict:
    """-> {verdict: helped|neutral|hurt|insufficient_data, metrics: {...}, lesson: str|None}. Never guesses on thin data."""
    p = d["proposal"]
    start = _t(d["ts"])
    horizon = HORIZON_DAYS[d["kind"]]
    frac = float((d.get("action") or {}).get("fraction", p["params"].get("fraction", 0)))
    checks = db.get_checkins(400)
    if d["kind"] == "volume_change":
        before_c = _window(checks, start - timedelta(days=BEFORE_DAYS), start)
        after_c = _window(checks, start, start + timedelta(days=horizon))
        sets = db.get_sets(None, 4000)
        sd = _strength_delta(_window(sets, start - timedelta(days=BEFORE_DAYS), start), _window(sets, start, start + timedelta(days=horizon)))
        if len(after_c) < MIN_READINESS_POINTS or len(before_c) < 1:
            return {"verdict": "insufficient_data", "metrics": {"checkins_after": len(after_c)}, "lesson": None}
        rd = mean(_readiness(after_c)) - mean(_readiness(before_c))
        m = {"readiness_change": round(rd, 1), "strength_change": None if sd is None else round(sd, 3)}
        if frac < 0:
            verdict = "hurt" if (sd is not None and sd <= -0.05) else "helped" if rd >= 4 and (sd is None or sd >= -0.03) else "neutral"
            word = f"cutting volume {abs(frac):.0%}"
        else:
            verdict = "hurt" if (rd <= -6 or (sd is not None and sd <= -0.03)) else "helped" if (sd is not None and sd >= 0.01 and rd >= -4) else "neutral"
            word = f"adding {frac:.0%} volume"
        lesson = {"helped": f"For this athlete, {word} worked: readiness {rd:+.0f}"
                            + (f", strength {sd:+.1%}" if sd is not None else "") + f" over {horizon} days.",
                  "hurt": f"For this athlete, {word} backfired: readiness {rd:+.0f}"
                          + (f", strength {sd:+.1%}" if sd is not None else "") + f" over {horizon} days. Be more cautious next time.",
                  "neutral": None}[verdict]
        return {"verdict": verdict, "metrics": m, "lesson": lesson}

    # calorie_change: did the weight trend move the way the goal wants?
    goal = (db.get_profile() or {}).get("goal", "maintain")
    after_c = _window(checks, start, start + timedelta(days=horizon))
    before_c = _window(checks, start - timedelta(days=BEFORE_DAYS), start)
    after, before = _slope_kg_per_week(after_c), _slope_kg_per_week(before_c)
    if after is None:
        return {"verdict": "insufficient_data", "metrics": {"weigh_ins_after": sum(1 for c in after_c if c.get("bodyweight"))}, "lesson": None}
    want = {"cut": -1.0, "bulk": 1.0}.get(goal, 0.0)
    moved = after - (before if before is not None else 0.0)
    m = {"weight_slope_kg_per_week": round(after, 2), "slope_before": None if before is None else round(before, 2), "goal": goal}
    if want == 0.0:
        verdict = "helped" if abs(after) <= 0.25 else "neutral" if abs(after) <= 0.5 else "hurt"
    else:
        toward = moved * want
        verdict = "helped" if toward >= 0.15 else "hurt" if toward <= -0.3 else "neutral"
    word = f"a {abs(frac):.0%} calorie {'cut' if frac < 0 else 'increase'}"
    lesson = {"helped": f"For this athlete, {word} moved weight the right way ({after:+.2f} kg/week toward a {goal} goal).",
              "hurt": f"For this athlete, {word} moved weight the wrong way ({after:+.2f} kg/week on a {goal} goal).",
              "neutral": None}[verdict]
    return {"verdict": verdict, "metrics": m, "lesson": lesson}


def due(limit: int = 300) -> list[dict]:
    out = []
    for d in ledger.recent(limit, "applied"):
        if d.get("trigger_event") != "experiment" and d["kind"] in HORIZON_DAYS and not (d.get("outcome") or {}).get("reflected") and clock.age_days(d["ts"]) >= HORIZON_DAYS[d["kind"]]:
            out.append(d)
    return out


def run(max_items: int = 20) -> list[dict]:
    """Reflect on every decision whose horizon has passed. Thin data waits (up to 3x the horizon), then is recorded as such."""
    done = []
    for d in due()[:max_items]:
        s = score(d)
        age = clock.age_days(d["ts"])
        if s["verdict"] == "insufficient_data" and age < HORIZON_DAYS[d["kind"]] * MAX_WAIT_FACTOR:
            continue  # not enough evidence yet: try again on a later run
        lesson_id = None
        if s["lesson"]:
            lesson_id = memory.add("procedural", s["lesson"], 0.75 if s["verdict"] == "hurt" else 0.6, source_decision=d["id"])["id"]
        ledger.update(d["id"], outcome={"reflected": True, "verdict": s["verdict"], "metrics": s["metrics"], "lesson_id": lesson_id,
                                        "reflected_at": clock.iso()})
        done.append({"decision": d["id"], "kind": d["kind"], "verdict": s["verdict"], "metrics": s["metrics"], "lesson": s["lesson"]})
    return done
