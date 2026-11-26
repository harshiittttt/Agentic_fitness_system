"""Session routes the app needs around a workout: undo a mis-tapped set, see today's sets, the end-of-workout summary,
and a quick body-weight weigh-in. Kept in its own router so main.py only needs one include line."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.memory import db
from backend.tools import exercises as ex
from backend.tools import plan_edit as pe
from backend.tools.calculators import rpe_adjusted_e1rm

router = APIRouter()


def _set_row(r: dict) -> dict:
    return {"id": r["id"], "ts": r["ts"], "session": r["session"], "exercise": r["exercise"],
            "weight": r["weight"], "reps": r["reps"], "rpe": r["rpe"]}


@router.get("/session/sets")
def session_sets(session: str | None = None, exercise: str | None = None):
    """Sets logged in one session (default today), oldest first. Optionally only one exercise."""
    day = session or ex.today()
    rows = [r for r in db.get_sets(exercise, 1000) if r["session"] == day]
    return [_set_row(r) for r in reversed(rows)]


class Unskip(BaseModel):
    exercise: str | None = None      # one exercise by name; omitted = the latest skip of today
    everything: bool = False         # true = put every exercise skipped today back


@router.post("/live/unskip")
def live_unskip(b: Unskip | None = None):
    """Put a skipped exercise back into today's session (the app has a skip button; this is its undo). Same result shape as /live/say:
    {intent: 'unskip', reply, restored: [names], state}. Nothing skipped -> restored is [] and the reply says so."""
    from backend.coach import live
    b = b or Unskip()
    return live.unskip_now(b.exercise, b.everything)


@router.delete("/session/set/{set_id}")
def delete_set(set_id: int):
    """Undo one logged set. Returns the deleted values so the app can put them back in the logger to correct them."""
    with db.conn() as c:
        row = c.execute("SELECT * FROM sets WHERE id = ?", (set_id,)).fetchone()
        if not row:
            raise HTTPException(404, "That set is already gone")
        c.execute("DELETE FROM sets WHERE id = ?", (set_id,))
    return {"ok": True, "deleted": _set_row(dict(row))}


def _local_day(ts: str) -> str:
    """The local calendar day of a stored UTC timestamp, so 'today' means the same day the lifter and ex.today() mean."""
    try:
        return datetime.fromisoformat(ts).astimezone().date().isoformat()
    except ValueError:
        return ts[:10]


def _minutes_between(a: str, b: str) -> int | None:
    try:
        return round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60)
    except ValueError:
        return None


@router.get("/session/summary")
def session_summary(session: str | None = None):
    """What you did in a workout: sets, volume, best lift per exercise, PRs, time, and what comes next."""
    day = session or ex.today()
    rows = [r for r in db.get_sets(None, 2000) if r["session"] == day]
    rows.sort(key=lambda r: r["id"])
    lifted = [r for r in rows if r["weight"] is not None and r["reps"]]
    by_ex: dict[str, list[dict]] = {}
    for r in rows:
        by_ex.setdefault(r["exercise"], []).append(r)

    exercises, prs = [], []
    for name, sets in by_ex.items():
        valid = [s for s in sets if s["reps"]]
        best = max(valid, key=lambda s: (s["weight"] or 0) * 100 + s["reps"], default=None)
        exercises.append({"name": name, "sets": len(sets),
                          "volume_kg": round(sum((s["weight"] or 0) * s["reps"] for s in valid)),
                          "best": {"weight": best["weight"], "reps": best["reps"]} if best else None})
        scored = [s for s in valid if s["weight"]]
        if scored:
            today_best = max(rpe_adjusted_e1rm(s["weight"], s["reps"], s["rpe"]) for s in scored)
            older = [rpe_adjusted_e1rm(s["weight"], s["reps"], s["rpe"]) for s in db.get_sets(name, 1000)
                     if s["session"] != day and s["weight"] and s["reps"]]
            if len(older) >= 3 and today_best > max(older) * 1.01:
                prs.append(name)

    duration = _minutes_between(rows[0]["ts"], rows[-1]["ts"]) if len(rows) >= 2 else None
    view = pe.plan_view()
    planned = done_planned = 0
    day_name, nxt = None, None
    if view:
        cur = (view.get("today_override") or {}).get("day") or view["days"][view["today_index"]]
        day_name = cur["name"]
        counts = {n: len(s) for n, s in by_ex.items()}
        planned = sum(e["sets"] for e in cur["exercises"])
        done_planned = sum(min(e["sets"], counts.get(e["name"], 0)) for e in cur["exercises"])
        if len(view["days"]) > 1:
            n = view["days"][(view["today_index"] + 1) % len(view["days"])]
            nxt = {"name": n["name"], "exercises": len(n["exercises"]), "est_minutes": n.get("est_minutes")}
    finished = bool(planned) and done_planned >= planned
    if finished and day == ex.today():     # the app shows the finish screen: close the session against its split day (idempotent)
        try:
            from backend.coach import session_flow
            session_flow.on_session_finished(day)
        except Exception:
            pass
    return {"session": day, "sets": len(rows), "volume_kg": round(sum(r["weight"] * r["reps"] for r in lifted)),
            "exercises": exercises, "prs": prs, "duration_min": duration if duration and duration > 0 else None,
            "day_name": day_name, "planned_sets": planned, "done_planned_sets": done_planned,
            "finished": finished, "next": nxt}


class WeighIn(BaseModel):
    bodyweight: float = Field(ge=30, le=300)


@router.post("/session/weighin")
def weigh_in(w: WeighIn):
    """Record a body weight without disturbing the readiness inputs: today's check-in row is updated in place, otherwise a new
    row carries over the latest sleep/soreness/energy (a weight-only row would make readiness fall back to defaults)."""
    today = _local_day(db.now())
    with db.conn() as c:
        last = c.execute("SELECT * FROM checkins ORDER BY id DESC LIMIT 1").fetchone()
        if last and _local_day(last["ts"]) == today:
            c.execute("UPDATE checkins SET bodyweight = ? WHERE id = ?", (w.bodyweight, last["id"]))
        else:
            c.execute("INSERT INTO checkins (ts, sleep_h, soreness, energy, bodyweight, note) VALUES (?,?,?,?,?,NULL)",
                      (db.now(), last["sleep_h"] if last else None, last["soreness"] if last else None,
                       last["energy"] if last else None, w.bodyweight))
    return {"ok": True}


@router.get("/session/checkins")
def checkin_summary(limit: int = 30):
    """Body-weight history (one point per day, oldest first) and today's check-in values, for the Profile screen."""
    rows = db.get_checkins(max(1, min(limit, 120)))
    per_day: dict[str, float] = {}
    for r in sorted(rows, key=lambda r: (r["ts"], r["id"])):   # oldest first; the last reading of a day wins
        if r["bodyweight"]:
            per_day[_local_day(r["ts"])] = r["bodyweight"]
    weights = [{"day": d, "kg": kg} for d, kg in per_day.items()]
    today = next((r for r in rows if _local_day(r["ts"]) == _local_day(db.now()) and (r["sleep_h"] or r["soreness"] or r["energy"])), None)
    return {"weights": weights, "last_weight": weights[-1] if weights else None,
            "today": {"sleep_h": today["sleep_h"], "soreness": today["soreness"], "energy": today["energy"]} if today else None}
