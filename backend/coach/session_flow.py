"""Session flow: the single source of truth for WHICH day of the split every training session was, and what happens when one ends.

Before this, "which day is next" was `number of distinct session labels % days`, so a stray label, a changed split or a session just
after midnight silently moved you to a different day, and finishing a workout wrote nothing durable. Now every training session is a
row in `training_session` (created lazily, backfilled from existing sets, so old databases keep working) that records the split day
(index + name + its exercises) it was, when it started/ended and its status.

CONTRACT (other workstreams call these; signatures are stable):

    on_split_changed(old_plan, new_plan) -> None
        Call after ANY structural change to the split. Re-anchors the rotation (by day name, else exercise overlap, else day 1),
        keeps an in-progress session and today's override alive, and carries over next-session targets.

    on_session_finished(session, auto=False) -> dict
        Close the session against its split day, write an episodic memory + PRs, compute next-session load targets onto the plan and
        fire the post-workout agent loop in a background thread (never blocks, never raises).

Also: rotation_index(plan), current_label(), close_stale(), last_session(), next_targets(), status().
"""
from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from datetime import date, datetime

from backend.memory import db

STALE_HOURS = 4.0   # a session with sets and no new set for this long is over, whether or not anyone pressed "end"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS training_session (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT NOT NULL UNIQUE,            -- the sets.session label (local calendar date, e.g. 2026-11-19)
  day_index INTEGER, day_name TEXT, day_exercises TEXT, split_len INTEGER,   -- NULL day_index = not part of the rotation
  started_ts TEXT, ended_ts TEXT, status TEXT NOT NULL DEFAULT 'active',     -- active | completed | auto_closed
  n_sets INTEGER NOT NULL DEFAULT 0, result TEXT
);
"""


@contextmanager
def _c():
    with db.conn() as c:
        c.executescript(SCHEMA)
        yield c


def _today() -> str:
    from backend.tools import exercises as ex
    return ex.today()


def _now() -> datetime:
    return datetime.fromisoformat(db.now())


def _age_hours(ts: str | None) -> float:
    return (_now() - datetime.fromisoformat(ts)).total_seconds() / 3600 if ts else 1e9


def _row(r) -> dict:
    d = dict(r)
    d["day_exercises"] = json.loads(d["day_exercises"]) if d.get("day_exercises") else []
    d["result"] = json.loads(d["result"]) if d.get("result") else None
    return d


def _day_snapshot(plan: dict, idx: int) -> tuple[str, str, int]:
    day = plan["days"][idx]
    return day["name"], json.dumps([e["name"] for e in day["exercises"]]), len(plan["days"])


# ---------------------------------------------------------------- matching a recorded day to the current split
def resolve(row: dict, plan: dict) -> int | None:
    """Where the day this session was now lives in `plan`: same position+name, else same name (nearest), else best exercise overlap."""
    days = plan.get("days") or []
    i, nm = row.get("day_index"), row.get("day_name")
    if i is None or not days:
        return None
    if i < len(days) and days[i]["name"] == nm:
        return i
    same = [k for k, d in enumerate(days) if d["name"] == nm]
    if same:
        return min(same, key=lambda k: abs(k - i))
    old = {e.lower() for e in row.get("day_exercises") or []}
    best, score = None, 0.0
    for k, d in enumerate(days):
        new = {e["name"].lower() for e in d["exercises"]}
        j = len(old & new) / len(old | new) if old | new else 0.0
        if j > score:
            best, score = k, j
    return best if score >= 0.34 else None


# ---------------------------------------------------------------- registering sessions (lazy + backfill)
def _skips_between(plan: dict | None, after: str | None, upto: str | None = None) -> int:
    """Skipped sessions (plan['skipped'], written by skip_session) dated after `after` and, if given, strictly before `upto`.
    Each one moves the rotation on by one day, exactly as if that day had been trained."""
    dates = {s.get("date") for s in (plan or {}).get("skipped") or [] if DATE_RE.match(str(s.get("date") or ""))}
    return sum(1 for d in dates if (after is None or d > after) and (upto is None or d < upto))


def _label_stats(c) -> list[dict]:
    return [dict(r) for r in c.execute(
        "SELECT session AS label, MIN(id) AS mid, MIN(ts) AS t0, MAX(ts) AS t1, COUNT(*) AS n FROM sets GROUP BY session ORDER BY MIN(id)").fetchall()]


def sync(plan: dict | None = None) -> None:
    """Make the table agree with the sets: register unseen labels, refresh set counts, drop rows whose sets are gone.
    A database that has sets but no session table (every install before this) is backfilled the way the old rule behaved."""
    plan = plan if plan is not None else db.get_plan()
    total = len((plan or {}).get("days") or [])
    today = _today()
    with _c() as c:
        stats = _label_stats(c)
        known = {r["label"]: dict(r) for r in c.execute("SELECT * FROM training_session").fetchall()}
        live_labels = {s["label"] for s in stats}
        for lab, r in list(known.items()):
            if lab not in live_labels:       # sets undone or history reset (demo mode): a session without sets is not a session
                c.execute("DELETE FROM training_session WHERE label = ?", (lab,))
                known.pop(lab)
        last = c.execute("SELECT * FROM training_session WHERE day_index IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        last = _row(last) if last else None
        legacy = not known
        n_rot = 0
        for s in stats:
            lab = s["label"]
            if lab in known:
                if known[lab]["n_sets"] != s["n"]:
                    c.execute("UPDATE training_session SET n_sets = ? WHERE label = ?", (s["n"], lab))
                continue
            in_rotation = bool(DATE_RE.match(lab)) and total > 0
            if in_rotation and not legacy and last is not None and lab < last["label"]:
                in_rotation = False        # a back-dated label never moves the rotation
            idx = name = exs = None
            if in_rotation:
                if legacy:
                    idx = (n_rot + _skips_between(plan, last["label"] if last else None, lab)) % total
                else:
                    resolved = resolve(last, plan) if last else None
                    idx = ((resolved + 1 + _skips_between(plan, last["label"], lab)) % total) if resolved is not None else 0
                idx = _day_by_exercises(c, plan, lab, idx)
                name, exs, _ = _day_snapshot(plan, idx)
                n_rot += 1
            fresh = lab == today or _age_hours(s["t1"]) < STALE_HOURS
            status = "active" if fresh and in_rotation else "completed"
            c.execute("INSERT INTO training_session (label, day_index, day_name, day_exercises, split_len, started_ts, ended_ts, status, n_sets) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (lab, idx, name, exs, total if in_rotation else None, s["t0"], None if status == "active" else s["t1"], status, s["n"]))
            if in_rotation:
                last = {"label": lab, "day_index": idx, "day_name": name, "day_exercises": json.loads(exs)}


def _day_by_exercises(c, plan: dict, label: str, expected: int) -> int:
    """The rotation says which day this session SHOULD be; the sets say what was trained. If none of the lifts logged belong to the expected day but
    some belong to another one, the session was that other day (someone who skipped Lower A and lifted Upper A must not be recorded as Lower A)."""
    done = {r[0].lower() for r in c.execute("SELECT DISTINCT exercise FROM sets WHERE session = ?", (label,)).fetchall() if r[0]}
    days = plan.get("days") or []
    if not done or not days:
        return expected
    names = [{e["name"].lower() for e in d["exercises"]} for d in days]
    if done & names[expected]:
        return expected
    best = max(range(len(days)), key=lambda k: len(done & names[k]))
    return best if done & names[best] else expected


def _last_rotation_row() -> dict | None:
    with _c() as c:
        r = c.execute("SELECT * FROM training_session WHERE day_index IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    return _row(r) if r else None


def get(label: str) -> dict | None:
    with _c() as c:
        r = c.execute("SELECT * FROM training_session WHERE label = ?", (label,)).fetchone()
    return _row(r) if r else None


def sessions(limit: int = 20) -> list[dict]:
    sync()
    with _c() as c:
        return [_row(r) for r in c.execute("SELECT * FROM training_session ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def count() -> int:
    sync()
    with _c() as c:
        return c.execute("SELECT COUNT(*) FROM training_session").fetchone()[0]


def current_label() -> str:
    """The label new sets belong to: today's local date, unless a session that began before midnight is still going (last set < 4 h ago)."""
    today = _today()
    try:
        sync()
        with _c() as c:
            r = c.execute("SELECT label FROM training_session WHERE status = 'active' AND label < ? ORDER BY id DESC LIMIT 1", (today,)).fetchone()
            if r and DATE_RE.match(r["label"]) and (date.fromisoformat(today) - date.fromisoformat(r["label"])).days == 1:
                t1 = c.execute("SELECT MAX(ts) FROM sets WHERE session = ?", (r["label"],)).fetchone()[0]
                if t1 and _age_hours(t1) < STALE_HOURS:
                    return r["label"]
    except Exception:
        pass
    return today


