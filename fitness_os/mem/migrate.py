"""Bridge from the legacy memory to the belief store.

`import_legacy()` copies every durable row of `memory_item` (fitness_os/memory.py: episodic / semantic / procedural lessons) and of the older
`memory` table (backend/memory/db.py: injury / preference / skip / semantic notes) into `mem_fact` as beliefs (source "imported"), choosing a
predicate, a sensitivity and a pin from the text (an injury becomes `injury_site`, health, pinned). It is idempotent: a private table
`mem_migrated(src, fact_id)` remembers what was imported, so running it again (or after every write) adds nothing twice.

`fitness_os/memory.py` calls `on_add` / `on_status` after its own writes so the new store stays in step; both call `ensure()` first, which runs
the import once per database file. Nothing here ever raises into the legacy code path.
"""
from __future__ import annotations

import re
import sqlite3

from backend.memory import db

from . import store
from .types import register_predicate

register_predicate("note", functional=False, half_life=180, sensitivity="normal", cls="other")
register_predicate("lesson", functional=False, half_life=200, sensitivity="normal", cls="response")
register_predicate("skips_exercise", functional=False, half_life=90, sensitivity="normal", cls="training")
register_predicate("care_flag", functional=False, half_life=120, sensitivity="mental", cls="risk")
register_predicate("preference_note", functional=False, half_life=240, sensitivity="normal", cls="training")

SCHEMA = "CREATE TABLE IF NOT EXISTS mem_migrated (src TEXT PRIMARY KEY, fact_id INTEGER NOT NULL, ts TEXT NOT NULL);"
_ready: set[str] = set()          # database files whose legacy rows have been imported in this process
_schema: set[str] = set()

HEALTH = re.compile(r"\b(pain|painful|injur\w*|hurts?|hurting|ache\w*|strain\w*|sprain\w*|surgery|tendon\w*|pinch\w*|sore|flare\w*)\b", re.I)
_PREFIXES = [
    (re.compile(r"^dislikes?:?\s+(.+)$", re.I), "dislikes_exercise", "preference", "You dislike {o}."),
    (re.compile(r"^likes?:\s+(.+)$", re.I), "likes_exercise", "preference", "You like {o}."),
    (re.compile(r"^pain\s*/\s*injury:?\s+(.+)$", re.I), "injury_site", "constraint", "Pain or injury: {o}."),
    (re.compile(r"^availability:?\s+(.+)$", re.I), "trains_days_per_week", "constraint", "Availability: {o}."),
    (re.compile(r"^can only train (\d)\s+days?.*$", re.I), "trains_days_per_week", "constraint", "You can only train {o} days per week."),
    (re.compile(r"^equipment:?\s+(.+)$", re.I), "available_equipment", "semantic", "Equipment: {o}."),
    (re.compile(r"^prefers\s+(.+?)\s+over\s+.+$", re.I), "likes_exercise", "preference", None),
]


def _c():
    return store._c()


def _ensure_schema() -> None:
    if db.DB_PATH in _schema:
        return
    with _c() as c:
        c.executescript(SCHEMA)
    _schema.add(db.DB_PATH)


def classify(table: str, kind: str, text: str) -> dict:
    """-> {pred, obj, body, kind, sens, polarity}: how one legacy row becomes a belief."""
    t = " ".join(str(text or "").split()).strip()
    body = t.rstrip()
    sens = None
    for rx, pred, fkind, tmpl in _PREFIXES:
        m = rx.match(t)
        if m:
            o = m.group(1).strip().rstrip(".")
            if pred == "trains_days_per_week":
                d = re.search(r"\d", o)
                o = d.group(0) if d else o
            return {"pred": pred, "obj": o, "body": tmpl.format(o=o) if tmpl else t, "kind": fkind, "sens": sens, "polarity": -1 if pred == "dislikes_exercise" else 0}
    if table == "memory":
        if kind == "injury":
            return {"pred": "injury_site", "obj": t.rstrip("."), "body": f"Pain or injury: {t.rstrip('.')}.", "kind": "constraint", "sens": None, "polarity": 0}
        if kind == "preference":
            return {"pred": "preference_note", "obj": t, "body": t, "kind": "preference", "sens": None, "polarity": 0}
        if kind == "skip":
            ex = t.split("|", 1)[-1].strip()
            return {"pred": "skips_exercise", "obj": ex, "body": f"You skipped {ex}.", "kind": "episodic", "sens": None, "polarity": -1}
        if t.lower().startswith("care flag"):
            return {"pred": "care_flag", "obj": t, "body": t, "kind": "semantic", "sens": "mental", "polarity": 0}
    if table == "memory_item":
        if kind == "episodic":
            return {"pred": "happened", "obj": t, "body": t, "kind": "episodic", "sens": None, "polarity": 0}
        if kind == "procedural":
            return {"pred": "lesson", "obj": t, "body": t, "kind": "procedural", "sens": None, "polarity": 0}
        if t.lower().startswith("pr on"):
            return {"pred": "milestone", "obj": t, "body": t, "kind": "episodic", "sens": None, "polarity": 0}
    if re.search(r"\bveg(an|etarian)\b|\bno meat\b", t, re.I):
        return {"pred": "diet_type", "obj": "vegan" if re.search(r"\bvegan\b", t, re.I) else "vegetarian", "body": t, "kind": "semantic", "sens": None, "polarity": 0}
    if HEALTH.search(t):
        return {"pred": "injury_site", "obj": t.rstrip("."), "body": body, "kind": "constraint", "sens": None, "polarity": 0}
    return {"pred": "note", "obj": t, "body": t, "kind": "semantic", "sens": None, "polarity": 0}


