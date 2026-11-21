"""12-week simulation harness. Runs the REAL agents against a simulated lifter in a throwaway database.

    python -m backend.sim.run            # full report
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

from backend.memory import db
from backend.sim.athlete import SCENARIOS, SESSION_DAYS, Athlete, Scenario
from backend.tools.demo import PROFILE

START = datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc)  # a Monday


def run(sc: Scenario, cadence: str = "weekly", seed: int = 1) -> dict:
    """cadence: 'none' (control: plan never adapts), 'weekly' (review once a week), 'session' (after every session)."""
    from backend.agents import orchestrator as orch

    tmp = tempfile.mkdtemp()
    real_path, real_now = db.DB_PATH, db.now
    clock = {"t": START}
    db.DB_PATH = os.path.join(tmp, "sim.db")
    db.now = lambda: clock["t"].isoformat(timespec="seconds")
    try:
        orch.onboard(dict(PROFILE))
        a = Athlete(sc, seed)
        vol_trace, change_weeks, vetoes = [], [], 0
        for week in range(1, sc.weeks + 1):
            for si, dow in enumerate(SESSION_DAYS):
                clock["t"] = START + timedelta(weeks=week - 1, days=dow)
                vol = db.get_plan()["volume_mult"]
                day = clock["t"].date().isoformat()
                for s in a.train(week, vol):
                    db.add_set(day, s["exercise"], s["weight"], s["reps"], s["rpe"])
                db.add_checkin(**a.checkin())
                if sc.pain_at == (week, si):
                    orch.log_and_coach(day, "bench 60 for 8, sharp pain in my shoulder")
                do_review = cadence == "session" or (cadence == "weekly" and si == len(SESSION_DAYS) - 1)
                if do_review:
                    r = orch.review()
                    if r.get("changed"):
                        change_weeks.append(week)
                    vetoes += "Recovery veto" in r.get("verdict", "")
            vol_trace.append(db.get_plan()["volume_mult"])
        diffs = [round(b - a_, 3) for a_, b in zip([1.0] + vol_trace, vol_trace) if round(b - a_, 3) != 0]
        reversals = sum(1 for x, y in zip(diffs, diffs[1:]) if x * y < 0)
        return {"scenario": sc.name, "cadence": cadence, "final_e1rm": a.total_e1rm(),
                "start_e1rm": sum(Athlete(sc).e1rm.values()), "vol_trace": vol_trace,
                "n_changes": len(change_weeks), "change_weeks": change_weeks, "reversals": reversals, "vetoes": vetoes}
    finally:
        db.DB_PATH, db.now = real_path, real_now


def report(seeds: tuple = (1, 2, 3)) -> list[dict]:
    rows = []
    for sc in SCENARIOS:
        for cad in ("none", "weekly", "session"):
            rs = [run(sc, cad, s) for s in seeds]
            rows.append({**rs[0], "final_e1rm": round(sum(r["final_e1rm"] for r in rs) / len(rs), 1),
                         "n_changes": round(sum(r["n_changes"] for r in rs) / len(rs), 1),
                         "reversals": round(sum(r["reversals"] for r in rs) / len(rs), 1),
                         "min_vol": min(min(r["vol_trace"]) for r in rs), "max_vol": max(max(r["vol_trace"]) for r in rs)})
    return rows


def main() -> None:
    rows = report()
    print(f"{'scenario':34}{'cadence':9}{'final e1RM':>11}{'vs ctrl':>9}{'changes':>9}{'reversals':>10}{'vol range':>13}")
    ctrl = {}
    for r in rows:
        if r["cadence"] == "none":
            ctrl[r["scenario"]] = r["final_e1rm"]
        delta = f"{(r['final_e1rm'] / ctrl[r['scenario']] - 1) * 100:+.1f}%" if r["cadence"] != "none" else "-"
        print(f"{r['scenario']:34}{r['cadence']:9}{r['final_e1rm']:>11}{delta:>9}{r['n_changes']:>9}{r['reversals']:>10}"
              f"{r['min_vol']:>7.2f}-{r['max_vol']:.2f}")
        if r["cadence"] == "weekly":
            print(f"{'':34}volume by week: {' '.join(f'{v:.2f}' for v in r['vol_trace'])}  (seed 1)")


if __name__ == "__main__":
    main()
