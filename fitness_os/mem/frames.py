"""The athlete's daily feature matrix: one row per calendar day, built read-only from the existing tables, deterministic and offline.

    rows = frames.build(120)           # list of dicts, ascending by date, None = unknown (never invented, never imputed)
    frames.today()                     # today's row + personal baselines + z-scores + plain-language deviations computed by code
    frames.data_hash(rows)             # stable fingerprint: the miner skips unchanged data

Row keys: date, dow (0=Monday), sleep_h, soreness, energy, bodyweight, kcal, protein_g, carbs_g, fat_g, trained (bool), n_sets, volume_kg, rpe_mean,
readiness (0-100, via backend.policy.adaptation.readiness_score, only when sleep+soreness+energy are all known), pain_note (bool), and
`e1rm_<lift>` (best set of that day through `rpe_adjusted_e1rm`; the lift name has spaces replaced by underscores: `e1rm_bench_press`).
Extras for the miner: n_checkins, meals_n, last_meal_h (clock hour of the last meal, only when it was logged the same day), late_meal (last meal at
or after 21:00, else None when unknown), logged (any check-in that day).

Truths about the tables: `sets.session` is the session DATE label (the truth; `sets.ts` is only insert time). Check-ins have no date column, so their
day is the date part of `ts` (shifted by the optional env FITNESS_TZ_OFFSET_H for athletes whose local day differs from UTC). Meal clock times come
from `meals.ts` and are used only when that timestamp falls on the meal's own `day` (demo/imported data is back-dated, so it has no usable time).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, timedelta, timezone

import numpy as np

from backend.memory import db
from backend.tools.calculators import rpe_adjusted_e1rm

from .. import clock

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PAIN = re.compile(r"\b(pain|hurt|hurts|ache|aching|tweak|tweaked|strain|strained|injur\w*|sprain\w*|pinch\w*|twinge)\b", re.I)
LATE_HOUR = 21.0
BASE_KEYS = ("sleep_h", "soreness", "energy", "bodyweight", "kcal", "protein_g", "carbs_g", "fat_g", "n_sets", "volume_kg", "rpe_mean", "readiness",
             "rhr", "hrv_ms", "steps", "active_kcal", "sleep_score")     # wearable columns: None on days (or whole histories) without a device
# SD floors keep a z-score honest when an athlete's recent values barely vary (a wrist device reports resting HR in whole bpm, HRV with ~5-10 ms day-to-day noise).
WEARABLE_KEYS = ("rhr", "hrv_ms", "steps", "active_kcal", "sleep_score")
_SD_FLOOR = {"rhr": 2.0, "hrv_ms": 4.0, "steps": 1500.0, "active_kcal": 75.0, "sleep_score": 4.0, "sleep_h": 0.4, "soreness": 0.5, "energy": 0.5, "bodyweight": 0.4, "kcal": 150.0, "protein_g": 10.0, "readiness": 5.0, "volume_kg": 500.0}
_LABEL = {"rhr": "resting heart rate", "hrv_ms": "HRV", "steps": "steps", "sleep_h": "sleep", "soreness": "soreness", "energy": "energy", "bodyweight": "bodyweight", "kcal": "calories", "protein_g": "protein",
          "readiness": "readiness"}


def lift_key(lift: str) -> str:
    return "e1rm_" + re.sub(r"\s+", "_", lift.strip().lower())


def _tz() -> float:
    try:
        return float(os.environ.get("FITNESS_TZ_OFFSET_H", "0") or 0)
    except ValueError:
        return 0.0


def _local(ts: str) -> tuple[str, float] | None:
    """(YYYY-MM-DD, clock hour) of an ISO timestamp, shifted by FITNESS_TZ_OFFSET_H. None when unparsable."""
    try:
        d = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    off = _tz()
    if off:
        d = d + timedelta(hours=off)
    return d.date().isoformat(), d.hour + d.minute / 60.0


def _end_date(end: str | None) -> date:
    return date.fromisoformat(end[:10]) if end else clock.now().date()


def _ok(v, lo: float, hi: float) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if lo <= f <= hi and f == f else None


def set_rows(days: int = 120, end: str | None = None) -> list[dict]:
    """Raw working sets in the window, in logging order: {date, exercise, weight, reps, rpe, idx (position within that exercise's session), id}."""
    e = _end_date(end)
    s = (e - timedelta(days=days - 1)).isoformat()
    with db.conn() as c:
        rows = c.execute("SELECT id, session, exercise, weight, reps, rpe FROM sets WHERE session BETWEEN ? AND ? ORDER BY id", (s, e.isoformat())).fetchall()
    out, pos = [], {}
    for r in rows:
        if not _DATE.match(r["session"] or "") or not r["reps"] or not r["weight"] or r["weight"] <= 0:
            continue
        k = (r["session"], r["exercise"])
        pos[k] = pos.get(k, -1) + 1
        out.append({"id": r["id"], "date": r["session"], "exercise": r["exercise"], "weight": float(r["weight"]), "reps": int(r["reps"]),
                    "rpe": _ok(r["rpe"], 1, 10), "idx": pos[k]})
    return out


def build(days: int = 120, end: str | None = None) -> list[dict]:
    """One dict per calendar day over the last `days` days ending at `end` (default today per fitness_os.clock). Read-only, deterministic."""
    from backend.policy.adaptation import readiness_score
    e = _end_date(end)
    start = e - timedelta(days=days - 1)
    s_iso, e_iso = start.isoformat(), e.isoformat()
    pad = (start - timedelta(days=2)).isoformat() + "T00:00:00", (e + timedelta(days=2)).isoformat() + "T23:59:59"
    with db.conn() as c:
        sets = c.execute("SELECT session, exercise, weight, reps, rpe, note FROM sets WHERE session BETWEEN ? AND ? ORDER BY id", (s_iso, e_iso)).fetchall()
        chk = c.execute("SELECT ts, sleep_h, soreness, energy, bodyweight, note FROM checkins WHERE ts BETWEEN ? AND ? ORDER BY id", pad).fetchall()
        meals = c.execute("SELECT ts, day, kcal, protein, carbs, fat FROM meals WHERE day BETWEEN ? AND ? ORDER BY id", (s_iso, e_iso)).fetchall()
    rows: dict[str, dict] = {}
    for i in range(days):
        d = start + timedelta(days=i)
        rows[d.isoformat()] = {"date": d.isoformat(), "dow": d.weekday(), "sleep_h": None, "soreness": None, "energy": None, "bodyweight": None, "kcal": None,
                               "protein_g": None, "carbs_g": None, "fat_g": None, "trained": False, "n_sets": 0, "volume_kg": None, "rpe_mean": None,
                               "readiness": None, "pain_note": False, "n_checkins": 0, "meals_n": 0, "last_meal_h": None, "late_meal": None, "logged": False}
    for r in chk:
        loc = _local(r["ts"])
        if not loc or loc[0] not in rows:
            continue
        f = rows[loc[0]]
        f["n_checkins"] += 1
        f["logged"] = True
        for col, key, lo, hi in (("sleep_h", "sleep_h", 0.5, 16), ("soreness", "soreness", 1, 5), ("energy", "energy", 1, 5), ("bodyweight", "bodyweight", 25, 400)):
            v = _ok(r[col], lo, hi)
            if v is not None:
                f[key] = v                      # the latest entry of the day wins
        if r["note"] and _PAIN.search(r["note"]):
            f["pain_note"] = True
    day_macro: dict[str, dict[str, float]] = {}
    for r in meals:
        f = rows.get(r["day"])
        if f is None:
            continue
        m = day_macro.setdefault(r["day"], {"kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "kn": 0, "pn": 0})
        for col, key in (("kcal", "kcal"), ("protein", "protein_g"), ("carbs", "carbs_g"), ("fat", "fat_g")):
            if r[col] is not None:
                m[key] += float(r[col])
                if key == "kcal":
                    m["kn"] += 1
                if key == "protein_g":
                    m["pn"] += 1
        f["meals_n"] += 1
        loc = _local(r["ts"])
        if loc and loc[0] == r["day"]:
            f["last_meal_h"] = loc[1] if f["last_meal_h"] is None else max(f["last_meal_h"], loc[1])
    for day, m in day_macro.items():
        f = rows[day]
        if m["kn"]:
            f["kcal"] = m["kcal"]
        if m["pn"]:
            f["protein_g"], f["carbs_g"], f["fat_g"] = m["protein_g"], m["carbs_g"], m["fat_g"]
        if f["last_meal_h"] is not None:
            f["late_meal"] = f["last_meal_h"] >= LATE_HOUR
    lifts: set[str] = set()
    rpes: dict[str, list[float]] = {}
    for r in sets:
        f = rows.get(r["session"])
        if f is None:
            continue
        f["trained"] = True
        f["n_sets"] += 1
        w, reps = r["weight"], r["reps"]
        if w and reps and w > 0:
            f["volume_kg"] = (f["volume_kg"] or 0.0) + float(w) * int(reps)
            if 1 <= int(reps) <= 12:
                k = lift_key(r["exercise"])
                lifts.add(k)
                rp = _ok(r["rpe"], 1, 10)
                v = rpe_adjusted_e1rm(float(w), int(reps), rp)
                if v > 0 and (f.get(k) is None or v > f[k]):
                    f[k] = v
        rp = _ok(r["rpe"], 1, 10)
        if rp is not None:
            rpes.setdefault(r["session"], []).append(rp)
        if r["note"] and _PAIN.search(r["note"]):
            f["pain_note"] = True
    try:   # wearable data (additive): fills sleep_h only where no manual check-in value exists; adds rhr/hrv_ms/steps/active_kcal/sleep_score/sleep_src
        from backend.wearables import fill_frames
        fill_frames([rows[k] for k in sorted(rows)])
    except Exception:
        pass
    out = []
    for key in sorted(rows):
        f = rows[key]
        for k in WEARABLE_KEYS:
            f.setdefault(k, None)               # stable schema even when the wearable hook is unavailable
        for k in lifts:
            f.setdefault(k, None)
        if key in rpes:
            f["rpe_mean"] = sum(rpes[key]) / len(rpes[key])
        if f["sleep_h"] is not None and f["soreness"] is not None and f["energy"] is not None:
            f["readiness"] = float(readiness_score(f["sleep_h"], int(round(f["soreness"])), int(round(f["energy"])))["score"])
        out.append(f)
    return out


def lifts_in(rows: list[dict]) -> list[str]:
    """Lift column names (`e1rm_*`) present, most-logged first."""
    keys = {k for r in rows[:1] for k in r if k.startswith("e1rm_")}
    return sorted(keys, key=lambda k: -sum(1 for r in rows if r.get(k) is not None))


def arrays(rows: list[dict], keys: list[str] | tuple[str, ...]) -> dict[str, np.ndarray]:
    """Columns as float arrays with NaN for unknown (booleans become 0/1; None stays NaN)."""
    out = {}
    for k in keys:
        out[k] = np.array([np.nan if r.get(k) is None else float(r[k]) for r in rows], dtype=float)
    return out


def data_hash(rows: list[dict]) -> str:
    """Stable fingerprint of the content (not the timestamp of the run): the same logs always give the same hash."""
    h = hashlib.sha1()
    for r in rows:
        h.update(json.dumps(r, sort_keys=True, default=lambda x: round(float(x), 3) if isinstance(x, float) else str(x)).encode())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------- today versus this athlete's normal
def _baseline(rows: list[dict], key: str, window: int = 42) -> dict | None:
    vals = [r[key] for r in rows[-(window + 1):-1] if r.get(key) is not None]
    if len(vals) < 7:
        return None
    a = np.asarray(vals, float)
    med = float(np.median(a))
    mad = float(np.median(np.abs(a - med))) * 1.4826
    sd = max(float(np.std(a, ddof=1)), 1e-9)
    sd = min(sd, mad) if mad > 0 else sd                  # robust: a single typo does not widen what "normal" means
    return {"mean": float(np.mean(a)), "sd": max(sd, _SD_FLOOR.get(key, 0.0)), "n": len(vals)}


def _fmt(key: str, v: float) -> str:
    return {"sleep_h": f"{v:.1f} h", "soreness": f"{v:.0f}/5", "energy": f"{v:.0f}/5", "bodyweight": f"{v:.1f} kg", "kcal": f"{v:,.0f} kcal",
            "protein_g": f"{v:.0f} g", "readiness": f"{v:.0f}", "rhr": f"{v:.0f} bpm", "hrv_ms": f"{v:.0f} ms", "steps": f"{v:,.0f} steps"}.get(key, f"{v:.1f}")


def _delta(key: str, d: float) -> str:
    a = abs(d)
    return {"sleep_h": f"{a:.1f} h", "soreness": f"{a:.1f} points", "energy": f"{a:.1f} points", "bodyweight": f"{a:.1f} kg", "kcal": f"{a:,.0f} kcal",
            "protein_g": f"{a:.0f} g", "readiness": f"{a:.0f} points", "rhr": f"{a:.0f} bpm", "hrv_ms": f"{a:.0f} ms", "steps": f"{a:,.0f} steps"}.get(key, f"{a:.1f}")


def today(days: int = 90, end: str | None = None) -> dict:
    """{"frame", "baseline": {metric: {mean, sd, n}}, "z": {metric: z}, "deviations": [sentence], "date"}. Numbers are computed here, never by a model.

    Deviations mention only the metrics that are known today and differ from the athlete's own recent normal by at least 1 SD ("slept 5.2 h: about
    1.6 h below your usual 6.8 h"). Calories and protein are judged on yesterday (today is still incomplete).
    """
    rows = build(max(days, 50), end)
    if not rows:
        return {"frame": None, "baseline": {}, "z": {}, "deviations": [], "date": None}
    cur, prev = rows[-1], rows[-2] if len(rows) > 1 else None
    base, z, dev = {}, {}, []
    for key in ("sleep_h", "soreness", "energy", "bodyweight", "readiness"):
        b = _baseline(rows, key)
        if b is None:
            continue
        base[key] = b
        v = cur.get(key)
        if v is None:
            continue
        z[key] = round((v - b["mean"]) / b["sd"], 2)
        if abs(z[key]) >= 1.0:
            hi = v > b["mean"]
            word = "above" if hi else "below"
            if key == "bodyweight":
                dev.append(f"weighed {_fmt(key, v)}: {_delta(key, v - b['mean'])} {word} your 6-week average of {_fmt(key, b['mean'])}")
            elif key == "readiness":
                dev.append(f"readiness {_fmt(key, v)}: {_delta(key, v - b['mean'])} {word} your usual {_fmt(key, b['mean'])}")
            else:
                verb = {"sleep_h": "slept", "soreness": "soreness is", "energy": "energy is"}[key]
                dev.append(f"{verb} {_fmt(key, v)}: about {_delta(key, v - b['mean'])} {word} your usual {_fmt(key, b['mean'])}")
    for key in ("hrv_ms", "rhr"):               # wearable readings: judged only when the athlete has a device history and today's value is in
        b = _baseline(rows, key)
        if b is None:
            continue
        base[key] = b
        v = cur.get(key)
        if v is None:
            continue
        z[key] = round((v - b["mean"]) / b["sd"], 2)
        if abs(z[key]) >= 1.0:
            dev.append(f"{_LABEL[key]} {_fmt(key, v)}: about {_delta(key, v - b['mean'])} {'above' if v > b['mean'] else 'below'} your usual {_fmt(key, b['mean'])}")
    if prev is not None:
        for key in ("kcal", "protein_g"):
            b = _baseline(rows[:-1], key)
            if b is None:
                continue
            base[key] = b
            v = prev.get(key)
            if v is None:
                continue
            zz = round((v - b["mean"]) / b["sd"], 2)
            z[key] = zz
            if abs(zz) >= 1.0:
                what = "protein" if key == "protein_g" else "calories"
                shown = _fmt(key, v) if key == "kcal" else f"{_fmt(key, v)} of {what}"
                dev.append(f"yesterday you logged {shown}: about {_delta(key, v - b['mean'])} {'above' if v > b['mean'] else 'below'} your usual {_fmt(key, b['mean'])}")
    if not cur.get("logged"):
        dev.append("no check-in logged yet today")
    return {"frame": cur, "baseline": base, "z": z, "deviations": dev, "date": cur["date"]}
