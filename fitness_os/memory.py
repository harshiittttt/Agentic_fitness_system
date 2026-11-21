"""L4 memory: episodic (what happened), semantic (durable facts) and procedural (what works for THIS athlete).

Agents never receive the whole database. They receive a small `recall()` result, ranked by salience, freshness and relevance
to the event, so memory stays cheap, correct and specific. No embeddings yet: ranking is salience x time-decay plus keyword
overlap, which is deterministic, testable and needs no API. (The `embedding` column is intentionally absent; adding vectors
later only changes `recall`'s scoring.)
"""
from __future__ import annotations

import re
from contextlib import contextmanager

from backend.memory import db

from . import clock

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_item (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, last_seen TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('episodic','semantic','procedural')),
  body TEXT NOT NULL, salience REAL NOT NULL,
  pinned INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  source_decision INTEGER, hits INTEGER NOT NULL DEFAULT 1
);
"""
# status: active | archived (decayed or merged away) | flagged (needs the athlete to confirm) | resolved (athlete answered)

HALF_LIFE_DAYS = {"episodic": 21.0, "semantic": 180.0, "procedural": 120.0}
ARCHIVE_BELOW = 0.08
_STOP = set("the a an and or of to in on at is are was were be been it its this that for with from by as i you your my we our they their them he she his her".split())


@contextmanager
def _c():
    with db.conn() as c:
        c.executescript(SCHEMA)
        yield c


def _mirror(fn: str, *args) -> None:
    """Keep the belief store (mem_fact) in step with this table. The first call also imports everything already here. Never raises, never changes
    what this module returns: the legacy behaviour is exactly as before."""
    try:
        from .mem import migrate
        getattr(migrate, fn)(*args)
    except Exception:
        pass


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9\.]+", text.lower()) if (len(t) > 2 or t.isdigit()) and t not in _STOP}  # numbers always count


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def _row(r) -> dict:
    d = dict(r)
    d["pinned"] = bool(d["pinned"])
    return d


def effective(item: dict) -> float:
    """Salience after time decay. Pinned items (injuries, safety facts) never fade."""
    if item.get("pinned"):
        return float(item["salience"])
    age = max(0.0, clock.age_days(item["last_seen"]))
    return float(item["salience"]) * 0.5 ** (age / HALF_LIFE_DAYS.get(item["kind"], 60.0))


def add(kind: str, body: str, salience: float = 0.5, *, pinned: bool = False, source_decision: int | None = None,
        min_sim: float = 0.8, status: str = "active") -> dict:
    """Store a memory, merging into a near-identical active one (repeats reinforce instead of piling up)."""
    body = " ".join(body.split())
    if not body:
        raise ValueError("empty memory")
    salience = min(1.0, max(0.0, salience))
    with _c() as c:
        for r in c.execute("SELECT * FROM memory_item WHERE kind = ? AND status IN ('active','flagged')", (kind,)).fetchall():
            same = r["body"].lower() == body.lower() if min_sim >= 0.98 else similarity(r["body"], body) >= min_sim  # near-exact = exact
            if same:
                new_sal = min(1.0, max(r["salience"], salience) + 0.05)
                c.execute("UPDATE memory_item SET last_seen = ?, hits = hits + 1, salience = ?, pinned = MAX(pinned, ?) WHERE id = ?",
                          (clock.iso(), new_sal, int(pinned), r["id"]))
                merged_id = r["id"]
                break
        else:
            merged_id = None
        if merged_id is None:
            cur = c.execute("INSERT INTO memory_item (ts, last_seen, kind, body, salience, pinned, status, source_decision) VALUES (?,?,?,?,?,?,?,?)",
                            (clock.iso(), clock.iso(), kind, body, salience, int(pinned), status, source_decision))
            new_id = cur.lastrowid
    # mirror only after the transaction above has committed (the belief store uses its own connection)
    if merged_id is not None:
        _mirror("on_add", merged_id, True)
        return {"id": merged_id, "merged": True}
    _mirror("on_add", new_id, False)
    return {"id": new_id, "merged": False}


def get(item_id: int) -> dict | None:
    with _c() as c:
        r = c.execute("SELECT * FROM memory_item WHERE id = ?", (item_id,)).fetchone()
        return _row(r) if r else None


def items(kind: str | None = None, status: str | None = "active", limit: int = 100) -> list[dict]:
    q, args = "SELECT * FROM memory_item WHERE 1=1", []
    if kind:
        q += " AND kind = ?"; args.append(kind)
    if status:
        q += " AND status = ?"; args.append(status)
    with _c() as c:
        return [_row(r) for r in c.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()]


def set_status(item_id: int, status: str) -> bool:
    with _c() as c:
        ok = c.execute("UPDATE memory_item SET status = ? WHERE id = ?", (status, item_id)).rowcount > 0
    if ok:
        _mirror("on_status", item_id, status)
    return ok


def recall(query: str = "", k: int = 6, kinds: tuple[str, ...] | None = None) -> list[dict]:
    """Top-k active memories: 60% salience x freshness, 40% keyword overlap with the query. Small on purpose."""
    q = tokens(query)
    scored = []
    for it in items(status="active", limit=500):
        if kinds and it["kind"] not in kinds:
            continue
        eff = effective(it)
        if eff < ARCHIVE_BELOW:
            continue
        overlap = len(q & tokens(it["body"])) / max(1, len(q)) if q else 0.0
        scored.append((0.6 * eff + 0.4 * overlap, it["id"], it, eff))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [{"id": it["id"], "kind": it["kind"], "body": it["body"], "strength": round(eff, 2), "seen": it["hits"]}
            for _, _, it, eff in scored[:k]]


def archive_stale() -> int:
    """Decay: archive anything that has faded below the floor. Pinned items are exempt."""
    n = 0
    for it in items(status="active", limit=1000):
        if not it["pinned"] and effective(it) < ARCHIVE_BELOW:
            set_status(it["id"], "archived")
            n += 1
    return n
