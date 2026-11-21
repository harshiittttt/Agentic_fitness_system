"""Synthetic athletes with PLANTED truth, written into the current (scratch) database through the normal tables.

Used to prove the Insight Engine (planted effects must be found, null athletes must yield nothing) and by every agent's integration tests.

    from fitness_os.mem import synth
    truth = synth.make_athlete(seed=7, days=84, effects=synth.default_effects())   # a world where sleep, volume, Mondays ... matter
    truth = synth.make_athlete(seed=7, days=84)                                    # NULL world: same mess, no effects (false-discovery tests)

What it writes (all back-dated; `sets.session` is the session DATE label, the truth the app reads):
  * `sets`      weight / reps / RPE per set, rounded to 2.5 kg plates, with the athlete's reporting bias baked in
  * `checkins`  sleep_h, soreness, energy, bodyweight (about 6% of days missing, fields randomly missing, occasional typos)
  * `meals`     3-4 meals a day with clock times (`ts`), about 12% of days not logged, a few half-logged days
  * profile + a starter plan (no LLM; same shape as onboarding)
The world is fully determined by `seed`. `noise` scales every random component (1.0 = realistic, 0 = clean, 2 = a messy athlete).

Mechanics (so a reader can see why a naive correlation would be fooled): every lift progresses over time (the confounder), progress depends on the
weekly volume / frequency the athlete happened to do, day-to-day form follows an autocorrelated random walk, and sleep / soreness / energy are
themselves correlated (energy depends on sleep), so "energy predicts lifting" looks true even when only sleep does.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np

from backend.memory import db

from .. import clock

LIFTS = ("squat", "bench press", "deadlift", "overhead press", "barbell row")
_REL = {"squat": 1.30, "bench press": 0.95, "deadlift": 1.65, "overhead press": 0.62, "barbell row": 0.85}      # e1RM / bodyweight, before the athlete factor
_ROT = (("squat", "bench press", "barbell row"), ("deadlift", "overhead press", "bench press"),
        ("squat", "overhead press", "barbell row"), ("deadlift", "bench press", "squat"))
_REPS = (3, 4, 5, 5, 6, 8, 10)
BASE_SLEEP, BASE_ENERGY, BASE_SORENESS = 7.2, 3.3, 2.5


def default_effects(strength: float = 1.0) -> dict[str, dict[str, Any]]:
    """Every effect the miner can find, at plausible-but-clear sizes. `strength` scales the sizes (0.5 = subtle)."""
    s = strength
    return {
        "sleep_perf": {"pct_per_hour": 2.0 * s},                    # lag 0: last night's sleep -> today's lifts
        "soreness_perf": {"pct_per_point": -2.0 * s},
        "energy_perf": {"pct_per_point": 1.8 * s},
        "volume": {"sweet_spot": 48, "gain_pct_week": 2.5 * s, "width": 24},     # weekly working sets; progress per week peaks near the sweet spot
        "frequency": {"pct_week_per_session": 1.0 * s},
        "weekday_adherence": {"weekday": 0, "p": 1.0 - 0.5 * s},   # Monday sessions are attended with probability p (others ~94%)
        "weekday_perf": {"weekday": 3, "pct": -3.5 * s},            # Thursday sessions run low
        "rest_gap": {"pct_per_day": 1.5 * s},                       # more days since the last session -> stronger (up to 4 days)
        "rpe_bias": {"b": 0.6 * s},                                 # reported reps-in-reserve = true * (1 + b): effort is under-reported
        "protein_soreness": {"points_per_25g": -0.5 * s},           # more protein today -> less sore tomorrow
        "maintenance": {"offset_kcal": 320 * s},                    # real maintenance = formula + this
        "late_meal_sleep": {"hours": -0.9 * s},                     # a meal after 21:00 -> sleep shorter that night
        "bodyweight_strength": {"pct_per_kg": 1.5 * s},
        "deload": {"rebound_pct": 4.0 * s, "period": 4},
    }


def _norm_effects(effects) -> dict[str, dict[str, Any]]:
    if effects in (None, False, {}):
        return {}
    if effects in ("all", "planted", True):
        return default_effects()
    return {k: dict(v or {}) for k, v in effects.items()}


def _ar1(rng: np.random.Generator, n: int, phi: float, sd: float) -> np.ndarray:
    out = np.empty(n)
    if sd <= 0:
        return np.zeros(n)
    e = rng.normal(0.0, sd * math.sqrt(max(1e-9, 1 - phi * phi)), n)
    out[0] = rng.normal(0.0, sd)
    for i in range(1, n):
        out[i] = phi * out[i - 1] + e[i]
    return out


def _plate(w: float) -> float:
    return max(20.0, round(w / 2.5) * 2.5)


def _iso(d: date, hh: float) -> str:
    return datetime(d.year, d.month, d.day, int(hh), int((hh % 1) * 60), tzinfo=timezone.utc).isoformat(timespec="seconds")


def _vol_term(V: float, fx: dict) -> float:
    """Weekly progress (log units) contributed by volume: concave with a personal peak, steeper penalty beyond the peak (overreach)."""
    v = fx.get("volume")
    if not v:
        return 0.0
    star, gain, w = float(v["sweet_spot"]), float(v["gain_pct_week"]) / 100.0, float(v.get("width", 24))
    width = w if V <= star else 0.65 * w
    return gain * (1.0 - ((V - star) / width) ** 2)


def make_athlete(seed: int, days: int = 84, effects: dict | str | None = None, noise: float = 1.0, *, end: str | None = None,
                 missing: float = 0.06, reset: bool = True) -> dict:
    """Write one synthetic athlete into the current database and return the planted `truth` (see module docstring).

    `effects` None/{} = null world. `end` = last date (default: today per fitness_os.clock). `reset` clears profile/plan/sets/checkins/meals/memory first.
    """
    from backend.tools import calculators as calc
    from backend.tools.planner import build_plan
    from backend.tools.profile_edit import nutrition_targets

    rng = np.random.default_rng(int(seed))
    fx = _norm_effects(effects)
    nz = float(noise)
    end_d = date.fromisoformat(end[:10]) if end else clock.now().date()
    D = [end_d - timedelta(days=days - 1 - i) for i in range(days)]
    dow = [d.weekday() for d in D]

    # ---- the person ----
    sex = "m" if rng.random() < 0.8 else "f"
    w0 = float(np.round(rng.uniform(68, 95) if sex == "m" else rng.uniform(55, 75), 1))
    profile = {"age": int(rng.integers(22, 46)), "sex": sex, "height_cm": int(rng.integers(165, 191) if sex == "m" else rng.integers(155, 175)), "weight_kg": w0,
               "goal": "maintain", "days_per_week": 4, "activity": "moderate", "injuries": [], "avoid": [], "experience": "intermediate"}
    formula = float(calc.tdee(calc.mifflin_st_jeor(w0, profile["height_cm"], profile["age"], sex), "moderate"))
    offset = float(fx.get("maintenance", {}).get("offset_kcal", 0.0))
    tdee_true = formula + offset
    strength = float(rng.uniform(0.85, 1.2)) * (1.0 if sex == "m" else 0.68)
    e0 = {l: _REL[l] * w0 * strength * float(rng.uniform(0.95, 1.05)) for l in LIFTS}
    g0 = float(rng.uniform(0.002, 0.005))                                              # baseline progression, log per week (the confounder)
    pl = fx.get("plateau")
    plateau_from = int(days * float(pl.get("start_frac", 0.6))) if pl else None

    # ---- habits ----
    special = [int(fx["weekday_adherence"]["weekday"])] if "weekday_adherence" in fx else []
    if "weekday_perf" in fx:
        special.append(int(fx["weekday_perf"]["weekday"]))
    habit = list(dict.fromkeys(special + [int(x) for x in rng.permutation(7)]))[:4]
    wts = np.full(7, 0.10)
    wts[habit] = 1.0
    wts = wts / wts.sum()
    n_weeks = days // 7 + 3
    vol_ar = _ar1(rng, n_weeks, 0.3, 0.26 * nz if nz else 0.26)
    v_star = float(fx["volume"]["sweet_spot"]) if "volume" in fx else None
    base_v = (v_star * 1.12 * float(rng.uniform(0.92, 1.1))) if v_star else float(rng.uniform(38, 60))
    phase = np.zeros(n_weeks)
    for k in range(1, n_weeks):
        phase[k] = float(np.clip(0.7 * phase[k - 1] + rng.normal(0, 110), -450, 450))
    deload = fx.get("deload")
    period = int(deload.get("period", 4)) if deload else 0

    # ---- daily latent noise (autocorrelated, so naive p-values lie) ----
    stress = _ar1(rng, days, 0.6, 1.0)
    sleep_nz = _ar1(rng, days, 0.25, 0.65 * nz)
    sore_nz = _ar1(rng, days, 0.3, 0.55 * nz)
    energy_nz = _ar1(rng, days, 0.3, 0.60 * nz)
    form_slow = _ar1(rng, days, 0.7, 0.008 * nz)
    form_day = rng.normal(0, 0.010 * nz, days)
    water = _ar1(rng, days, 0.5, 0.35 * nz)
    lift_form = {l: _ar1(rng, days, 0.5, 0.008 * nz) for l in LIFTS}
    protein_ar = _ar1(rng, days, 0.3, 24.0 * nz if nz else 0.0)

    b_rpe = float(fx.get("rpe_bias", {}).get("b", 0.0))
    late_h = float(fx.get("late_meal_sleep", {}).get("hours", 0.0))
    prot_pts = float(fx.get("protein_soreness", {}).get("points_per_25g", 0.0))
    p_dip = float(fx["weekday_adherence"]["p"]) if "weekday_adherence" in fx else None
    dip_day = special[0] if "weekday_adherence" in fx else None
    wd_perf = fx.get("weekday_perf")

    # ---- state ----
    W = w0
    Wlist: list[float] = []
    L = 0.0                       # shared log-level of all lifts (progression)
    rebound_week = -1
    sets_rows: list[tuple] = []
    check_rows: list[tuple] = []
    meal_rows: list[tuple] = []
    sess_count = 0
    last_train = None
    late_prev = 0
    prot_prev, trained_prev, sets_prev = None, 0, 0
    sleep_prev = BASE_SLEEP
    prot_mean = 1.8 * w0
    week_i = -1
    week_days: set[int] = set()
    week_v = week_f = 0
    week_plan_v = 0.0
    week_n_sets = 4
    week_is_deload = False
    weekly: list[dict] = []
    # truth collectors
    tr_x: dict[str, list] = {k: [] for k in ("sleep", "sleep1", "soreness", "energy", "gap", "bw")}
    tr_e: dict[str, list] = {k: [] for k in tr_x}
    f_log: list[float] = []       # w / true capacity of each set with a usable RPE (to express the planted bias as a slope)
    attend = {d: [0, 0] for d in range(7)}      # weekday -> [planned, attended]
    kcal_logged_days = 0
    formula_kcal = tdee_true

    def close_week() -> None:
        nonlocal L, rebound_week
        if week_i < 0:
            return
        weekly.append({"week": week_i, "volume": week_v, "freq": week_f, "deload": week_is_deload})
        g = g0
        if plateau_from is not None and i_cur >= plateau_from:
            g = 0.0
        else:
            g += _vol_term(float(week_v), fx) if "volume" in fx else 0.0
            if "frequency" in fx:
                g += float(fx["frequency"]["pct_week_per_session"]) / 100.0 * (week_f - 4)
        if plateau_from is not None and i_cur >= plateau_from:
            g = 0.0
        L += g
        if week_is_deload and deload:
            rebound_week = week_i + 1

    i_cur = 0
    for i in range(days):
        i_cur = i
        d, wd = D[i], dow[i]
        if i == 0 or wd == 0:
            if i > 0:
                close_week()
            week_i += 1
            week_is_deload = bool(period and week_i >= period - 1 and (week_i + 1) % period == 0)
            week_plan_v = base_v * math.exp(vol_ar[week_i]) * (0.5 if week_is_deload else 1.0)
            f_plan = int(rng.choice([4, 4, 4, 5, 5]))
            if week_is_deload:
                f_plan = max(3, f_plan - 1)
            week_days = {int(x) for x in rng.choice(7, size=f_plan, replace=False, p=wts)}
            week_n_sets = int(np.clip(round(week_plan_v / (3.0 * f_plan)), 2, 8))
            week_v = week_f = 0
        # -------- meals for today (and the weight they will produce) --------
        kcal_i = (tdee_true + phase[min(week_i, n_weeks - 1)]) * (1.0 + rng.normal(0, 0.05 * nz))
        prot_i = max(60.0, prot_mean + protein_ar[i])
        late_i = int(rng.random() < 0.30)
        # -------- the morning check-in --------
        sleep_true = float(np.clip(BASE_SLEEP + (0.35 if wd in (5, 6) else 0.0) - 0.35 * stress[i] + sleep_nz[i] + late_h * late_prev, 3.5, 10.0))
        energy_true = BASE_ENERGY + 0.45 * (sleep_true - BASE_SLEEP) - 0.35 * stress[i] + energy_nz[i]
        sore_true = 1.9 + 0.9 * trained_prev + 0.012 * (sets_prev - 40) * trained_prev + sore_nz[i]
        if prot_prev is not None:
            sore_true += prot_pts / 25.0 * (prot_prev - prot_mean)
        sore_obs = int(np.clip(round(sore_true), 1, 5))
        en_obs = int(np.clip(round(energy_true), 1, 5))
        # -------- training --------
        trains = False
        if wd in week_days:
            attend[wd][0] += 1
            p_att = p_dip if (dip_day is not None and wd == dip_day) else 0.94
            if rng.random() < p_att:
                trains = True
                attend[wd][1] += 1
        gap = (i - last_train) if last_train is not None else 3
        n_sets_today = 0
        if trains:
            e = 0.0
            comps = {"sleep": 0.0, "sleep1": 0.0, "soreness": 0.0, "energy": 0.0, "gap": 0.0, "bw": 0.0}
            s = fx.get("sleep_perf")
            if s:
                if "threshold" in s:
                    comps["sleep"] = float(s["pct"]) / 100.0 * (sleep_true >= float(s["threshold"]))
                else:
                    comps["sleep"] = float(s["pct_per_hour"]) / 100.0 * (sleep_true - BASE_SLEEP)
            s = fx.get("sleep_perf_lag1")
            if s:
                comps["sleep1"] = float(s["pct_per_hour"]) / 100.0 * (sleep_prev - BASE_SLEEP)
            s = fx.get("soreness_perf")
            if s:
                comps["soreness"] = float(s["pct_per_point"]) / 100.0 * (sore_obs - BASE_SORENESS)
            s = fx.get("energy_perf")
            if s:
                comps["energy"] = float(s["pct_per_point"]) / 100.0 * (en_obs - BASE_ENERGY)
            s = fx.get("rest_gap")
            if s:
                comps["gap"] = float(s["pct_per_day"]) / 100.0 * (min(gap, 4) - 2.5)
            s = fx.get("bodyweight_strength")
            if s:
                wsm = float(np.mean(Wlist[-7:] + [W])) if Wlist else W
                comps["bw"] = float(s["pct_per_kg"]) / 100.0 * (wsm - w0)
            e = sum(comps.values())
            if wd_perf and wd == int(wd_perf["weekday"]):
                e += float(wd_perf["pct"]) / 100.0
            if rebound_week == week_i and deload:
                e += float(deload["rebound_pct"]) / 100.0
            for k, v in comps.items():
                tr_e[k].append(v * 100.0)
            tr_x["sleep"].append(sleep_true); tr_x["sleep1"].append(sleep_prev); tr_x["soreness"].append(sore_obs)
            tr_x["energy"].append(en_obs); tr_x["gap"].append(min(gap, 4)); tr_x["bw"].append(float(np.mean(Wlist[-7:] + [W])) - w0)
            lifts_today = _ROT[sess_count % len(_ROT)]
            sess_count += 1
            for lift in lifts_today:
                cap_plan = e0[lift] * math.exp(L)
                cap = cap_plan * math.exp(e + form_slow[i] + form_day[i] + lift_form[lift][i] + rng.normal(0, 0.008 * nz))
                n_k = int(np.clip(week_n_sets + int(rng.integers(-1, 2)), 2, 8))
                for k in range(n_k):
                    reps_t = int(rng.choice(_REPS))
                    rir_target = int(rng.choice((1, 2, 2, 3)))
                    w = _plate(cap_plan / (1.0 + (reps_t + rir_target) / 30.0))
                    cap_k = cap * (1.0 - 0.012 * k) * math.exp(rng.normal(0, 0.006 * nz))
                    T = 30.0 * (cap_k / w - 1.0)
                    if T < reps_t:                       # failed the target reps: stops at what was possible
                        reps = max(1, int(math.floor(T)))
                        rir_true = max(0.0, T - reps)
                    else:
                        reps, rir_true = reps_t, T - reps_t
                    rir_rep = max(0.0, rir_true * (1.0 + b_rpe) + rng.normal(0, 0.45 * nz))
                    rpe = float(np.clip(10.0 - round(rir_rep * 2) / 2, 5.0, 10.0))
                    wr = w
                    if rng.random() < 0.004:             # fat-finger typo
                        wr = w + 20.0
                    rpe_out = None if rng.random() < 0.08 else rpe
                    sets_rows.append((_iso(d, 18 + k * 0.05), d.isoformat(), lift, wr, reps, rpe_out, None))
                    n_sets_today += 1
                    if rpe_out is not None and wr == w:
                        f_log.append(w / cap_k)
            last_train = i
            week_v += n_sets_today
            week_f += 1
        # -------- write the day's logs --------
        if rng.random() >= missing * (1 + 0.5 * (nz > 1.5)):
            sl = None if rng.random() < 0.07 else (round(sleep_true * 4) / 4 if rng.random() < 0.5 else round(sleep_true, 1))
            if sl is not None and rng.random() < 0.01:
                sl = 3.5
            so = None if rng.random() < 0.07 else sore_obs
            en = None if rng.random() < 0.07 else en_obs
            bw = None if rng.random() < 0.22 else round(W + water[i], 1)
            if bw is not None and rng.random() < 0.005:
                bw = round(bw + 5.0, 1)
            note = "knee pain on stairs" if rng.random() < 0.015 else None
            check_rows.append((_iso(d, 7.5), sl, so, en, bw, note))
            if rng.random() < 0.05:                      # a second entry the same day (weigh-in only)
                check_rows.append((_iso(d, 21.0), None, None, None, round(W + water[i] + rng.normal(0, 0.2), 1), None))
        if rng.random() >= 0.12:
            frac = 0.55 if rng.random() < 0.05 else 1.0
            splits = np.array([0.24, 0.34, 0.10, 0.32]) * rng.uniform(0.9, 1.1, 4)
            splits = splits / splits.sum()
            hours = [8.0 + rng.normal(0, 0.5), 13.0 + rng.normal(0, 0.7), 16.5 + rng.normal(0, 0.7),
                     (21.6 + abs(rng.normal(0, 0.5))) if late_i else (19.3 + rng.normal(0, 0.6))]
            names = ("breakfast", "lunch", "snack", "dinner")
            for j in range(4):
                kc = kcal_i * splits[j] * frac
                pr = prot_i * splits[j] * frac
                fat = 0.27 * kc / 9
                carb = max(0.0, (kc - pr * 4 - fat * 9) / 4)
                meal_rows.append((_iso(d, float(np.clip(hours[j], 5.5, 23.7))), d.isoformat(), names[j], None, round(kc), round(pr, 1), round(carb, 1), round(fat, 1)))
            kcal_logged_days += frac == 1.0
        # -------- tomorrow's state --------
        Wlist.append(W)
        W = W + (kcal_i - tdee_true) / 7700.0
        late_prev, prot_prev, trained_prev, sets_prev, sleep_prev = late_i, prot_i, int(trains), n_sets_today, sleep_true
    close_week()

    # ---- write ----
    if reset:
        db.reset_all()
    db.save_profile(profile)
    try:
        plan = build_plan(profile["days_per_week"], profile["goal"])
    except Exception:
        plan = {"days": []}
    plan["nutrition"] = nutrition_targets(profile)
    plan["base_calories"] = plan["nutrition"]["calories"]
    plan["base_volume"] = 1.0
    db.save_plan(plan)
    with db.conn() as c:
        c.executemany("INSERT INTO sets (ts, session, exercise, weight, reps, rpe, note) VALUES (?,?,?,?,?,?,?)", sets_rows)
        c.executemany("INSERT INTO checkins (ts, sleep_h, soreness, energy, bodyweight, note) VALUES (?,?,?,?,?,?)", check_rows)
        c.executemany("INSERT INTO meals (ts, day, name, grams, kcal, protein, carbs, fat, estimated) VALUES (?,?,?,?,?,?,?,?,0)", meal_rows)

    # ---- the truth, in the units the miner reports ----
    def lin(name: str) -> float | None:
        x, y = np.asarray(tr_x[name], float), np.asarray(tr_e[name], float)
        if len(x) < 3 or np.var(x) == 0:
            return None
        return float(np.cov(x, y, bias=True)[0, 1] / np.var(x))

    truth_fx: dict[str, dict] = {}
    for name, spec in fx.items():
        truth_fx[name] = dict(spec)
    for name, comp in (("sleep_perf", "sleep"), ("sleep_perf_lag1", "sleep1"), ("soreness_perf", "soreness"), ("energy_perf", "energy"),
                       ("rest_gap", "gap"), ("bodyweight_strength", "bw")):
        if name in fx:
            truth_fx[name]["slope_pct_per_unit"] = lin(comp)
            truth_fx[name]["sign"] = 1 if (truth_fx[name]["slope_pct_per_unit"] or 0) >= 0 else -1
    if "volume" in fx:
        vs = [w_["volume"] for w_ in weekly if w_["volume"] > 0]
        grid = np.linspace(min(vs), max(vs), 200) if vs else np.array([0.0])
        gv = np.array([_vol_term(float(v), fx) for v in grid])
        truth_fx["volume"].update(range=[float(min(vs)), float(max(vs))] if vs else None,
                                  best_in_range=float(grid[int(np.argmax(gv))]) if vs else None)
    if "rpe_bias" in fx and len(f_log) > 5:
        # reported RIR = true RIR * (1 + b): the implied e1RM of a set is inflated by w * (b / (1 + b)) * reported_RIR / 30 (structural slope per reported RIR)
        truth_fx["rpe_bias"]["slope_pct_per_rir"] = float(100 * (b_rpe / (1.0 + b_rpe)) * np.mean(f_log) / 30.0)
        truth_fx["rpe_bias"]["bias_reps_at_rpe8"] = 2.0 * b_rpe / (1.0 + b_rpe)
    if "weekday_adherence" in fx:
        rates = {k: (v[1] / v[0] if v[0] else None) for k, v in attend.items()}
        truth_fx["weekday_adherence"]["realized_rates"] = rates
    if "maintenance" in fx:
        truth_fx["maintenance"].update(formula_tdee=formula, true_tdee=tdee_true)
    return {"seed": int(seed), "days": days, "start": D[0].isoformat(), "end": D[-1].isoformat(), "null": not fx, "noise": nz,
            "effects": truth_fx, "profile": profile, "formula_tdee": formula, "true_tdee": tdee_true, "habit_weekdays": habit,
            "sessions": sess_count, "sets": len(sets_rows), "checkins": len(check_rows), "meals": len(meal_rows), "weekly": weekly,
            "plateau_from_day": plateau_from, "progression_pct_week": g0 * 100.0}
