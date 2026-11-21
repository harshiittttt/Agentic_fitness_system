"""Provider-neutral wearable ingest: normalised batch + Apple Health / CSV file import.

Rules: strict validation and size limits; idempotent upserts keyed (source, date, metric); units normalised to one canonical unit per metric;
out-of-range values are REJECTED and reported (never clipped); provenance (source, imported_at) is stored on every row; a manual check-in value
for the same day always wins over a wearable value (the merge happens at read time, nothing manual is ever overwritten).
Tables are created here (CREATE IF NOT EXISTS) so backend/memory/db.py is untouched; fitness_os.privacy export/delete_all cover them automatically.
"""
from __future__ import annotations

import csv
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from xml.etree import ElementTree as ET

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.memory import db

router = APIRouter(prefix="/wearables", tags=["wearables"])

SCHEMA = """
CREATE TABLE IF NOT EXISTS wearable_daily (
  source TEXT NOT NULL, day TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL, imported_at TEXT NOT NULL,
  PRIMARY KEY (source, day, metric)
);
CREATE TABLE IF NOT EXISTS wearable_workouts (
  source TEXT NOT NULL, day TEXT NOT NULL, start TEXT NOT NULL, kind TEXT NOT NULL, minutes REAL NOT NULL, kcal REAL, imported_at TEXT NOT NULL,
  PRIMARY KEY (source, start, kind)
);
"""

# metric -> (canonical unit, lo, hi, {accepted unit: multiplier to canonical})
METRICS: dict[str, tuple[str, float, float, dict[str, float]]] = {
    "sleep_h": ("h", 0.5, 16.0, {"h": 1.0, "hr": 1.0, "hours": 1.0, "min": 1 / 60, "minutes": 1 / 60, "s": 1 / 3600, "sec": 1 / 3600}),
    "sleep_score": ("score", 0.0, 100.0, {"score": 1.0, "%": 1.0}),
    "resting_hr": ("bpm", 25.0, 130.0, {"bpm": 1.0, "count/min": 1.0}),
    "hrv_ms": ("ms", 5.0, 300.0, {"ms": 1.0, "s": 1000.0}),
    "steps": ("count", 0.0, 100000.0, {"count": 1.0, "steps": 1.0}),
    "active_kcal": ("kcal", 0.0, 10000.0, {"kcal": 1.0, "cal": 1.0, "kj": 0.239006}),
}
MAX_RECORDS, MAX_WORKOUTS, MAX_REJECT_DETAIL = 2000, 200, 50
MAX_AGE_DAYS = 5 * 366
MAX_UPLOAD = {"apple_health": 400 * 1024 * 1024, "csv": 20 * 1024 * 1024}
_SOURCE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KIND = re.compile(r"^[A-Za-z0-9 _/-]{1,40}$")


def _init(c) -> None:
    c.executescript(SCHEMA)


def _today() -> date:
    try:
        from fitness_os import clock
        return clock.now().date()
    except Exception:
        return datetime.now(timezone.utc).date()


class Record(BaseModel):
    date: str
    metric: str
    value: float
    unit: str | None = None


class Workout(BaseModel):
    start: str = Field(max_length=40)
    kind: str
    minutes: float
    kcal: float | None = None


class IngestBatch(BaseModel):
    source: str
    records: list[Record] = Field(default_factory=list, max_length=MAX_RECORDS)
    workouts: list[Workout] = Field(default_factory=list, max_length=MAX_WORKOUTS)


# ---- validation ----
def _check_day(d: str) -> str | None:
    """Reason string when the date is unusable, else None."""
    if not _DAY.match(d or ""):
        return "date must be YYYY-MM-DD"
    try:
        x = date.fromisoformat(d)
    except ValueError:
        return "not a real date"
    t = _today()
    if x > t + timedelta(days=1):
        return "date is in the future"
    if x < t - timedelta(days=MAX_AGE_DAYS):
        return "date is too old"
    return None


