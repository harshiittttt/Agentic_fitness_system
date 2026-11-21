"""The ONE-ROUND DEBATE, deterministic and in code (the model only voices it).

The athlete is the boss, but the coach may object ONCE when a request conflicts with evidence, the goal or light safety:

    request --> hard limit?  ------------------------------> REFUSE   (explain once, never negotiable; see POLICY 'hard' rows)
            --> no concern?  ------------------------------> APPLY
            --> concern, first time for this change key ---> PUSHBACK (reasons + evidence + a concrete alternative; nothing applied)
            --> same change key asked again on a LATER turn -> APPLY with user_override = true
            --> a different change key ---------------------> the pending pushback is reset and the new request is judged on its own

State lives in the table `coach_pushback` (own table; conversation.py is not touched). The model cannot skip or force the debate: it does
not choose whether the athlete insisted, code decides by matching the normalized change key on a later turn (`begin_turn()` is called
once per athlete message), so an immediate re-call in the same reply never applies.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, field

from backend.memory import db

from . import clock, ledger

PENDING_WINDOW_MIN = 45     # how long a pushback waits for the athlete to insist
USED_WINDOW_HOURS = 24      # after complying, the same change is never pushed back on again in this window
LOW_READINESS = 55
BIG_VOLUME_JUMP = 0.15      # +15% weekly sets counts as "a lot"
STEEP_CALORIE_DROP = 0.20
SURPLUS_ON_CUT = 0.15
RECENT_INJURY_DAYS = 42
CLEAR_TOO_SOON_DAYS = 28

# The policy table. 'hard' rows are enforced as refusals in user_ops/split_ops/control (never debatable);
# 'debate' rows are the ones below: pushed back once, then the athlete's decision wins.
POLICY = [
    {"id": "injury_load", "kind": "hard", "ops": "any exercise change", "rule": "Never load through a reported injury/pain (exercise excluded by the restriction list)", "enforced_in": "split_ops._check_exercise"},
    {"id": "calorie_floor", "kind": "hard", "ops": "set_calories, set_macros", "rule": "Calories below 1500 (m) / 1200 (f) are refused", "enforced_in": "user_ops._set_calories/_set_macros"},
    {"id": "no_rest_day", "kind": "hard", "ops": "restructure_split, set_days_per_week, rebuild_plan", "rule": "More than 6 training days (fewer than 1 rest day) refused", "enforced_in": "split_ops"},
    {"id": "volume_bounds", "kind": "hard", "ops": "change_volume", "rule": "Volume must stay within 0.4x-1.6x baseline", "enforced_in": "user_ops._change_volume"},
    {"id": "absurd_value", "kind": "hard", "ops": "all", "rule": "Absurd numbers (weights, macros, calories above 6000, fraction above 100%) refused", "enforced_in": "user_ops"},
    {"id": "frozen_domain", "kind": "hard", "ops": "training/nutrition writes", "rule": "A domain frozen for safety (escalation) or the kill switch blocks the write; pain_active blocks clearing restrictions", "enforced_in": "control.submit_user_change"},
    {"id": "care_layer", "kind": "hard", "ops": "chat", "rule": "Disordered-eating or crisis language is answered by backend/coach/care.py before any model or tool runs", "enforced_in": "orchestrator._chat"},
    {"id": "cal_cut_vs_bulk", "kind": "debate", "ops": "set_calories, set_macros", "rule": "Goal is bulk but the target falls below estimated maintenance", "enforced_in": "debate.concerns"},
    {"id": "cal_steep_cut", "kind": "debate", "ops": "set_calories, set_macros", "rule": "Target is more than 20% below the current one", "enforced_in": "debate.concerns"},
    {"id": "cal_surplus_on_cut", "kind": "debate", "ops": "set_calories, set_macros", "rule": "Goal is cut but the target rises more than 15%", "enforced_in": "debate.concerns"},
    {"id": "volume_low_readiness", "kind": "debate", "ops": "plan edits that add 15%+ weekly sets", "rule": "Big volume jump while readiness is low (below 55 or 4 low check-ins) or an injury is active", "enforced_in": "debate.concerns"},
    {"id": "deload_due", "kind": "debate", "ops": "plan edits that add 5%+ weekly sets", "rule": "More volume while the data says a deload is due (readiness averaging low over the last check-ins)", "enforced_in": "debate.concerns"},
    {"id": "no_lower_body", "kind": "debate", "ops": "split/exercise edits", "rule": "Plan would contain no lower-body work although you can do some", "enforced_in": "debate.concerns"},
    {"id": "six_days", "kind": "debate", "ops": "restructure, set_days, rebuild", "rule": "6 training days for a beginner or while readiness is low", "enforced_in": "debate.concerns"},
    {"id": "experiment_running", "kind": "debate", "ops": "training edits", "rule": "A plateau experiment is running; another change confounds the result", "enforced_in": "debate.concerns"},
    {"id": "clear_recent_injury", "kind": "debate", "ops": "clear_restriction", "rule": "The restriction was reported less than 4 weeks ago", "enforced_in": "debate.concerns"},
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS coach_pushback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, key TEXT NOT NULL, op TEXT NOT NULL, params TEXT, reasons TEXT, evidence TEXT, alternative TEXT,
  turn INTEGER NOT NULL, status TEXT NOT NULL
);
"""
# status: pending | used (the athlete insisted, change applied) | reset (a different request came first) | expired


