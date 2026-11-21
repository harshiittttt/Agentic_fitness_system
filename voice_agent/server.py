"""Streaming voice front end for the coach (runs on the PC, uses the GPU).

    phone mic --PCM16 16 kHz--> [VAD -> Smart Turn v3 -> Whisper] -> coach brain (the existing /voice/command) -> [Kokoro] --PCM16 24 kHz--> phone

Why it exists: the phone's own recogniser decides end-of-speech badly and cuts you off, and a request-reply chain can never feel live.
Here audio streams continuously, a turn-detection model (not a timer) decides when you are done, the brain's answer is spoken as it is
produced, and talking over the coach interrupts it. The brain is NOT replaced: set logging, missing-info questions, grounding and the diet
planner all stay in backend/ and are reached through its HTTP API.

Wire protocol (WebSocket /ws?token=...):
  client -> server  binary: PCM16 mono 16 kHz.   text: {"type": "screen", "screen": "live"}
  server -> client  binary: 0x01 + PCM16 mono 24 kHz.   text: {"t": "user"|"bot"|"clear"|"state", ...}

Run (Windows, the venv in D:\\voice-proto):  python voice_agent/server.py   (env: BACKEND_URL, VOICE_PORT)
"""
from __future__ import annotations

import asyncio
import audioop
import glob
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODELS = os.environ.get("VOICE_MODELS", "D:/voice-proto/models")
os.environ.setdefault("HF_HOME", "D:/voice-proto/hf")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ["ONNX_PROVIDER"] = os.environ.get("ONNX_PROVIDER", "CUDAExecutionProvider")
for _d in glob.glob(os.path.join(sys.prefix, "Lib", "site-packages", "nvidia", "*", "bin")):   # CUDA runtime libs installed as pip wheels
    os.add_dll_directory(_d)
    os.environ["PATH"] = _d + os.pathsep + os.environ["PATH"]

import httpx
from fastapi import FastAPI, WebSocket
from loguru import logger

logger.remove()
logger.add(sys.stderr, level=os.environ.get("VOICE_LOG", "WARNING"))

from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (BotStartedSpeakingFrame, BotStoppedSpeakingFrame, Frame, InputAudioRawFrame, InputTransportMessageFrame, InterruptionFrame, LLMContextFrame,
                                   OutputAudioRawFrame, OutputTransportMessageFrame, OutputTransportMessageUrgentFrame,
                                   TTSSpeakFrame, UserStartedSpeakingFrame, TranscriptionFrame, TTSStoppedFrame, TTSStartedFrame)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.serializers.base_serializer import FrameSerializer
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

BACKEND = os.environ.get("BACKEND_URL", "http://127.0.0.1:8001").rstrip("/")
IN_RATE, OUT_RATE = 16000, int(os.environ.get("VOICE_OUT_RATE", "16000"))      # the phone's audio module plays 16 kHz PCM
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
VOICE = os.environ.get("VOICE_NAME", "af_heart")
VAD_STOP_SECS = float(os.environ.get("VAD_STOP_SECS", "0.25"))
# The turn model answers "finished" at once when the sentence sounds finished. When it says "not finished" it still gives up after this much silence
# (library default 3 s made a short Hindi command wait ~5 s). Whatever it cuts off too early is caught by the coach brain ("fragment": held and joined to the next words).
TURN_STOP_SECS = float(os.environ.get("VOICE_TURN_STOP_SECS", "1.3"))

# ---- models are loaded once and shared by every connection ----
import faster_whisper
import pipecat.services.kokoro.tts as _kt
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.whisper.stt import WhisperSTTService

_REAL_WHISPER = faster_whisper.WhisperModel
_whisper_cache: dict = {}
_kokoro_cache: dict = {}
_REAL_KOKORO = _kt.Kokoro