def normalise(metric: str, value: float, unit: str | None) -> tuple[float | None, str | None]:
    """(canonical value, None) or (None, reason). Never clips."""
    spec = METRICS.get(metric)
    if spec is None:
        return None, f"unknown metric (allowed: {', '.join(METRICS)})"
    canon, lo, hi, units = spec
    u = (unit or canon).strip().lower()
    if u not in units:
        return None, f"unit '{unit}' not accepted for {metric} (use {', '.join(units)})"
    if value != value or value in (float("inf"), float("-inf")):
        return None, "value is not a finite number"
    v = float(value) * units[u]
    if not (lo <= v <= hi):
        return None, f"{round(v, 2)} {canon} outside plausible range {lo}-{hi}"
    return round(v, 3), None


def _upsert_daily(c, source: str, day: str, metric: str, v: float, ts: str) -> str:
    r = c.execute("SELECT value FROM wearable_daily WHERE source=? AND day=? AND metric=?", (source, day, metric)).fetchone()
    if r is None:
        c.execute("INSERT INTO wearable_daily (source, day, metric, value, imported_at) VALUES (?,?,?,?,?)", (source, day, metric, v, ts))
        return "inserted"
    if abs(r["value"] - v) < 1e-9:
        return "unchanged"
    c.execute("UPDATE wearable_daily SET value=?, imported_at=? WHERE source=? AND day=? AND metric=?", (v, ts, source, day, metric))
    return "updated"


def ingest(source: str, records: list[dict], workouts: list[dict] | None = None) -> dict:
    """Validate + upsert. Returns counts and the rejected items (with reasons). Valid items are stored even if others are rejected."""
    if not _SOURCE.match(source or ""):
        raise ValueError("source must be 1-32 chars of a-z, 0-9, _ or - (e.g. apple_health, garmin, oura)")
    ts = db.now()
    out = {"source": source, "inserted": 0, "updated": 0, "unchanged": 0, "rejected": 0, "rejected_detail": [], "workouts_inserted": 0, "workouts_updated": 0}

    def rej(what: str, why: str) -> None:
        out["rejected"] += 1
        if len(out["rejected_detail"]) < MAX_REJECT_DETAIL:
            out["rejected_detail"].append({"item": what, "reason": why})

    with db.conn() as c:
        _init(c)
        for r in records:
            why = _check_day(r["date"])
            v = None
            if why is None:
                v, why = normalise(r["metric"], r["value"], r.get("unit"))
            if why:
                rej(f"{r.get('date')}/{r.get('metric')}", why)
                continue
            out[_upsert_daily(c, source, r["date"], r["metric"], v, ts)] += 1
        for w in workouts or []:
            try:
                st = datetime.fromisoformat(w["start"])
            except (ValueError, TypeError):
                rej(f"workout {w.get('start')}", "start must be an ISO timestamp")
                continue
            day = st.date().isoformat()
            if _check_day(day):
                rej(f"workout {w['start']}", _check_day(day))
                continue
            if not _KIND.match(w["kind"] or ""):
                rej(f"workout {w['start']}", "kind must be 1-40 plain characters")
                continue
            if not (1 <= w["minutes"] <= 1440):
                rej(f"workout {w['start']}", "minutes outside plausible range 1-1440")
                continue
            kc = w.get("kcal")
            if kc is not None and not (0 <= kc <= 15000):
                rej(f"workout {w['start']}", "kcal outside plausible range 0-15000")
                continue
            ex = c.execute("SELECT 1 FROM wearable_workouts WHERE source=? AND start=? AND kind=?", (source, w["start"], w["kind"])).fetchone()
            c.execute("INSERT OR REPLACE INTO wearable_workouts (source, day, start, kind, minutes, kcal, imported_at) VALUES (?,?,?,?,?,?,?)",
                      (source, day, w["start"], w["kind"], float(w["minutes"]), kc, ts))
            out["workouts_updated" if ex else "workouts_inserted"] += 1
    return out


# ---- read side (what readiness / frames consume) ----
def daily(start: str, end: str) -> dict[str, dict[str, dict]]:
    """{day: {metric: {value, source, imported_at}}}. Several sources for one metric/day: the most recently imported wins."""
    res: dict[str, dict[str, dict]] = {}
    with db.conn() as c:
        _init(c)
        for r in c.execute("SELECT * FROM wearable_daily WHERE day BETWEEN ? AND ? ORDER BY imported_at, source", (start, end)):
            res.setdefault(r["day"], {})[r["metric"]] = {"value": r["value"], "source": r["source"], "imported_at": r["imported_at"]}
    return res


def fill_frames(rows: list[dict]) -> list[dict]:
    """Additive hook for fitness_os.mem.frames.build: adds wearable columns and fills sleep_h ONLY where no manual check-in value exists.
    Adds keys rhr, hrv_ms, steps, active_kcal, sleep_score, sleep_src ('manual' | wearable source | None). Never raises."""
    try:
        if not rows:
            return rows
        w = daily(rows[0]["date"], rows[-1]["date"])
    except Exception:
        w = {}
    for f in rows:
        m = w.get(f["date"], {})
        f["sleep_src"] = "manual" if f.get("sleep_h") is not None else None
        if f.get("sleep_h") is None and "sleep_h" in m:
            f["sleep_h"], f["sleep_src"] = m["sleep_h"]["value"], m["sleep_h"]["source"]
        for col, key in (("rhr", "resting_hr"), ("hrv_ms", "hrv_ms"), ("steps", "steps"), ("active_kcal", "active_kcal"), ("sleep_score", "sleep_score")):
            f[col] = m[key]["value"] if key in m else None
    return rows


def recent_summary(days: int = 7) -> dict:
    """Small packet for agent context: last `days` of wearable metrics with provenance, plus a simple RHR/HRV baseline delta. {} when no data."""
    t = _today()
    d = daily((t - timedelta(days=28)).isoformat(), t.isoformat())
    if not d:
        return {}
    cut = (t - timedelta(days=days - 1)).isoformat()
    last = {k: v for k, v in sorted(d.items()) if k >= cut}

    def delta(metric):
        vals = [(k, m[metric]["value"]) for k, m in sorted(d.items()) if metric in m]
        if len(vals) < 5:
            return None
        recent, base = [v for k, v in vals if k >= cut], [v for k, v in vals if k < cut]
        if not recent or len(base) < 3:
            return None
        return round(sum(recent) / len(recent) - sum(base) / len(base), 1)
    return {"days": {k: {mm: x["value"] for mm, x in m.items()} for k, m in last.items()},
            "rhr_delta_vs_baseline": delta("resting_hr"), "hrv_delta_vs_baseline": delta("hrv_ms"),
            "sources": sorted({x["source"] for m in last.values() for x in m.values()}),
            "note": "wearable-derived; manual check-ins take precedence; absent days are unknown, not zero"}


def status() -> dict:
    with db.conn() as c:
        _init(c)
        per = [dict(r) for r in c.execute("SELECT source, COUNT(*) n, MIN(day) first_day, MAX(day) last_day, MAX(imported_at) last_import FROM wearable_daily GROUP BY source")]
        mets = [dict(r) for r in c.execute("SELECT metric, COUNT(*) n FROM wearable_daily GROUP BY metric")]
        nw = c.execute("SELECT COUNT(*) FROM wearable_workouts").fetchone()[0]
    return {"connected": bool(per or nw), "sources": per, "metrics": mets, "workouts": nw, "accepted_metrics": {k: v[0] for k, v in METRICS.items()}}


def delete_data(source: str | None = None) -> dict:
    with db.conn() as c:
        _init(c)
        q, a = ("WHERE source = ?", (source,)) if source else ("", ())
        n1 = c.execute(f"DELETE FROM wearable_daily {q}", a).rowcount
        n2 = c.execute(f"DELETE FROM wearable_workouts {q}", a).rowcount
    return {"deleted": True, "daily_rows": n1, "workout_rows": n2, "source": source}


# ---- file import ----
def _merge_intervals(iv: list[tuple[datetime, datetime]]) -> float:
    iv.sort()
    tot, cur_s, cur_e = 0.0, None, None
    for s, e in iv:
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                tot += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
        elif e > cur_e:
            cur_e = e
    if cur_e is not None:
        tot += (cur_e - cur_s).total_seconds()
    return tot / 3600


