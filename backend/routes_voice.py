"""POST /voice/command: the one endpoint behind "Hey coach, <anything>". The logic lives in backend/coach/voice_control.py."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class VoiceCommand(BaseModel):
    transcript: str
    screen: str = "unknown"      # today | coach | nutrition | insights | setup | live | unknown
    day: str | None = None       # YYYY-MM-DD: the day selected in Fuel
    ambient: bool = False
    followup: bool = False       # the user is answering a question we just asked
    lang: str | None = None      # "en" | "hinglish": the language the athlete spoke, so the model answers in it (voice agent sets it)


_pool = ThreadPoolExecutor(max_workers=8)
DEADLINE_S = 15.0       # the app waits at most this long for a spoken answer; after it the user hears a plain "try again" instead of 40 s of silence


@router.post("/voice/command")
def voice_command(c: VoiceCommand):
    from backend.coach import voice_control
    fut = _pool.submit(voice_control.handle, c.transcript, c.screen, c.day, c.ambient, c.followup, c.lang)
    try:
        return fut.result(timeout=DEADLINE_S)
    except FutureTimeout:
        return {"reply": "That is taking too long on my side. Say it again in a few words.", "intent": "fallback", "actions": [], "expects": None, "moment": False,
                "pr": False, "safety_flag": None, "state": None, "saved": []}
