"""A simulated lifter with HIDDEN true strength and fatigue. The agents never see these; they only see
what a real user would log (weights, reps, RPE, check-ins). This is a test dummy, not a trained model."""
from __future__ import annotations

import random
from dataclasses import dataclass, field

LIFTS = {"squat": 120.0, "bench press": 90.0, "deadlift": 150.0}  # starting true e1RM (kg)
BASELINE_FATIGUE = 0.06  # steady-state fatigue at volume 1.0; only EXCESS fatigue hurts
SESSION_DAYS = (0, 1, 3, 4)  # Mon Tue Thu Fri


@dataclass
class Scenario:
    name: str
    base_gain: float = 0.005            # fraction of e1RM gained per session at volume 1.0 (fresh)
    rpe_noise: float = 0.4
    stress: dict = field(default_factory=dict)       # week -> extra fatigue per session (life stress, bad sleep)
    plateau_from_week: int | None = None  # gains stop unless volume is raised above 1.0
    pain_at: tuple | None = None        # (week, session_index) where the lifter reports sharp pain
    weeks: int = 12


SCENARIOS = [
    Scenario("steady progress"),
    Scenario("noisy but progressing", base_gain=0.003, rpe_noise=0.9),
    Scenario("plateau at week 5", plateau_from_week=5),
    Scenario("overreaching weeks 6-8", stress={6: 0.014, 7: 0.014, 8: 0.014}),
    Scenario("pain reported week 4", pain_at=(4, 1)),
]


class Athlete:
    def __init__(self, sc: Scenario, seed: int = 1):
        self.sc = sc
        self.rng = random.Random(seed)
        self.e1rm = dict(LIFTS)
        self.fatigue = BASELINE_FATIGUE

    @property
    def excess(self) -> float:
        return max(0.0, self.fatigue - BASELINE_FATIGUE)

    def train(self, week: int, volume: float) -> list[dict]:
        """One session. Returns the sets a real user would log. Updates hidden state."""
        sc, rng = self.sc, self.rng
        self.fatigue = self.fatigue * 0.8 + 0.012 * volume + sc.stress.get(week, 0.0)
        sets = []
        for lift, true in self.e1rm.items():
            cap = true * (1 - self.excess)          # what the body can do today
            w = round(0.75 * true / 2.5) * 2.5       # the lifter loads from their planned (unfatigued) level
            for k in range(3):
                capk = cap * (1 - 0.02 * k)
                rir = 30 * (capk / w - 1) - 8        # reps in reserve after 8 reps (Epley, inverted)
                reps = 8 if rir >= 0 else max(3, int(8 + rir))
                rpe = 10.0 if rir < 0 else max(5.0, min(10.0, 10 - rir))
                rpe = round(max(5.0, min(10.0, rpe + rng.gauss(0, sc.rpe_noise))) * 2) / 2
                sets.append({"exercise": lift, "weight": w, "reps": reps, "rpe": rpe})
        self._adapt(week, volume)
        return sets

    def _adapt(self, week: int, volume: float) -> None:
        sc = self.sc
        gain = sc.base_gain * max(0.4, 1 - week / 40)
        if sc.plateau_from_week is not None and week >= sc.plateau_from_week:
            gain *= min(1.0, max(0.0, (volume - 1.0) * 5))   # only more volume restarts progress
        else:
            gain *= min(1.3, volume ** 0.6)
        gain *= max(0.0, 1 - 6 * self.excess)               # fatigue blunts adaptation
        for lift in self.e1rm:
            self.e1rm[lift] *= 1 + gain * (1 + self.rng.gauss(0, 0.1))

    def checkin(self) -> dict:
        e = self.excess
        r = self.rng
        return {
            "sleep_h": round(max(4.5, min(9.0, 7.6 - 18 * e + r.gauss(0, 0.5))), 1),
            "soreness": int(max(1, min(5, round(2 + 40 * e + r.gauss(0, 0.5))))),
            "energy": int(max(1, min(5, round(4 - 30 * e + r.gauss(0, 0.5))))),
        }

    def total_e1rm(self) -> float:
        return round(sum(self.e1rm.values()), 1)