def rotation_index(plan: dict) -> int:
    """Index in plan['days'] of the session you are in today, or the next one if today has none yet.
    The next day is the one AFTER the day of the last session that was started, matched to the CURRENT split by name."""
    total = len(plan["days"])
    sync(plan)
    last = _last_rotation_row()
    if not last:
        return _skips_between(plan, None) % total     # nothing trained yet: each skipped day (e.g. "I already did legs yesterday") still moves the start on
    idx = resolve(last, plan)
    if idx is not None:
        name, exs, n = _day_snapshot(plan, idx)
        if (idx, name, n) != (last["day_index"], last["day_name"], last["split_len"]) or json.loads(exs) != last["day_exercises"]:
            with _c() as c:   # self-heal: remember the day in the split as it is now, so later edits keep matching
                c.execute("UPDATE training_session SET day_index = ?, day_name = ?, day_exercises = ?, split_len = ? WHERE id = ?",
                          (idx, name, exs, n, last["id"]))
    if idx is None:
        return 0                       # that day no longer exists anywhere in the split: start again from day 1
    if last["label"] == current_label():
        return idx
    return (idx + 1 + _skips_between(plan, last["label"])) % total


# ---------------------------------------------------------------- split changed
def on_split_changed(old_plan: dict | None, new_plan: dict | None) -> None:
    if not new_plan or not new_plan.get("days"):
        return None
    if old_plan and old_plan.get("days"):
        sync(old_plan)      # sessions not registered yet were trained on the OLD split: pin them to it before it is forgotten
    sync(new_plan)
    last = _last_rotation_row()
    plan = db.get_plan() or new_plan
    total = len(new_plan["days"])
    if last:
        idx = resolve(last, new_plan)
        in_progress = last["label"] == current_label()
        if idx is None:
            idx = min(last["day_index"], total - 1) if in_progress else total - 1   # never wipe today; otherwise the next day is day 1
        name, exs, n = _day_snapshot(new_plan, idx)
        with _c() as c:
            c.execute("UPDATE training_session SET day_index = ?, day_name = ?, day_exercises = ?, split_len = ? WHERE id = ?",
                      (idx, name, exs, n, last["id"]))
        changed = False
        ov = plan.get("today_override")
        if ov and ov.get("date") == _today() and in_progress and ov.get("day_index") != idx:
            ov["day_index"] = idx          # the athlete is mid-session: keep the time-fitted session alive on its (moved) day
            changed = True
        if old_plan and "next_targets" in old_plan and not plan.get("next_targets"):
            have = {e["name"] for d in new_plan["days"] for e in d["exercises"]}
            keep = {k: v for k, v in old_plan["next_targets"].items() if k in have}
            if keep:
                plan["next_targets"] = keep
                changed = True
        if changed:
            db.save_plan(plan)
    return None


