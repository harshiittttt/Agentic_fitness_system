"""Always-on watchdogs. Plain code that runs outside the agents, so no agent can talk another out of a flag."""
from __future__ import annotations

from collections import defaultdict

from backend.agents import specialists as sp
from backend.memory import db
from backend.policy.adaptation import has_safety_flag, readiness_score

from . import clock, ledger
from .schema import Proposal

# Phrases that always escalate: stop and refer, never coach through.
RED_FLAGS = ("chest pain", "chest tight", "faint", "passed out", "blacked out", "can't breathe", "cannot breathe",
             "shortness of breath", "numb", "tingl", "severe pain", "heart racing", "सीने में दर्द", "बेहोश")
RESTRICTION_LANGUAGE = ("starve", "starving myself", "skip meals", "skipped all meals", "purge", "throw up", "binge",
                        "punish", "i hate my body", "don't deserve to eat", "fast all day", "laxative", "guilty for eating",
                        "eating less to", "eat less to", "भूखा रह", "भूखे रह", "खाना कम खा", "कम खा रहा", "पतला हो जाऊँ", "पतला हो जाऊं", "उल्टी कर", "खुद को सज़ा")
EXTREME_LOW_KCAL = 1000


def _recent_notes(days: int = 14) -> list[tuple[str, str, str]]:
    out = []
    for c in db.get_checkins(60):
        if c.get("note") and clock.age_days(c["ts"]) <= days:
            out.append((c["ts"][:10], "checkin", c["note"]))
    for s in db.get_sets(limit=300):
        if s.get("note"):
            out.append((s["ts"][:10], "set", s["note"]))
    return out


def red_flag(text: str) -> str | None:
    t = text.lower()
    return next((f for f in RED_FLAGS if f in t), None)


def injury_watchdog() -> list[dict]:
    notes = _recent_notes(14)
    for day, src, text in notes:
        rf = red_flag(text)
        if rf:
            return [{"watchdog": "injury", "severity": "escalate", "evidence": f"{day} {src}: '{text}' (red flag: {rf})",
                     "proposal": Proposal(agent="watchdog", kind="escalate", params={"domain": "training", "reason": rf},
                                          rationale=f"Red-flag symptom reported ('{rf}'). Stop training advice and see a medical professional.",
                                          evidence=f"{day}: {text}", confidence=1.0)}]
    days = defaultdict(list)
    for day, src, text in notes:
        flag = has_safety_flag(text)
        if flag:
            days[day].append(text)
    if len(days) >= 2:
        sample = next(iter(days.values()))[0]
        return [{"watchdog": "injury", "severity": "warn", "evidence": f"Pain/injury mentioned on {len(days)} separate days: {sorted(days)}",
                 "proposal": Proposal(agent="watchdog", kind="restriction",
                                      params={"text": f"Pain reported repeatedly ('{sample[:60]}'): avoid loading the affected area; no volume increases"},
                                      rationale="Repeated pain mentions across sessions.", evidence=f"days: {sorted(days)}", confidence=0.9)}]
    return []


def overtraining_watchdog() -> list[dict]:
    checks = db.get_checkins(8)
    if len(checks) < 4:
        return []
    scores = [readiness_score(c["sleep_h"] or 7.5, c["soreness"] or 3, c["energy"] or 3)["score"] for c in checks[:4]]
    avg = sum(scores) / len(scores)
    an = sp.analyst_report()
    falling = [k for k, v in an["lifts"].items() if v["trend"] == "down"]
    if avg < sp.PERSISTENT_LOW_AVG and falling:
        return [{"watchdog": "overtraining", "severity": "warn",
                 "evidence": f"Readiness avg {avg:.0f} over last 4 check-ins and falling lifts: {falling}",
                 "proposal": Proposal(agent="watchdog", kind="volume_change", params={"fraction": -0.15},
                                      rationale="Low readiness plus falling performance: classic overreach. Cut volume 15%.",
                                      evidence=f"readiness {scores}, falling {falling}", confidence=0.8)}]
    return []


def eating_watchdog() -> list[dict]:
    for day, src, text in _recent_notes(14):
        t = text.lower()
        hit = next((p for p in RESTRICTION_LANGUAGE if p in t), None)
        if hit:
            return [{"watchdog": "disordered_eating", "severity": "escalate", "evidence": f"{day} {src}: '{text}'",
                     "proposal": Proposal(agent="watchdog", kind="escalate", params={"domain": "nutrition", "reason": hit},
                                          rationale="Language that can signal disordered eating. Calorie coaching is paused; please talk to a qualified professional.",
                                          evidence=f"{day}: {text}", confidence=1.0)}]
    totals: dict[str, float] = defaultdict(float)
    for d in {c["ts"][:10] for c in db.get_checkins(30)}:
        for m in db.get_meals(d):
            totals[d] += m["kcal"] or 0
    low_days = [d for d, k in totals.items() if 0 < k < EXTREME_LOW_KCAL and clock.age_days(d + "T00:00:00") <= 7]
    if len(low_days) >= 3:
        return [{"watchdog": "disordered_eating", "severity": "escalate", "evidence": f"Under {EXTREME_LOW_KCAL} kcal logged on {sorted(low_days)}",
                 "proposal": Proposal(agent="watchdog", kind="escalate", params={"domain": "nutrition", "reason": "extreme deficit"},
                                      rationale="Several days of extremely low intake. Calorie changes are frozen; please speak to a professional.",
                                      evidence=f"low days {sorted(low_days)}", confidence=0.9)}]
    return []


def run_all() -> list[dict]:
    return injury_watchdog() + overtraining_watchdog() + eating_watchdog()


def already_open(w: dict) -> bool:
    """Don't re-raise a flag whose effect is already in force."""
    p: Proposal = w["proposal"]
    if p.kind == "escalate":
        return p.params["domain"] in ledger.frozen_domains() or "all" in ledger.frozen_domains()
    if p.kind == "restriction":
        return any(m["text"] == p.params["text"] for m in db.get_memory(30) if m["kind"] == "injury")
    if p.kind == "volume_change":
        return any(d["agent"] == "watchdog" and d["kind"] == "volume_change" and clock.age_days(d["ts"]) < 7
                   and d["status"] in ("applied", "pending_veto", "pending_approval") for d in ledger.recent(50))
    return False