# Whisper writes Hindi in Devanagari unless the prompt shows it Roman-letter Hinglish ("maar diya", "paanch bar"), which is how the coach's parsers
# and the user's own typing read. Measured on Hindi speech: no prompt -> Devanagari with errors; this prompt -> "Bhai, maini squat ka ek set maar diya,
# 60 kilo aur 5 bar." English is unchanged. Language is auto-detected per utterance.
PROMPT = ("Chalo shuru karte hain. Dumbbell press ke teen set kiye, aath reps, bees kilo. Thoda rest karo, phir agla exercise. Kal ka diet plan kya hai? "
          "Bench press, deadlift, squat, reps, weight, protein.")


class _PromptedWhisper:
    def __init__(self, model):
        self._m = model

    def transcribe(self, audio, **kw):
        kw.setdefault("initial_prompt", PROMPT)
        kw.setdefault("beam_size", 1)
        kw.setdefault("condition_on_previous_text", False)
        return self._m.transcribe(audio, **kw)


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


_PROMPT_WORDS = set(_norm(PROMPT).split())


def is_prompt_echo(text: str) -> bool:
    """On noise Whisper sometimes just repeats words of its prompt ("Dumbbell press, deadlift, squat, reps, weight, protein."): not something the user said.
    'protein' or 'bench press' alone are real commands; four or more words that ALL come from the prompt are not."""
    w = _norm(text).split()
    return len(w) >= 4 and all(x in _PROMPT_WORDS for x in w)


def _shared_whisper(size, **kw):
    key = (size, kw.get("device"), kw.get("compute_type"))
    if key not in _whisper_cache:
        _whisper_cache[key] = _PromptedWhisper(_REAL_WHISPER(size, **kw))
    return _whisper_cache[key]


def _shared_kokoro(model, voices):
    if (model, voices) not in _kokoro_cache:
        _kokoro_cache[(model, voices)] = _REAL_KOKORO(model, voices)
    return _kokoro_cache[(model, voices)]


faster_whisper.WhisperModel = _shared_whisper
_kt.Kokoro = _shared_kokoro


def make_stt():
    return WhisperSTTService(settings=WhisperSTTService.Settings(model=WHISPER_MODEL, language=None), device="cuda", compute_type="float16")


def make_tts():
    return KokoroTTSService(model_path=f"{MODELS}/kokoro-v1.0.onnx", voices_path=f"{MODELS}/voices-v1.0.bin", settings=KokoroTTSService.Settings(voice=VOICE))


def warm_up() -> float:
    """Load both models and run each once, so the first real sentence does not pay for GPU start-up (measured: ~4 s)."""
    import numpy as np
    t = time.time()
    stt, tts = make_stt(), make_tts()
    stt._model.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1, language="en")
    tts._kokoro.create("Ready when you are.", voice=VOICE, speed=1.0, lang="en-us")
    return time.time() - t


