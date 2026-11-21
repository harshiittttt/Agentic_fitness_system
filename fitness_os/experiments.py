"""Experiment registry + plateau experiments (docs: 'Plateau experiment', store 5).

    detector: a main lift has been flat for several sessions
      1. rule out confounders first (chronic short sleep, too few sessions): if present, fix that, do not experiment
      2. rank hypotheses (accumulated fatigue vs too little stimulus) from the athlete's readiness and what already failed
      3. register ONE change with ONE success metric, applied through the normal policy gate (so it can still be blocked)
      4. run 14 days; while it runs, other agents may not change the same domain (it would confound the result)
      5. measure: keep only if the lift moved up, otherwise revert; the verdict becomes procedural memory

Deterministic and testable. The LLM never decides whether an experiment worked.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import timedelta

from backend.agents import specialists as sp
from backend.memory import db

from . import clock, control, ledger, memory, reflection
from .schema import Proposal

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiment (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, lift TEXT NOT NULL, hypothesis TEXT NOT NULL, label TEXT NOT NULL,
  change TEXT NOT NULL, metric TEXT NOT NULL, baseline TEXT,
  starts TEXT, ends TEXT, status TEXT NOT NULL, verdict TEXT, result TEXT, decision_id INTEGER
);
"""
# status: pending (change awaits approval/veto window) | active | concluded | aborted

WINDOW_DAYS = 14
COOLDOWN_DAYS = 7
FAILED_MEMORY_DAYS = 60
MIN_SESSIONS_21D = 4
SLEEP_FLOOR = 6.5
WORKED_AT, FAILED_AT = 0.01, -0.02          # change in best e1RM
GRACE_DAYS = 7                               # extra wait if there were too few sessions in the window

HYPOTHESES = {
    "fatigue": {"fraction": -0.10, "label": "accumulated fatigue is masking progress", "word": "a 10% volume cut"},
    "stimulus": {"fraction": 0.05, "label": "the stimulus is too small to keep adapting", "word": "5% more volume"},
}


@contextmanager
def _c():
    with db.conn() as c:
        c.executescript(SCHEMA)
        yield c


def _row(r) -> dict:
    d = dict(r)
    for k in ("change", "baseline", "result"):
        if d.get(k):
            d[k] = json.loads(d[k])
    return d


def get(exp_id: int) -> dict | None:
    with _c() as c:
        r = c.execute("SELECT * FROM experiment WHERE id = ?", (exp_id,)).fetchone()
        return _row(r) if r else None


def listing(limit: int = 20, status: str | None = None) -> list[dict]:
    with _c() as c:
        q, a = "SELECT * FROM experiment", []
        if status:
            q += " WHERE status = ?"; a.append(status)
        return [_row(r) for r in c.execute(q + " ORDER BY id DESC LIMIT ?", (*a, limit)).fetchall()]


def running() -> dict | None:
    for e in listing(10):
        if e["status"] in ("pending", "active"):
            return e
    return None


def held_domains() -> set[str]:
    """Domains other agents must leave alone while an experiment runs (a second change would confound the result)."""
    return {"training"} if running() else set()


def _update(exp_id: int, **f) -> None:
    sets, vals = [], []
    for k, v in f.items():
        sets.append(f"{k} = ?")
        vals.append(json.dumps(v) if isinstance(v, (dict, list)) else v)
    with _c() as c:
        c.execute(f"UPDATE experiment SET {', '.join(sets)} WHERE id = ?", (*vals, exp_id))


# ---------------- evidence ----------------
def _lift_sets(lift: str) -> list[dict]:
    return [s for s in db.get_sets(None, 4000) if s["exercise"] == lift]


def _best(lift: str, start, end) -> tuple[float | None, int]:
    win = reflection._window(_lift_sets(lift), start, end)
    best = reflection._best_e1rm(win).get(lift)
    return best, len({s["session"] for s in win})


def confounders() -> list[str]:
    """Things that explain a plateau better than the program does. Fix these before experimenting."""
    out = []
    checks = [c for c in db.get_checkins(14) if c.get("sleep_h")]
    if len(checks) >= 4 and sum(c["sleep_h"] for c in checks) / len(checks) < SLEEP_FLOOR:
        out.append(f"you have averaged under {SLEEP_FLOOR:g} hours of sleep recently")
    since = clock.now() - timedelta(days=21)
    sessions = {s["session"] for s in db.get_sets(None, 2000) if clock.parse(s["ts"]) >= since}
    if len(sessions) < MIN_SESSIONS_21D:
        out.append(f"you have trained {len(sessions)} times in the last 3 weeks, which is too few to judge the program")
    return out


def _failed_recently(lift: str) -> set[str]:
    return {e["hypothesis"] for e in listing(50, "concluded")
            if e["lift"] == lift and e["verdict"] != "worked" and clock.age_days(e["ts"]) <= FAILED_MEMORY_DAYS}


def ranked(lift: str) -> list[str]:
    """Fatigue first when readiness is low, stimulus first when you are fresh. Anything that just failed is skipped."""
    avg = sp.recovery_assess().get("recent_avg")
    order = ["fatigue", "stimulus"] if avg is not None and avg < 65 else ["stimulus", "fatigue"]
    skip = _failed_recently(lift)
    return [h for h in order if h not in skip]


