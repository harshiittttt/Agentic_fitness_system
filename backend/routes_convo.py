"""/convo/*: the Conversation Core's HTTP API (contracts: docs/CONVO_CONTRACTS.md). One include line in main.py."""
from __future__ import annotations

import json
from typing import Iterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

router = APIRouter()
_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class ConvoTurn(BaseModel):
    text: str = ""
    turn_id: str | None = None
    conn_id: str = "default"
    lang: str | None = None
    screen: str = "live"
    asr: dict | None = None
    set_active: bool | None = None
    rest_remaining: int | None = None
    ambient: bool = False


class ConvoInterrupt(BaseModel):
    conn_id: str = "default"
    turn_id: str
    spoken: str = ""
    reason: str = "barge_in"


class ConvoEvent(BaseModel):
    conn_id: str = "default"
    event: str
    data: dict = Field(default_factory=dict)
    turn_id: str | None = None


class ConvoTool(BaseModel):
    conn_id: str = "default"
    turn_id: str | None = None
    name: str
    args: dict = Field(default_factory=dict)
    utterance: str = ""
    lang: str | None = None
    asr: dict | None = None
    set_active: bool | None = None
    rest_remaining: int | None = None


def _lines(events: Iterator[dict], turn_id: str | None) -> Iterator[str]:
    """NDJSON, one event per line, flushed as produced. Whatever happens inside, the stream ends with a `done`."""
    done = False
    try:
        for e in events:
            if e is None:
                continue
            done = done or e.get("t") == "done"
            yield json.dumps(e, ensure_ascii=False, default=str) + "\n"
    except Exception as e:                                                       # noqa: BLE001 - never leave the voice agent without a closing line
        yield json.dumps({"t": "error", "turn_id": turn_id, "code": "internal", "message": type(e).__name__, "fallback": False}) + "\n"
        if not done:
            yield json.dumps({"t": "done", "turn_id": turn_id, "brain": "converse", "intent": "fallback", "said": "", "expects": None, "interrupted": False, "fallback": False,
                              "result": {"reply": "", "intent": "fallback", "actions": [], "expects": None, "moment": False, "pr": False, "safety_flag": None, "state": None, "saved": [],
                                         "next_set": None}, "ms": {}}) + "\n"


@router.post("/convo/turn")
def convo_turn(b: ConvoTurn, brain: str | None = None):
    from backend.convo import turn
    req = b.model_dump()
    return StreamingResponse(_lines(turn.run_turn(req, brain), b.turn_id), media_type="application/x-ndjson", headers=_HEADERS)


@router.post("/convo/interrupt")
def convo_interrupt(b: ConvoInterrupt):
    from backend.convo import session
    dlg = session.get(b.conn_id, create=False)
    if dlg is None:
        return {"ok": False, "error": "unknown_turn"}
    stored = dlg.interrupt(b.turn_id, b.spoken)
    if stored is None:
        return {"ok": False, "error": "unknown_turn"}
    return {"ok": True, "turn_id": b.turn_id, "stored": stored}


@router.post("/convo/event")
def convo_event(b: ConvoEvent):
    from backend.convo import proactive
    return StreamingResponse(_lines(proactive.handle_event(b.model_dump()), b.turn_id), media_type="application/x-ndjson", headers=_HEADERS)


@router.post("/convo/tool")
def convo_tool(b: ConvoTool):
    from backend.convo import turn
    return turn.run_tool(b.model_dump())


class ConvoReset(BaseModel):
    conn_id: str = "default"


@router.post("/convo/reset")
def convo_reset(b: ConvoReset):
    """Forget one connection's dialogue (a new workout, or a test)."""
    from backend.convo import session
    session.drop(b.conn_id)
    return {"ok": True}


@router.get("/convo/status")
def convo_status():
    from backend.convo import llm, session, turn
    return {"brain": turn.brain_mode(), "have_key": llm.have_key(), "models": llm.model_chain() if llm.have_key() else [], "dead_models": llm.dead_models(),
            "dialogues": session.count()}