# ---------------------------------------------------------------- session finished
_bg: list[threading.Thread] = []


def wait_background(timeout: float = 5.0) -> None:
    for t in list(_bg):
        t.join(timeout)
    _bg.clear()


def _run_loop(payload: dict) -> None:
    try:
        from fitness_os import loop
        loop.run_event("session.logged", payload, record=False)
    except Exception:
        pass   # a model failure must never matter to the athlete


def _fire_loop(payload: dict) -> str:
    from backend.agents.base import have_key
    if not have_key():
        return "skipped: no LLM key"
    t = threading.Thread(target=_run_loop, args=(payload,), daemon=True, name="post-workout-loop")
    _bg.append(t)
    t.start()
    return "scheduled"


def _plan_targets(plan: dict) -> dict[str, dict]:
    return {e["name"]: e for d in plan.get("days", []) for e in d["exercises"]}


def compute_targets(sets: list[dict], plan: dict) -> dict[str, dict]:
    """Next-session load per exercise: autoregulate from the session's heaviest (e1RM) working set against the plan's rep/RPE target."""
    from backend.tools import calculators as calc
    tgt, out = _plan_targets(plan), {}
    by: dict[str, list[dict]] = {}
    for s in sets:
        if s["weight"] and s["reps"]:
            by.setdefault(s["exercise"], []).append(s)
    for name, ss in by.items():
        top = max(ss, key=lambda s: calc.rpe_adjusted_e1rm(s["weight"], s["reps"], s["rpe"]))
        t = tgt.get(name, {})
        sug = calc.next_set_suggestion(top["weight"], top["reps"], top["rpe"], t.get("reps", top["reps"]), t.get("target_rpe", 8.0))
        out[name] = {"weight": sug["weight"], "reps": sug["reps"], "reason": sug["reason"], "from": f"{top['weight']:g}x{top['reps']}"}
    return out


def _prs(label: str, sets: list[dict]) -> list[str]:
    from backend.tools.calculators import rpe_adjusted_e1rm as e1
    prs = []
    for name in sorted({s["exercise"] for s in sets}):
        best = max(e1(s["weight"], s["reps"], s["rpe"]) for s in sets if s["exercise"] == name)
        older = [e1(s["weight"], s["reps"], s["rpe"]) for s in db.get_sets(name, 500) if s["session"] != label and s["weight"] and s["reps"]]
        if len(older) >= 3 and best > max(older) * 1.01:
            prs.append(name)
    return prs