# ---------------- lifecycle ----------------
def start(lift: str | None = None) -> dict:
    if running():
        return {"started": False, "reason": "an experiment is already running; only one at a time so results are not confounded"}
    lift = lift or next(iter(sp.analyst_report()["plateaus"]), None)
    if not lift:
        return {"started": False, "reason": "no plateau detected"}
    conf = confounders()
    if conf:
        return {"started": False, "lift": lift, "reason": "fix this first: " + " and ".join(conf), "confounders": conf}
    cands = ranked(lift)
    if not cands:
        return {"started": False, "lift": lift, "reason": "every hypothesis was already tried recently; try something else by hand"}
    now = clock.now()
    before, _ = _best(lift, now - timedelta(days=WINDOW_DAYS), now)
    blocked = []
    for h in cands:
        spec = HYPOTHESES[h]
        res = control.submit(Proposal(
            agent="orchestrator", kind="volume_change", params={"fraction": spec["fraction"]}, confidence=0.6,
            rationale=f"Your {lift} has stalled, so I am testing whether {spec['label']}: {spec['word']} for {WINDOW_DAYS} days, then I keep it or undo it.",
            evidence=f"{lift} best e1RM {before:.1f} kg, flat over recent sessions" if before else f"{lift} flat over recent sessions"), trigger="experiment")
        if res["status"] in ("applied", "pending_veto", "pending_approval"):
            active = res["status"] == "applied"
            with _c() as c:
                eid = c.execute(
                    "INSERT INTO experiment (ts, lift, hypothesis, label, change, metric, baseline, starts, ends, status, decision_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (clock.iso(), lift, h, spec["label"], json.dumps({"kind": "volume_change", "fraction": spec["fraction"]}), f"e1rm:{lift}",
                     json.dumps({"e1rm": before}), clock.iso() if active else None, clock.plus(days=WINDOW_DAYS) if active else None,
                     "active" if active else "pending", res["id"])).lastrowid
            return {"started": True, "experiment": eid, "lift": lift, "hypothesis": h, "decision": res["id"], "status": res["status"]}
        blocked.append({"hypothesis": h, "fired": res.get("fired")})
    return {"started": False, "lift": lift, "reason": "every candidate change was blocked by a safety rule", "blocked": blocked}


def activate_pending() -> list[dict]:
    """A change that needed approval or a veto window starts the clock only once it is actually applied."""
    out = []
    for e in listing(10, "pending"):
        d = ledger.get(e["decision_id"]) if e["decision_id"] else None
        if d and d["status"] == "applied":
            _update(e["id"], status="active", starts=clock.iso(), ends=clock.plus(days=WINDOW_DAYS))
            out.append({"experiment": e["id"], "status": "active"})
        elif not d or d["status"] in ("rejected", "vetoed", "blocked", "reverted"):
            _update(e["id"], status="aborted", verdict="never_applied")
            out.append({"experiment": e["id"], "status": "aborted"})
    return out


def evaluate_due() -> list[dict]:
    out = []
    for e in listing(10, "active"):
        ends, starts = clock.parse(e["ends"]), clock.parse(e["starts"])
        if clock.now() < ends:
            continue
        before, _ = _best(e["lift"], starts - timedelta(days=WINDOW_DAYS), starts)
        after, n_after = _best(e["lift"], starts, ends)
        if n_after < 2 and clock.now() < ends + timedelta(days=GRACE_DAYS):
            continue  # too few sessions in the window: give it one more week before calling it
        delta = (after / before - 1) if (before and after) else None
        verdict = "inconclusive" if delta is None else "worked" if delta >= WORKED_AT else "failed" if delta <= FAILED_AT else "inconclusive"
        reverted = False
        if verdict != "worked":
            reverted = "error" not in control.revert(e["decision_id"])
        spec = HYPOTHESES[e["hypothesis"]]
        d = ledger.get(e["decision_id"]) or {}
        ledger.update(e["decision_id"], outcome={**(d.get("outcome") or {}), "reflected": True, "verdict": verdict, "experiment": e["id"],
                                                 "metrics": {"e1rm_change": None if delta is None else round(delta, 3)}})
        change = f" ({delta:+.1%} e1RM)" if delta is not None else " (not enough data)"
        lesson = (f"A stalled {e['lift']} broke through with {spec['word']}{change}. Kept." if verdict == "worked"
                  else f"A stalled {e['lift']} did not respond to {spec['word']}{change}. Undone; try a different lever next time.")
        mem = memory.add("procedural", lesson, 0.8 if verdict == "worked" else 0.65, source_decision=e["decision_id"])
        _update(e["id"], status="concluded", verdict=verdict,
                result={"e1rm_before": before, "e1rm_after": after, "change": delta, "sessions_in_window": n_after, "reverted": reverted, "lesson_id": mem["id"]})
        out.append({"experiment": e["id"], "lift": e["lift"], "verdict": verdict, "reverted": reverted, "change": None if delta is None else round(delta, 3)})
    return out


def maybe_start() -> dict:
    """Called by the weekly review. Cheap and safe to call any time: it only starts when every condition holds."""
    last = next(iter(listing(1, "concluded")), None)
    if last and clock.age_days(last["ts"]) < WINDOW_DAYS + COOLDOWN_DAYS:
        return {"started": False, "reason": "cooling down after the last experiment"}
    return start()
