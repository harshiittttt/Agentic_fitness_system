"""POST /diag/log: the phone's black-box recorder. The app sends short event records (voice recognizer events, screen open/close, JS crashes) so what
happens on the device can be read from the server log (`docker logs`) and GET /diag/recent. Bounded and text only: no audio, no secrets, 400 events kept."""
from __future__ import annotations

import collections
import json
import logging
import threading

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter()
_log = logging.getLogger("uvicorn.error")
_recent: collections.deque = collections.deque(maxlen=400)
_lock = threading.Lock()


class DiagEvent(BaseModel):
    t: int = 0                                   # phone clock, ms
    kind: str = Field(max_length=40)
    data: dict | str | int | float | bool | None = None


class DiagBatch(BaseModel):
    app: str = Field("", max_length=40)
    events: list[DiagEvent] = Field(default_factory=list, max_length=100)


def _clip(v, n=300):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s[:n]


@router.post("/diag/log")
def diag_log(b: DiagBatch):
    with _lock:
        for e in b.events:
            rec = {"t": e.t, "kind": e.kind, "data": _clip(e.data) if e.data is not None else ""}
            _recent.append(rec)
            _log.info("DIAG %s %s %s", b.app, e.kind, rec["data"])
    return {"ok": True, "n": len(b.events)}


@router.get("/diag/recent")
def diag_recent(limit: int = 100):
    with _lock:
        return list(_recent)[-max(1, min(limit, 400)):]


@router.get("/diag/llm")
def diag_llm():
    """Which models answer right now: the order they are tried, which are resting after a spent quota (seconds left), and whether a second provider is set."""
    from backend.agents import base
    return {"main": base.role_models("main"), "compose": base.role_models("compose"), "cooldown_seconds": base.cooldowns(), "second_provider": bool(base._cfg2("main"))}