@contextmanager
def _c():
    with db.conn() as c:
        c.executescript(SCHEMA)
        yield c


@dataclass
class Concern:
    rule: str
    reason: str
    evidence: str
    alternative: dict            # {"text": str, "op": str|None, "params": dict}


@dataclass
class Gate:
    status: str                  # go | pushback | wait
    override: bool = False
    concerns: list[Concern] = field(default_factory=list)
    record_id: int | None = None


# ---- turns ----
def begin_turn() -> int:
    """Called once per athlete message. A pushback can only be overridden on a LATER turn."""
    n = int(ledger.get_setting("coach_turn", 0)) + 1
    ledger.set_setting("coach_turn", n)
    return n


def current_turn() -> int:
    return int(ledger.get_setting("coach_turn", 0))


# ---- change key ----
def change_key(op: str, params: dict, prep=None) -> str:
    f = (prep.facts if prep else {}) or {}
    if op in ("set_calories", "set_macros", "set_goal"):
        if f.get("cal_from") and f.get("cal_to") is not None:
            return f"{op}:{'cut' if f['cal_to'] < f['cal_from'] else 'raise'}"
        return op
    if op == "change_volume":
        return f"{op}:{'up' if (f.get('volume_to') or 0) > (f.get('volume_from') or 0) else 'down'}"
    if op == "set_days_per_week":
        return f"{op}:{f.get('days_after', params.get('days'))}"
    if op == "restructure_split":
        if isinstance(params.get("days"), list) and params["days"] and isinstance(params["days"][0], dict):
            return "restructure_split:custom:" + hashlib.sha1(json.dumps(params["days"], sort_keys=True, default=str).encode()).hexdigest()[:8]
        return f"restructure_split:{str(params.get('style', '')).lower().replace(' ', '_')}:{f.get('days_after', '')}"
    if op in ("add_exercise", "remove_exercise", "change_sets_reps_rpe", "replace_exercise", "move_exercise"):
        day = str(params.get("day") or params.get("from_day") or "").lower()
        ex_ = str(params.get("name") or params.get("exercise") or "").lower()
        return f"{op}:{day}:{ex_}:{str(params.get('replacement') or params.get('to_day') or '').lower()}"
    if op == "clear_restriction":
        return f"{op}:{' '.join(str(params.get('text', '')).lower().split())}"
    return op + ":" + json.dumps({k: params[k] for k in sorted(params)}, sort_keys=True, default=str)[:160]


# ---- the policy: concerns ----
def _env() -> dict:
    from backend.agents import specialists as sp

    prof = db.get_profile() or {}
    try:
        rec = sp.recovery_assess()
    except Exception:
        rec = {}
    inj = [m for m in db.get_memory(60) if m["kind"] == "injury" and db.age_days(m["ts"]) <= RECENT_INJURY_DAYS]
    score = (rec.get("readiness") or {}).get("score")
    return {"profile": prof, "plan": db.get_plan() or {}, "recovery": rec, "score": score, "injuries": inj,
            "low": bool((score is not None and score < LOW_READINESS) or rec.get("persistent_low"))}


