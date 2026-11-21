"""Benchmarks of the Insight Engine against synthetic athletes with known truth. Run once by hand (a few minutes):

    set PYTHONIOENCODING=utf-8
    python -m fitness_os.mem.bench_insight                      # everything
    python -m fitness_os.mem.bench_insight --null 30 --planted 20 --weeks 4 8 12 16
    python -m fitness_os.mem.bench_insight --only null|planted|forecast|frames

(a) NULL athletes (no planted effects, same autocorrelated mess): the share of hypotheses CONFIRMED per run must stay < 10%, reported per family.
(b) PLANTED athletes: a planted effect counts as RECOVERED when it is confirmed with the right sign and its estimate is inside the CI or within
    +-35% of the truth; power is reported per family and per data length (4, 8, 12, 16 weeks).
(c) FORECASTS beat the naive last-value baseline (skill > 0) for e1rm and readiness, and the 80% interval covers within 0.15 of nominal.
(d) FRAMES are deterministic and fast.
All databases are scratch files; nothing touches the real one.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from collections import defaultdict

import numpy as np

END = "2026-03-10"
os.environ.setdefault("FITNESS_NOW", f"{END}T12:00:00+00:00")
os.environ["FITNESS_NOW"] = f"{END}T12:00:00+00:00"

from backend.memory import db            # noqa: E402

from fitness_os.mem import frames, miner, synth      # noqa: E402

FAMILY_KEY = {"sleep_perf": "sleep_performance:sleep_h>perf:lag0", "soreness_perf": "soreness_performance:soreness>perf:lag0",
              "energy_perf": "energy_performance:energy>perf:lag0", "rest_gap": "rest_gap:days_since_session>perf:lag0",
              "bodyweight_strength": "bodyweight_strength:bodyweight_7d>perf:lag0", "volume": "volume_response:weekly_sets>next_week_progress:sweet_spot",
              "frequency": "frequency_response:sessions_per_week>next_week_progress:linear", "weekday_adherence": "weekday_adherence:trained>weekday:lag0",
              "weekday_perf": "weekday_performance:perf>weekday:lag0", "rpe_bias": "rpe_bias:rpe>implied_e1rm:within_session",
              "protein_soreness": "protein_recovery:protein_g>soreness:lag1", "maintenance": "maintenance_calories:intake_vs_weight>tdee:window",
              "late_meal_sleep": "late_meals_sleep:late_meal>sleep_h:lag0", "deload": "deload_response:deload_week>perf_next_week:lag1w"}
UNPLANTED = ("sleep_performance:sleep_h>perf:lag1", "weekday_adherence:logged>weekday:lag0")


def _scratch() -> None:
    db.DB_PATH = os.path.join(tempfile.mkdtemp(), "bench.db")


def _near(est: float, truth: float, ci) -> bool:
    lo, hi = ci
    inside = np.isfinite(lo) and np.isfinite(hi) and lo <= truth <= hi
    return bool(inside or abs(est - truth) <= 0.35 * abs(truth))


def _truth_value(name: str, t: dict, r: dict) -> float | None:
    if name in ("sleep_perf", "soreness_perf", "energy_perf", "rest_gap", "bodyweight_strength"):
        return t.get("slope_pct_per_unit")
    if name == "volume":
        return t.get("best_in_range")
    if name == "frequency":
        return t["pct_week_per_session"]
    if name == "weekday_adherence":
        return t["weekday"]
    if name == "weekday_perf":
        return t["pct"]
    if name == "rpe_bias":
        return t.get("slope_pct_per_rir")
    if name == "protein_soreness":
        return t["points_per_25g"]
    if name == "maintenance":
        return t["offset_kcal"]
    if name == "late_meal_sleep":
        return t["hours"]
    if name == "deload":
        return t["rebound_pct"]
    return None


def _judge(name: str, truth: dict, r: dict | None) -> tuple[bool, bool]:
    """(confirmed with the right sign, recovered: also right magnitude)."""
    if r is None or not r["passes"]:
        return False, False
    tv = _truth_value(name, truth, r)
    if tv is None:
        return False, False
    if name == "weekday_adherence":
        ok = r["weekday"] == int(tv)
        return ok, ok
    if name == "volume":
        # a sweet spot must be inside the observed range and near the true best
        rng = truth.get("range") or [0, 0]
        ok = rng[0] <= r["effect"] <= rng[1]
        return ok, bool(ok and _near(r["effect"], tv, r["ci"]))
    if name == "weekday_perf":
        sign_ok = r["weekday"] == truth["weekday"] and np.sign(r["effect"]) == np.sign(tv)
        return bool(sign_ok), bool(sign_ok and _near(r["effect"], tv, r["ci"]))
    if name == "maintenance":
        return bool(np.sign(r["effect"]) == np.sign(tv)), bool(np.sign(r["effect"]) == np.sign(tv) and _near(r["effect"], tv, r["ci"]))
    sign_ok = np.sign(r["effect"]) == np.sign(tv)
    return bool(sign_ok), bool(sign_ok and _near(r["effect"], tv, r["ci"]))


def bench_null(n: int = 30, weeks: tuple[int, ...] = (12,), n_perm: int = 999) -> dict:
    _scratch()
    out = {}
    for wk in weeks:
        fam_conf, fam_test, share, any_conf, tested_tot = defaultdict(int), defaultdict(int), [], 0, 0
        for seed in range(1000, 1000 + n):
            synth.make_athlete(seed, wk * 7, None, end=END)
            res = miner.analyze(n_perm=n_perm, n_boot=200)
            c = [r for r in res if r["passes"]]
            share.append(len(c) / max(1, len(res)))
            any_conf += bool(c)
            for r in res:
                fam_test[r["family"]] += 1
                fam_conf[r["family"]] += bool(r["passes"])
            tested_tot += len(res)
        out[wk] = {"athletes": n, "mean_share_confirmed": float(np.mean(share)), "runs_with_any_confirmed": any_conf / n,
                   "per_family": {f: {"tested": fam_test[f], "confirmed": fam_conf[f], "rate": fam_conf[f] / max(1, fam_test[f])} for f in sorted(fam_test)}}
    return out


def bench_planted(n: int = 20, weeks: tuple[int, ...] = (4, 8, 12, 16), n_perm: int = 999, strength: float = 1.0) -> dict:
    _scratch()
    out = {}
    for wk in weeks:
        conf, rec, tested = defaultdict(int), defaultdict(int), defaultdict(int)
        extra_conf = 0
        for seed in range(2000, 2000 + n):
            truth = synth.make_athlete(seed, wk * 7, synth.default_effects(strength), end=END)
            res = {r["key"]: r for r in miner.analyze(n_perm=n_perm, n_boot=200)}
            for name, key in FAMILY_KEY.items():
                r = res.get(key)
                tested[name] += r is not None
                c, g = _judge(name, truth["effects"][name], r)
                conf[name] += c
                rec[name] += g
            extra_conf += sum(bool(res[k]["passes"]) for k in UNPLANTED if k in res)
        out[wk] = {"athletes": n, "confirmed_right_sign": {k: conf[k] / n for k in FAMILY_KEY}, "recovered": {k: rec[k] / n for k in FAMILY_KEY},
                   "tested_share": {k: tested[k] / n for k in FAMILY_KEY}, "unplanted_confirmed_per_run": extra_conf / n}
    return out


def bench_frames(days: int = 120) -> dict:
    _scratch()
    synth.make_athlete(5, days, synth.default_effects(), end=END)
    frames.build(days)
    t = time.perf_counter()
    reps = 10
    for _ in range(reps):
        a = frames.build(days)
    ms = (time.perf_counter() - t) * 1000 / reps
    b = frames.build(days)
    return {"deterministic": frames.data_hash(a) == frames.data_hash(b), "ms_per_build": round(ms, 1), "under_200ms": ms < 200, "days": len(a)}


def bench_forecast(n: int = 6, weeks: int = 12) -> dict:
    from fitness_os.mem import forecast
    return forecast.backtest(n_athletes=n, weeks=weeks)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--null", type=int, default=30)
    ap.add_argument("--planted", type=int, default=20)
    ap.add_argument("--weeks", type=int, nargs="+", default=[4, 8, 12, 16])
    ap.add_argument("--only", choices=["null", "planted", "forecast", "frames"], default=None)
    ap.add_argument("--forecast-athletes", type=int, default=8)
    a = ap.parse_args(argv)
    t0 = time.time()
    if a.only in (None, "frames"):
        print("\n=== (d) frames ===")
        print(bench_frames())
    if a.only in (None, "null"):
        print("\n=== (a) NULL athletes: false discoveries ===")
        res = bench_null(a.null, weeks=tuple(sorted({12, *a.weeks[-1:]})))
        for wk, d in res.items():
            print(f"{wk} weeks: mean share confirmed {d['mean_share_confirmed']:.3%}, runs with any confirmed {d['runs_with_any_confirmed']:.1%} ({d['athletes']} athletes)")
            for f, v in d["per_family"].items():
                print(f"   {f:24s} tested {v['tested']:3d} confirmed {v['confirmed']:2d} rate {v['rate']:.1%}")
    if a.only in (None, "planted"):
        print("\n=== (b) PLANTED athletes: recovery ===")
        res = bench_planted(a.planted, weeks=tuple(a.weeks))
        names = list(FAMILY_KEY)
        print("recovered = confirmed, right sign, magnitude inside CI or +-35% of truth  (confirmed-with-right-sign in brackets)")
        print(f"{'family':22s}" + "".join(f"{w:>14d}w" for w in res))
        for k in names:
            print(f"{k:22s}" + "".join(f"{res[w]['recovered'][k]:>8.0%} ({res[w]['confirmed_right_sign'][k]:.0%}){'' if res[w]['tested_share'][k] > 0.9 else '*'}" for w in res))
        print("(* = the hypothesis could be tested in under 90% of athletes: not enough data)")
        print("unplanted confirmed per run: " + ", ".join(f"{w}w {res[w]['unplanted_confirmed_per_run']:.2f}" for w in res))
    if a.only in (None, "forecast"):
        print("\n=== (c) forecasts ===")
        print(bench_forecast(a.forecast_athletes))
    print(f"\n(total {time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
