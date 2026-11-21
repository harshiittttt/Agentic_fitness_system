"""The miner: finds statistically valid things about THIS athlete from their own logs, and writes them as discoveries and trait beliefs.

    miner.run(force=False)   nightly: skip when the frames' data_hash is unchanged
    miner.run_full()         weekly: same, forced
    miner.analyze(...)       pure (no writes): every hypothesis tested, with q-values and the confirm/candidate decision (used by the benchmark)

How a pattern earns the word "confirmed" (all of it, in this order):
  1. the hypothesis is PRE-REGISTERED here (one variable pair and one lag; nothing is searched over lags or thresholds);
  2. both variables are cleaned of the athlete's progression trend and weekday rhythm, and of their other check-in variables (partial effect);
  3. block-permutation + Bartlett-corrected p-value (stats.partial_test), moving-block bootstrap CI;
  4. Benjamini-Hochberg across ALL hypotheses tested in the run: q < 0.03 and >= 42 days of history (strict because the miner peeks nightly; see Q_CONFIRM);
  5. enough data (minimum n), an effect larger than a practical-significance floor, a CI that excludes zero, and the same direction in BOTH halves
     of the history;
  6. physiologically implausible directions (e.g. "more sleep, weaker lifts") are capped at `candidate`.
Statements and recommendations are produced by code from the numbers (no model), in plain second person.
"""
from __future__ import annotations

import math
import time
import zlib
from datetime import date
from types import SimpleNamespace
from typing import Any, Callable

import numpy as np

from backend.memory import db

from .. import clock
from . import frames, stats, store
from .types import register_predicate

register_predicate("maintenance_kcal", functional=True, half_life=75, sensitivity="body", cls="nutrition")

DAYS = 182
# Q_CONFIRM is deliberately strict: the miner re-tests every night, and each night is another look at the same accumulating data. Measured on 60
# null athletes looked at every 3 days: q<0.10 gave a false confirm at SOME night for 38% of athletes (3% at one fixed look); q<0.03 gives 13%, and q<0.03 plus a 42-day history floor gives ~8%.
# Once confirmed, a pattern is held at the looser Q_KEEP (hysteresis in decide()), so a real effect is not flickered away.
Q_CONFIRM, Q_CANDIDATE, Q_KEEP = 0.03, 0.25, 0.20
MIN_SPAN_CONFIRM = 42           # days of history before anything can be FIRST confirmed (the earliest, noisiest nightly looks produced a third of the false confirms)
Q_PLAUSIBLE_OVERRIDE = 0.10     # a candidate with an implausible direction needs at least this q to be listed at all
KCAL_PER_KG = 7700.0
STRENGTH_MODE = "top"
HARD_RPE = 8.5
RIR_LO, RIR_HI = 0.5, 4.5      # implied reps in reserve kept for the RPE-bias test
PLANNED_RATE = 0.30          # a weekday can only count as "a day you usually train" if you train on at least 30% of them
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
EXPECTED_SIGN = {"sleep_performance": 1, "energy_performance": 1, "soreness_performance": -1, "hrv_performance": 1, "rhr_performance": -1, "protein_recovery": -1, "late_meals_sleep": -1}
STATUS_ORDER = {"retired": 0, "candidate": 1, "weakening": 2, "confirmed": 3}


# ====================================================== context: aligned arrays, performance index ======================================================
def _clean_series(v: np.ndarray) -> np.ndarray:
    """Drop gross typos in one lift's e1RM series: values more than 20% away from the median of their neighbours (+-3 sessions)."""
    v = v.copy()
    idx = np.where(np.isfinite(v))[0]
    vals = v[idx]
    for j, i in enumerate(idx):
        nb = np.concatenate([vals[max(0, j - 3):j], vals[j + 1:j + 4]])
        if len(nb) >= 3:
            m = float(np.median(nb))
            if m > 0 and (vals[j] > 1.2 * m or vals[j] < 0.8 * m):
                v[i] = np.nan
    return v


def _shift(a: np.ndarray, k: int) -> np.ndarray:
    """a shifted so that out[i] = a[i-k] (k >= 0); the first k entries are NaN."""
    out = np.full(len(a), np.nan)
    if k == 0:
        return a.copy()
    out[k:] = a[:-k]
    return out


def _session_strength(rows: list[dict], sets: list[dict], lifts: list[str]) -> dict[str, np.ndarray]:
    """Per-day, per-lift strength: the mean of the better half of that day's set-level e1RMs (less noisy than the single best set, which picks the luckiest
    rating). Falls back to nothing (the caller uses frames' best-set column) when set rows are not supplied."""
    from backend.tools.calculators import rpe_adjusted_e1rm
    if not sets:
        return {}
    pos = {r["date"]: i for i, r in enumerate(rows)}
    per: dict[tuple[str, int], list[float]] = {}
    for s in sets:
        i = pos.get(s["date"])
        if i is None or not (1 <= s["reps"] <= 12):
            continue
        v = rpe_adjusted_e1rm(s["weight"], s["reps"], s["rpe"])
        if v > 0:
            per.setdefault((frames.lift_key(s["exercise"]), i), []).append((v, s["rpe"]))
    out = {k: np.full(len(rows), np.nan) for k in lifts}
    for (k, i), vs in per.items():
        if k in out:
            hard = [v for v, rp in vs if rp is not None and rp >= HARD_RPE]       # near-failure sets carry the least rating error (it scales with reps in reserve)
            if hard and STRENGTH_MODE == "hard":
                out[k][i] = float(np.mean(hard))
            else:
                a = sorted((v for v, _ in vs), reverse=True)
                out[k][i] = float(np.mean(a[:max(1, int(math.ceil(len(a) / 2)))]))
    return out


def make_ctx(rows: list[dict], sets: list[dict], profile: dict | None, n_perm: int = 1999, n_boot: int = 400) -> SimpleNamespace:
    n = len(rows)
    t = np.arange(n, dtype=float)
    A = frames.arrays(rows, ["dow", "sleep_h", "soreness", "energy", "bodyweight", "kcal", "protein_g", "trained", "n_sets", "volume_kg", "logged", "late_meal",
                             "meals_n", "rhr", "hrv_ms", "steps"])
    dow = A["dow"].astype(int)
    kc = A["kcal"]
    med = np.nanmedian(kc) if np.isfinite(kc).any() else float("nan")
    complete = np.isfinite(kc) & (kc >= 0.6 * med)
    A["complete"] = complete.astype(float)
    lifts = [k for k in frames.lifts_in(rows) if sum(1 for r in rows if r.get(k) is not None) >= 10]
    sess = _session_strength(rows, sets, lifts)
    raw, res = {}, {}
    for k in lifts:
        v = _clean_series(sess[k] if k in sess else frames.arrays(rows, [k])[k])
        lv = np.log(v)
        raw[k] = lv
        r, _ = stats.detrend(lv, t, None, 21.0)
        res[k] = stats.winsorize(r, 4.0)
    perf = np.full(n, np.nan)
    if res:
        M = np.vstack([res[k] for k in lifts])
        fin = np.isfinite(M)
        cnt = fin.sum(axis=0)
        perf = np.where(cnt > 0, np.where(fin, M, 0.0).sum(axis=0) / np.maximum(cnt, 1), np.nan)
    trained = A["trained"] > 0
    gap = np.full(n, np.nan)
    last = None
    for i in range(n):
        if trained[i]:
            if last is not None:
                gap[i] = i - last
            last = i
    ordn = np.array([date.fromisoformat(r["date"]).toordinal() for r in rows])
    week = (ordn - dow - (ordn[0] - dow[0])) // 7
    return SimpleNamespace(rows=rows, sets=sets, profile=profile, n=n, t=t, dow=dow, A=A, lifts=lifts, raw=raw, res=res, perf=perf, gap=gap, week=week,
                           trend=stats.trend_basis(t), lin=np.column_stack([np.ones(n), t / max(n, 1)]), dowd=stats.dow_dummies(dow), n_perm=n_perm, n_boot=n_boot, trained=trained)


def _seed(key: str) -> int:
    return zlib.crc32(key.encode()) & 0x7FFFFFFF


def _pt(ctx, key: str, x, y, nuis) -> dict:
    return stats.partial_test(x, y, nuis, n_perm=ctx.n_perm, n_boot=ctx.n_boot, seed=_seed(key))


def _used_mask(x, y, nuis) -> np.ndarray:
    m = np.isfinite(x) & np.isfinite(y)
    if nuis is not None:
        m &= np.all(np.isfinite(nuis), axis=1)
    return m


def _base(key: str, family: str, **kw) -> dict:
    return {"key": key, "family": family, "ok": True, **kw}