def _apple_dt(s: str) -> datetime:
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")   # wall-clock time as the phone recorded it


_APPLE = {"HKQuantityTypeIdentifierRestingHeartRate": "resting_hr", "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hrv_ms",
          "HKQuantityTypeIdentifierStepCount": "steps", "HKQuantityTypeIdentifierActiveEnergyBurned": "active_kcal"}
_ASLEEP = ("Asleep", "AsleepCore", "AsleepDeep", "AsleepREM", "AsleepUnspecified")


def parse_apple_health(path: str, since_days: int = 365) -> tuple[list[dict], list[dict]]:
    """Stream export.xml with iterparse (constant memory). Returns (records, workouts). Daily steps/kcal are summed, RHR/HRV averaged,
    sleep = union of asleep intervals per wake-up date (overlapping sources are not double counted)."""
    with open(path, "rb") as fh:
        head = fh.read(262144)
    if b"<!ENTITY" in head.upper():
        raise ValueError("XML with entity declarations is refused")
    cutoff = (_today() - timedelta(days=since_days)).isoformat()
    sums: dict[tuple[str, str], list[float]] = {}
    sleep: dict[str, list] = {}
    workouts: list[dict] = []
    n = 0
    for _, el in ET.iterparse(path, events=("end",)):
        tag = el.tag
        if tag == "Record":
            t = el.get("type")
            try:
                if t in _APPLE:
                    day = (el.get("endDate") or el.get("startDate") or "")[:10]
                    if day >= cutoff:
                        v = float(el.get("value"))
                        unit = (el.get("unit") or "").lower()
                        m = _APPLE[t]
                        if m == "active_kcal" and unit in ("kj",):
                            v *= 0.239006
                        if m == "hrv_ms" and unit == "s":
                            v *= 1000
                        sums.setdefault((day, m), []).append(v)
                elif t == "HKCategoryTypeIdentifierSleepAnalysis" and (el.get("value") or "").replace("HKCategoryValueSleepAnalysis", "") in _ASLEEP:
                    s, e = _apple_dt(el.get("startDate")), _apple_dt(el.get("endDate"))
                    if e.date().isoformat() >= cutoff and e > s:
                        sleep.setdefault(e.date().isoformat(), []).append((s, e))
            except (TypeError, ValueError):
                pass
            n += 1
        elif tag == "Workout":
            try:
                st = _apple_dt(el.get("startDate"))
                if st.date().isoformat() >= cutoff:
                    dur, du = float(el.get("duration")), (el.get("durationUnit") or "min").lower()
                    mins = dur if du.startswith("min") else dur / 60 if du.startswith("s") else dur * 60 if du.startswith("h") else dur
                    kc = el.get("totalEnergyBurned")
                    workouts.append({"start": st.isoformat(), "kind": re.sub(r"^HKWorkoutActivityType", "", el.get("workoutActivityType") or "Other")[:40] or "Other",
                                     "minutes": round(mins, 1), "kcal": float(kc) if kc and (el.get("totalEnergyBurnedUnit") or "kcal").lower() in ("kcal", "cal") else None})
            except (TypeError, ValueError):
                pass
        if tag in ("Record", "Workout", "MetadataEntry", "HeartRateVariabilityMetadataList", "InstantaneousBeatsPerMinute", "WorkoutEvent", "WorkoutStatistics", "WorkoutRoute"):
            el.clear()
    records: list[dict] = []
    for (day, m), vals in sums.items():
        v = sum(vals) if m in ("steps", "active_kcal") else sum(vals) / len(vals)
        records.append({"date": day, "metric": m, "value": v, "unit": METRICS[m][0]})
    for day, iv in sleep.items():
        records.append({"date": day, "metric": "sleep_h", "value": _merge_intervals(iv), "unit": "h"})
    return records, workouts


