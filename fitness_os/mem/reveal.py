"""The weekly "what I learned about you" reveal.

    weekly(force=False) -> dict     builds this week's cards from what is NEW since the last reveal, marks them surfaced, stores the reveal, and may
                                    queue ONE nudge pointing at the screen (through the control plane, so quiet hours and the daily cap still apply)
    latest() -> dict                the most recent stored reveal (what the screen shows)

Cards (all numbers come from code, never from a model):
  discovery   a validated pattern that has not been surfaced yet (surfaced_ts is empty)
  hit         a forecast of mine that landed inside the range I gave you
  surprise    a forecast that missed by the most: the richest thing I learn from
  changed     "you used to ... now ..." (a belief that was superseded this week)
  learned     new things I picked up (non-sensitive ones; health items are flagged so the screen can collapse them)
"""
from __future__ import annotations

import importlib
import json
from datetime import timedelta

from .. import clock
from . import store

dossier = importlib.import_module(f"{__package__}.dossier")   # not `from . import dossier`: the package facade has a function of that name

SCHEMA = """
CREATE TABLE IF NOT EXISTS mem_reveal (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, cards TEXT NOT NULL, nudge TEXT
);
"""
NUDGE_TEXT = "I picked up a few new things about you this week. Take a look in Profile, under What I know about you."
UNIT = {"e1rm": " kg", "bodyweight": " kg"}
WHAT = {"e1rm": "strength", "readiness": "readiness", "adherence": "training", "bodyweight": "bodyweight"}


def _c():
    cm = store._c()
    return cm


def _ensure(c) -> None:
    c.executescript(SCHEMA)


def _last_ts() -> str | None:
    with store._c() as c:
        _ensure(c)
        r = c.execute("SELECT ts FROM mem_reveal ORDER BY id DESC LIMIT 1").fetchone()
    return r["ts"] if r else None


