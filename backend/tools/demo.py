"""Synthetic history so the app can be explored before real data exists."""
from __future__ import annotations

import random
from datetime import date, timedelta

from backend.memory import db

PROFILE = {"age": 28, "sex": "m", "height_cm": 178, "weight_kg": 82, "goal": "maintain",
           "days_per_week": 4, "activity": "moderate", "injuries": [], "avoid": []}

# exercise -> (start e1RM-ish working weight, kg gained per session, reps)
LIFTS = {
    "bench press": (70, 0.9, 8),
    "squat": (90, 1.2, 6),
    "deadlift": (110, 1.4, 5),
    "overhead press": (42.5, 0.0, 8),  # deliberate plateau, so the Analyst has something to flag
}


def seed(weeks: int = 8) -> dict:
    from backend.agents import orchestrator as orch

    if not db.get_plan():
        orch.onboard(dict(PROFILE))
    rng = random.Random(42)
    start = date.today() - timedelta(days=weeks * 7)
    n = 0
    d = start
    sess = 0
    while d < date.today():
        if d.weekday() in (0, 1, 3, 4):  # Mon, Tue, Thu, Fri
            sess += 1
            for name, (w0, gain, reps) in LIFTS.items():
                w = round((w0 + gain * sess) / 2.5) * 2.5
                for k in range(3):
                    rpe = min(10.0, round(7 + k * 0.5 + rng.uniform(-0.4, 0.4), 1))
                    db.add_set(d.isoformat(), name, w, reps - (1 if k == 2 else 0), rpe)
                    n += 1
            db.add_checkin(sleep_h=round(rng.uniform(6.2, 8.4), 1), soreness=rng.randint(1, 4),
                           energy=rng.randint(2, 5), bodyweight=round(82 + rng.uniform(-0.4, 0.4) - sess * 0.02, 1))
        d += timedelta(days=1)
    return {"sessions": sess, "sets": n}