def _mapped(c, src: str) -> int | None:
    r = c.execute("SELECT m.fact_id FROM mem_migrated m JOIN mem_fact f ON f.id = m.fact_id WHERE m.src = ?", (src,)).fetchone()
    return r["fact_id"] if r else None


def _import_row(src: str, table: str, kind: str, text: str, ts: str, salience: float, pinned: bool, status: str) -> str:
    """-> 'imported' | 'already' | 'skipped'."""
    if not str(text or "").strip():
        return "skipped"
    with _c() as c:
        if _mapped(c, src):
            return "already"
    if status in ("flagged", "resolved"):                   # a question for the athlete, not a belief
        return "skipped"
    k = classify(table, kind, text)
    res = store.add_fact(k["pred"], k["obj"], body=k["body"], kind=k["kind"], source="imported", sensitivity=k["sens"], polarity=k["polarity"], salience=float(salience),
                         pinned=True if pinned else None, valid_from=ts, evidence=[{"t": "legacy", "src": src}])
    fid = res["id"]
    if not fid:
        return "skipped"
    if status == "archived":
        store.update_fact(fid, status="archived")
    with _c() as c:
        c.execute("INSERT OR REPLACE INTO mem_migrated (src, fact_id, ts) VALUES (?,?,?)", (src, fid, store.now()))
    return "imported"


def import_legacy() -> dict:
    """Import `memory_item` and the older `memory` table into `mem_fact` (source "imported"). Idempotent. Never raises."""
    rep = {"memory_item": 0, "memory": 0, "already": 0, "skipped": 0}
    try:
        _ensure_schema()
        with _c() as c:
            try:
                items = [dict(r) for r in c.execute("SELECT * FROM memory_item ORDER BY id")]
            except sqlite3.OperationalError:
                items = []
            try:
                old = [dict(r) for r in c.execute("SELECT * FROM memory ORDER BY id")]
            except sqlite3.OperationalError:
                old = []
        for it in items:
            r = _import_row(f"item:{it['id']}", "memory_item", it["kind"], it["body"], it["ts"], it["salience"], bool(it["pinned"]), it["status"])
            rep["memory_item" if r == "imported" else r] += 1
        for m in old:
            r = _import_row(f"mem:{m['id']}", "memory", m["kind"], m["text"], m["ts"], 0.6, False, "active")
            rep["memory" if r == "imported" else r] += 1
    except Exception as e:
        rep["error"] = f"{type(e).__name__}: {e}"[:160]
    return rep


def ensure() -> None:
    """Run import_legacy once per database file (cheap afterwards)."""
    if db.DB_PATH in _ready:
        return
    _ready.add(db.DB_PATH)
    import_legacy()


# ---- mirrors called by fitness_os/memory.py after its own writes -------------------------------------------------------------------------
def on_add(item_id: int, merged: bool) -> None:
    """A memory_item was added (or merged into an existing one): keep mem_fact in step. Never raises."""
    try:
        ensure()
        with _c() as c:
            row = c.execute("SELECT * FROM memory_item WHERE id = ?", (item_id,)).fetchone()
            fid = _mapped(c, f"item:{item_id}")
        if row is None:
            return
        if fid is None:
            _import_row(f"item:{item_id}", "memory_item", row["kind"], row["body"], row["ts"], row["salience"], bool(row["pinned"]), row["status"])
        elif merged:
            store.reinforce(fid, True, weight=0.5)
    except Exception:
        pass


def on_status(item_id: int, status: str) -> None:
    try:
        ensure()
        with _c() as c:
            fid = _mapped(c, f"item:{item_id}")
        if fid is None:
            row = None
            with _c() as c:
                row = c.execute("SELECT * FROM memory_item WHERE id = ?", (item_id,)).fetchone()
            if row is not None and status == "active":
                _import_row(f"item:{item_id}", "memory_item", row["kind"], row["body"], row["ts"], row["salience"], bool(row["pinned"]), "active")
            return
        if status in ("archived", "resolved"):
            store.update_fact(fid, status="archived")
        elif status == "active":
            f = store.get_fact(fid)
            if f and f["status"] == "archived" and f["user_state"] != "denied":
                store.update_fact(fid, status="active")
    except Exception:
        pass
