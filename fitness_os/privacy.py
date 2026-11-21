"""Security and privacy (docs section 10): 'let the athlete export or delete everything', 'log all access'.

    export_all()    everything stored about the athlete, every table, as one JSON-able dict
    delete_all()    wipe every table; requires the exact confirmation phrase; leaves one tombstone row in the access log
    access log      every export / delete is recorded with a timestamp (the log itself is deleted with the data, except the tombstone)

Health data is sensitive: this module never sends anything anywhere, it only reads and deletes local rows.
"""
from __future__ import annotations

from contextlib import contextmanager

from backend.memory import db

from . import clock

CONFIRM = "DELETE EVERYTHING"
SCHEMA = """
CREATE TABLE IF NOT EXISTS access_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, action TEXT NOT NULL, detail TEXT);
"""
EXPORT_VERSION = 1


@contextmanager
def _c():
    with db.conn() as c:
        c.executescript(SCHEMA)
        yield c


def log_access(action: str, detail: str = "") -> None:
    with _c() as c:
        c.execute("INSERT INTO access_log (ts, action, detail) VALUES (?,?,?)", (clock.iso(), action, detail[:200]))


def access_log(limit: int = 50) -> list[dict]:
    with _c() as c:
        return [dict(r) for r in c.execute("SELECT * FROM access_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def tables() -> list[str]:
    with _c() as c:
        # the FTS5 index keeps internal shadow tables (mem_fts_data, ...): deleting rows from those directly would corrupt it, so only the virtual table itself counts
        return [r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'mem\\_fts\\_%' ESCAPE '\\' ORDER BY name").fetchall()]


def export_all() -> dict:
    """Every row of every table. JSON columns stay as text; nothing is summarised or dropped."""
    out: dict = {"export_version": EXPORT_VERSION, "exported_at": clock.iso(), "tables": {}}
    with _c() as c:
        for t in tables():
            rows = [dict(r) for r in c.execute(f'SELECT * FROM "{t}"').fetchall()]
            for r in rows:   # embedding vectors are binary and meaningless to a person: keep the record, not the bytes
                if isinstance(r.get("vec"), (bytes, bytearray)):
                    r["vec"] = f"<{len(r['vec'])} bytes>"
            out["tables"][t] = rows
    out["counts"] = {t: len(rows) for t, rows in out["tables"].items()}
    log_access("export", f"{sum(out['counts'].values())} rows in {len(out['counts'])} tables")
    return out


def delete_all(confirm: str) -> dict:
    if confirm != CONFIRM:
        raise ValueError(f"To delete everything, send the exact phrase: {CONFIRM}")
    wiped = {}
    with _c() as c:
        for t in tables():
            wiped[t] = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            c.execute(f'DELETE FROM "{t}"')
        try:
            c.execute("DELETE FROM sqlite_sequence")
        except Exception:
            pass
    log_access("delete_all", f"{sum(wiped.values())} rows removed")
    return {"deleted": True, "rows_removed": sum(wiped.values()), "tables": wiped}