# ====================================================== performance-linked hypotheses ======================================================
def _perf_hyp(ctx, key: str, family: str, x: np.ndarray, covs: list[np.ndarray], *, floor: float, unit: str, label: str, use_dow: bool = True,
              min_n: int = 24) -> dict | None:
    if not np.isfinite(ctx.perf).any():
        return None
    # y already has each lift's own progression removed; a linear trend mops up what is left. Covariates only ADJUST: a missing covariate value is
    # replaced by the athlete's own average inside the adjustment (the row is not lost, and nothing is shown to the athlete).
    fill = [np.where(np.isfinite(c), c, np.nanmean(c) if np.isfinite(c).any() else 0.0) for c in covs]
    cols = [ctx.lin] + ([ctx.dowd] if use_dow else []) + [c[:, None] for c in fill]
    N = np.column_stack(cols)
    m = _used_mask(x, ctx.perf, N)
    if m.sum() < min_n:
        return None
    r = _pt(ctx, key, x, ctx.perf, N)
    if not r["ok"]:
        return None
    xs = x[m]
    sd_x = float(np.std(xs))
    eff = 100.0 * r["slope"]
    lo, hi = 100.0 * r["ci"][0], 100.0 * r["ci"][1]
    q1, q2 = np.quantile(xs, [1 / 3, 2 / 3])
    hi_g, lo_g = xs >= q2, xs <= q1
    yr = stats.residualize(ctx.perf[m], np.column_stack([N[m], x[m]])) + r["slope"] * (x[m] - x[m].mean())
    contrast = 100.0 * (float(np.mean(yr[hi_g])) - float(np.mean(yr[lo_g]))) if hi_g.sum() >= 3 and lo_g.sum() >= 3 else None
    h = [100.0 * (h_[0]) for h_ in r["halves"]]
    return _base(key, family, n=int(m.sum()), n_eff=r["n_eff"], p=r["p"], p_perm=r["p_perm"], p_an=r["p_analytic"], effect=eff, ci=(lo, hi), gate_ci=(lo, hi), unit=unit,
                 sd_x=sd_x, size=abs(eff) * sd_x, floor=floor, min_n=min_n, agree=r["agree"], halves=h, per_sd=eff * sd_x, contrast=contrast,
                 q_cut_lo=float(q1), q_cut_hi=float(q2), label=label, lifts=[k[5:].replace("_", " ") for k in ctx.lifts], x_mean=float(np.mean(xs)))


def _predictors(ctx) -> dict[str, np.ndarray]:
    """The daily candidate predictors of how a session goes. Each performance hypothesis is tested as a PARTIAL effect holding the others fixed, which both
    removes confounding between them (energy follows sleep) and removes the variance the others explain, so real effects are not drowned."""
    A = ctx.A
    return {"sleep0": A["sleep_h"], "sleep1": _shift(A["sleep_h"], 1), "soreness": A["soreness"], "energy": A["energy"], "gap": np.minimum(ctx.gap, 5.0)}


def _others(ctx, *drop: str) -> list[np.ndarray]:
    return [v for k, v in _predictors(ctx).items() if k not in drop]


def h_sleep(ctx, lag: int) -> dict | None:
    P = _predictors(ctx)
    return _perf_hyp(ctx, f"sleep_performance:sleep_h>perf:lag{lag}", "sleep_performance", P[f"sleep{lag}"], _others(ctx, f"sleep{lag}"), floor=0.6,
                     unit="% e1RM per hour", label=f"lag{lag}")


def h_soreness(ctx) -> dict | None:
    return _perf_hyp(ctx, "soreness_performance:soreness>perf:lag0", "soreness_performance", ctx.A["soreness"], _others(ctx, "soreness"), floor=0.6,
                     unit="% e1RM per point", label="lag0")


def h_energy(ctx) -> dict | None:
    return _perf_hyp(ctx, "energy_performance:energy>perf:lag0", "energy_performance", ctx.A["energy"], _others(ctx, "energy"), floor=0.6,
                     unit="% e1RM per point", label="lag0")


def h_rest_gap(ctx) -> dict | None:
    return _perf_hyp(ctx, "rest_gap:days_since_session>perf:lag0", "rest_gap", np.minimum(ctx.gap, 5.0), _others(ctx, "gap"), floor=0.6,
                     unit="% e1RM per rest day", label="lag0")


# Wearable-driven hypotheses. Pre-registered like the rest (one pair, one lag, no search). They exist only for athletes who import a device: with fewer than
# WEARABLE_MIN_DAYS days of the reading, or a constant series, they return None (nothing is tested, nothing is shown, and BH is not charged for them).
WEARABLE_MIN_DAYS = 24


def _wearable_hyp(ctx, key: str, family: str, x: np.ndarray, unit: str, label: str, floor: float) -> dict | None:
    if int(np.isfinite(x).sum()) < WEARABLE_MIN_DAYS or float(np.nanstd(x)) <= 1e-9:
        return None
    return _perf_hyp(ctx, key, family, x, _others(ctx), floor=floor, unit=unit, label=label, min_n=WEARABLE_MIN_DAYS)


def h_hrv(ctx) -> dict | None:       # the overnight reading is in before the session
    return _wearable_hyp(ctx, "hrv_performance:hrv_ms>perf:lag0", "hrv_performance", ctx.A["hrv_ms"] / 10.0, "% e1RM per 10 ms HRV", "lag0", 0.6)


def h_rhr(ctx) -> dict | None:
    return _wearable_hyp(ctx, "rhr_performance:rhr>perf:lag0", "rhr_performance", ctx.A["rhr"], "% e1RM per bpm", "lag0", 0.6)


def h_steps(ctx) -> dict | None:     # yesterday's activity (today's count is still incomplete at session time)
    return _wearable_hyp(ctx, "steps_performance:steps_prev_day>perf:lag1", "steps_performance", _shift(ctx.A["steps"] / 1000.0, 1), "% e1RM per 1000 steps", "lag1", 0.6)


def h_bodyweight(ctx) -> dict | None:
    bw = ctx.A["bodyweight"]
    sm = np.full(ctx.n, np.nan)
    for i in range(ctx.n):
        w = bw[max(0, i - 6):i + 1]
        w = w[np.isfinite(w)]
        if len(w) >= 2:
            sm[i] = float(np.median(w))
    return _perf_hyp(ctx, "bodyweight_strength:bodyweight_7d>perf:lag0", "bodyweight_strength", sm, _others(ctx), floor=0.25, unit="% e1RM per kg", label="lag0",
                     min_n=30)


# ====================================================== weekly: volume, frequency ======================================================
def _weekly(ctx) -> SimpleNamespace | None:
    wk = ctx.week
    rows = []
    for w in np.unique(wk):
        ix = np.where(wk == w)[0]
        if len(ix) < 7:
            continue
        sets_w = float(np.nansum(ctx.A["n_sets"][ix]))
        freq = float(np.sum(ctx.trained[ix]))
        lv = []
        for k in ctx.lifts:
            v = ctx.raw[k][ix]
            v = v[np.isfinite(v)]
            if len(v):
                lv.append((k, float(np.mean(v))))
        rows.append((int(w), sets_w, freq, dict(lv)))
    if len(rows) < 6:
        return None
    # level per week = mean over lifts of (weekly mean - lift's own overall mean)
    mean_lift = {k: float(np.nanmean(ctx.raw[k])) for k in ctx.lifts}
    P = []
    for w, v, f, lv in rows:
        P.append(float(np.mean([lv[k] - mean_lift[k] for k in lv])) if len(lv) >= 2 else float("nan"))
    P = np.array(P)
    V = np.array([r[1] for r in rows])
    F = np.array([r[2] for r in rows])
    W = np.array([r[0] for r in rows])
    d = np.full(len(rows), np.nan)
    for i in range(len(rows) - 1):
        if W[i + 1] == W[i] + 1:
            d[i] = P[i + 1] - P[i]
    return SimpleNamespace(V=V, F=F, W=W, d=d, P=P)


def _weekly_nuis(wk, extra: list[np.ndarray]) -> np.ndarray:
    t = np.arange(len(wk.W), dtype=float)
    return np.column_stack([np.ones(len(t)), t] + [e[:, None] for e in extra])


def _ma1_whiten(x, y, N) -> tuple[np.ndarray, np.ndarray, np.ndarray, float, np.ndarray]:
    """Weekly progress is a DIFFERENCE of two noisy weekly levels, so its errors are MA(1) with a negative lag-1 correlation (up to -0.5): plain least squares
    throws that structure away. Estimate the lag-1 correlation from the residuals, then filter x, y and the nuisance columns by the inverse MA(1) (generalised
    least squares); the filtered data are what the permutation test, the t-test and the bootstrap then see. Returns (x, y, N, theta, kept rows)."""
    m = _used_mask(x, y, N)
    xs, ys, Ns = np.asarray(x, float)[m], np.asarray(y, float)[m], N[m]
    if len(ys) < 10:
        return xs, ys, Ns, 0.0, m
    X = np.column_stack([Ns, xs])
    res = stats.residualize(ys, X)
    den = float((res * res).sum())
    rho = float((res[:-1] * res[1:]).sum() / den) * len(res) / max(len(res) - X.shape[1], 1) if den > 0 else 0.0     # small-sample deflation of the residual acf undone
    rho = float(np.clip(rho, -0.48, 0.0))
    theta = (1.0 - math.sqrt(1.0 - 4.0 * rho * rho)) / (2.0 * rho) if rho < -1e-9 else 0.0

    def filt(v: np.ndarray) -> np.ndarray:
        out = np.empty_like(v, dtype=float)
        out[0] = v[0]
        for i in range(1, len(v)):
            out[i] = v[i] - theta * out[i - 1]
        return out

    return filt(xs), filt(ys), np.column_stack([filt(Ns[:, j]) for j in range(Ns.shape[1])]), float(theta), m


def _pt_weekly(ctx, key: str, x, y, N) -> dict:
    """partial_test on GLS-filtered weekly data (halves of >= 5 weeks may replicate: a 16-week history has only ~7 usable weeks per half)."""
    xw, yw, Nw, theta, m = _ma1_whiten(x, y, N)
    r = stats.partial_test(xw, yw, Nw, n_perm=ctx.n_perm, n_boot=ctx.n_boot, seed=_seed(key), min_half=5)
    r["theta"] = theta
    r["_w"] = (xw, yw, Nw, m)
    return r