_CSV_ALIASES = {
    "date": ("date", "calendar date", "day", "calendardate", "startdate"),
    "sleep_h": ("sleep_hours", "sleep hours", "sleep_h", "sleep"),
    "sleep_min": ("sleep_minutes", "sleep minutes", "sleep_min", "total sleep (min)"),
    "sleep_score": ("sleep_score", "sleep score"),
    "resting_hr": ("resting_hr", "resting hr", "resting heart rate", "rhr"),
    "hrv_ms": ("hrv", "hrv_ms", "hrv (ms)", "heart rate variability"),
    "steps": ("steps",),
    "active_kcal": ("active_kcal", "active calories", "active kcal", "calories active"),
}


def parse_csv(path: str) -> tuple[list[dict], list[dict], list[str]]:
    """Generic/Garmin-style daily CSV (one row per day). Streams row by row. Returns (records, workouts=[], notes)."""
    records, notes = [], []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        rd = csv.reader(fh)
        header = next(rd, None)
        if not header:
            raise ValueError("empty CSV")
        cols = {}
        for i, h in enumerate(header):
            hn = h.strip().lower()
            for key, names in _CSV_ALIASES.items():
                if hn in names and key not in cols:
                    cols[key] = i
        if "date" not in cols:
            raise ValueError("CSV needs a date column (date / Calendar Date / day)")
        found = [k for k in cols if k != "date"]
        if not found:
            raise ValueError("CSV has no recognised metric columns")
        notes.append("columns used: " + ", ".join(found))
        for ln, row in enumerate(rd, start=2):
            if ln > MAX_ROWS_CSV:
                raise ValueError(f"CSV exceeds {MAX_ROWS_CSV} rows")
            if not row or cols["date"] >= len(row):
                continue
            day = row[cols["date"]].strip()[:10]
            for key in found:
                if cols[key] >= len(row) or not row[cols[key]].strip():
                    continue
                raw = row[cols[key]].strip().replace(",", "")
                try:
                    val = float(raw)
                except ValueError:
                    records.append({"date": day, "metric": key, "value": float("nan"), "unit": None})
                    continue
                if key == "sleep_min":
                    records.append({"date": day, "metric": "sleep_h", "value": val, "unit": "min"})
                else:
                    records.append({"date": day, "metric": key, "value": val, "unit": None})
    return records, [], notes


MAX_ROWS_CSV = 20000


# ---- routes ----
@router.post("/ingest")
def post_ingest(b: IngestBatch) -> dict:
    try:
        return ingest(b.source, [r.model_dump() for r in b.records], [w.model_dump() for w in b.workouts])
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/import")
async def post_import(request: Request, format: str, source: str | None = None, since_days: int = 365) -> dict:
    """Raw-body upload (Content-Type: application/octet-stream, no multipart): `?format=apple_health` (export.xml) or `?format=csv`."""
    if format not in MAX_UPLOAD:
        raise HTTPException(400, "format must be apple_health or csv")
    if not 1 <= since_days <= MAX_AGE_DAYS:
        raise HTTPException(400, "since_days out of range")
    source = source or ("apple_health" if format == "apple_health" else "csv")
    cap = MAX_UPLOAD[format]
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > cap:
        raise HTTPException(413, f"file larger than {cap // 1048576} MB")
    fd, tmp = tempfile.mkstemp(suffix=".upload")
    try:
        size = 0
        with os.fdopen(fd, "wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > cap:
                    raise HTTPException(413, f"file larger than {cap // 1048576} MB")
                out.write(chunk)
        if size == 0:
            raise HTTPException(400, "empty upload")
        try:
            if format == "apple_health":
                recs, works = parse_apple_health(tmp, since_days)
                notes: list[str] = []
            else:
                recs, works, notes = parse_csv(tmp)
            res = ingest(source, recs[: MAX_RECORDS * 200], works[: MAX_WORKOUTS * 50])
        except (ValueError, ET.ParseError, UnicodeDecodeError) as e:
            raise HTTPException(400, f"could not read file: {e}")
        res["notes"] = notes
        return res
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


@router.get("/status")
def get_status() -> dict:
    return status()


@router.delete("/data")
def delete_my_data(source: str | None = None) -> dict:
    if source is not None and not _SOURCE.match(source):
        raise HTTPException(400, "bad source")
    return delete_data(source)