# ---- wire format ----
class PcmSerializer(FrameSerializer):
    """Also counts what arrives, so "the phone connects but nothing is heard" can be told apart from "the phone sends silence" (see report())."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.rx = {"bytes": 0, "frames": 0, "peak": 0, "since": time.time()}

    async def serialize(self, frame: Frame):
        if isinstance(frame, OutputAudioRawFrame):
            return b"\x01" + frame.audio
        if isinstance(frame, InterruptionFrame):
            return json.dumps({"t": "clear"})
        if isinstance(frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)):
            return json.dumps(frame.message)
        return None

    async def deserialize(self, data):
        if isinstance(data, (bytes, bytearray)):
            self.rx["bytes"] += len(data)
            self.rx["frames"] += 1
            try:
                self.rx["peak"] = max(self.rx["peak"], audioop.max(bytes(data[: len(data) // 2 * 2]), 2))
            except Exception:
                pass
            return InputAudioRawFrame(audio=bytes(data), sample_rate=IN_RATE, num_channels=1)
        try:
            return InputTransportMessageFrame(message=json.loads(data))
        except Exception:
            return None


def say(processor: FrameProcessor, **msg):
    return processor.push_frame(OutputTransportMessageFrame(message=msg))


class Gate(FrameProcessor):
    """Half-duplex safety. With echo cancelling working (most phones) you can talk over the coach. On a phone where it is weak the coach would hear itself and
    cut itself off, so the app can switch "talk over the coach" off: microphone audio is then dropped while the coach speaks (and a moment after)."""

    GRACE_S = 0.35

    def __init__(self, **kw):
        super().__init__(**kw)
        self.barge_in = True
        self.speaking = False
        self.stopped_at = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self.speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.speaking = False
            self.stopped_at = time.time()
        elif isinstance(frame, InputTransportMessageFrame) and isinstance(frame.message, dict) and frame.message.get("type") == "config":
            self.barge_in = bool(frame.message.get("barge_in", True))
        elif isinstance(frame, InputAudioRawFrame) and not self.barge_in and (self.speaking or time.time() - self.stopped_at < self.GRACE_S):
            return
        await self.push_frame(frame, direction)


def is_echo(heard: str, said: str, age_s: float, window_s: float = 4.0) -> bool:
    """Words that are (nearly) all from what the coach itself said a moment ago: its own voice coming back through the microphone, not the user."""
    h, s = _norm(heard).split(), set(_norm(said).split())
    if len(h) < 3 or age_s > window_s + len(said) / 14.0:
        return False
    return sum(1 for w in h if w in s) / len(h) >= 0.8


class Brain(FrameProcessor):
    """One finished user turn in, one spoken answer out. Calls the coach's own /voice/command, so every rule and tool stays where it is."""

    def __init__(self, token: str, backend: str = BACKEND, **kw):
        super().__init__(**kw)
        self.token = token
        self.screen = "live"
        self.follow = False                 # the coach just asked a question: the next turn is the answer
        self.carry = ""                      # an unfinished sentence the coach is waiting to hear the end of
        self.work: asyncio.Task | None = None
        self.last_bot = ("", 0.0)           # what the coach said last, and when (to tell its own echo from the user)
        self.http = httpx.AsyncClient(base_url=backend, timeout=20.0, headers={"Authorization": f"Bearer {token}"})

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputTransportMessageFrame):
            msg = frame.message if isinstance(frame.message, dict) else {}
            if msg.get("type") == "screen":
                self.screen = str(msg.get("screen") or "live")
            elif msg.get("type") == "interrupt":                       # a tap on the phone: stop talking now, keep listening
                if self.work and not self.work.done():
                    self.work.cancel()
                await self.push_interruption_task_frame_and_wait()
            return
        if isinstance(frame, InterruptionFrame):
            if self.work and not self.work.done():
                self.work.cancel()
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, LLMContextFrame):
            text = ""
            for m in reversed(frame.context.get_messages()):
                if m.get("role") == "user":
                    c = m.get("content")
                    text = c if isinstance(c, str) else " ".join(p.get("text", "") for p in c if isinstance(p, dict))
                    break
            frame.context.set_messages([])
            text = text.strip()
            if text and is_prompt_echo(text):
                logger.warning(f"ignored a prompt echo: {text!r}")
                text = ""
            if text:
                if self.work and not self.work.done():
                    self.work.cancel()
                self.work = asyncio.create_task(self.answer(text))
            return
        await self.push_frame(frame, direction)

    async def answer(self, text: str):
        said, at = self.last_bot
        if said and is_echo(text, said, time.time() - at):
            logger.warning(f"ignored the coach's own echo: {text!r}")
            await say(self, t="state", s="listening")
            return
        full = (self.carry + " " + text).strip()
        self.carry = ""
        t0 = time.time()
        print(f"[voice] heard: {full!r}", flush=True)
        await say(self, t="user", text=full)
        await say(self, t="state", s="thinking")
        try:
            r = await self.http.post("/voice/command", json={"transcript": full, "screen": self.screen, "day": None, "ambient": False, "followup": self.follow})
            r.raise_for_status()
            out = r.json()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"brain failed: {e}")
            await self.push_frame(TTSSpeakFrame("Sorry, I lost you for a second. Say that again?"))
            return
        reply = (out.get("reply") or "").strip()
        if out.get("intent") == "fragment":        # an unfinished sentence: say nothing, keep it, join it to the next words
            self.carry = full
            await say(self, t="state", s="listening")
            return
        self.follow = bool(out.get("expects"))
        await say(self, t="bot", text=reply, intent=out.get("intent"), ms=int((time.time() - t0) * 1000), result=out)      # the app applies sets / navigation from the same result it always got
        if reply:
            line = spoken(reply)
            self.last_bot = (line, time.time())
            await self.push_frame(TTSSpeakFrame(line))


