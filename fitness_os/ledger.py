"""Decision ledger (append-only history of every autonomous action) + control settings."""
from __future__ import annotations

import json
from contextlib import contextmanager

from backend.memory import db

from . import clock

SCHEMA = """
CREATE TABLE IF NOT EXISTS decision (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, agent TEXT NOT NULL, trigger_event TEXT, kind TEXT NOT NULL,
  proposal TEXT NOT NULL, action TEXT, rationale TEXT, evidence TEXT,
  tier TEXT NOT NULL, fired TEXT, status TEXT NOT NULL,
  veto_until TEXT, undo TEXT, outcome TEXT, reverted_by INTEGER, resolved_ts TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
# status: applied | pending_veto | pending_approval | escalated | blocked | rejected | vetoed | reverted


@contextmanager
def _c():
    with db.conn() as c:
        c.executescript(SCHEMA)
        yield c


def _row(r) -> dict:
    d = dict(r)
    for k in ("proposal", "action", "fired", "undo", "outcome"):
        if d.get(k):
            d[k] = json.loads(d[k])
    return d


def append(*, agent: str, kind: str, proposal: dict, action: dict | None, rationale: str, evidence: str,
           tier: str, fired: list[str], status: str, trigger: str = "manual", veto_until: str | None = None,
           undo: dict | None = None) -> int:
    with _c() as c:
        cur = c.execute(
            "INSERT INTO decision (ts, agent, trigger_event, kind, proposal, action, rationale, evidence, tier,"
            " fired, status, veto_until, undo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (clock.iso(), agent, trigger, kind, json.dumps(proposal), json.dumps(action), rationale, evidence,
             tier, json.dumps(fired), status, veto_until, json.dumps(undo) if undo else None))
        return cur.lastrowid


def proposal_of(d: dict):
    """The Proposal behind a ledger row. Old revert rows stored {"revert_of": id} instead of a proposal; read those as a plain note, never crash."""
    from .schema import Proposal
    try:
        return Proposal(**d["proposal"])
    except Exception:
        text = (d.get("rationale") or "A change").strip()
        rid = (d.get("proposal") or {}).get("revert_of") if isinstance(d.get("proposal"), dict) else None
        return Proposal(agent=d.get("agent") or "you", kind="note", params={"text": text, **({"revert_of": rid} if rid else {})},
                        rationale=text if len(text) >= 3 else "A change", evidence=d.get("evidence") or "")


def get(decision_id: int) -> dict | None:
    with _c() as c:
        r = c.execute("SELECT * FROM decision WHERE id = ?", (decision_id,)).fetchone()
        return _row(r) if r else None


def recent(limit: int = 30, status: str | None = None) -> list[dict]:
    with _c() as c:
        if status:
            rows = c.execute("SELECT * FROM decision WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit))
        else:
            rows = c.execute("SELECT * FROM decision ORDER BY id DESC LIMIT ?", (limit,))
        return [_row(r) for r in rows.fetchall()]


def update(decision_id: int, **fields) -> None:
    cols, vals = [], []
    for k, v in fields.items():
        cols.append(f"{k} = ?")
        vals.append(json.dumps(v) if isinstance(v, (dict, list)) else v)
    with _c() as c:
        c.execute(f"UPDATE decision SET {', '.join(cols)} WHERE id = ?", (*vals, decision_id))


# ---- settings: kill switch, autonomy dial, frozen domains ----
def set_setting(key: str, value) -> None:
    with _c() as c:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))


def get_setting(key: str, default=None):
    with _c() as c:
        r = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default


def kill_switch() -> bool:
    return bool(get_setting("kill_switch", False))


def dial() -> str:
    return get_setting("dial", "balanced")  # hands_on | balanced | autopilot


def frozen_domains() -> list[str]:
    return get_setting("frozen", [])


def freeze(domain: str) -> None:
    f = set(frozen_domains())
    f.add(domain)
    set_setting("frozen", sorted(f))


def unfreeze(domain: str) -> None:
    set_setting("frozen", [d for d in frozen_domains() if d != domain])


def applied_volume_increase(days: float = 7) -> float:
    """Sum of volume increases applied (or about to be) in the last `days`, for the weekly load cap."""
    total = 0.0
    for d in recent(200):
        if d["kind"] == "volume_change" and d["status"] in ("applied", "pending_veto") and clock.age_days(d["ts"]) <= days:
            f = (d.get("action") or {}).get("fraction", 0)
            if f > 0:
                total += f
    return total
