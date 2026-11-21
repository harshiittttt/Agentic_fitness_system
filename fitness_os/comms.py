"""comms-mcp (docs layer 3): the outbound channel to the athlete, with quiet hours and a frequency cap enforced in CODE.

An agent can only PROPOSE a `nudge`; policy.check_action asks `check()` here and blocks it inside quiet hours or over the cap, so no
prompt can talk the system into spamming. Approved nudges land in the `nudge` table, which the app reads (GET /agents/nudges).
"""
from __future__ import annotations

from contextlib import contextmanager

from backend.memory import db

from . import clock

SCHEMA = """
CREATE TABLE IF NOT EXISTS nudge (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, agent TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'unread', decision_id INTEGER
);
"""
QUIET_START, QUIET_END = 22, 7      # athlete-local hours: no outreach from 22:00 to 07:00
DAILY_CAP = 2                       # at most this many nudges per rolling 24 h
MIN_GAP_HOURS = 6                   # and never two within this many hours
MAX_LEN = 240


@contextmanager
def _c():
    with db.conn() as c:
        c.executescript(SCHEMA)
        yield c


def _local_hour() -> int:
    from . import scheduler          # lazy: scheduler imports control, control imports policy, policy imports this
    return scheduler.athlete_now().hour


def in_quiet_hours(hour: int | None = None) -> bool:
    h = _local_hour() if hour is None else hour
    return h >= QUIET_START or h < QUIET_END


def recent(limit: int = 20, hours: float | None = None) -> list[dict]:
    with _c() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM nudge ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
    return [r for r in rows if hours is None or clock.age_days(r["ts"]) * 24 <= hours]


def check() -> tuple[bool, str, str]:
    """-> (allowed, explanation, rule that fired)."""
    if in_quiet_hours():
        return False, f"Quiet hours ({QUIET_START}:00-{QUIET_END:02d}:00): no messages to you at night.", "quiet_hours"
    last_day = recent(10, hours=24)
    if len(last_day) >= DAILY_CAP:
        return False, f"Already sent {len(last_day)} messages in the last 24 hours; holding to avoid spamming you.", "nudge_cap"
    if last_day and clock.age_days(last_day[0]["ts"]) * 24 < MIN_GAP_HOURS:
        return False, f"The last message was under {MIN_GAP_HOURS} hours ago.", "nudge_gap"
    return True, "", ""


def clean(text: str) -> str:
    return " ".join(str(text or "").split())[:MAX_LEN]


def add(agent: str, text: str, decision_id: int | None = None) -> int:
    with _c() as c:
        return c.execute("INSERT INTO nudge (ts, agent, text, status, decision_id) VALUES (?,?,?, 'unread', ?)",
                         (clock.iso(), agent, clean(text), decision_id)).lastrowid


def unread() -> list[dict]:
    with _c() as c:
        return [dict(r) for r in c.execute("SELECT * FROM nudge WHERE status = 'unread' ORDER BY id DESC").fetchall()]


def mark_read(nudge_id: int) -> bool:
    with _c() as c:
        return c.execute("UPDATE nudge SET status = 'read' WHERE id = ?", (nudge_id,)).rowcount > 0


def remove(nudge_id: int) -> None:
    with _c() as c:
        c.execute("DELETE FROM nudge WHERE id = ?", (nudge_id,))