def on_session_finished(session: str, auto: bool = False) -> dict:
    from fitness_os import curator, memory
    plan = db.get_plan() or {}
    sync(plan or None)
    sets = [s for s in reversed(db.get_sets(limit=2000)) if s["session"] == session and s["weight"] is not None and s["reps"]]
    row = get(session)
    if not sets or not row:
        return {"session": session, "closed": False, "reason": "nothing logged"}
    if row["status"] != "active" and row["ended_ts"] and row["result"] and row["result"].get("n_sets") == len(sets):
        return {**row["result"], "already_closed": True}     # ending twice must not double-write memory or re-fire the loop
    prs = _prs(session, sets)
    tonnes = round(sum(s["weight"] * s["reps"] for s in sets) / 1000, 1)
    targets = compute_targets(sets, plan) if plan.get("days") else {}
    day_name = row.get("day_name") or "training"
    mem = curator.deterministic_summary({"day": session if DATE_RE.match(session) else db.now()[:10], "sets": sets, "checkins": [], "meals": [], "decisions": []})
    mem_id = None
    if mem:
        mem = mem.rstrip(".") + f" ({day_name})."
        mem_id = memory.add("episodic", mem, 0.55 + (0.1 if prs else 0.0), min_sim=0.98).get("id")
    for p in prs:
        best = max((s for s in sets if s["exercise"] == p), key=lambda s: s["weight"] * 100 + s["reps"])
        memory.add("semantic", f"PR on {p}: {best['weight']:g} x {best['reps']} ({session})", 0.7, min_sim=0.98)
    if targets and plan.get("days"):
        fresh = db.get_plan() or plan
        nt = dict(fresh.get("next_targets") or {})
        nt.update({k: {**v, "session": session} for k, v in targets.items()})
        fresh["next_targets"] = nt
        db.save_plan(fresh)
    result = {"session": session, "closed": True, "auto": auto, "day_index": row["day_index"], "day_name": day_name,
              "n_sets": len(sets), "tonnes": tonnes, "prs": prs, "targets": targets, "memory_id": mem_id}
    payload = {"session": session, "day": day_name, "sets": [{k: s[k] for k in ("exercise", "weight", "reps", "rpe")} for s in sets], "prs": prs}
    result["loop"] = _fire_loop(payload)
    with _c() as c:
        c.execute("UPDATE training_session SET status = ?, ended_ts = ?, n_sets = ?, result = ? WHERE label = ?",
                  ("auto_closed" if auto else "completed", db.now(), len(sets), json.dumps(result), session))
    return result


def close_stale() -> list[str]:
    """Safety net: close sessions that have sets but were never ended. Called by the scheduler tick and when a new live session opens."""
    sync()
    closed = []
    with _c() as c:
        cand = [dict(r) for r in c.execute("SELECT label FROM training_session WHERE status = 'active'").fetchall()]
        last_ts = {r["label"]: r["t1"] for r in _label_stats(c)}
    for r in cand:
        if _age_hours(last_ts.get(r["label"])) >= STALE_HOURS:
            try:
                res = on_session_finished(r["label"], auto=True)
                if res.get("closed"):
                    closed.append(r["label"])
            except Exception:
                continue
    return closed


# ---------------------------------------------------------------- read side
def last_session(before: str | None = None) -> dict | None:
    """The most recent finished session (for the coach context): {label, day_name, n_sets, tonnes, prs, ...}."""
    sync()
    with _c() as c:
        rows = [_row(r) for r in c.execute("SELECT * FROM training_session WHERE status != 'active' ORDER BY id DESC LIMIT 1").fetchall()]
    return rows[0] if rows else None


def next_targets(plan: dict | None = None, day_index: int | None = None) -> list[str]:
    """Human-readable next-session targets, for the exercises of one day (default: the next/today session)."""
    plan = plan or db.get_plan() or {}
    nt = plan.get("next_targets") or {}
    if not nt or not plan.get("days"):
        return []
    idx = rotation_index(plan) if day_index is None else day_index
    day = plan["days"][idx % len(plan["days"])]
    return [f"{e['name']} {nt[e['name']]['weight']:g}x{nt[e['name']]['reps']}" for e in day["exercises"] if e["name"] in nt]


def status() -> dict:
    """The one truth about where the athlete is in the split (used by chat, live and the Today screen alike)."""
    plan = db.get_plan() or {}
    if not plan.get("days"):
        return {"has_plan": False}
    idx = rotation_index(plan)
    last = _last_rotation_row()
    return {"has_plan": True, "today_index": idx, "day_name": plan["days"][idx]["name"], "split_len": len(plan["days"]),
            "in_session_today": bool(last and last["label"] == current_label()), "sessions": count(),
            "last_label": last["label"] if last else None}