def spoken(reply: str, limit: int = 100) -> str:
    """The screen shows the whole answer; the voice says the first sentence or two. A coach who talks for 10 s mid-set is a bad coach."""
    parts = [x for x in re.split(r"(?<=[.!?])\s+", reply.strip()) if x]
    if not parts:
        return reply
    out = parts[0]
    if len(parts) > 1 and len(out) + 1 + len(parts[1]) <= limit and out.rstrip()[-1:] != "?":
        out += " " + parts[1]
    return out


import itertools
_conn_ids = itertools.count(1)
app = FastAPI()
_ready = {"warm_s": None}


@app.on_event("startup")
async def _startup():
    _ready["warm_s"] = await asyncio.get_event_loop().run_in_executor(None, warm_up)
    print(f"voice agent ready (models warm in {_ready['warm_s']:.1f}s), backend {BACKEND}", flush=True)


@app.get("/health")
def health():
    return {"ok": _ready["warm_s"] is not None, "warm_s": _ready["warm_s"]}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    token = websocket.query_params.get("token", "")
    backend = BACKEND
    port = websocket.query_params.get("port", "")
    if port.isdigit() and websocket.client and websocket.client.host in ("127.0.0.1", "::1"):     # only the local gateway may say which person's backend
        backend = f"http://127.0.0.1:{int(port)}"
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            ok = (await c.get(f"{backend}/live/state", headers={"Authorization": f"Bearer {token}"})).status_code == 200
    except Exception:
        ok = False
    if not ok:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    ser = PcmSerializer()
    transport = FastAPIWebsocketTransport(websocket, FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True, add_wav_header=False,
                                                                           serializer=ser, audio_in_sample_rate=IN_RATE, audio_out_sample_rate=OUT_RATE))
    pair = LLMContextAggregatorPair(LLMContext(), user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=VAD_STOP_SECS)),
                                                                                                              user_turn_strategies=UserTurnStrategies(stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3(params=SmartTurnParams(stop_secs=TURN_STOP_SECS)))])))
    brain = Brain(token, backend)
    cid = next(_conn_ids)
    print(f"[voice {cid}] connected (backend {backend})", flush=True)

    async def report():
        last = 0
        while True:
            await asyncio.sleep(3)
            rx = ser.rx if ser else {"bytes": 0, "frames": 0, "peak": 0}
            print(f"[voice {cid}] mic: {rx['frames']} frames, {rx['bytes'] // 1024} KB total, {(rx['bytes'] - last) // 1024} KB in the last 3 s, loudest sample {rx['peak']}/32767", flush=True)
            last, rx["peak"] = rx["bytes"], 0

    reporter = asyncio.create_task(report())
    pipe = Pipeline([transport.input(), Gate(), make_stt(), pair.user(), brain, make_tts(), transport.output()])
    task = PipelineTask(pipe, params=PipelineParams(audio_in_sample_rate=IN_RATE, audio_out_sample_rate=OUT_RATE, allow_interruptions=True))

    @transport.event_handler("on_client_disconnected")
    async def _gone(_t, _c):
        await task.cancel()

    try:
        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        reporter.cancel()
        print(f"[voice {cid}] closed", flush=True)
        await brain.http.aclose()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("VOICE_PORT", "8765")), log_level="warning")