def h_volume_linear(ctx) -> dict | None:
    wk = _weekly(ctx)
    if wk is None:
        return None
    N = _weekly_nuis(wk, [wk.F])
    key = "volume_response:weekly_sets>next_week_progress:linear"
    m = _used_mask(wk.V, wk.d, N)
    if m.sum() < 10:
        return None
    r = _pt_weekly(ctx, key, wk.V, wk.d, N)
    if not r["ok"]:
        return None
    sd = float(np.std(wk.V[m]))
    eff = 100.0 * r["slope"] * 10.0                       # % per week of progress per +10 sets
    ci = (100.0 * r["ci"][0] * 10.0, 100.0 * r["ci"][1] * 10.0)
    return _base(key, "volume_response", n=int(m.sum()), n_eff=r["n_eff"], p=r["p"], effect=eff, ci=ci, gate_ci=ci, unit="% progress per week per +10 sets",
                 size=abs(100.0 * r["slope"]) * sd, floor=0.25, min_n=10, agree=r["agree"], halves=[100 * h[0] * 10 for h in r["halves"]], sd_x=sd,
                 v_mean=float(np.mean(wk.V[m])), v_lo=float(np.quantile(wk.V[m], 0.1)), v_hi=float(np.quantile(wk.V[m], 0.9)), unit_n="weeks")


def h_volume_sweet(ctx) -> dict | None:
    wk = _weekly(ctx)
    if wk is None:
        return None
    key = "volume_response:weekly_sets>next_week_progress:sweet_spot"
    base_N = _weekly_nuis(wk, [wk.F])
    m0 = np.isfinite(wk.V) & np.isfinite(wk.d) & np.all(np.isfinite(base_N), axis=1)
    if m0.sum() < 12:
        return None
    vbar = float(np.mean(wk.V[m0]))
    vc = wk.V - vbar
    N = np.column_stack([base_N, vc])
    x = vc ** 2
    r = _pt_weekly(ctx, key, x, wk.d, N)
    if not r["ok"]:
        return None
    xw, dw, Nw, _m = r["_w"]
    beta, *_ = np.linalg.lstsq(np.column_stack([Nw, xw]), dw, rcond=None)
    b, c = float(beta[-2]), float(beta[-1])
    p10, p90 = float(np.quantile(wk.V[m0], 0.1)), float(np.quantile(wk.V[m0], 0.9))
    vstar = vbar - b / (2 * c) if c < 0 else float("nan")
    interior = c < 0 and p10 <= vstar <= p90
    gain = abs(c) * max((vstar - vbar - (p10 - vbar)) ** 2, (p90 - vstar) ** 2) * 100.0 if interior else 0.0
    # uncertainty of the sweet spot: block bootstrap over weeks
    rng = np.random.default_rng(_seed(key) + 7)
    n = int(m0.sum())
    idx0 = np.where(m0)[0]
    vs = []
    for _ in range(300):
        st = rng.integers(0, max(1, n - 3), size=int(math.ceil(n / 3)))
        ii = idx0[((st[:, None] + np.arange(3)[None, :]).ravel())[:n] % n]
        try:
            bb, *_ = np.linalg.lstsq(np.column_stack([N[ii], x[ii]]), wk.d[ii], rcond=None)
        except np.linalg.LinAlgError:
            continue
        if bb[-1] < 0:
            vs.append(vbar - bb[-2] / (2 * bb[-1]))
    lo, hi = (float(np.quantile(vs, 0.1)), float(np.quantile(vs, 0.9))) if len(vs) >= 60 else (float("nan"), float("nan"))
    cc = (100 * r["ci"][0], 100 * r["ci"][1])
    return _base(key, "volume_response", n=n, n_eff=r["n_eff"], p=r["p"], effect=float(vstar) if np.isfinite(vstar) else 0.0, ci=(lo, hi), gate_ci=cc,
                 unit="sets per week", size=gain if (interior and c < 0) else 0.0, floor=0.5, min_n=14, agree=bool(r["agree"] and interior),
                 halves=[100 * h[0] for h in r["halves"]], interior=bool(interior), curvature=c, v_mean=vbar, v_lo=p10, v_hi=p90, unit_n="weeks",
                 sign_override=-1 if c < 0 else 1, direction_ok=bool(interior))


def h_frequency(ctx) -> dict | None:
    wk = _weekly(ctx)
    if wk is None:
        return None
    key = "frequency_response:sessions_per_week>next_week_progress:linear"
    N = _weekly_nuis(wk, [wk.V])
    m = _used_mask(wk.F, wk.d, N)
    if m.sum() < 10 or np.std(wk.F[m]) == 0:
        return None
    r = _pt_weekly(ctx, key, wk.F, wk.d, N)
    if not r["ok"]:
        return None
    sd = float(np.std(wk.F[m]))
    eff = 100.0 * r["slope"]
    ci = (100.0 * r["ci"][0], 100.0 * r["ci"][1])
    return _base(key, "frequency_response", n=int(m.sum()), n_eff=r["n_eff"], p=r["p"], effect=eff, ci=ci, gate_ci=ci, unit="% progress per week per extra weekly session",
                 size=abs(eff) * sd, floor=0.25, min_n=10, agree=r["agree"], halves=[100 * h[0] for h in r["halves"]], sd_x=sd, f_mean=float(np.mean(wk.F[m])),
                 unit_n="weeks")


# ====================================================== weekday effects (stratified permutation) ======================================================
def _perm_max_stat(stat_fn: Callable[[np.ndarray], float], labels: np.ndarray, strata: np.ndarray, n_perm: int, seed: int) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    obs = stat_fn(labels)
    L = stats.stratified_labels(labels, strata, n_perm, rng)
    null = np.array([stat_fn(L[i]) for i in range(n_perm)])
    return float((1 + np.sum(null >= obs - 1e-12)) / (n_perm + 1)), obs


def _cluster_boot(weeks: np.ndarray, fn: Callable[[np.ndarray], float], n_boot: int, seed: int) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    uw = np.unique(weeks)
    members = [np.where(weeks == w)[0] for w in uw]
    vals = []
    for _ in range(n_boot):
        pick = rng.integers(0, len(uw), len(uw))
        ix = np.concatenate([members[j] for j in pick])
        v = fn(ix)
        if np.isfinite(v):
            vals.append(v)
    if len(vals) < 30:
        return float("nan"), float("nan")
    return float(np.quantile(vals, 0.025)), float(np.quantile(vals, 0.975))


def h_adherence_training(ctx) -> dict | None:
    key = "weekday_adherence:trained>weekday:lag0"
    n = ctx.n
    if n < 42:
        return None
    trained = ctx.trained.astype(float)
    rate = np.array([trained[ctx.dow == d].mean() if (ctx.dow == d).sum() else 0.0 for d in range(7)])
    # the weekdays you usually train on: as many as you say you train per week (profile), else as many as you actually do, taken from the busiest weekdays
    k_plan = None
    try:
        k_plan = int((ctx.profile or {}).get("days_per_week") or 0)
    except (TypeError, ValueError):
        pass
    K = int(np.clip(k_plan if k_plan else round(float(np.sum(trained)) / (n / 7.0)), 3, 6))
    planned = [int(d) for d in np.argsort(-rate, kind="stable")[:K] if rate[d] >= PLANNED_RATE]
    if len(planned) < 3:
        return None
    sel = np.isin(ctx.dow, planned)
    # only complete calendar weeks, one slot per (week, planned weekday)
    wk = ctx.week
    full = np.array([(wk == w).sum() == 7 for w in wk])
    sel &= full
    y = trained[sel]
    lab = ctx.dow[sel]
    st = wk[sel]
    if sel.sum() < stats.min_n("weekday") or len(np.unique(st)) < 5:
        return None

    def worst(labels: np.ndarray) -> float:
        best = -1.0
        for d in planned:
            a = labels == d
            if a.sum() < 4 or (~a).sum() < 4:
                continue
            best = max(best, float(y[~a].mean() - y[a].mean()))
        return best

    p, obs = _perm_max_stat(worst, lab, st, 1999, _seed(key))
    # which weekday, and its uncertainty
    dips = {d: float(y[lab != d].mean() - y[lab == d].mean()) for d in planned if (lab == d).sum() >= 4 and (lab != d).sum() >= 4}
    if not dips:
        return None
    d_star = max(dips, key=dips.get)
    dip = dips[d_star]
    a = lab == d_star
    ids = np.arange(len(y))

    def dip_fn(ix: np.ndarray) -> float:
        aa = lab[ix] == d_star
        return float(y[ix][~aa].mean() - y[ix][aa].mean()) if aa.sum() and (~aa).sum() else float("nan")

    ci = _cluster_boot(st, dip_fn, 400, _seed(key) + 3)
    half = np.array([w <= np.median(st) for w in st])
    hs = []
    for hmask in (half, ~half):
        aa = a & hmask
        bb = ~a & hmask
        hs.append(float(y[bb].mean() - y[aa].mean()) if aa.sum() >= 3 and bb.sum() >= 3 else float("nan"))
    agree = stats.split_half_agree(dip, hs)
    return _base(key, "weekday_adherence", n=int(len(y)), p=p, effect=dip, ci=ci, gate_ci=ci, unit="share of sessions", size=abs(dip), floor=0.20, min_n=24,
                 agree=agree, halves=hs, weekday=int(d_star), rate=float(y[a].mean()), rate_other=float(y[~a].mean()), weeks=int(len(np.unique(st))),
                 planned=planned, unit_n="planned sessions")