def _readiness_text(env: dict) -> str:
    rec, parts = env["recovery"], []
    if env["score"] is not None:
        parts.append(f"readiness {env['score']}/100")
    if rec.get("persistent_low"):
        parts.append(f"averaging {rec.get('recent_avg')} over your last 4 check-ins")
    return ", ".join(parts)


def concerns(prep, params: dict | None = None) -> list[Concern]:
    op, f, out = prep.op, prep.facts, []
    env = _env()
    prof = env["profile"]
    goal = f.get("goal") or prof.get("goal", "maintain")

    if op in ("set_calories", "set_macros") and f.get("cal_from"):
        cur, new, tdee = f["cal_from"], f["cal_to"], f.get("tdee")
        if goal == "bulk" and tdee and new < tdee:
            alt = max(round(tdee * 1.05), cur if new < cur else new)
            out.append(Concern("cal_cut_vs_bulk", f"Your goal is a bulk, but {new} kcal is below your estimated maintenance ({tdee} kcal).",
                               f"goal bulk; current target {cur} kcal; estimated maintenance {tdee} kcal",
                               {"text": f"Stay at or above maintenance, around {alt} kcal, or switch the goal to cut first.", "op": "set_calories", "params": {"calories": alt}}))
        if new < cur * (1 - STEEP_CALORIE_DROP):
            step = round(cur * 0.9)
            out.append(Concern("cal_steep_cut", f"{new} kcal is a {round((1 - new / cur) * 100)}% drop from {cur} kcal, which will cost training performance and muscle.",
                               f"current target {cur} kcal; a sustainable deficit is 10-20%",
                               {"text": f"A 10% step first: {step} kcal, then reassess in two weeks.", "op": "set_calories", "params": {"calories": step}}))
        if goal == "cut" and new > cur * (1 + SURPLUS_ON_CUT):
            step = round(cur * 1.05)
            out.append(Concern("cal_surplus_on_cut", f"Your goal is a cut, but {new} kcal is {round((new / cur - 1) * 100)}% above your target of {cur} kcal.",
                               f"goal cut; current target {cur} kcal",
                               {"text": f"A small refeed step to about {step} kcal, or change the goal to maintain.", "op": "set_calories", "params": {"calories": step}}))

    sb, sa = f.get("sets_before"), f.get("sets_after")
    if op in ("restructure_split", "set_days_per_week", "add_exercise", "change_sets_reps_rpe", "change_volume", "replace_exercise", "rebuild_plan", "move_exercise") and sb and sa:
        jump = sa / sb - 1
        if jump >= BIG_VOLUME_JUMP and (env["low"] or env["injuries"]):
            why = _readiness_text(env) or "low readiness"
            inj = f"; active injury: {env['injuries'][0]['text']}" if env["injuries"] else ""
            out.append(Concern("volume_low_readiness", f"That adds {round(jump * 100)}% weekly sets ({sb} -> {sa}) while you are {'run down' if env['low'] else 'carrying an injury'}.",
                               f"{why}{inj}; weekly sets {sb} -> {sa}",
                               {"text": "Add 10% (about " + str(round(sb * 1.1)) + " sets a week) now and add more once readiness is back above 65.", "op": "change_volume", "params": {"fraction": 0.1}}))
        if jump >= 0.05 and env["recovery"].get("persistent_low"):
            out.append(Concern("deload_due", f"More volume ({sb} -> {sa} weekly sets) when your recovery data says a deload is due.",
                               f"readiness averaging {env['recovery'].get('recent_avg')} over the last 4 check-ins",
                               {"text": "Take a deload week first: about 30% fewer sets, then build back up.", "op": "change_volume", "params": {"fraction": -0.3}}))

    if op in ("restructure_split", "remove_exercise", "replace_exercise", "move_exercise", "rebuild_plan") and f.get("lower_before", True) and f.get("lower_after") is False:
        from backend.tools import split_ops as so
        ctx = so.make_ctx()
        if any(so.EXERCISES[n][1] in so.LOWER_PATTERNS for n in ctx.allowed):
            out.append(Concern("no_lower_body", "That leaves your week with no lower-body work at all (no squat, hinge or leg work).",
                               "your equipment and injury list still allow leg training; skipping legs costs strength and balance",
                               {"text": "Keep one short legs day (squat or leg press, a hinge, a calf raise) and shape the rest your way.", "op": None, "params": {}}))

    if op in ("restructure_split", "set_days_per_week", "rebuild_plan") and f.get("days_after") == 6:
        beginner = prof.get("experience") == "beginner"
        if beginner or env["low"]:
            out.append(Concern("six_days", "Six training days leaves one rest day a week" + (" for a beginner." if beginner else " while you are run down."),
                               ("experience beginner" if beginner else _readiness_text(env)) + "; recovery is where the gains happen",
                               {"text": "Five days with two rest days, then step up to six if you are recovering well.", "op": "set_days_per_week", "params": {"days": 5}}))

    if op in ("restructure_split", "set_days_per_week", "rebuild_plan", "add_exercise", "remove_exercise", "replace_exercise", "change_sets_reps_rpe", "move_exercise", "change_volume"):
        if not (op == "replace_exercise" and params and params.get("scope") == "today"):
            from . import experiments
            exp = experiments.running()
            if exp:
                out.append(Concern("experiment_running", f"A plateau experiment on {exp['lift']} ({exp['label']}) is running; changing the program now muddies the result.",
                                   f"experiment active until about {exp.get('ends') or 'it ends'}",
                                   {"text": "Wait until it finishes (or end it), then change the split.", "op": None, "params": {}}))

    if op == "clear_restriction" and f.get("age_days", 999) <= CLEAR_TOO_SOON_DAYS:
        out.append(Concern("clear_recent_injury", f"You reported '{(f.get('texts') or [''])[0]}' only {round(f['age_days'])} days ago.",
                           "restrictions are normally lifted after pain-free movement for a couple of weeks",
                           {"text": "Keep it a little longer, or lift it and test lightly with a lighter load.", "op": None, "params": {}}))
    return out


