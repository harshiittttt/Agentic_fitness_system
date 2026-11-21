"""Conversation memory: what you and the coach actually said, across BOTH the chat tab and the live training session.

Before this the coach was stateless: every message reached the model alone, so it forgot the previous sentence. Now every turn is stored
here and the most recent ones are replayed to the model, so a plan discussed in chat is still known in the gym and vice versa.

Interface (stable; other modules depend on it):
    record(channel, role, content, meta=None) -> id      channel: 'chat' | 'live'; role: 'user' | 'assistant' | 'tool'
    history(channel=None, limit=12, since_days=7)        oldest first, both channels merged unless a channel is given
    pending_pushback(...) / set_pushback(...)            the 'one round of debate' state (see coach_tools)
    last_user_turns(n)                                   the athlete's own latest words only
    clear()
"""
from __future__ import annotations

import json
from contextlib import contextmanager

from backend.memory import db

from . import clock

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_message (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, channel TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, meta TEXT
);
"""


@contextmanager
def _c():
    with db.conn() as c:
        c.executescript(SCHEMA)
        yield c


def record(channel: str, role: str, content: str, meta: dict | None = None, learn: bool = True) -> int:
    if channel not in ("chat", "live") or role not in ("user", "assistant", "tool"):
        raise ValueError("bad channel or role")
    content = (content or "").strip()
    if not content:
        raise ValueError("empty message")
    with _c() as c:
        mid = c.execute("INSERT INTO conversation_message (ts, channel, role, content, meta) VALUES (?,?,?,?,?)",
                        (clock.iso(), channel, role, content[:4000], json.dumps(meta) if meta else None)).lastrowid
    if role == "user" and learn:
        # What the athlete states as fact (dislikes, pain, days available...) becomes durable memory. Never raises, never reads coach lines.
        try:
            from . import learn as _learn
            _learn.learn_from_user_turn(content, channel)
        except Exception:
            pass
        try:   # the same words, as beliefs with provenance in the athlete model (fitness_os/mem); a failure here changes nothing above
            from .mem import ingest as _ingest
            _ingest.observe(content, source="chat", channel=channel, defer_llm=True)
        except Exception:
            pass
    return mid


def history(channel: str | None = None, limit: int = 12, since_days: float = 7) -> list[dict]:
    q, a = "SELECT * FROM conversation_message WHERE 1=1", []
    if channel:
        q += " AND channel = ?"; a.append(channel)
    with _c() as c:
        rows = c.execute(q + " ORDER BY id DESC LIMIT ?", (*a, limit * 3)).fetchall()
    out = []
    for r in rows:
        if clock.age_days(r["ts"]) > since_days:
            continue
        d = dict(r)
        d["meta"] = json.loads(d["meta"]) if d["meta"] else None
        out.append(d)
        if len(out) >= limit:
            break
    return list(reversed(out))


def last_user_turns(n: int = 3, channel: str | None = None) -> list[str]:
    """The athlete's own most recent messages, newest last (for context that must not include the coach's words)."""
    return [m["content"] for m in history(channel, limit=n * 3) if m["role"] == "user"][-n:]


def clear() -> None:
    with _c() as c:
        c.execute("DELETE FROM conversation_message")