def h_adherence_logging(ctx) -> dict | None:
    key = "weekday_adherence:logged>weekday:lag0"
    n = ctx.n
    if n < 42:
        return None
    wk = ctx.week
    full = np.array([(wk == w).sum() == 7 for w in wk])
    y = ctx.A["logged"][full]
    lab = ctx.dow[full]
    st = wk[full]
    if len(y) < stats.min_n("weekday") or len(np.unique(st)) < 5 or y.mean() > 0.97:
        return None

    def worst(labels: np.ndarray) -> float:
        best = -1.0
        for d in range(7):
            a = labels == d
            best = max(best, float(y[~a].mean() - y[a].mean()))
        return best

    p, obs = _perm_max_stat(worst, lab, st, 1999, _seed(key))
    dips = {d: float(y[lab != d].mean() - y[lab == d].mean()) for d in range(7)}
    d_star = max(dips, key=dips.get)
    dip = dips[d_star]
    a = lab == d_star

    def dip_fn(ix: np.ndarray) -> float:
        aa = lab[ix] == d_star
        return float(y[ix][~aa].mean() - y[ix][aa].mean()) if aa.sum() and (~aa).sum() else float("nan")

    ci = _cluster_boot(st, dip_fn, 400, _seed(key) + 3)
    half = st <= np.median(st)
    hs = [float(y[~a & h].mean() - y[a & h].mean()) if (a & h).sum() >= 3 else float("nan") for h in (half, ~half)]
    return _base(key, "weekday_adherence", n=int(len(y)), p=p, effect=dip, ci=ci, gate_ci=ci, unit="share of days", size=abs(dip), floor=0.25, min_n=28,
                 agree=stats.split_half_agree(dip, hs), halves=hs, weekday=int(d_star), rate=float(y[a].mean()), rate_other=float(y[~a].mean()),
                 weeks=int(len(np.unique(st))), unit_n="days", what="logging")


def h_weekday_perf(ctx) -> dict | None:
    key = "weekday_performance:perf>weekday:lag0"
    P = _predictors(ctx)
    adj = [np.where(np.isfinite(v), v, np.nanmean(v) if np.isfinite(v).any() else 0.0) for k, v in P.items() if k != "sleep1"]
    gap = P["gap"]
    m = np.isfinite(ctx.perf) & np.isfinite(gap)
    if m.sum() < stats.min_n("weekday"):
        return None
    N = np.column_stack([ctx.lin] + [a[:, None] for a in adj])[m]
    y = stats.residualize(ctx.perf[m], N)
    lab = ctx.dow[m]
    st = ctx.week[m]
    if len(np.unique(st)) < 5:
        return None
    sd = float(np.std(y))

    def tmax(labels: np.ndarray) -> float:
        best = 0.0
        for d in range(7):
            a = labels == d
            if a.sum() < 4 or (~a).sum() < 4:
                continue
            se = math.sqrt(y[a].var(ddof=1) / a.sum() + y[~a].var(ddof=1) / (~a).sum()) or 1e-9
            best = max(best, abs(float(y[a].mean() - y[~a].mean())) / se)
        return best

    p, obs = _perm_max_stat(tmax, lab, st, 1999, _seed(key))
    effs = {}
    for d in range(7):
        a = lab == d
        if a.sum() >= 4 and (~a).sum() >= 4:
            effs[d] = float(y[a].mean() - y[~a].mean())
    if not effs:
        return None
    d_star = max(effs, key=lambda d: abs(effs[d]))
    eff = 100.0 * effs[d_star]
    a = lab == d_star

    def fn(ix: np.ndarray) -> float:
        aa = a[ix]
        return 100.0 * float(y[ix][aa].mean() - y[ix][~aa].mean()) if aa.sum() and (~aa).sum() else float("nan")

    ci = _cluster_boot(st, fn, 400, _seed(key) + 3)
    half = st <= np.median(st)
    hs = [100.0 * float(y[a & h].mean() - y[~a & h].mean()) if (a & h).sum() >= 3 and (~a & h).sum() >= 3 else float("nan") for h in (half, ~half)]
    return _base(key, "weekday_performance", n=int(m.sum()), p=p, effect=eff, ci=ci, gate_ci=ci, unit="% e1RM", size=abs(eff), floor=1.5, min_n=24,
                 agree=stats.split_half_agree(eff, hs), halves=hs, weekday=int(d_star), n_day=int(a.sum()), unit_n="sessions")


# ====================================================== RPE reporting bias ======================================================
def _rpe_sets(ctx) -> SimpleNamespace | None:
    """Sets with a usable RPE from sessions with >= 2 such sets, plus the implied reps-in-reserve `u` (load and reps given your usual strength)."""
    from backend.tools.calculators import rpe_adjusted_e1rm
    S = [s for s in ctx.sets if s["rpe"] is not None and s["rpe"] >= 5.5 and 1 <= s["reps"] <= 12]     # below RPE 5.5 nobody can judge reps in reserve
    if len(S) < 40:
        return None
    gid, g = {}, []
    for s in S:
        g.append(gid.setdefault((s["date"], s["exercise"]), len(gid)))
    g = np.array(g)
    keep = np.bincount(g)[g] >= 2
    S = [s for s, k in zip(S, keep) if k]
    g = g[keep]
    if len(S) < 40:
        return None
    _, g = np.unique(g, return_inverse=True)
    ng = int(g.max()) + 1
    if ng < 12:
        return None
    e = np.array([rpe_adjusted_e1rm(s["weight"], s["reps"], s["rpe"]) for s in S])
    w = np.array([s["weight"] for s in S])
    reps = np.array([s["reps"] for s in S], float)
    x = np.array([10.0 - s["rpe"] for s in S])
    idx = np.array([s["idx"] for s in S], float)
    y = np.log(e)
    cnt = np.bincount(g, minlength=ng).astype(float)
    # session strength from OTHER sessions of the same lift (within 3 weeks): independent of this session's rating noise
    ex = [s["exercise"] for s in S]
    dord = np.array([date.fromisoformat(s["date"]).toordinal() for s in S])
    sess_e: dict[tuple, float] = {}
    for k in range(ng):
        m = np.where(g == k)[0]
        sess_e[(ex[m[0]], int(dord[m[0]]))] = float(np.median(e[m]))
    def typical(lf: str, d0: int) -> float:
        others = [v for (l2, d2), v in sess_e.items() if l2 == lf and d2 != d0 and abs(d2 - d0) <= 21]
        if len(others) < 2:
            others = [v for (l2, d2), v in sess_e.items() if l2 == lf and d2 != d0]
        return float(np.median(others)) if len(others) >= 2 else float("nan")

    ratio = {k_: v / typical(k_[0], k_[1]) for k_, v in sess_e.items() if np.isfinite(typical(k_[0], k_[1]))}
    c_ref = np.empty(len(S))
    for i in range(len(S)):
        tp = typical(ex[i], int(dord[i]))
        same_day = [r_ for (l2, d2), r_ in ratio.items() if d2 == dord[i] and l2 != ex[i]]
        c_ref[i] = tp * (float(np.median(same_day)) if same_day else 1.0)      # typical strength for this lift x how today's session is going on the other lifts
    ok = np.isfinite(c_ref)
    if ok.sum() < 40:
        return None
    S = [s for s, k in zip(S, ok) if k]
    e, w, reps, x, idx, y, c_ref, g = e[ok], w[ok], reps[ok], x[ok], idx[ok], y[ok], c_ref[ok], g[ok]
    _, g = np.unique(g, return_inverse=True)
    ng = int(g.max()) + 1
    cnt = np.bincount(g, minlength=ng).astype(float)
    keep2 = cnt[g] >= 2
    S = [s for s, k in zip(S, keep2) if k]
    e, w, reps, x, idx, y, c_ref, g = e[keep2], w[keep2], reps[keep2], x[keep2], idx[keep2], y[keep2], c_ref[keep2], g[keep2]
    if len(S) < 40:
        return None
    _, g = np.unique(g, return_inverse=True)
    ng = int(g.max()) + 1
    cnt = np.bincount(g, minlength=ng).astype(float)
    u = 30.0 * c_ref / w - reps                                   # reps in reserve (+30) implied by the load and reps, given your usual strength
    return SimpleNamespace(S=S, e=e, w=w, reps=reps, x=x, idx=idx, y=y, c_ref=c_ref, g=g, u=u)


def _rpe_core(w, reps, x, idx, y, c_ref, g) -> dict | None:
    """The instrumented within-session estimator on one set table: slope of implied-e1RM (log) per reserve-rep REPORTED, instrumented by the reserve the
    load and reps imply given your usual strength. Returns None when the data are too thin or the instrument too weak."""
    u = 30.0 * c_ref / w - reps - 30.0
    # keep sets whose IMPLIED reserve is 0.5-4.5 reps (selection uses the instrument, not the rating)
    ix = np.where((u >= RIR_LO) & (u <= RIR_HI))[0]
    if len(ix) < 40:
        return None
    for _ in range(2):
        _, gg = np.unique(g[ix], return_inverse=True)
        ix = ix[np.bincount(gg)[gg] >= 2]
        if len(ix) < 40:
            return None
    _, gg = np.unique(g[ix], return_inverse=True)
    ng = int(gg.max()) + 1
    if ng < 12:
        return None
    cnt = np.bincount(gg, minlength=ng).astype(float)

    def demean(v: np.ndarray) -> np.ndarray:
        return v - (np.bincount(gg, weights=v, minlength=ng) / cnt)[gg]

    D = np.column_stack([demean(idx[ix]), demean(idx[ix] ** 2)])
    xr, yr, ur = (stats.residualize(demean(v[ix]), D) for v in (x, y, u))
    yr = stats.winsorize(yr, 4.0)
    ur = stats.winsorize(ur, 4.0)
    sux = float((ur * xr).sum())
    suu = float((ur * ur).sum())
    if np.std(ur) < 1e-9 or sux / suu < 0.2:        # weak instrument: the load/reps tell us too little about effort
        return None
    return {"ix": ix, "g": gg, "ng": ng, "xr": xr, "yr": yr, "ur": ur, "slope": float((ur * yr).sum() / sux), "fs": sux / suu,
            "vx": float(np.var(xr)), "vy": float(np.var(yr))}


def _rpe_e1rm(w: np.ndarray, reps: np.ndarray, x: np.ndarray) -> np.ndarray:
    """log of the Epley max implied by a set at `x` reserve reps (what backend.tools.calculators.rpe_adjusted_e1rm computes)."""
    n = np.round(reps + x)
    return np.log(np.where(n <= 1, w, np.round(w * (1.0 + n / 30.0), 1)))