def latest() -> dict:
    with store._c() as c:
        _ensure(c)
        r = c.execute("SELECT * FROM mem_reveal ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return {"ts": None, "cards": [], "age_days": None}
    return {"id": r["id"], "ts": r["ts"], "cards": json.loads(r["cards"]), "nudge": json.loads(r["nudge"]) if r["nudge"] else None, "age_days": round(clock.age_days(r["ts"]), 1)}


def _fmt(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def _prediction_cards(since: str) -> list[dict]:
    rows = [r for r in store.predictions(resolved=True, limit=100) if (r.get("resolved_ts") or "") > since and r.get("error") is not None]
    hits, misses = [], []
    for r in rows:
        if r["lo"] is not None and r["hi"] is not None and r["lo"] <= r["actual"] <= r["hi"]:
            hits.append(r)
        elif r["lo"] is not None and r["hi"] is not None:
            misses.append(r)
        elif abs(r["error"]) > 0:
            (hits if abs(r["error"]) <= 0.03 * max(1.0, abs(r["predicted"])) else misses).append(r)
    cards = []

    def what(r):
        return (f"{r['subject']} " if r.get("subject") else "") + WHAT.get(r["target"], r["target"])

    if hits:
        r = min(hits, key=lambda x: abs(x["error"]) / max(1.0, abs(x["predicted"])))
        u = UNIT.get(r["target"], "")
        cards.append({"id": f"p{r['id']}", "type": "hit", "title": "I called this one", "text": f"I expected your {what(r)} around {_fmt(r['predicted'])}{u} and you got {_fmt(r['actual'])}{u}.",
                      "evidence": [f"error {_fmt(abs(r['error']))}{u}"], "sensitive": False})
    if misses:
        r = max(misses, key=lambda x: abs(x["error"]) / max(1.0, abs(x["predicted"])))
        u = UNIT.get(r["target"], "")
        way = "above" if r["error"] > 0 else "below"
        cards.append({"id": f"p{r['id']}", "type": "surprise", "title": "You surprised me", "text": f"I expected your {what(r)} around {_fmt(r['predicted'])}{u} and you got {_fmt(r['actual'])}{u}, "
                      f"{_fmt(abs(r['error']))}{u} {way} what I expected. That is how I learn what is different about you.", "evidence": [f"{_fmt(abs(r['error']))}{u} {way}"], "sensitive": False})
    return cards


def _change_cards(since: str) -> list[dict]:
    out = []
    try:
        with store._c() as c:
            rows = c.execute("SELECT o.body AS before, o.sensitivity AS sens, n.body AS now_body, n.predicate AS pred, n.id AS nid FROM mem_fact n JOIN mem_fact o ON n.supersedes = o.id "
                             "WHERE n.observed_at > ? AND n.status = 'active' ORDER BY n.id DESC LIMIT 4", (since,)).fetchall()
        for r in rows:
            out.append({"id": f"c{r['nid']}", "type": "changed", "title": "Something changed", "text": f"{dossier.phrase({'body': r['before']})} before. Now: {dossier.phrase({'body': r['now_body'], 'predicate': r['pred']})[0].lower()}"
                        f"{dossier.phrase({'body': r['now_body'], 'predicate': r['pred']})[1:]}.", "evidence": [], "fact_id": r["nid"], "sensitive": r["sens"] != "normal"})
    except Exception:
        pass
    return out


def _learned_cards(since: str) -> list[dict]:
    new = [f for f in store.facts(status=("active",), limit=100, order="id") if f["observed_at"] > since and f["predicate"] not in dossier.HIDE_PREDS
           and f["source"] != "imported" and f["conf"] >= 0.55 and not f.get("supersedes")]
    cards = []
    normal = [f for f in new if f["sensitivity"] == "normal"][:4]
    sens = [f for f in new if f["sensitivity"] != "normal"][:3]
    if normal:
        cards.append({"id": "learned", "type": "learned", "title": "New this week", "text": "; ".join(dossier.phrase(f).rstrip(".") for f in normal) + ".",
                      "evidence": [f"{len(normal)} new"], "fact_ids": [f["id"] for f in normal], "sensitive": False})
    if sens:
        cards.append({"id": "learned_health", "type": "learned", "title": "Health notes", "text": "; ".join(dossier.phrase(f).rstrip(".") for f in sens) + ".", "evidence": [], "fact_ids": [f["id"] for f in sens],
                      "sensitive": True})
    return cards


def weekly(force: bool = False) -> dict:
    """Build, store and mark surfaced. Nothing new -> no card, no row, no nudge. Calling it twice in a row does nothing the second time."""
    try:
        last = _last_ts()
        since = last or (clock.now() - timedelta(days=14)).isoformat(timespec="seconds")
        cards: list[dict] = []
        marked = []
        for d in store.discoveries(status=("confirmed", "weakening"), limit=30):
            if d.get("surfaced_ts") or d["user_state"] == "denied":
                continue
            cards.append({"id": f"d{d['id']}", "type": "discovery", "title": d["title"], "text": d["statement"] + (" This may be changing." if d["status"] == "weakening" else ""),
                          "recommendation": d.get("recommendation"), "evidence": dossier.stat_chips(d), "discovery_id": d["id"], "sensitive": False})
            marked.append(d["id"])
        cards += _prediction_cards(since)
        cards += _change_cards(since)
        cards += _learned_cards(since)
        if not cards:
            return {"generated": False, "cards": [], "marked": 0, "nudge": None}
        for did in marked:
            store.set_discovery(did, surfaced_ts=clock.iso())
        nudge = _nudge(cards)
        with store._c() as c:
            _ensure(c)
            rid = c.execute("INSERT INTO mem_reveal (ts, cards, nudge) VALUES (?,?,?)", (clock.iso(), json.dumps(cards, ensure_ascii=False), json.dumps(nudge) if nudge else None)).lastrowid
        store.audit("reveal.weekly", rid, f"{len(cards)} cards, {len(marked)} discoveries surfaced")
        return {"generated": True, "id": rid, "ts": clock.iso(), "cards": cards, "marked": len(marked), "nudge": nudge}
    except Exception as e:
        return {"generated": False, "cards": [], "marked": 0, "nudge": None, "error": f"{type(e).__name__}: {e}"[:120]}


def _nudge(cards: list[dict]) -> dict | None:
    """ONE short message pointing at the screen. It never carries a health item; quiet hours and the daily cap are enforced by the control plane."""
    if not any(not c.get("sensitive") for c in cards):
        return None
    try:
        from .. import control
        from ..schema import Proposal
        p = Proposal(agent="adherence", kind="nudge", params={"text": NUDGE_TEXT}, rationale="A weekly summary of what was learned about the athlete is ready.", confidence=0.8)
        r = control.submit(p, trigger="reveal.weekly")
        return {"status": r.get("status"), "decision_id": r.get("id")}
    except Exception as e:
        return {"status": "error", "error": type(e).__name__}
