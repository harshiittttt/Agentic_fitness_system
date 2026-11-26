"""Dialogue store: one `Dialogue` per voice connection. It keeps what the coach actually SAID (cut at the point the athlete interrupted), the athlete's words,
a rolling summary of older talk, the one open question the coach asked, and the cached events of every turn (so a repeated turn id is replayed, never re-run).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

IDLE_DROP_SECS = 30 * 60
WINDOW_MESSAGES = 16          # verbatim messages sent to the model (about 8 exchanges)
FOLD_AT = 26                  # when the history passes this many messages the oldest are folded into the summary
KEEP_TURNS = 40               # cached turn event lists
PENDING_TTL = 90.0

_lock = threading.RLock()
_dialogues: "OrderedDict[str, Dialogue]" = OrderedDict()


class Dialogue:
    def __init__(self, conn_id: str):
        self.conn_id = conn_id
        self.created = time.time()
        self.last_used = time.time()
        self.messages: list[dict] = []          # {role: user|assistant, content, turn_id, interrupted?}
        self.summary = ""
        self.turns: "OrderedDict[str, dict]" = OrderedDict()   # turn_id -> {events, said, ts}
        self.pending: dict | None = None        # the question the coach asked: {tool, args, exercise, ts, question}
        self.actions: list[dict] = []           # recent tool results: {ts, name, text}
        self.next_weights: dict[str, float] = {}
        self.memory_facts: list[str] = []
        self.set_active = False
        self.rest_until = 0.0
        self.last_user_ts = 0.0
        self.last_coach_ts = 0.0
        self.coach_lines: list[float] = []      # timestamps of unsolicited lines (proactive budget)
        self.rest_nudges = 0
        self.lang: str | None = None
        self.inflight: dict = {}
        self.memory_started = False
        self.rest_over_ts = 0.0
        self.pr_flag = False
        self.lock = threading.RLock()

    # -- history -------------------------------------------------------------------------------------------------------------------------------------
    def add_user(self, text: str, turn_id: str) -> None:
        self.messages.append({"role": "user", "content": text, "turn_id": turn_id, "ts": time.time()})
        self.last_user_ts = time.time()
        self._fold()

    def add_coach(self, text: str, turn_id: str, interrupted: bool = False) -> None:
        if not text.strip():
            return
        self.messages.append({"role": "assistant", "content": text.strip(), "turn_id": turn_id, "interrupted": interrupted, "ts": time.time()})
        self.last_coach_ts = time.time()

    def record_action(self, name: str, text: str) -> None:
        self.actions.append({"ts": time.time(), "name": name, "text": text})
        del self.actions[:-8]

    def history_for_model(self) -> list[dict]:
        out = []
        for m in self.messages[-WINDOW_MESSAGES:]:
            c = m["content"]
            if m["role"] == "assistant" and m.get("interrupted"):
                c += " (the athlete cut you off here)"
            out.append({"role": m["role"], "content": c})
        # the model requires the conversation to start with a user message
        while out and out[0]["role"] != "user":
            out.pop(0)
        return out

    def recent_user_text(self, secs: float = 90.0, n: int = 2, exclude_turn: str | None = None) -> str:
        now = time.time()
        got = [m["content"] for m in self.messages if m["role"] == "user" and now - m["ts"] <= secs and m.get("turn_id") != exclude_turn]
        return " ".join(got[-n:])

    def last_coach_text(self) -> str:
        for m in reversed(self.messages):
            if m["role"] == "assistant":
                return m["content"]
        return ""

    def _fold(self) -> None:
        if len(self.messages) <= FOLD_AT:
            return
        old, self.messages = self.messages[:-WINDOW_MESSAGES], self.messages[-WINDOW_MESSAGES:]
        bits = []
        for m in old:
            who = "Athlete" if m["role"] == "user" else "Coach"
            flat = re.sub(r"\s+", " ", m["content"])[:90]
            bits.append(f"{who}: {flat}")
        self.summary = (self.summary + " | " if self.summary else "") + " | ".join(bits)
        self.summary = self.summary[-900:]

    # -- pending question ----------------------------------------------------------------------------------------------------------------------------
    def set_pending(self, **kw) -> None:
        self.pending = {**kw, "ts": time.time()}

    def get_pending(self) -> dict | None:
        p = self.pending
        if p and time.time() - p["ts"] <= PENDING_TTL:
            return p
        self.pending = None
        return None

    # -- turns (idempotency) -------------------------------------------------------------------------------------------------------------------------
    def cached_turn(self, turn_id: str) -> dict | None:
        return self.turns.get(turn_id)

    def store_turn(self, turn_id: str, events: list[dict], said: str) -> None:
        self.turns[turn_id] = {"events": events, "said": said, "ts": time.time()}
        while len(self.turns) > KEEP_TURNS:
            self.turns.popitem(last=False)

    # -- interruption --------------------------------------------------------------------------------------------------------------------------------
    def interrupt(self, turn_id: str, spoken: str) -> str | None:
        """Replace what the coach said in this turn by what was actually heard. Returns the stored text, or None for an unknown turn."""
        spoken = (spoken or "").strip()
        hit = None
        for m in reversed(self.messages):
            if m["role"] == "assistant" and m.get("turn_id") == turn_id:
                hit = m
                break
        rec = self.turns.get(turn_id)
        if hit is None and rec is None:
            return None
        if hit is not None:
            if spoken:
                hit["content"], hit["interrupted"] = spoken, True
            else:
                self.messages.remove(hit)
        if rec is not None:
            rec["said"] = spoken
            rec["interrupted"] = True
        if self.pending and spoken and self.pending.get("question") and self.pending["question"] not in spoken:
            self.pending = None           # the question itself was never heard
        elif self.pending and not spoken:
            self.pending = None
        return spoken


def get(conn_id: str, create: bool = True) -> "Dialogue | None":
    cid = (conn_id or "default")[:80]
    with _lock:
        _drop_idle()
        d = _dialogues.get(cid)
        if d is None and create:
            d = _dialogues[cid] = Dialogue(cid)
        if d is not None:
            d.last_used = time.time()
            _dialogues.move_to_end(cid)
        while len(_dialogues) > 50:
            _dialogues.popitem(last=False)
        return d


def reset(conn_id: str) -> "Dialogue":
    cid = (conn_id or "default")[:80]
    with _lock:
        _dialogues.pop(cid, None)
    return get(cid)


def drop(conn_id: str) -> None:
    with _lock:
        _dialogues.pop((conn_id or "default")[:80], None)


def clear_all() -> None:
    with _lock:
        _dialogues.clear()


def count() -> int:
    return len(_dialogues)


def _drop_idle() -> None:
    now = time.time()
    for k in [k for k, d in _dialogues.items() if now - d.last_used > IDLE_DROP_SECS]:
        _dialogues.pop(k, None)


# ---------------------------------------------------------------- debug log (JSONL beside the database; best effort, never raises)
def _dir() -> Path | None:
    try:
        from backend.memory import db
        p = Path(db.DB_PATH).resolve().parent / "convo"
        p.mkdir(parents=True, exist_ok=True)
        return p
    except Exception:
        return None


def persist(conn_id: str, record: dict) -> None:
    if os.environ.get("CONVO_LOG", "1") == "0":
        return
    d = _dir()
    if not d:
        return
    try:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", conn_id or "default")[:60]
        with open(d / f"{safe}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass
