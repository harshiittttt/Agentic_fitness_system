"""POST /voice/command: the one endpoint behind "Hey coach, <anything>". The logic lives in backend/coach/voice_control.py."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class VoiceCommand(BaseModel):
    transcript: str
    screen: str = "unknown"      # today | coach | nutrition | insights | setup | live | unknown
    day: str | None = None       # YYYY-MM-DD: the day selected in Fuel
    ambient: bool = False
    followup: bool = False       # the user is answering a question we just asked


@router.post("/voice/command")
def voice_command(c: VoiceCommand):
    from backend.coach import voice_control
    return voice_control.handle(c.transcript, c.screen, c.day, c.ambient, c.followup)