def _rpe_honest(d: SimpleNamespace, sig_s: float, sig_r: float, phi: float, a_g: np.ndarray, rng: np.random.Generator) -> dict | None:
    """One synthetic HONEST version of this athlete's sets: same loads, reps, sessions and usual-strength reference, true capacity = the session's level less
    fatigue plus small set-to-set noise, ratings = true reserve + rating noise, rounded to halves and floored at failure (RPE 10), and a set whose capacity
    falls short of the planned reps stops early. Run through the very same estimator, it shows what an honest rater's slope looks like with THIS design."""
    n = len(d.w)
    cap = np.exp(a_g[d.g] - phi * d.idx + rng.normal(0.0, sig_s, n))
    T = 30.0 * (cap / d.w - 1.0)
    reps = np.where(T < d.reps, np.maximum(1.0, np.floor(T)), d.reps)
    r = np.maximum(0.0, T - reps)
    xs = np.maximum(0.0, np.round((r + rng.normal(0.0, sig_r, n)) * 2.0) / 2.0)
    keep = xs <= 4.5                                 # a reported RPE below 5.5 is dropped, as in the real table
    if keep.sum() < 40:
        return None
    ys = _rpe_e1rm(d.w, reps, xs)
    return _rpe_core(d.w[keep], reps[keep], xs[keep], d.idx[keep], ys[keep], d.c_ref[keep], d.g[keep])