# ---- the state machine ----
def _latest(c, key: str, status: str):
    return c.execute("SELECT * FROM coach_pushback WHERE key = ? AND status = ? ORDER BY id DESC LIMIT 1", (key, status)).fetchone()


def _expire(c) -> None:
    for r in c.execute("SELECT id, ts FROM coach_pushback WHERE status = 'pending'").fetchall():
        if clock.age_days(r["ts"]) * 1440 > PENDING_WINDOW_MIN:
            c.execute("UPDATE coach_pushback SET status = 'expired' WHERE id = ?", (r["id"],))


def gate(key: str, op: str, params: dict, found: list[Concern], turn: int | None = None) -> Gate:
    turn = current_turn() if turn is None else turn
    with _c() as c:
        _expire(c)
        c.execute("UPDATE coach_pushback SET status = 'reset' WHERE status = 'pending' AND key != ?", (key,))   # a different request resets the debate
        if not found:
            return Gate("go")
        used = _latest(c, key, "used")
        if used and clock.age_days(used["ts"]) * 24 <= USED_WINDOW_HOURS:
            return Gate("go", override=True, concerns=found)                  # the one round is spent; never twice
        pend = _latest(c, key, "pending")
        if pend:
            if turn <= pend["turn"]:
                return Gate("wait", concerns=found, record_id=pend["id"])      # same reply: the athlete has not answered yet
            c.execute("UPDATE coach_pushback SET status = 'used' WHERE id = ?", (pend["id"],))
            return Gate("go", override=True, concerns=found, record_id=pend["id"])
        rid = c.execute("INSERT INTO coach_pushback (ts, key, op, params, reasons, evidence, alternative, turn, status) VALUES (?,?,?,?,?,?,?,?, 'pending')",
                        (clock.iso(), key, op, json.dumps(params, default=str), json.dumps([x.reason for x in found]), json.dumps([x.evidence for x in found]),
                         json.dumps(found[0].alternative), turn)).lastrowid
        return Gate("pushback", concerns=found, record_id=rid)


def pending() -> list[dict]:
    with _c() as c:
        _expire(c)
        return [dict(r) for r in c.execute("SELECT * FROM coach_pushback WHERE status = 'pending' ORDER BY id DESC").fetchall()]


def history(limit: int = 20) -> list[dict]:
    with _c() as c:
        return [dict(r) for r in c.execute("SELECT * FROM coach_pushback ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def clear() -> None:
    with _c() as c:
        c.execute("DELETE FROM coach_pushback")
