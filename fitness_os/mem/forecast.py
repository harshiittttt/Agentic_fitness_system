"""Forecasts that are logged, resolved and scored: the system says what it expects, then checks itself.

    forecast.predict("e1rm", "squat", horizon_days=3)   -> {value, lo, hi, horizon, model, basis[...], prediction_id}
    forecast.predict("readiness") / ("bodyweight", horizon_days=7) / ("adherence")
    forecast.resolve_due()                              # nightly (jobs.py): match due predictions against what actually happened
    forecast.calibration("e1rm")                        # {n, mae, naive_mae, skill, coverage, nominal}
    forecast.surprises(5)                               # the biggest standardised misses, with a plain explanation

Models are deliberately simple and robust (Theil-Sen local trends, AR(1) shrinkage, shrunk weekday rates); what makes them personal is the adjustment by
the athlete's own CONFIRMED discoveries (sleep, soreness, energy, rest days, measured maintenance calories). Intervals are the 80% range of the model's own
out-of-sample errors on this athlete's history, so they are as wide as this athlete is unpredictable. Skill is measured against the naive "same as last time".
Nothing here needs a network or a model; it never raises from a background path (`resolve_due`).
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import numpy as np

from backend.memory import db

from .. import clock
from . import frames, miner, stats, store

NOMINAL = 0.80
Z80 = 1.2816
E1RM_WIDEN = 1.25     # e1RM errors are heavier-tailed than the rolling sample shows; chosen on synthetic athletes so the 80% interval covers ~80%
TARGETS = ("e1rm", "readiness", "adherence", "bodyweight")
_DEFAULT_H = {"e1rm": 3, "readiness": 1, "bodyweight": 7, "adherence": 1}
_E1RM_WINDOW_DAYS = 42
_E1RM_POINTS = 10


def _today(as_of: str | None) -> date:
    return date.fromisoformat(as_of[:10]) if as_of else clock.now().date()


def _history(as_of: date, days: int = 120) -> list[dict]:
    return miner.trim_leading(frames.build(days, as_of.isoformat()))


def _series(rows: list[dict], key: str) -> tuple[np.ndarray, np.ndarray]:
    t = np.array([i for i, r in enumerate(rows) if r.get(key) is not None], float)
    y = np.array([rows[int(i)][key] for i in t], float)
    return t, y


# ====================================================== models: (history up to now) -> expected value at t_target ======================================================
def _model_e1rm(t: np.ndarray, y: np.ndarray, t_target: float) -> float | None:
    """Level from the median of the last 6 sessions, trend from a long (12-week) robust line shrunk toward flat: day-to-day e1RM is noisy, so a short
    trend extrapolates the noise (measured on synthetic athletes: it loses to 'same as last time'; this version beats it)."""
    if len(t) < 6:
        return None
    keep = t >= t[-1] - 84
    t2, y2 = t[keep], np.log(y[keep])
    b = 0.0
    if len(t2) >= 8:
        b, _ = stats.theil_sen(t2, y2)
        b = float(np.clip(0.7 * b, -0.0015, 0.0015))
    lv = float(np.median(np.log(y[-6:]) - b * t[-6:]))
    return float(math.exp(lv + b * t_target))


def _model_readiness(t: np.ndarray, y: np.ndarray, t_target: float) -> float | None:
    """Personal normal (median of the last 4 weeks) plus a shrunk carry-over of how today differs from it (AR(1), phi estimated and capped)."""
    if len(y) < 8:
        return None
    w = y[t >= t[-1] - 28]
    if len(w) < 8:
        w = y[-14:]
    mu = float(np.median(w))
    dev = y - mu
    phi = float(np.clip(stats.acf(dev[-60:], 1)[0], 0.0, 0.7)) if len(dev) >= 12 else 0.0
    h = max(1.0, t_target - t[-1])
    return float(np.clip(mu + (phi ** h) * (y[-1] - mu), 0, 100))


def _model_bodyweight(t: np.ndarray, y: np.ndarray, t_target: float) -> float | None:
    """Level = median of the last 5 weigh-ins (water noise is large), trend = a 4-week robust line shrunk by half."""
    if len(t) < 6:
        return None
    keep = t >= t[-1] - 28
    tt, yy = t[keep], y[keep]
    if len(tt) < 6 or tt[-1] - tt[0] < 10:
        return None
    b, _ = stats.theil_sen(tt, yy)
    b = float(np.clip(0.5 * b, -0.1, 0.1))                      # kg/day
    lv = float(np.median(y[-5:] - b * t[-5:]))
    return float(lv + b * t_target)


MODELS = {"e1rm": _model_e1rm, "readiness": _model_readiness, "bodyweight": _model_bodyweight}


def _interval(model, t: np.ndarray, y: np.ndarray, h_days: float, *, log_space: bool, min_errors: int = 6, max_errors: int = 24) -> tuple[float, int]:
    """Spread of the model's OWN out-of-sample errors on this athlete (rolling origin): relative errors for e1RM, absolute otherwise.
    Returns (sigma, n_errors). The 80% half-width is Z80 * sigma, inflated a little when there are few errors to learn from."""
    errs = []
    start = max(4, len(t) - max_errors - 1)
    for j in range(start, len(t)):
        # forecast point j from the points before it, as if standing at the time of point j-1 with the same horizon the real call will use
        tj = t[j]
        prior = t < tj - max(0.0, h_days - 1.0)
        if prior.sum() < 4:
            continue
        pred = model(t[prior], y[prior], float(tj))
        if pred is None or pred <= 0 and log_space:
            continue
        errs.append((y[j] - pred) / pred if log_space else y[j] - pred)
    if len(errs) < min_errors:
        return float("nan"), len(errs)
    e = np.asarray(errs)
    sig = float(np.quantile(np.abs(e), NOMINAL)) / Z80                           # empirical 80% half-width of this athlete's own errors (bias included), in 'sigma' units
    sig *= (1.0 + 0.8 / math.sqrt(len(e))) * (E1RM_WIDEN if log_space else 1.0)
    return float(sig), len(e)


# ====================================================== discovery adjustments ======================================================
def _confirmed(prefix: str) -> dict | None:
    for d in store.discoveries(status=("confirmed",), limit=100):
        if d["key"].startswith(prefix):
            return d
    return None


def _adjust_e1rm(rows: list[dict], t_target: float, today_idx: int, base: float, lift_key: str) -> tuple[float, list[str]]:
    """Add the confirmed partial effects the athlete's own data supports (each is % of e1RM per unit, relative to their usual). Only what is KNOWN at
    forecast time is used: the target day's morning check-in is known only for a same-day forecast; last night's sleep is known one day ahead."""
    basis, mult = [], 0.0
    horizon = int(round(t_target - today_idx))
    trained = [r for r in rows if r["trained"]]

    def usual(key: str) -> float | None:
        v = [r[key] for r in trained if r.get(key) is not None]
        return float(np.mean(v)) if len(v) >= 8 else None

    last = rows[today_idx]
    for prefix, label, key, known in (("sleep_performance:sleep_h>perf:lag0", "sleep", "sleep_h", horizon <= 0), ("sleep_performance:sleep_h>perf:lag1", "last night's sleep", "sleep_h", horizon == 1),
                                      ("soreness_performance:soreness>perf:lag0", "soreness", "soreness", horizon <= 0),
                                      ("energy_performance:energy>perf:lag0", "energy", "energy", horizon <= 0)):
        d = _confirmed(prefix)
        if not d or not known or last.get(key) is None:
            continue
        u = usual(key)
        est = (d.get("effect") or {}).get("estimate")
        if u is None or est is None:
            continue
        adj = float(est) / 100.0 * (last[key] - u)
        mult += adj
        basis.append(f"{label} {last[key]:g} vs your usual {u:.1f}: {adj * 100:+.1f}% (confirmed pattern)")
    d = _confirmed("rest_gap:days_since_session>perf:lag0")
    if d:
        idx = [i for i, r in enumerate(rows) if r["trained"]]
        if idx:
            gap_now = min(5.0, t_target - idx[-1])
            gaps = [min(5.0, b - a) for a, b in zip(idx, idx[1:])]
            est = (d.get("effect") or {}).get("estimate")
            if gaps and est is not None and len(gaps) >= 8:
                adj = float(est) / 100.0 * (gap_now - float(np.mean(gaps)))
                mult += adj
                basis.append(f"{gap_now:.0f} rest days vs your usual {np.mean(gaps):.1f}: {adj * 100:+.1f}% (confirmed pattern)")
    return base * (1.0 + mult), basis


# ====================================================== predict ======================================================
def _naive(rows: list[dict], target: str, key: str | None, today_idx: int) -> float | None:
    if target == "adherence":
        return float(rows[today_idx - 6]["trained"]) if today_idx >= 6 else None   # same weekday last week
    for r in reversed(rows[: today_idx + 1]):
        if r.get(key) is not None:
            return float(r[key])
    return None


def predict(target: str, subject: str | None = None, **kw: Any) -> dict:
    """Forecast `target` ("e1rm" with subject=lift, "readiness", "adherence" = chance you train that day, "bodyweight") `horizon_days` ahead.
    kw: horizon_days, as_of (YYYY-MM-DD; default today), log (default True), use_discoveries (default True).
    Returns {value, lo, hi, horizon (date), model, basis[], prediction_id, n, naive}; value is None with a `reason` when the data are too thin."""
    if target not in TARGETS:
        return {"value": None, "reason": f"unknown target {target}"}
    try:
        today = _today(kw.get("as_of"))
        h = int(kw.get("horizon_days", _DEFAULT_H[target]))
        rows = _history(today)
        if not rows:
            return {"value": None, "reason": "no data yet"}
        ti = len(rows) - 1                                    # index of `today` (rows end at today)
        target_date = today + timedelta(days=h)
        t_target = float(ti + h)
        basis: list[str] = []
        key: str | None = None
        if target == "e1rm":
            if not subject:
                return {"value": None, "reason": "e1rm needs a lift"}
            key = frames.lift_key(subject)
            if not rows or key not in rows[0]:
                return {"value": None, "reason": f"no {subject} sets logged"}
            t, y = _series(rows, key)
            keep = ~np.isnan(miner._clean_series(y))
            t, y = t[keep], y[keep]
            # forecasting day `today` itself: that day's sets are the target, not history
            if h <= 0:
                m = t < ti
                t, y = t[m], y[m]
            model, log_space = _model_e1rm, True
        elif target == "readiness":
            key = "readiness"
            t, y = _series(rows, key)
            if h <= 0:
                m = t < ti
                t, y = t[m], y[m]
            model, log_space = _model_readiness, False
        elif target == "bodyweight":
            key = "bodyweight"
            t, y = _series(rows, key)
            model, log_space = _model_bodyweight, False
        else:
            return _predict_adherence(rows, today, h, kw)
        if len(t) < 4:
            return {"value": None, "reason": "not enough history yet", "n": int(len(t))}
        base = model(t, y, t_target)
        if base is None:
            return {"value": None, "reason": "not enough history yet", "n": int(len(t))}
        value = base
        if kw.get("use_discoveries", True):
            if target == "e1rm":
                value, basis = _adjust_e1rm(rows, t_target, ti, base, key)
            elif target == "bodyweight":
                value, basis = _adjust_bodyweight(rows, ti, t_target, base, t, y)
        sig, ne = _interval(model, t, y, float(max(h, 1)), log_space=log_space)
        if not np.isfinite(sig):
            sig = (0.04 * value) if log_space else (12.0 if target == "readiness" else 0.9)
            basis.append("interval is a wide default: not enough history to measure how predictable you are yet")
            ne = 0
        half = Z80 * sig * (value if log_space else 1.0)
        lo, hi = value - half, value + half
        if target == "readiness":
            lo, hi = max(0.0, lo), min(100.0, hi)
        naive = _naive(rows, target, key, ti if h > 0 else ti - 1)
        out = {"value": float(value), "lo": float(lo), "hi": float(hi), "horizon": target_date.isoformat(), "model": {"e1rm": "theil-sen trend", "readiness": "personal normal + AR(1)",
               "bodyweight": "theil-sen trend"}[target] + (" + confirmed patterns" if basis and not basis[0].startswith("interval") else ""), "basis": basis, "n": int(len(t)),
               "naive": naive, "nominal": NOMINAL, "n_errors": ne}
        if kw.get("log", True):
            out["prediction_id"] = _log(target, subject, out, today, int(h))
        return out
    except Exception as e:           # a forecast must never break the app
        return {"value": None, "reason": f"{type(e).__name__}: {e}"[:160]}


def _adjust_bodyweight(rows: list[dict], ti: int, t_target: float, base: float, t: np.ndarray, y: np.ndarray) -> tuple[float, list[str]]:
    """With a confirmed personal maintenance estimate, blend the energy balance of the last 2 weeks into the weight trend."""
    d = _confirmed("maintenance_calories:")
    est = ((d or {}).get("effect") or {}).get("estimate")
    if not d or est is None:
        return base, []
    last = [r for r in rows[-14:] if r.get("kcal") is not None and r["kcal"] > 0]
    kc = [r["kcal"] for r in last]
    if len(kc) < 10 or float(np.median(kc)) <= 0:
        return base, []
    med = float(np.median(kc))
    kc = [k for k in kc if k >= 0.6 * med]
    bal = (float(np.mean(kc)) - float(est)) / frames_kcal_per_kg()
    horizon = t_target - t[-1]
    if horizon <= 0:
        return base, []
    b_trend = (base - float(np.median(y[-5:]))) / max(1.0, horizon)
    mixed = 0.5 * b_trend + 0.5 * bal
    adj = (mixed - b_trend) * horizon
    return base + adj, [f"intake {np.mean(kc):,.0f} kcal vs your measured maintenance {float(est):,.0f}: {adj:+.2f} kg (confirmed pattern)"]


def frames_kcal_per_kg() -> float:
    return miner.KCAL_PER_KG


def _predict_adherence(rows: list[dict], today: date, h: int, kw: dict) -> dict:
    n = len(rows)
    if n < 21:
        return {"value": None, "reason": "not enough history yet", "n": n}
    ti = n - 1
    target_date = today + timedelta(days=h)
    wd = target_date.weekday()
    hist = rows[: ti + 1] if h > 0 else rows[:ti]
    recent = hist[-84:]
    same = [1.0 if r["trained"] else 0.0 for r in recent if r["dow"] == wd]
    overall = float(np.mean([1.0 if r["trained"] else 0.0 for r in recent]))
    k = 2.0
    p = (sum(same) + k * overall) / (len(same) + k)           # this weekday's rate, shrunk toward the athlete's overall rate
    nn = len(same) + k
    z = Z80
    centre = (p + z * z / (2 * nn)) / (1 + z * z / nn)
    half = z * math.sqrt(p * (1 - p) / nn + z * z / (4 * nn * nn)) / (1 + z * z / nn)
    naive = None
    for r in reversed(hist):
        if r["dow"] == wd:
            naive = 1.0 if r["trained"] else 0.0
            break
    basis = [f"you trained on {int(sum(same))} of your last {len(same)} {miner.WEEKDAYS[wd]}s"]
    out = {"value": float(p), "lo": float(max(0.0, centre - half)), "hi": float(min(1.0, centre + half)), "horizon": target_date.isoformat(), "model": "shrunk weekday rate",
           "basis": basis, "n": len(same), "naive": naive, "nominal": NOMINAL}
    if kw.get("log", True):
        out["prediction_id"] = _log("adherence", None, out, today, h)
    return out


def _log(target: str, subject: str | None, out: dict, today: date, h: int) -> int:
    ex = [p for p in store.predictions(target, subject, resolved=False, limit=60) if (p.get("features") or {}).get("target_date") == out["horizon"]
          and (p.get("features") or {}).get("as_of") == today.isoformat()]
    if ex:
        return ex[0]["id"]
    feats = {"target_date": out["horizon"], "as_of": today.isoformat(), "naive": out.get("naive"), "nominal": out.get("nominal"), "basis": out.get("basis"), "h": h}
    return store.log_prediction(target, out["value"], f"{out['horizon']}T23:59:59+00:00", subject=subject, lo=out["lo"], hi=out["hi"], model=out["model"], features=feats)


# ====================================================== resolve ======================================================
def _actual(rows_by_date: dict[str, dict], dates: list[str], target: str, subject: str | None, tdate: date, now: date) -> float | None:
    """What actually happened for one prediction, or None when it cannot be told yet (or the data for that day never arrived)."""
    d0 = tdate.isoformat()
    r = rows_by_date.get(d0)
    if target == "e1rm":
        key = frames.lift_key(subject or "")
        for k in range(0, 4):                                   # your next session of that lift within 3 days of the forecast date
            dk = (tdate + timedelta(days=k)).isoformat()
            if tdate + timedelta(days=k) > now:
                return None
            rr = rows_by_date.get(dk)
            if rr and rr.get(key) is not None:
                return float(rr[key])
        return None
    if tdate >= now and target in ("readiness", "adherence"):
        return None                                             # the day is not over
    if target == "readiness":
        return float(r["readiness"]) if r and r.get("readiness") is not None else None
    if target == "adherence":
        return 1.0 if r and r["trained"] else (0.0 if r is not None else None)
    if target == "bodyweight":
        vals = [rows_by_date[(tdate + timedelta(days=k)).isoformat()]["bodyweight"] for k in (-1, 0, 1) if (tdate + timedelta(days=k)).isoformat() in rows_by_date
                and rows_by_date[(tdate + timedelta(days=k)).isoformat()].get("bodyweight") is not None and tdate + timedelta(days=k) <= now]
        return float(np.median(vals)) if vals else None
    return None


def resolve_due() -> dict:
    """Resolve every logged prediction whose day has passed against the frames. Idempotent; never raises."""
    try:
        now = clock.now().date()
        pend = store.predictions(resolved=False, limit=2000)
        if not pend:
            return {"resolved": 0, "pending": 0}
        rows = frames.build(200, now.isoformat())
        by = {r["date"]: r for r in rows}
        res = waiting = 0
        for p in pend:
            f = p.get("features") or {}
            td = f.get("target_date") or (p["horizon_ts"] or "")[:10]
            try:
                tdate = date.fromisoformat(td)
            except ValueError:
                continue
            a = _actual(by, list(by), p["target"], p["subject"], tdate, now)
            if a is None:
                waiting += 1
                continue
            store.resolve_prediction(p["id"], a)
            res += 1
        return {"resolved": res, "pending": waiting}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"[:160]}


# ====================================================== scoring ======================================================
def calibration(target: str, subject: str | None = None) -> dict:
    """Out-of-sample scorecard: {n, mae, naive_mae, skill (1 - mae/naive_mae; > 0 beats 'same as last time'), coverage (share of actuals inside the stated interval), nominal}."""
    ps = [p for p in store.predictions(target, subject, resolved=True, limit=5000) if p["actual"] is not None]
    if not ps:
        return {"n": 0, "mae": None, "naive_mae": None, "skill": None, "coverage": None, "nominal": NOMINAL}
    sq = target == "adherence"        # a probability is scored by squared error (Brier), the others by absolute error
    f_err = (lambda a, b: (a - b) ** 2) if sq else (lambda a, b: abs(a - b))
    err = np.array([f_err(p["actual"], p["predicted"]) for p in ps])
    nv = [(f_err(p["actual"], (p.get("features") or {})["naive"]) if (p.get("features") or {}).get("naive") is not None else float("nan")) for p in ps]
    nv = np.array(nv)
    ok = np.isfinite(nv)
    mae = float(err[ok].mean()) if ok.any() else float(err.mean())
    nmae = float(nv[ok].mean()) if ok.any() else None
    cov = [p["lo"] <= p["actual"] <= p["hi"] for p in ps if p["lo"] is not None and p["hi"] is not None]
    return {"n": len(ps), "mae": mae, "naive_mae": nmae, "skill": (1.0 - mae / nmae) if nmae else None, "coverage": float(np.mean(cov)) if cov and target != "adherence" else None, "nominal": NOMINAL,
            "n_compared": int(ok.sum()), "metric": "brier" if sq else "mae"}


def surprises(limit: int = 5, target: str | None = None) -> list[dict]:
    """The most surprising resolved predictions (largest error in units of the forecast's own uncertainty), each with a plain explanation from that day's logs."""
    ps = [p for p in store.predictions(target, None, resolved=True, limit=2000) if p["actual"] is not None and p["lo"] is not None and p["hi"] is not None]
    out = []
    for p in ps:
        sigma = (p["hi"] - p["lo"]) / (2 * Z80)
        if sigma <= 0:
            continue
        z = (p["actual"] - p["predicted"]) / sigma
        out.append((abs(z), z, p))
    out.sort(key=lambda x: -x[0])
    res = []
    for _, z, p in out[: max(0, limit)]:
        res.append({"id": p["id"], "target": p["target"], "subject": p["subject"], "date": (p.get("features") or {}).get("target_date"), "predicted": round(p["predicted"], 2),
                    "actual": round(p["actual"], 2), "z": round(z, 2), "explanation": _explain(p, z)})
    return res


def _explain(p: dict, z: float) -> str:
    what = {"e1rm": f"your {p['subject']} estimated max", "readiness": "your readiness", "bodyweight": "your bodyweight", "adherence": "whether you would train"}[p["target"]]
    d = (p.get("features") or {}).get("target_date")
    if p["target"] == "adherence":
        head = f"I gave {p['predicted'] * 100:.0f}% that you would train on {d}; you {'did' if p['actual'] >= 0.5 else 'did not'}."
    else:
        unit = "" if p["target"] == "readiness" else (" kg" if p["target"] == "bodyweight" else "")
        diff = p["actual"] - p["predicted"]
        pct = f" ({diff / p['predicted'] * 100:+.1f}%)" if p["target"] == "e1rm" and p["predicted"] else ""
        head = (f"I expected {what} around {p['predicted']:.1f}{unit} ({p['lo']:.1f} to {p['hi']:.1f}) on {d}; it was {p['actual']:.1f}{unit}{pct}, "
                f"{abs(z):.1f}x further {'up' if z > 0 else 'down'} than my usual error.")
    try:
        rows = frames.build(60, d)
        t = frames.today(60, d)
        dev = [s for s in t["deviations"] if not s.startswith("no check-in")]
        if dev:
            head += " That day you " + "; ".join(dev[:2]) + "."
    except Exception:
        pass
    return head


# ====================================================== back-test on synthetic athletes (used by bench_insight) ======================================================
def backtest(n_athletes: int = 6, weeks: int = 16, start_week: int = 8, lifts: tuple[str, ...] = ("squat", "bench press"), seed0: int = 3000,
             use_discoveries: bool = True) -> dict:
    """Replay history day by day (the clock stands at each date, only older rows are visible), log forecasts, resolve them, score them.
    Discovery adjustments are tested by mining once at `start_week` and then forecasting the weeks after it."""
    import os
    import tempfile

    from . import synth
    end = "2026-03-10"
    db.DB_PATH = os.path.join(tempfile.mkdtemp(), "forecast.db")
    out: dict[str, list] = {t: [] for t in ("e1rm", "readiness", "bodyweight", "adherence")}
    for s in range(n_athletes):
        synth.make_athlete(seed0 + s, weeks * 7, synth.default_effects(), end=end)
        d0 = date.fromisoformat(end) - timedelta(days=weeks * 7 - 1)
        first = d0 + timedelta(days=start_week * 7)
        os.environ["FITNESS_NOW"] = f"{(first - timedelta(days=1)).isoformat()}T20:00:00+00:00"
        miner.run(force=True)
        cur = first
        last = date.fromisoformat(end) - timedelta(days=8)
        while cur <= last:
            os.environ["FITNESS_NOW"] = f"{cur.isoformat()}T20:00:00+00:00"
            for lift in lifts:
                predict("e1rm", lift, horizon_days=3, use_discoveries=use_discoveries)
            predict("readiness", horizon_days=1)
            if (cur - first).days % 3 == 0:
                predict("bodyweight", horizon_days=7, use_discoveries=use_discoveries)
            predict("adherence", horizon_days=1)
            cur += timedelta(days=2)
        os.environ["FITNESS_NOW"] = f"{end}T23:00:00+00:00"
        resolve_due()
    res = {}
    for t in TARGETS:
        res[t] = calibration(t)
    return {"athletes": n_athletes, "weeks": weeks, "start_week": start_week, **{k: {kk: (round(vv, 3) if isinstance(vv, float) else vv) for kk, vv in v.items()} for k, v in res.items()}}