def _rpe_null(d: SimpleNamespace, obs: dict, n_sim: int, seed: int) -> tuple[float, float, np.ndarray] | None:
    """Calibrated null for the slope: (mean, sd, draws) of the instrumented slope for an honest rater with this athlete's own noise. The two noise levels
    (set-to-set capacity noise, rating noise) are matched to the spread of the observed implied maxes and ratings."""
    ng = int(d.g.max()) + 1
    cnt = np.bincount(d.g, minlength=ng).astype(float)
    ix0 = d.idx - (np.bincount(d.g, weights=d.idx, minlength=ng) / cnt)[d.g]
    y0 = d.y - (np.bincount(d.g, weights=d.y, minlength=ng) / cnt)[d.g]
    phi = float(np.clip(-(ix0 * y0).sum() / max(float((ix0 * ix0).sum()), 1e-9), 0.0, 0.05))
    a_g = np.bincount(d.g, weights=d.y + phi * d.idx, minlength=ng) / cnt
    cache: dict[tuple[float, float], tuple[float, float]] = {}

    def stat(ss: float, sr: float) -> tuple[float, float]:
        if (ss, sr) not in cache:
            rs = [q for q in (_rpe_honest(d, ss, sr, phi, a_g, np.random.default_rng(seed + j)) for j in range(2)) if q]
            cache[(ss, sr)] = (float(np.mean([q["vx"] for q in rs])), float(np.mean([q["vy"] for q in rs]))) if rs else (float("nan"), float("nan"))
        return cache[(ss, sr)]

    def loss(ss: float, sr: float) -> float:
        vx, vy = stat(ss, sr)
        return math.log(vx / obs["vx"]) ** 2 + math.log(vy / obs["vy"]) ** 2 if np.isfinite(vx) and np.isfinite(vy) and vx > 0 and vy > 0 else float("inf")

    # the rating noise sets the spread of the ratings, the capacity noise adds to the spread of the implied maxes: a small grid over both
    ss, sr = min(((a_, b_) for a_ in (0.0, 0.003, 0.006, 0.01, 0.015, 0.022) for b_ in (0.1, 0.2, 0.3, 0.45, 0.6, 0.8, 1.0, 1.3, 1.7, 2.2)), key=lambda sv: loss(*sv))
    draws = [q["slope"] for j in range(n_sim) if (q := _rpe_honest(d, ss, sr, phi, a_g, np.random.default_rng(seed + 1000 + j)))]
    if len(draws) < max(20, n_sim // 2):
        return None
    dr = np.array(draws)
    return float(dr.mean()), float(max(dr.std(ddof=1), 1e-6)), dr


def h_rpe_bias(ctx, n_sim: int = 120) -> dict | None:
    """Do your easy-rated sets imply a bigger max than your hard-rated sets from the SAME session? If you rate effort easier than it was, the reps-in-reserve
    you report inflate the e1RM of light sets. Naively regressing the implied e1RM on the reported RPE would flag even an honest athlete (a noisy or rounded
    rating is part of both sides), so the reported RPE is instrumented with what the load and reps themselves say about effort.
    That still leaves a small negative offset for an honest rater (measured: about -0.1 % per RPE point, growing with rating noise): RPE cannot go past
    failure (10), so near-failure ratings are censored, and a set that falls short of its planned reps stops early, which ties the reps (in the instrument)
    to the set's own strength (in the outcome). The p-value and the reported effect are therefore CALIBRATED: they are measured against honest synthetic
    versions of this very athlete's sets (same loads, reps and sessions, noise levels matched to the data), not against zero."""
    d = _rpe_sets(ctx)
    if d is None:
        return None
    key = "rpe_bias:rpe>implied_e1rm:within_session"
    S, w, e = d.S, d.w, d.e
    obs = _rpe_core(d.w, d.reps, d.x, d.idx, d.y, d.c_ref, d.g)
    if obs is None:
        return None
    nl = _rpe_null(d, obs, n_sim, _seed(key))
    if nl is None:
        return None
    m0, s0, draws = nl
    ix, ur, xr, yr, gg, ng = obs["ix"], obs["ur"], obs["xr"], obs["yr"], obs["g"], obs["ng"]
    raw = obs["slope"]
    slope = raw - m0                                                # offset removed: an honest rater is centred on zero
    p = float((1 + np.sum(np.abs(draws - m0) >= abs(raw - m0) - 1e-12)) / (len(draws) + 1))
    rng = np.random.default_rng(_seed(key) + 7)
    members = [np.where(gg == k)[0] for k in range(ng)]
    boots = []
    for _ in range(300):
        pick = rng.integers(0, ng, ng)
        jx = np.concatenate([members[j] for j in pick])
        dd = float((ur[jx] * xr[jx]).sum())
        if dd > 0:
            boots.append(float((ur[jx] * yr[jx]).sum() / dd))
    if len(boots) < 100:
        return None
    # the simulated spread omits some real-data variance (the usual-strength reference is itself estimated): never trust fewer standard errors than the
    # observed cluster bootstrap gives
    se_b = float(np.std(boots, ddof=1))
    z = abs(raw - m0) / max(se_b, s0, 1e-9)
    p_norm = float(math.erfc(z / math.sqrt(2)))             # smooth in the tail (a rank p over ~100 draws cannot go below 0.01)
    p = p_norm if p_norm < 0.05 else max(p, p_norm)
    ci = (100 * (float(np.quantile(boots, 0.025)) - m0), 100 * (float(np.quantile(boots, 0.975)) - m0))
    Sk = [S[i] for i in ix]
    dates = np.array([s["date"] for s in Sk])
    med = np.sort(np.unique(dates))[len(np.unique(dates)) // 2]
    hs = []
    for h in (dates < med, dates >= med):
        dd = float((ur[h] * xr[h]).sum())
        hs.append(100 * (float((ur[h] * yr[h]).sum() / dd) - m0) if h.sum() >= 30 and dd > 0 else float("nan"))
    eff = 100 * slope
    fbar = float(np.mean(w[ix] / e[ix]))
    reps_bias = 60.0 * slope / max(fbar, 0.3)                   # reps of reserve over-estimated at a reported RPE 8 (RIR 2); b/(1+b) = 30 s / f
    return _base(key, "rpe_bias", n=int(len(ix)), sessions=ng, p=p, effect=eff, ci=ci, gate_ci=ci, unit="% e1RM per RPE point easier", size=abs(eff), floor=0.5,
                 min_n=40, agree=stats.split_half_agree(eff, hs), halves=hs, reps_bias=reps_bias, unit_n="sets",
                 null_offset=100 * m0, null_sd=100 * s0)


# ====================================================== nutrition and recovery ======================================================
def h_protein(ctx) -> dict | None:
    A = ctx.A
    key = "protein_recovery:protein_g>soreness:lag1"
    prot = np.where(A["complete"] > 0, A["protein_g"], np.nan)
    x = prot
    y = np.append(A["soreness"][1:], np.nan)          # tomorrow's soreness
    trained = A["trained"]
    covs = [A["n_sets"], trained, A["soreness"]]
    N = np.column_stack([ctx.trend, ctx.dowd] + [c[:, None] for c in covs])
    m = _used_mask(x, y, N)
    if m.sum() < 24:
        return None
    r = _pt(ctx, key, x, y, N)
    if not r["ok"]:
        return None
    sd = float(np.std(x[m]))
    eff = 25.0 * r["slope"]
    ci = (25.0 * r["ci"][0], 25.0 * r["ci"][1])
    return _base(key, "protein_recovery", n=int(m.sum()), n_eff=r["n_eff"], p=r["p"], effect=eff, ci=ci, gate_ci=ci, unit="soreness points per +25 g protein",
                 size=abs(r["slope"]) * sd, floor=0.10, min_n=24, agree=r["agree"], halves=[25 * h[0] for h in r["halves"]], sd_x=sd, unit_n="days")


def h_late_meals(ctx) -> dict | None:
    A = ctx.A
    key = "late_meals_sleep:late_meal>sleep_h:lag0"
    late = np.where(A["meals_n"] >= 2, A["late_meal"], np.nan)
    x = _shift(late, 1)                                  # last night's dinner -> this morning's sleep
    y = A["sleep_h"]
    N = np.column_stack([ctx.trend, ctx.dowd])
    m = _used_mask(x, y, N)
    if m.sum() < 24 or (x[m] == 1).sum() < 8 or (x[m] == 0).sum() < 8:
        return None
    r = _pt(ctx, key, x, y, N)
    if not r["ok"]:
        return None
    return _base(key, "late_meals_sleep", n=int(m.sum()), n_eff=r["n_eff"], p=r["p"], effect=r["slope"], ci=r["ci"], gate_ci=r["ci"], unit="hours of sleep",
                 size=abs(r["slope"]), floor=0.25, min_n=24, agree=r["agree"], halves=[h[0] for h in r["halves"]], n_late=int((x[m] == 1).sum()),
                 unit_n="nights", late_hour=frames.LATE_HOUR)


def formula_tdee(profile: dict | None, weight: float) -> float | None:
    from backend.tools import calculators as calc
    try:
        return float(calc.tdee(calc.mifflin_st_jeor(weight, float(profile["height_cm"]), int(profile["age"]), str(profile.get("sex", "m"))),
                               profile.get("activity", "moderate")))
    except Exception:
        return None


def h_maintenance(ctx) -> dict | None:
    key = "maintenance_calories:intake_vs_weight>tdee:window"
    if not ctx.profile:
        return None
    n = ctx.n
    W = min(n, 56)
    sl = slice(n - W, n)
    bw, kc, comp = ctx.A["bodyweight"][sl], ctx.A["kcal"][sl], ctx.A["complete"][sl] > 0
    td = np.arange(W, dtype=float)
    wmask = np.isfinite(bw)
    kmask = comp & np.isfinite(kc)
    if kmask.sum() < 28 or kmask.sum() < 0.6 * W or wmask.sum() < 12 or (td[wmask].max() - td[wmask].min()) < 24:
        return None
    wbar = float(np.mean(bw[wmask]))
    f = formula_tdee(ctx.profile, wbar)
    if f is None:
        return None

    def est(ix: np.ndarray) -> float:
        wi = ix[wmask[ix]]
        ki = ix[kmask[ix]]
        if len(wi) < 8 or len(ki) < 14 or np.ptp(td[wi]) < 14:
            return float("nan")
        # windsorise water-weight spikes through a robust line
        s, _ = stats.theil_sen(td[wi], bw[wi])
        return float(np.mean(kc[ki]) - KCAL_PER_KG * s)

    full = np.arange(W)
    e_all = est(full)
    if not np.isfinite(e_all):
        return None
    rng = np.random.default_rng(_seed(key))
    b = 7
    nb = int(math.ceil(W / b))
    vals = []
    for _ in range(400):
        st = rng.integers(0, W - b + 1, size=nb)
        ix = (st[:, None] + np.arange(b)[None, :]).ravel()[:W]
        v = est(ix)
        if np.isfinite(v):
            vals.append(v)
    if len(vals) < 100:
        return None
    sd = float(np.std(vals))
    diff = e_all - f
    z = diff / max(sd, 1e-9)
    p = stats.t_two_sided_p(z, max(3.0, W / 7.0 - 1))
    ci = (float(np.quantile(vals, 0.025)) - f, float(np.quantile(vals, 0.975)) - f)
    h = W // 2
    hs = [est(np.arange(0, h)) - f, est(np.arange(h, W)) - f]
    return _base(key, "maintenance_calories", n=int(kmask.sum()), p=float(p), effect=float(diff), ci=ci, gate_ci=ci, unit="kcal per day", size=abs(diff), floor=150.0,
                 min_n=28, agree=stats.split_half_agree(diff, hs), halves=hs, estimate=float(e_all), formula=float(f), se=sd, weigh_ins=int(wmask.sum()), window=W,
                 unit_n="logged days")


# ====================================================== deload and PRs ======================================================
def h_deload(ctx) -> dict | None:
    wk = _weekly(ctx)
    key = "deload_response:deload_week>perf_next_week:lag1w"
    if wk is None or len(wk.V) < 8:
        return None
    V = wk.V
    deload = np.zeros(len(V), bool)
    for i in range(2, len(V)):
        lo = 3 if (i >= 3 and wk.W[i] - wk.W[i - 3] == 3) else 2
        prev = V[i - lo:i]
        if wk.W[i] - wk.W[i - lo] == lo and np.all(prev > 0) and V[i] <= 0.7 * prev.mean():
            deload[i] = True
    post = np.zeros(len(V), bool)
    for i in np.where(deload)[0]:
        if i + 1 < len(V) and wk.W[i + 1] == wk.W[i] + 1:
            post[i + 1] = True
    perf = ctx.perf
    wk_of_day = ctx.week
    wmean = np.full(len(V), np.nan)
    wcnt = np.zeros(len(V))
    for j, w in enumerate(wk.W):
        v = perf[wk_of_day == w]
        v = v[np.isfinite(v)]
        if len(v):
            wmean[j] = float(v.mean())
            wcnt[j] = len(v)
    elig = np.isfinite(wmean) & (wcnt >= 2) & ~deload
    k = int((post & elig).sum())
    if k < 3 or (elig & ~post).sum() < 6:
        return None
    pm = wmean[post & elig]
    om = wmean[elig & ~post]
    diff = float(pm.mean() - om.mean())
    rng = np.random.default_rng(_seed(key))
    ev = np.where(elig)[0]
    null = np.empty(3999)
    for i in range(3999):
        pick = rng.choice(len(ev), k, replace=False)
        sel = np.zeros(len(ev), bool)
        sel[pick] = True
        null[i] = wmean[ev][sel].mean() - wmean[ev][~sel].mean()
    p = float((1 + np.sum(np.abs(null) >= abs(diff) - 1e-12)) / 4000)
    se = math.sqrt(pm.var(ddof=1) / k + om.var(ddof=1) / len(om)) if k > 1 else float("nan")
    tc = stats.t_ppf_two_sided(0.95, max(1.0, k - 1))
    ci = (100 * (diff - tc * se), 100 * (diff + tc * se))
    agree = float(np.mean(pm > om.mean())) >= 0.67 if diff > 0 else float(np.mean(pm < om.mean())) >= 0.67
    return _base(key, "deload_response", n=k, p=p, effect=100 * diff, ci=ci, gate_ci=ci, unit="% e1RM in the week after a lighter week", size=abs(100 * diff),
                 floor=1.5, min_n=3, agree=bool(agree), halves=[], unit_n="lighter weeks")


def h_prs(ctx) -> list[dict]:
    """What preceded personal records: sleep, energy and soreness on PR days versus other training days (detrended)."""
    out = []
    n = ctx.n
    pr = np.zeros(n)
    for k in ctx.lifts:
        v = _clean_series(frames.arrays(ctx.rows, [k])[k])
        best, cnt = -np.inf, 0
        for i in range(n):
            if not np.isfinite(v[i]):
                continue
            if cnt >= 4 and v[i] >= 1.01 * best:
                pr[i] = 1
            best = max(best, v[i])
            cnt += 1
    tr = ctx.trained
    if pr.sum() < 5:
        return out
    x = np.where(tr, pr, np.nan)
    N = np.column_stack([ctx.trend, ctx.dowd])
    for name, col, floor, unit, noun in (("sleep_h", "sleep_h", 0.3, "hours of sleep", "sleep"), ("energy", "energy", 0.3, "energy points", "energy"),
                                         ("soreness", "soreness", 0.3, "soreness points", "soreness")):
        key = f"pr_precursors:{name}>pr:lag0"
        y = ctx.A[col]
        m = _used_mask(x, y, N)
        if m.sum() < 20 or (x[m] == 1).sum() < 5:
            continue
        r = _pt(ctx, key, x, y, N)
        if not r["ok"]:
            continue
        out.append(_base(key, "pr_precursors", n=int((x[m] == 1).sum()), n_all=int(m.sum()), n_eff=r["n_eff"], p=r["p"], effect=r["slope"], ci=r["ci"], gate_ci=r["ci"],
                         unit=unit, size=abs(r["slope"]), floor=floor, min_n=5, agree=r["agree"], halves=[h[0] for h in r["halves"]], var=noun, unit_n="personal records"))
    return out


# ====================================================== the registry ======================================================
def _hypotheses() -> list[Callable[[Any], Any]]:
    return [lambda c: h_sleep(c, 0), lambda c: h_sleep(c, 1), h_soreness, h_energy, h_volume_linear, h_volume_sweet, h_frequency, h_adherence_training,
            h_adherence_logging, h_weekday_perf, h_rest_gap, h_hrv, h_rhr, h_steps, h_rpe_bias, h_protein, h_maintenance, h_late_meals, h_bodyweight, h_deload, h_prs]


def trim_leading(rows: list[dict]) -> list[dict]:
    """Drop the calendar days before the athlete's first log (a new user's window is mostly empty; those days are 'not on the app yet', not 'rest days')."""
    for i, r in enumerate(rows):
        if r["trained"] or r["logged"] or r["meals_n"]:
            return rows[i:]
    return []


def analyze(days: int = DAYS, end: str | None = None, *, n_perm: int = 1999, n_boot: int = 400, rows: list[dict] | None = None, sets: list[dict] | None = None,
            profile: dict | None = None) -> list[dict]:
    """Test every hypothesis (pure: nothing is written). Each result carries p, q (BH over all that ran) and `gates` / `decision`."""
    rows = rows if rows is not None else frames.build(days, end)
    sets = sets if sets is not None else frames.set_rows(days, end)
    profile = profile if profile is not None else db.get_profile()
    rows = trim_leading(rows)
    sets = [s for s in sets if rows and s["date"] >= rows[0]["date"]]
    ctx = make_ctx(rows, sets, profile, n_perm, n_boot)
    results: list[dict] = []
    for fn in _hypotheses():
        try:
            r = fn(ctx)
        except Exception as e:          # one broken test never stops the others
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"[:120]}
        if r is None:
            continue
        results.extend(r if isinstance(r, list) else [r])
    results = [r for r in results if r.get("ok")]
    qs = stats.bh_fdr([r["p"] for r in results])
    for r, q in zip(results, qs):
        r["q"] = q
        r["span"] = len(rows)
        _gates(r)
    return results


def _gates(r: dict) -> None:
    ci = r.get("gate_ci") or (float("nan"), float("nan"))
    ci_ok = bool(np.all(np.isfinite(ci)) and ci[0] * ci[1] > 0)
    sign = r.get("sign_override") or (1 if (r["effect"] or 0) >= 0 else -1)
    exp = EXPECTED_SIGN.get(r["family"])
    plausible = (exp is None) or (sign == exp)
    if "direction_ok" in r and not r["direction_ok"]:
        plausible = False
    enough = r["n"] >= r["min_n"]
    big = r["size"] >= r["floor"]
    r["gates"] = {"q": r["q"] < Q_CONFIRM, "span": r.get("span", MIN_SPAN_CONFIRM) >= MIN_SPAN_CONFIRM, "n": enough, "size": big, "ci": ci_ok, "replicates": bool(r["agree"]), "plausible": plausible}
    r["gates_keep"] = {"q": r["q"] < Q_KEEP, "size": r["size"] >= 0.6 * r["floor"], "replicates": bool(r["agree"]), "plausible": plausible}
    r["passes"] = all(r["gates"].values())
    r["candidate_ok"] = r["q"] < Q_CANDIDATE and r["size"] >= 0.5 * r["floor"] and r["n"] >= 0.7 * r["min_n"] and (plausible or r["q"] < Q_PLAUSIBLE_OVERRIDE)
    r["sign"] = sign


# ====================================================== plain-language rendering (numbers from code, no model) ======================================================
def _tail(r: dict, show_n: bool = True) -> str:
    noun = r.get("unit_n", "sessions")
    base = f"{r['n']} {noun}; " if show_n else ""
    return base + ("the same direction shows in both halves of your history" if r["agree"] else "not yet the same in both halves of your history")


def _lifts_txt(r: dict) -> str:
    ls = r.get("lifts") or []
    return ", ".join(ls[:3]) if ls else "your lifts"


def _pct(x: float, nd: int = 1) -> str:
    return f"{abs(x):.{nd}f}%"


def render(r: dict) -> dict:
    """{title, statement, recommendation, consumers, trait} for one result."""
    fam, e = r["family"], r["effect"]
    up = e >= 0
    if fam == "sleep_performance":
        lag = int(r["label"][-1])
        when = "the morning you train" if lag == 0 else "the day before you train"
        c = r.get("contrast")
        ctr = f" (about {_pct(c)} {'higher' if c >= 0 else 'lower'} after {r['q_cut_hi']:.1f}+ h than after under {r['q_cut_lo']:.1f} h)" if c is not None else ""
        verb = "better" if up else "worse"
        st = (f"The more you sleep {'before' if lag == 0 else 'the night before the day before'} a session, the {verb} your lifts go: each extra hour of sleep goes with about "
              f"{'+' if up else '-'}{_pct(e)} on your estimated max for {_lifts_txt(r)}{ctr} ({_tail(r)}).") if lag == 0 else \
             (f"Sleep carries over: after a night with more sleep, your lifts the NEXT day go {verb} by about {'+' if up else '-'}{_pct(e)} per extra hour{ctr} ({_tail(r)}).")
        rec = (f"Protect sleep before your hardest sessions: aim for {r['q_cut_hi']:.1f}+ h. If you slept under {r['q_cut_lo']:.1f} h, expect a lower number and do not chase a record that day."
               if up else "")
        return {"title": "Sleep and your lifts" + ("" if lag == 0 else " (next day)"), "statement": st, "recommendation": rec, "consumers": ["training", "recovery", "coach"],
                "trait": ("response_profile", f"sleep->performance lag{lag}", e, "% e1RM per hour of sleep")}
    if fam in ("energy_performance", "soreness_performance"):
        what = "energy" if fam == "energy_performance" else "soreness"
        st = (f"Your lifts follow your {what} rating: each extra point of {what} goes with about {_pct(e)} {'more' if up else 'less'} on your estimated max "
              f"(for {_lifts_txt(r)}; {_tail(r)}).")
        rec = (f"Use your morning {what} rating to decide push or hold: at {'2/5 or lower' if what == 'energy' else '4/5 or higher'}, keep the planned load and skip max attempts."
               if r["passes"] else "")
        return {"title": f"{what.capitalize()} and your lifts", "statement": st, "recommendation": rec, "consumers": ["recovery", "training", "coach"],
                "trait": ("response_profile", f"{what}->performance", e, f"% e1RM per {what} point")}
    if fam in ("hrv_performance", "rhr_performance", "steps_performance"):
        what, per = {"hrv_performance": ("HRV", "10 ms"), "rhr_performance": ("resting heart rate", "1 bpm"),
                     "steps_performance": ("the previous day's steps", "1,000 steps")}[fam]
        eff = e
        st = (f"Your lifts follow {what}: each extra {per} goes with about {_pct(eff)} {'more' if eff >= 0 else 'less'} on your estimated max "
              f"(for {_lifts_txt(r)}; {_tail(r)}).")
        rec = {"hrv_performance": "Treat a low morning HRV as a cue to hold the planned load rather than chase a max.",
               "rhr_performance": "Treat a morning resting heart rate well above your normal as a cue to hold the planned load.",
               "steps_performance": "Keep the day before a heavy session lighter on steps." if eff < 0 else ""}[fam] if r["passes"] else ""
        return {"title": f"{what[0].upper() + what[1:]} and your lifts", "statement": st, "recommendation": rec, "consumers": ["recovery", "training", "coach"],
                "trait": ("response_profile", f"{fam.split('_')[0]}->performance", eff, f"% e1RM per {per}")}
    if fam == "volume_response":
        if r["key"].endswith("sweet_spot"):
            lo, hi = r["ci"]
            span = f"somewhere around {lo:.0f}-{hi:.0f}" if np.isfinite(lo) and np.isfinite(hi) else "close to"
            st = (f"Your strength progress has been best in weeks with {span} {'sets' if np.isfinite(lo) else 'about ' + format(e, '.0f') + ' sets'} (about {e:.0f} is the peak); "
                  f"weeks well above or below that were followed by slower progress ({_tail(r)}).")
            if not np.isfinite(lo):
                st = (f"Your strength progress has been best in weeks with about {e:.0f} working sets; weeks well above or below that were followed by slower progress ({_tail(r)}).")
            rec = f"Aim for roughly {e:.0f} working sets a week; adding more beyond that has not paid off for you so far."
            return {"title": "Your volume sweet spot", "statement": st, "recommendation": rec, "consumers": ["training"],
                    "trait": ("response_profile", "volume sweet spot", e, "working sets per week")}
        st = (f"Weeks with more sets were followed by {'faster' if up else 'slower'} progress: each extra 10 working sets a week goes with about {_pct(e, 2)} "
              f"{'more' if up else 'less'} strength progress the next week ({_tail(r)}).")
        rec = "Lean toward the higher end of your usual weekly volume while recovery stays good." if up else "More sets has not helped you; hold volume steady and add quality instead."
        return {"title": "Volume and progress", "statement": st, "recommendation": rec, "consumers": ["training"], "trait": ("response_profile", "volume linear", e, "% per 10 sets")}
    if fam == "frequency_response":
        st = (f"Weeks with more sessions were followed by {'faster' if up else 'slower'} progress: each extra weekly session goes with about {_pct(e, 2)} "
              f"{'more' if up else 'less'} strength progress the next week ({_tail(r)}).")
        rec = "A fourth or fifth session has been worth it for you; protect the extra day." if up else "More sessions did not speed you up; keep the days you can recover from."
        return {"title": "Training frequency", "statement": st, "recommendation": rec, "consumers": ["training"], "trait": ("response_profile", "frequency", e, "% per session/week")}
    if fam == "weekday_adherence":
        day = WEEKDAYS[r["weekday"]]
        if r.get("what") == "logging":
            st = (f"You log a check-in on {r['rate'] * 100:.0f}% of {day}s versus {r['rate_other'] * 100:.0f}% of other days ({_tail(r)}).")
            rec = f"Set a {day} reminder for your check-in, or make it a one-tap entry."
            return {"title": f"{day} check-ins", "statement": st, "recommendation": rec, "consumers": ["adherence", "coach"],
                    "trait": ("adherence_pattern", f"logging dips on {day}", r["rate"], "share of days")}
        st = (f"You train on {r['rate'] * 100:.0f}% of your {day}s versus {r['rate_other'] * 100:.0f}% of your other usual training days ({_tail(r)}).")
        rec = f"Plan {day} as a short or optional session, or move your key lift to a day you reliably show up."
        return {"title": f"{day} is your weak day", "statement": st, "recommendation": rec, "consumers": ["adherence", "training", "coach"],
                "trait": ("adherence_pattern", f"{day} sessions are often skipped", r["rate"], "share of sessions")}
    if fam == "weekday_performance":
        day = WEEKDAYS[r["weekday"]]
        st = f"Your {day} sessions run about {_pct(e)} {'stronger' if up else 'weaker'} than your other days, after accounting for your progress and rest days ({_tail(r)})."
        rec = (f"Do not schedule a max attempt on {day}; put the top set on another day." if not up else f"{day} is your best day for top sets: schedule heavy work there.")
        return {"title": f"{day} sessions", "statement": st, "recommendation": rec, "consumers": ["training"], "trait": ("response_profile", f"{day} performance", e, "% e1RM")}
    if fam == "rest_gap":
        st = (f"Rest before a session matters: each extra day since your last session goes with about {'+' if up else '-'}{_pct(e)} on your estimated max ({_tail(r)}).")
        rec = ("Give the hardest lift a rest day before it." if up else "Longer gaps have not helped you; keep sessions spaced evenly.")
        return {"title": "Rest days", "statement": st, "recommendation": rec, "consumers": ["training", "recovery"], "trait": ("recovery_profile", "rest gap", e, "% e1RM per rest day")}
    if fam == "rpe_bias":
        rb = r["reps_bias"]
        st = (f"Your sets rated one point easier on RPE imply an estimated max about {_pct(abs(e), 2)} {'higher' if up else 'lower'}, within the same session: a sign you rate "
              f"effort {'easier' if up else 'harder'} than it was, by roughly {abs(rb):.1f} rep(s) of reserve at RPE 8 ({_tail(r)}).")
        rec = f"Treat your RPE 8 as closer to 9 when planning loads; the coach will discount your easy ratings by about {abs(rb):.1f} rep." if up else \
              "You tend to rate effort harder than it was; the coach will nudge loads up slightly."
        return {"title": "How you rate effort", "statement": st, "recommendation": rec, "consumers": ["training", "coach", "live"],
                "trait": ("calibration_bias", "rpe underrates effort" if up else "rpe overrates effort", rb, "reps of reserve at RPE 8")}
    if fam == "protein_recovery":
        st = (f"The day after you eat more protein you report {'less' if e < 0 else 'more'} soreness: about {abs(e):.2f} points per +25 g of protein ({_tail(r)}).")
        rec = "Keep protein steady on hard training days, especially before leg days." if e < 0 else ""
        return {"title": "Protein and soreness", "statement": st, "recommendation": rec, "consumers": ["nutrition", "recovery"],
                "trait": ("recovery_profile", "protein->next-day soreness", e, "soreness points per 25 g")}
    if fam == "late_meals_sleep":
        st = (f"On nights after your last meal is past {r['late_hour']:.0f}:00 you sleep about {abs(e):.1f} h {'less' if e < 0 else 'more'} ({r['n_late']} late nights out of {r['n']}; {_tail(r, False)}).")
        rec = f"Finish dinner before {r['late_hour']:.0f}:00 on the nights before hard sessions." if e < 0 else ""
        return {"title": "Late meals and sleep", "statement": st, "recommendation": rec, "consumers": ["nutrition", "recovery"],
                "trait": ("recovery_profile", "late meals shorten sleep", e, "hours of sleep")}
    if fam == "maintenance_calories":
        d = r["effect"]
        st = (f"Your body behaves as if your maintenance is about {r['estimate']:,.0f} kcal a day, {abs(d):,.0f} {'above' if d > 0 else 'below'} the {r['formula']:,.0f} the "
              f"formula gives (from your last {r['window']} days of weight and food logs; {_tail(r)}).")
        rec = f"Base calorie targets on about {r['estimate']:,.0f} kcal instead of {r['formula']:,.0f}."
        return {"title": "Your real maintenance calories", "statement": st, "recommendation": rec, "consumers": ["nutrition", "coach"],
                "trait": ("maintenance_kcal", "maintenance", r["estimate"], "kcal per day")}
    if fam == "bodyweight_strength":
        st = f"Your lifts track your bodyweight: each extra kg goes with about {'+' if up else '-'}{_pct(e)} on your estimated max, beyond your usual progress ({_tail(r)})."
        rec = "Expect strength to move with weight changes; judge a cut or bulk by lifts relative to bodyweight." if up else ""
        return {"title": "Bodyweight and strength", "statement": st, "recommendation": rec, "consumers": ["training", "nutrition"],
                "trait": ("response_profile", "bodyweight->strength", e, "% e1RM per kg")}
    if fam == "deload_response":
        st = (f"After {r['n']} lighter weeks, your lifts the following week ran about {_pct(e)} {'higher' if up else 'lower'} than usual ({_tail(r)}).")
        rec = "A lighter week pays off for you; schedule one every few weeks." if up else "Lighter weeks have not led to a rebound for you; deload only when readiness says so."
        return {"title": "Deload response", "statement": st, "recommendation": rec, "consumers": ["training", "recovery"],
                "trait": ("recovery_profile", "deload rebound", e, "% e1RM")}
    if fam == "pr_precursors":
        var = r["var"]
        word = {"sleep": f"{abs(e):.1f} h {'more' if e > 0 else 'less'} sleep", "energy": f"{abs(e):.1f} points {'higher' if e > 0 else 'lower'} energy",
                "soreness": f"{abs(e):.1f} points {'more' if e > 0 else 'less'} soreness"}[var]
        st = f"Your {r['n']} personal records came on days with {word} than your other sessions ({_tail(r)})."
        rec = {"sleep": "Line up your next record attempt after a well-slept night.", "energy": "Save record attempts for days your energy is high.",
               "soreness": "Save record attempts for fresh days."}[var]
        return {"title": f"What came before your records ({var})", "statement": st, "recommendation": rec, "consumers": ["training", "coach"],
                "trait": ("response_profile", f"records follow {var}", e, r["unit"])}
    return {"title": fam, "statement": "", "recommendation": "", "consumers": [], "trait": None}


# ====================================================== persistence ======================================================
def _effect_json(r: dict, data_through: str) -> dict:
    out = {"family": r["family"], "estimate": round(float(r["effect"]), 4), "unit": r["unit"], "ci": [None if not np.isfinite(v) else round(float(v), 4) for v in r["ci"]],
           "p": round(float(r["p"]), 5), "q": round(float(r["q"]), 5), "n": int(r["n"]), "size": round(float(r["size"]), 4), "floor": r["floor"], "min_n": r["min_n"],
           "replicated": bool(r["agree"]), "halves": [None if not np.isfinite(h) else round(float(h), 4) for h in r.get("halves", [])], "data_through": data_through, "gates": r["gates"]}
    for k in ("n_eff", "per_sd", "contrast", "q_cut_lo", "q_cut_hi", "estimate", "formula", "weekday", "rate", "rate_other", "reps_bias", "v_mean", "interior", "se", "window"):
        if k in r and r[k] is not None:
            out[k] = (round(float(r[k]), 4) if isinstance(r[k], (float, np.floating)) else r[k])
    return out


def _existing_fail(prev: dict | None) -> int:
    if not prev:
        return 0
    eff = prev.get("effect") or {}
    return int(eff.get("fails", 0)) if isinstance(eff, dict) else 0


def decide(r: dict, prev: dict | None) -> str | None:
    """New status for this hypothesis given its stored row (or None = leave it alone)."""
    old = prev["status"] if prev else None
    if r["passes"]:
        return "confirmed"
    if old == "confirmed" and all(r["gates_keep"].values()) and r["gates"]["n"]:
        return "confirmed"
    fails = _existing_fail(prev) + 1
    if old in ("confirmed", "weakening"):
        return "retired" if fails >= 3 else "weakening"
    if old == "candidate":
        return "candidate" if r["candidate_ok"] else ("retired" if fails >= 3 else "candidate")
    if old == "retired":
        return None
    return "candidate" if r["candidate_ok"] else None


def _write_trait(trait: tuple | None, active: bool, body: str, key: str) -> bool:
    if not trait:
        return False
    pred, obj, val, unit = trait
    ex = [f for f in store.facts(predicate=pred, source="derived", status=("active",), limit=100) if f["object_key"] == store.norm(obj)]
    if active:
        if ex:
            store.update_fact(ex[0]["id"], value_num=float(val), body=body)
            return False
        res = store.add_fact(pred, obj, body=body, value_num=float(val), unit=unit, kind="trait", source="derived", evidence=[{"t": "discovery", "key": key}])
        return bool(res.get("created"))
    for f in ex:
        store.update_fact(f["id"], status="archived")
    return False


def run(force: bool = False) -> dict:
    """Test the whole hypothesis library on the last 26 weeks of data and update discoveries + trait beliefs. Never raises. Idempotent."""
    t0 = time.time()
    try:
        rows = frames.build(DAYS)
        h = frames.data_hash(rows)
        if len([r for r in rows if r["trained"] or r["logged"]]) < 14:
            return {"skipped": "not enough data yet", "days_with_data": len([r for r in rows if r["trained"] or r["logged"]])}
        if not force:
            for a in store.audit_log(60):
                if a["action"] == "miner.run" and a["ref"] == h:
                    return {"skipped": "unchanged", "data_hash": h}
        results = analyze(DAYS, rows=rows, sets=frames.set_rows(DAYS), profile=db.get_profile())
        through = rows[-1]["date"]
        counts = {"confirmed": 0, "candidate": 0, "weakening": 0, "retired": 0}
        new, traits = [], 0
        for r in results:
            prev = store.discovery_by_key(r["key"])
            status = decide(r, prev)
            if status is None:
                continue
            txt = render(r)
            eff = _effect_json(r, through)
            eff["fails"] = 0 if (status == "confirmed" or (status == "candidate" and r["candidate_ok"])) else _existing_fail(prev) + 1
            was_same_dir = bool(prev and isinstance(prev.get("effect"), dict) and np.sign(prev["effect"].get("estimate", 0)) == r["sign"])
            d = store.upsert_discovery(r["key"], r["family"], txt["title"], txt["statement"], eff, recommendation=txt["recommendation"] or None, consumers=txt["consumers"],
                                       status=status, evidence=[{"t": "frames", "days": DAYS, "n": r["n"], "through": through, "p": round(r["p"], 5), "q": round(r["q"], 5)}],
                                       data_hash=h, replicated=bool(r["agree"] and was_same_dir))
            counts[status] += 1
            if not prev:
                new.append(r["key"])
            try:
                if _write_trait(txt["trait"], status in ("confirmed",), txt["statement"], r["key"]):
                    traits += 1
            except Exception:
                pass
        store.audit("miner.run", h, f"tested {len(results)}, confirmed {counts['confirmed']}")
        return {"ran": True, "tested": len(results), **counts, "new": new, "traits_written": traits, "data_hash": h, "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"[:200]}


def run_full() -> dict:
    return run(force=True)
