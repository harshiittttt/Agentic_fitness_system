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
import random
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
                                   TTSSpeakFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame, VADParamsUpdateFrame, TranscriptionFrame, TTSStoppedFrame, TTSStartedFrame)
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
CAPTURE = os.environ.get("VOICE_CAPTURE", "1") != "0"
CAPTURE_DIR = os.environ.get("VOICE_CAPTURE_DIR", "D:/voice-proto/captures")
# The turn model answers "finished" at once when the sentence sounds finished. When it says "not finished" it still gives up after this much silence
# (library default 3 s made a short Hindi command wait ~5 s). Whatever it cuts off too early is caught by the coach brain ("fragment": held and joined to the next words).
TURN_STOP_SECS = float(os.environ.get("VOICE_TURN_STOP_SECS", "1.3"))
# Which brain answers: "legacy" = the old /voice/command, "converse" = the Conversation Core (/convo/turn). A phone can pick per connection with ?brain=.
BRAIN_DEFAULT = os.environ.get("VOICE_BRAIN", "legacy").strip().lower()
CONVO_READ_TIMEOUT = float(os.environ.get("VOICE_CONVO_TIMEOUT", "10"))      # silence on the Core's stream longer than this = it is stuck (the Core itself falls back at 6 s)
SPECULATE = os.environ.get("VOICE_SPECULATE", "1") != "0"                    # start the Core when the words are ready, before the turn model says "finished"
NUM_HOLD_S = float(os.environ.get("VOICE_NUM_HOLD", "1.2"))                  # a half-said set ("sixty kilos") waits this long for "five reps"
NUM_HOLD_MAX_S = float(os.environ.get("VOICE_NUM_HOLD_MAX", "4.0"))
# While the coach is talking, speech must last this long before it counts as talking over it: a "yeah" or "mm-hm" (0.2-0.4 s) is then not an interruption.
BARGE_START_SECS = float(os.environ.get("VOICE_BARGE_START_SECS", "0.15"))      # short on purpose: a "yeah" that cuts the coach off is put right by RESUME (below)
RESUME_AFTER_S = float(os.environ.get("VOICE_RESUME_AFTER", "2.2"))              # cut off by a sound that turns out to be no words (cough, noise): carry on after this long
RESUME_MAX_AGE_S = 10.0
KEEPWARM = os.environ.get("VOICE_KEEPWARM", "1") != "0"                      # hold the GPU at full clock while a phone is connected (see _PromptedWhisper)
TURN_LOG_DIR = os.environ.get("VOICE_TURN_LOG_DIR", "D:/voice-proto/turns")  # one JSON line per turn with the timings (set to "" to turn off)

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
PROMPT = ("Chalo shuru karte hain. Squat ka ek set maar diya, saath kilo, paanch reps. Agla exercise kaunsa hai? Kitne set bache hain? Thoda ruko, rest karne do. "
          "Weight badhao ya kam karo? Kal ka diet plan kya hai? Bench press, deadlift, goblet squat, leg curl, leg extension, lat pulldown, reps, weight, protein.")

UNCLEAR = "[[unclear]]"        # what the speech stage hands on when it heard something but is not sure what: the brain turns it into "say that again?"
# Whisper's own known hallucinations on silence / noise (it was trained on subtitled video)
_HALLUCINATIONS = re.compile(r"^\W*(?:thanks? for watching|thank you for watching|please subscribe|subscribe to|like and subscribe|subtitles? by|amara\.org|bye[- ]?bye|you)\W*$", re.I)
# Whisper's systematic mishearings of this app's own words (gym vocabulary, Hinglish). Only words that are wrong in every context are fixed here.
_STT_FIX = [(re.compile(p, re.I), r) for p, r in [
    (r"\bsit\b(?=\s+(?:mar|maar|kiy|kiye|kar|ho|pura|complete|karte))", "set"),
    (r"\bma+r\s+di(?:a|ya)\b", "maar diya"), (r"\bmaini\b", "maine"), (r"\bmaine\s+ek\s+sit\b", "maine ek set"),
    (r"\bchahe\b(?=\s+(?:reps?|bar|baar|set|huye))", "chhe"), (r"\b(?:vazhan|vazan|wazhan|vajhan|wajhan)\b", "vajan"),
    (r"\b(?:gobble|gobbler|goblin|gobbled|gobblet|goblet)\s+(?:squads?|squats?|squid|score)\b", "goblet squat"),
    (r"\b(?:dead\s+lifts?|deadlifted)\b", "deadlift"), (r"\bromanian dead\s*lifts?\b", "romanian deadlift"),
    (r"\bleg\s+(?:extent|extensions?)\b", "leg extension"), (r"\bleg\s+(?:girl|curls?)\b", "leg curl"),
    (r"\blat\s+pull\s*downs?\b", "lat pulldown"), (r"\bhanging\s+leg\s+(?:rays?|raises?)\b", "hanging leg raise"),
    (r"\b(?:bent|ben|bench)\s+press\b", "bench press"), (r"\bsquads\b", "squats"), (r"\bsquad\b", "squat"),
    (r"\b(?:rap|raps|wraps|rapes)\b(?=\W*$|\s+(?:at|for|and|done))", "reps"),
]]
# Roman-letter Hindi words that only a Hindi sentence contains (not "kilo", "set", "reps": those are English-gym words too)
_HINGLISH_WORDS = {"bhai", "kya", "hai", "hain", "ho", "gaya", "gayi", "kitne", "kitna", "kitni", "agla", "agle", "agli", "wapas", "ruko", "nahi", "nahin", "haan", "maine", "mera",
                   "meri", "thoda", "thodi", "aur", "karo", "kar", "karna", "karunga", "karenge", "diya", "bar", "baar", "batao", "bata", "chalo", "shuru", "bas", "abhi", "phir",
                   "accha", "achha", "theek", "vajan", "bache", "bacha", "lagao", "laga", "dena", "do", "mat", "kaunsa", "kaun", "kaise", "kab", "mein", "mai", "toh", "yeh", "woh"}


def fix_stt(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    for rx, rep in _STT_FIX:
        t = rx.sub(rep, t)
    return t


def is_hinglish(text: str) -> bool:
    """Devanagari, or at least two Roman-letter Hindi words ("agla exercise kaunsa hai"): the athlete spoke Hindi, so the coach answers in kind."""
    if any("ऀ" <= c <= "ॿ" for c in text):
        return True
    return sum(1 for w in re.findall(r"[a-z]+", text.lower()) if w in _HINGLISH_WORDS) >= 2


# Confidence limits for one transcribed segment. Measured on the test speech (see voice_agent/README.md): real speech scores -0.1..-0.6, mumble/noise < -1.0.
LOGPROB_MIN = float(os.environ.get("STT_LOGPROB_MIN", "-1.05"))
NOSPEECH_MAX = float(os.environ.get("STT_NOSPEECH_MAX", "0.6"))
NOSPEECH_LOGPROB = float(os.environ.get("STT_NOSPEECH_LOGPROB", "-0.7"))
COMPRESSION_MAX = 2.4
STT_STATS: dict = {"last": None}


class _PromptedWhisper:
    """Whisper with the gym prompt. Three speed measures (all measured on this PC, RTX 3050): one transcription at a time (a keep-warm ping may not collide with a real one),
    a ping every ~0.8 s while a phone is connected (after ~3 s idle the GPU drops to ~250 MHz and the same clip takes 600-670 ms instead of 370-410), and a remembered
    language (the language-detection pass costs ~165 ms; a wrong guess shows as a low confidence and is re-run with detection)."""
    LANG_REFRESH_EVERY = 4

    def __init__(self, model):
        import threading
        self._m = model
        self._lock = threading.Lock()
        self.last_lang: str | None = None
        self._since_detect = 0
        self.busy_until = 0.0

    def ping(self) -> bool:
        """Keep the GPU out of its idle clock. Skips (False) when a real transcription is running."""
        import numpy as np
        if not self._lock.acquire(blocking=False):
            return False
        try:
            list(self._m.transcribe(np.zeros(4000, dtype=np.float32), beam_size=1, language="en", without_timestamps=True)[0])
        finally:
            self._lock.release()
        return True

    def transcribe(self, audio, **kw):
        with self._lock:
            self.busy_until = time.time()
            try:
                return self._transcribe(audio, **kw)
            finally:
                self.busy_until = time.time()

    def _transcribe(self, audio, **kw):
        import dataclasses
        kw.setdefault("initial_prompt", PROMPT)
        kw.setdefault("beam_size", 1)
        kw.setdefault("condition_on_previous_text", False)
        t0 = time.time()
        hinted = False
        if kw.get("language") is None and self.last_lang in ("en", "hi") and self._since_detect < self.LANG_REFRESH_EVERY:
            kw["language"] = self.last_lang                       # same language as the last turns: skip the detection pass
            hinted = True
            self._since_detect += 1
        segs, info = self._m.transcribe(audio, **kw)
        segs = list(segs)
        if hinted and (not segs or min(s.avg_logprob for s in segs) < -0.75):      # the hint may be wrong (the athlete switched language): look again with detection
            kw["language"] = None
            hinted = False
            self._since_detect = 0
            segs, info = self._m.transcribe(audio, **kw)
            segs = list(segs)
        if not hinted:
            self._since_detect = 0
            if segs and info.language in ("en", "hi") and info.language_probability >= 0.8:
                self.last_lang = info.language
        print(f"[voice] whisper: {len(audio) / 16000:.1f}s of audio -> {[(s.text.strip(), round(s.no_speech_prob, 2), round(s.avg_logprob, 2), round(s.compression_ratio, 1)) for s in segs]}", flush=True)
        if kw.get("language") is None and info.language not in ("en", "hi"):      # short English clips get detected as Welsh, Latin ...: this is an English/Hindi app
            kw["language"] = "en"
            self.last_lang = None
            segs, info = self._m.transcribe(audio, **kw)
            segs = list(segs)
        out, dropped, lp = [], 0, []
        for s in segs:
            text = fix_stt(s.text)
            lp.append(s.avg_logprob)
            # Whisper's own rule for "this was silence": it must be BOTH unsure there was speech AND unsure of the words (pipecat alone dropped any segment with
            # no_speech_prob >= 0.4, which threw away short real sentences that were followed by a pause)
            if text and s.no_speech_prob > 0.5 and len(_norm(text).split()) >= 3 and all(x in _PROMPT_WORDS for x in _norm(text).split()):
                dropped += 1                          # Whisper repeating its own prompt on silence/noise
                continue
            if not text or _HALLUCINATIONS.match(text) or (s.no_speech_prob > NOSPEECH_MAX and s.avg_logprob < NOSPEECH_LOGPROB) or s.compression_ratio > COMPRESSION_MAX:
                dropped += 1
                continue
            if s.avg_logprob < LOGPROB_MIN:
                dropped += 1
                continue
            out.append(dataclasses.replace(s, text=text))
        STT_STATS["last"] = {"lang": info.language, "p": round(info.language_probability, 2), "segs": len(segs), "dropped": dropped,
                             "logprob": round(min(lp), 2) if lp else None, "ms": int((time.time() - t0) * 1000)}
        if segs and not out and any(not _HALLUCINATIONS.match(fix_stt(s.text) or "x") and s.no_speech_prob <= NOSPEECH_MAX and fix_stt(s.text) for s in segs):
            out = [dataclasses.replace(segs[0], text=UNCLEAR, no_speech_prob=0.0)]     # something was said but the recogniser is unsure: ask again instead of guessing
        return out, info


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


_PROMPT_WORDS = set(_norm(PROMPT).split())


def is_prompt_echo(text: str) -> bool:
    """On noise Whisper sometimes just repeats words of its prompt ("Dumbbell press, deadlift, squat, reps, weight, protein."): not something the user said.
    'protein' or 'bench press' or 'agla exercise kaunsa hai' are real commands (the prompt contains such phrases on purpose); seven or more words that ALL come from the prompt are not. Whisper's usual prompt echo on silence is caught in the speech stage (high no_speech_prob)."""
    w = _norm(text).split()
    return len(w) >= 7 and all(x in _PROMPT_WORDS for x in w)


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
    return WhisperSTTService(settings=WhisperSTTService.Settings(model=WHISPER_MODEL, language=None, no_speech_prob=1.01), device="cuda", compute_type="float16")


def make_tts():
    return KokoroTTSService(model_path=f"{MODELS}/kokoro-v1.0.onnx", voices_path=f"{MODELS}/voices-v1.0.bin", settings=KokoroTTSService.Settings(voice=VOICE))


ACK_TEXT = {"got_it": "Got it.", "okay": "Okay.", "mm_hm": "Mm-hm.", "one_sec": "One sec."}
ACK_PCM: dict[str, bytes] = {}          # the acknowledgements, rendered once at start-up: playing one costs no voice time at all


def render_acks(tts) -> None:
    import numpy as np
    for kind, text in ACK_TEXT.items():
        x, sr = tts._kokoro.create(text, voice=VOICE, speed=1.0, lang="en-us")
        pcm = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
        if sr != OUT_RATE:
            pcm, _ = audioop.ratecv(pcm, 2, 1, int(sr), OUT_RATE, None)
        ACK_PCM[kind] = pcm


def warm_up() -> float:
    """Load both models and run each once, so the first real sentence does not pay for GPU start-up (measured: ~4 s); render the acknowledgement clips."""
    import numpy as np
    t = time.time()
    stt, tts = make_stt(), make_tts()
    stt._model.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1, language="en")
    tts._kokoro.create("Ready when you are.", voice=VOICE, speed=1.0, lang="en-us")
    render_acks(tts)
    return time.time() - t


# ---- wire format ----
class PcmSerializer(FrameSerializer):
    """Also counts what arrives, so "the phone connects but nothing is heard" can be told apart from "the phone sends silence" (see report())."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.rx = {"bytes": 0, "frames": 0, "peak": 0, "since": time.time()}
        self.capture = bytearray()                       # the phone's audio, saved when the connection ends (debugging; VOICE_CAPTURE=0 turns it off)

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
            if CAPTURE and len(self.capture) < 16000 * 2 * 120:
                self.capture += bytes(data)
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


# ---- a half-said set, and what was actually heard of an interrupted answer ----
_NUMW = {"zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
         "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "point", "half",
         "ek", "do", "teen", "char", "paanch", "panch", "chhe", "che", "saat", "aath", "nau", "das", "saath", "pachas", "assi", "pachhattar"}
_NUM_STRICT = _NUMW - {"point", "half", "do", "che", "das", "teen", "char"}      # words that mean a number nearly everywhere ("do" and "teen" are also ordinary Hindi words)
_WEIGHT_UNITS = {"kilo", "kilos", "kg", "kgs", "kilogram", "kilograms", "pound", "pounds", "lb", "lbs", "plate", "plates"}
_REP_WORDS = {"rep", "reps", "times", "bar", "baar", "repetitions"}
_TRAILING = {"for", "and", "then", "at", "x", "with", "about", "around", "like", "of"}


def _is_num(tok: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:\.\d+)?", tok)) or tok in _NUMW


def numeric_fragment(text: str) -> bool:
    """A set that stops after the weight or in the middle ("sixty kilos", "sixty", "sixty for", "I did sixty kilos and"): the athlete paused and will say the reps next.
    The coach waits a moment for them instead of answering the half. A finished set ("sixty for five", "sixty kilos five reps") or a question is never a fragment."""
    if "?" in text:
        return False
    w = _norm(text).split()
    if not w or len(w) > 10:
        return False
    if any(x in _REP_WORDS for x in w):
        return False
    if any(_is_num(w[i + 1]) for i in range(len(w) - 1) if w[i] in ("for", "x", "times")):      # "sixty for five": weight and reps both said
        return False
    nums = [i for i, x in enumerate(w) if _is_num(x) and (x not in _NUMW or x in _NUM_STRICT)]
    if not nums:
        return False
    if w[-1] in _TRAILING:
        return True
    last = nums[-1]
    if last == len(w) - 1:
        return True
    return w[-1] in _WEIGHT_UNITS and last == len(w) - 2


def spoken_prefix(items: list, played_s: float) -> str:
    """items = [[text, seconds], ...] in speaking order ("" = an acknowledgement clip); played_s = how long the coach had been talking when it was cut off.
    -> the words that were heard: whole sentences, plus the part of the one that was cut (by share of its duration)."""
    out: list[str] = []
    t = 0.0
    for text, dur in items:
        if played_s >= t + dur:
            if text:
                out.append(text)
        else:
            if text and dur > 0:
                frac = (played_s - t) / dur
                words = text.split()
                k = int(len(words) * frac)
                if frac > 0.15 and k >= 1:
                    out.append(" ".join(words[:k]))
            break
        t += dur
    return " ".join(out).strip()


def unspoken_from(items: list, played_s: float) -> str:
    """The coach was cut off after played_s seconds: the text from the START of the sentence that was cut, to the end of what it had queued (acks are skipped).
    Used to carry on after a backchannel ("yeah") or a cough that interrupted it."""
    out: list[str] = []
    t = 0.0
    for text, dur in items:
        if t + dur > played_s and text:
            out.append(text)
        t += dur
    return " ".join(out).strip()


_BACK_WORDS = {"yeah", "yea", "ya", "yep", "yup", "yes", "ok", "okay", "right", "sure", "got", "it", "hmm", "hm", "mm", "mhm", "uh", "huh", "oh", "cool", "nice", "good", "fine", "alright",
               "i", "see", "understood", "makes", "sense", "true", "exactly", "haan", "han", "ji", "acha", "accha", "theek", "thik", "hai"}


def is_backchannel(text: str, p: float = 1.0) -> bool:
    """"yeah", "ok ok", "mm-hm", "got it": the athlete is only listening. A short, low-confidence sound ("But hi" for "okay") counts too."""
    w = _norm(text).split()
    if not w or len(w) > 4 or any(ch.isdigit() for ch in text) or "?" in text:
        return False
    return all(x in _BACK_WORDS for x in w) or (len(w) <= 2 and p < 0.6)


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
            await self._vad(BARGE_START_SECS)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.speaking = False
            self.stopped_at = time.time()
            await self._vad(0.2)
        elif isinstance(frame, InputTransportMessageFrame) and isinstance(frame.message, dict) and frame.message.get("type") == "config":
            self.barge_in = bool(frame.message.get("barge_in", True))
        elif isinstance(frame, InputAudioRawFrame) and not self.barge_in and (self.speaking or time.time() - self.stopped_at < self.GRACE_S):
            return
        await self.push_frame(frame, direction)

    async def _vad(self, start_secs: float):
        """How long speech must last before it counts as the athlete starting to talk: short while the coach is quiet, longer while it talks (so a "yeah" does not cut it off)."""
        if getattr(self, "_start_secs", None) == start_secs:
            return
        self._start_secs = start_secs
        await self.push_frame(VADParamsUpdateFrame(params=VADParams(start_secs=start_secs, stop_secs=VAD_STOP_SECS)), FrameDirection.DOWNSTREAM)


def is_echo(heard: str, said: str, age_s: float, window_s: float = 4.0) -> bool:
    """Words that are (nearly) all from what the coach itself said a moment ago: its own voice coming back through the microphone, not the user."""
    h, s = _norm(heard).split(), set(_norm(said).split())
    if len(h) < 3 or age_s > window_s + len(said) / 14.0:
        return False
    return sum(1 for w in h if w in s) / len(h) >= 0.8


class Clock:
    """Per-connection stopwatch for one turn: when the user stopped (VAD), when the words were ready, when the turn was released, when the coach's first sound and
    first real word left. Prints one timing line a turn and appends one JSON line (TURN_LOG_DIR) so latency can be read over many turns."""

    def __init__(self, cid: int = 0):
        self.cid = cid
        self.vad_stop = 0.0
        self.transcript_at = 0.0
        self.heard = 0.0
        self.brain_ms = 0
        self.first_audio_done = True
        self.first_sound = 0.0
        self.first_word = 0.0
        self.cur: dict | None = None
        self.extra: dict = {}
        self.marks: dict = {}

    def note(self, name: str, t: float):
        self.marks.setdefault(name, t)

    def start_turn(self, text: str = "", mode: str = ""):
        self.flush()
        self.first_audio_done = False
        self.first_sound = self.first_word = 0.0
        self.brain_ms = 0
        self.extra, self.marks = {}, {}
        self.cur = {"text": text, "mode": mode, "vad_stop": self.vad_stop, "transcript_at": self.transcript_at, "heard": self.heard}

    def flush(self):
        c, self.cur = self.cur, None
        if not c or not c["vad_stop"]:
            return
        v = c["vad_stop"]

        def ms(t):
            return int((t - v) * 1000) if t else None
        rec = {"ts": round(time.time(), 2), "cid": self.cid, "mode": c["mode"], "text": c["text"], "stop_to_transcript_ms": ms(c["transcript_at"]), "stop_to_release_ms": ms(c["heard"]),
               "stop_to_first_say_ms": ms(self.marks.get("first_say")), "stop_to_first_sound_ms": ms(self.first_sound), "stop_to_first_word_ms": ms(self.first_word), **self.extra}
        print(f"[voice {self.cid}] turn: {json.dumps({k: v for k, v in rec.items() if k not in ('text', 'ts')})}", flush=True)
        if TURN_LOG_DIR:
            try:
                os.makedirs(TURN_LOG_DIR, exist_ok=True)
                with open(os.path.join(TURN_LOG_DIR, time.strftime("turns_%Y%m%d.jsonl")), "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError:
                pass


class Spy(FrameProcessor):
    """Prints the speech-detection steps, so "audio arrives but nothing is understood" shows exactly where it stops."""

    def __init__(self, label: str, clock: Clock | None = None, **kw):
        super().__init__(**kw)
        self.label = label
        self.clock = clock

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        n = type(frame).__name__
        if self.clock is not None and n == "VADUserStoppedSpeakingFrame":
            self.clock.vad_stop = time.time()
        if n in ("UserStartedSpeakingFrame", "UserStoppedSpeakingFrame", "VADUserStartedSpeakingFrame", "VADUserStoppedSpeakingFrame", "InterruptionFrame"):
            print(f"[voice] {self.label}: {n}", flush=True)
        elif isinstance(frame, TranscriptionFrame):
            print(f"[voice] {self.label}: transcript {frame.text!r}", flush=True)
        elif n == "ErrorFrame":
            print(f"[voice] {self.label}: ERROR {getattr(frame, 'error', '')}", flush=True)
        await self.push_frame(frame, direction)


class InterruptShield(FrameProcessor):
    """The turn logic emits an "interruption" every time you START talking, and pipecat sends it back upstream to cancel whatever the speech-to-text stage is doing.
    When you pause for half a second inside a sentence ("sixty kilos ... five reps") the first half was being transcribed at that moment, and the resumption
    cancelled it: the first half of the sentence was lost. An interruption is only needed when there is something to interrupt (the coach talking or working),
    so otherwise it is not passed back to the speech-to-text stage."""

    def __init__(self, gate: "Gate", brain: "Brain", **kw):
        super().__init__(**kw)
        self.gate = gate
        self.brain = brain

    def busy(self) -> bool:
        return bool(self.gate.speaking or (self.brain.work and not self.brain.work.done()) or time.time() - self.gate.stopped_at < 0.5)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame) and direction == FrameDirection.UPSTREAM and not self.busy():
            return
        await self.push_frame(frame, direction)


class FirstAudio(FrameProcessor):
    """Sits after the voice: logs how long the athlete waited from the end of their sentence to the first sound of the coach (an acknowledgement clip counts),
    and to the first real word. Also tells the brain how long each spoken piece plays, for "what was actually heard" after an interruption."""

    def __init__(self, clock: Clock, cid: int, brain: "Brain | None" = None, **kw):
        super().__init__(**kw)
        self.clock = clock
        self.cid = cid
        self.brain = brain

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, TTSStartedFrame) and self.brain:
                self.brain.tts_start()
            elif isinstance(frame, TTSStoppedFrame) and self.brain:
                self.brain.tts_stop()
            elif isinstance(frame, OutputAudioRawFrame):
                c, now = self.clock, time.time()
                is_ack = bool((getattr(frame, "metadata", None) or {}).get("ack"))
                if not is_ack and self.brain:
                    self.brain.tts_audio(len(frame.audio), frame.sample_rate)
                if not is_ack and not c.first_word:
                    c.first_word = now
                if not c.first_audio_done and c.vad_stop:
                    c.first_audio_done = True
                    c.first_sound = now
                    print(f"[voice {self.cid}] timing: stop->words {int((c.heard - c.vad_stop) * 1000)} ms | brain {c.brain_ms} ms | words->first audio {int((now - c.heard) * 1000)} ms | "
                          f"TOTAL stop->first audio {int((now - c.vad_stop) * 1000)} ms{' (ack)' if is_ack else ''}", flush=True)
        await self.push_frame(frame, direction)


class EarlyTurn(FrameProcessor):
    """Sits right after the speech-to-text: tells the brain the words are ready (so it can start before the turn model releases the turn) and when."""

    def __init__(self, brain: "Brain", clock: Clock, **kw):
        super().__init__(**kw)
        self.brain = brain
        self.clock = clock

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
            self.clock.transcript_at = time.time()
            if frame.text.strip():
                self.brain.transcribing = True               # words exist: the turn logic (not the "no words" timer) decides whether to carry on
            try:
                self.brain.speculate(frame.text)
            except Exception as e:                                       # noqa: BLE001 (speculation is an extra: it must never break the turn)
                logger.warning(f"speculation failed: {e}")
        await self.push_frame(frame, direction)


# ---- what is not a command ----
# A lone "copy" / "right" / "okay" is the athlete talking to themselves or answering the coach's last line; the coach must not give a speech to it.
_IDLE = {"copy", "right", "ok", "okay", "yeah", "yep", "yes", "no", "hmm", "alright", "cool", "nice", "sure", "thanks", "thank you", "got it", "good", "fine", "oh", "wow", "huh", "hm", "mm"}
_PAUSE = re.compile(r"^(?:(?:wait|weight(?=\W+stop)|(?:give me )?(?:a|one) (?:sec|second|minute|moment)|coach|hold on|hang on|stop|stop it|pause|ruko|rukna|ruk ja|ruk jao|thehro|one sec(?:ond)?|just a (?:sec|second|minute|moment)|ek (?:minute|second|sec|min))\W*)+(?:please|coach|bhai|yaar|ji)?\W*$")
_HELLO = {"hello", "hi", "hey", "coach", "hello coach", "hey coach", "hi coach"}
# narration like "Boxing, wipes hands, adjust straps": third-person stage directions, never something to answer
_STAGE = re.compile(r"\b(?:wipes?|wiping|adjusts?|adjusting|grunts?|sighs?|exhales?|inhales?|chalks?|chalking|claps?|nods?|mumbles?|laughs?|clears? (?:his |my |the )?throat|steps? back|"
                    r"takes? a (?:deep )?breath|grabs? (?:a |the |his )?(?:water|towel|bar)|racks?|re-?racks?|unracks?|shakes? (?:out|it off)|stretch(?:es|ing))\b", re.I)
ACKS = ["Got it.", "Okay.", "Mm-hm."]
SET_LIKE = re.compile(r"\d")
SET_UNITS = re.compile(r"\b(?:kilos?|kgs?|kg|reps?|rep|times|bar|baar|set|sets|for|x|plates?)\b", re.I)


def classify_turn(text: str, follow: bool) -> str:
    """-> 'pass' (send to the coach) | 'idle' (ignore) | 'hello' (say "I'm here") | 'stage' (narration, ignore). Never drops anything with a number, a question mark,
    or anything said right after the coach asked something."""
    low = _norm(text)
    if follow or SET_LIKE.search(low) or "?" in text:
        return "pass"
    if low in _HELLO:
        return "hello"
    if low in _IDLE or _PAUSE.match(low):
        return "idle"
    if _STAGE.search(text) and len(low.split()) <= 14 and not re.search(r"\b(?:i|my|me|we|let'?s|coach|can|should|please|do)\b", low):
        return "stage"
    return "pass"


WRITE_TOOLS = {"log_set", "correct_last_set", "skip_exercise", "unskip_exercise", "swap_exercise", "end_workout", "log_note", "control_rest", "set_next_weight"}


class ConvoRun:
    """One POST /convo/turn running as a task. Its events pile up in a queue, so a run started early (speculation) can be adopted later without losing anything.
    Contract: docs/CONVO_CONTRACTS.md section 1. The queue ends with None; failures arrive as {"t": "_exc"} / {"t": "_http_error"}."""

    def __init__(self, http: httpx.AsyncClient, body: dict, brain_param: str):
        self.body = body
        self.turn_id = body["turn_id"]
        self.text = body["text"]
        self.q: asyncio.Queue = asyncio.Queue()
        self.wrote = False                      # a write tool already ran for this turn (a speculative run that wrote must be kept, never repeated)
        self.t0 = time.time()
        self.task = asyncio.create_task(self._go(http, brain_param))

    async def _go(self, http: httpx.AsyncClient, brain_param: str):
        try:
            async with http.stream("POST", "/convo/turn", params={"brain": brain_param}, json=self.body,
                                   timeout=httpx.Timeout(15.0, read=CONVO_READ_TIMEOUT)) as r:
                if r.status_code != 200:
                    await self.q.put({"t": "_http_error", "status": r.status_code})
                    return
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("t") == "tool" and ev.get("status") == "ok" and ev.get("name") in WRITE_TOOLS:
                        self.wrote = True
                    await self.q.put(ev)
        except asyncio.CancelledError:
            raise
        except Exception as e:                                          # noqa: BLE001 (any failure: the turn falls back to the old brain)
            await self.q.put({"t": "_exc", "exc": f"{type(e).__name__}: {e}"})
        finally:
            await self.q.put(None)

    def cancel(self):
        if not self.task.done():
            self.task.cancel()


class TurnRec:
    """What the coach said in one turn and how long each piece plays, so that after an interruption we know what the athlete ACTUALLY heard."""

    def __init__(self, turn_id: str, text: str):
        self.turn_id, self.text = turn_id, text
        self.items: list[list] = []        # [text, seconds]; text "" = an acknowledgement clip
        self.tts_map: list[int] = []       # index into items of each piece that goes through the voice, in order
        self.tts_i = -1
        self.cur_bytes = 0
        self.cur_sr = OUT_RATE
        self.started_at = 0.0              # when the first sound of this turn reached the speaker
        self.reported = False

    def add(self, text: str, secs: float | None = None, tts: bool = False) -> int:
        self.items.append([text, secs if secs is not None else max(0.4, len(text) / 14.0)])
        if tts:
            self.tts_map.append(len(self.items) - 1)
        return len(self.items) - 1


class Brain(FrameProcessor):
    """One finished user turn in, one spoken answer out.
    mode "legacy": the coach's own /voice/command (every rule and tool stays where it is).
    mode "converse": the Conversation Core, POST /convo/turn (docs/CONVO_CONTRACTS.md): the answer streams in sentence by sentence and each sentence is spoken
    the moment it arrives; the Core falls back to the old brain by itself, and so do we if the Core cannot be reached."""

    BRAIN_TIMEOUT_S = float(os.environ.get("VOICE_BRAIN_TIMEOUT", "12"))
    FILLER_AFTER_S = float(os.environ.get("VOICE_FILLER_AFTER", "1.4"))

    def __init__(self, token: str, backend: str = BACKEND, clock: Clock | None = None, cid: int = 0, mode: str = "legacy", **kw):
        super().__init__(**kw)
        self.token = token
        self.screen = "live"
        self.mode = mode if mode in ("legacy", "converse") else "legacy"
        self.conn_id = f"voice-{os.getpid()}-{cid}-{int(time.time())}"
        self.follow = False                 # the coach just asked a question: the next turn is the answer
        self.carry = ""                      # an unfinished sentence the coach is waiting to hear the end of
        self.work: asyncio.Task | None = None
        self.last_bot = ("", 0.0)           # what the coach said last, and when (to tell its own echo from the user)
        self.clock = clock or Clock()
        self.cid = cid
        self.last_unclear = 0.0
        self.turn_n = 0
        self.bot_speaking = False
        self.user_speaking = False
        self.user_stopped_at = 0.0
        self.hold_text = ""                  # a half-said set ("sixty kilos") held back a moment, in case "five reps" follows
        self.hold: asyncio.Task | None = None
        self.hold_n = 0
        self.hold_since = 0.0
        self.spec: dict | None = None        # a turn started before the turn model released it (speculation)
        self.committed = ""                  # text of a speculative turn that already wrote something: never run it a second time
        self.committed_at = 0.0
        self.rec: TurnRec | None = None
        self.resume: tuple[str, str, float] | None = None      # (turn_id, text to carry on with, when): set when a sound cut the coach off
        self.resume_timer: asyncio.Task | None = None
        self.transcribing = False
        self.user_started_at = 0.0
        self.set_active: bool | None = None
        self.rest_remaining: int | None = None
        self.lang_pref: str | None = None
        self.http = httpx.AsyncClient(base_url=backend, timeout=self.BRAIN_TIMEOUT_S, headers={"Authorization": f"Bearer {token}"},
                                      limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=120.0))

    # ------------------------------------------------------------------ frames
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self.bot_speaking = True
            if self.rec and not self.rec.started_at:
                self.rec.started_at = time.time()
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, BotStoppedSpeakingFrame):
            self.bot_speaking = False
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, UserStartedSpeakingFrame):
            self.user_speaking = True
            self.user_started_at = time.time()
            self._on_user_resumed()
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, UserStoppedSpeakingFrame):
            self.user_speaking = False
            self.user_stopped_at = time.time()
            if self.resume:
                self._arm_resume_timer()
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, InputTransportMessageFrame):
            msg = frame.message if isinstance(frame.message, dict) else {}
            if msg.get("type") == "screen":
                self.screen = str(msg.get("screen") or "live")
            elif msg.get("type") == "config":                          # the app tells what it knows: mid-set, rest timer, language (barge_in is handled by the Gate)
                if "set_active" in msg:
                    self.set_active = bool(msg["set_active"]) if msg["set_active"] is not None else None
                if "rest_remaining" in msg:
                    try:
                        self.rest_remaining = int(msg["rest_remaining"]) if msg["rest_remaining"] is not None else None
                    except (TypeError, ValueError):
                        self.rest_remaining = None
                if msg.get("lang") in ("en", "hinglish", "hi"):
                    self.lang_pref = msg["lang"]
            elif msg.get("type") == "interrupt":                       # a tap on the phone: stop talking now, keep listening
                await self._on_interrupted(tap=True)
                await self.push_interruption_task_frame_and_wait()
            return
        if isinstance(frame, InterruptionFrame):
            await self._on_interrupted()
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
                text = self._strip_committed(text)
            if text:
                self._turn_released(text)
            return
        await self.push_frame(frame, direction)

    # ------------------------------------------------------------------ carrying on after a sound that was not an interruption
    def _arm_resume_timer(self):
        if self.resume_timer and not self.resume_timer.done():
            self.resume_timer.cancel()
        self.resume_timer = asyncio.create_task(self._resume_if_no_words())

    async def _resume_if_no_words(self):
        """A cough or noise cut the coach off and no words followed: say the rest. If words ARE coming (the speech stage is working on them) the turn logic decides instead."""
        try:
            await asyncio.sleep(RESUME_AFTER_S)
            # still talking (the turn model can hold "yeah" open for seconds, so not for ever), or the words are being worked out: the turn logic decides
            while (self.user_speaking and time.time() - self.user_started_at < 4.0) or self.transcribing:
                if time.time() - (self.resume[2] if self.resume else 0) > RESUME_MAX_AGE_S:
                    return
                await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            return
        if self.resume and not self.transcribing and not (self.work and not self.work.done()):
            await self._resume("no words")

    async def _resume(self, why: str):
        r, self.resume = self.resume, None
        if not r or time.time() - r[2] > RESUME_MAX_AGE_S or not r[1] or self.bot_speaking:
            return
        rec = TurnRec(r[0], "")
        self.rec = rec
        self.last_bot = (r[1], time.time())
        print(f"[voice {self.cid}] carrying on after {why}: {r[1][:70]!r}", flush=True)
        await self._speak(r[1], rec)

    def _turn_released(self, text: str):
        """The turn model says the athlete is done. A half-said set is held back a moment first (the pause-in-the-middle bug); everything else goes to the brain."""
        if self.hold_text:                                              # the rest of a held set arrived: join it
            text = (self.hold_text + " " + text).strip()
            self._clear_hold()
        if numeric_fragment(text) and self.hold_n < 2 and UNCLEAR not in text:
            self.hold_n += 1
            self.hold_text = text
            self.hold_since = time.time()
            print(f"[voice {self.cid}] holding a half-said set: {text!r}", flush=True)
            if self.work and not self.work.done():
                self.work.cancel()
            self.hold = asyncio.create_task(self._hold_timer())
            return
        self.hold_n = 0
        if self.work and not self.work.done():
            self.work.cancel()
        run = self._adopt_spec(text)
        self.work = asyncio.create_task(self.answer(text, run))

    def _clear_hold(self):
        self.hold_text = ""
        if self.hold and not self.hold.done() and self.hold is not asyncio.current_task():
            self.hold.cancel()
        self.hold = None

    async def _hold_timer(self):
        """Wait for the second half of a set. While the athlete is talking again (or just stopped and the speech is still being transcribed) keep waiting, up to a limit."""
        try:
            t_end, hard = time.time() + NUM_HOLD_S, time.time() + NUM_HOLD_MAX_S
            while True:
                await asyncio.sleep(0.05)
                now = time.time()
                if self.user_speaking:
                    t_end = max(t_end, now + 0.5)
                elif self.user_stopped_at > self.hold_since:
                    t_end = max(t_end, self.user_stopped_at + 1.8)
                if now >= t_end or now >= hard:
                    break
            text, self.hold_text, self.hold = self.hold_text, "", None
            if text:
                self.work = asyncio.create_task(self.answer(text, self._adopt_spec(text)))
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------ speculation
    def speculate(self, text: str):
        """The words are ready but the turn model has not said "finished" yet: start the Core already. Nothing is spoken until the turn is released; if the athlete
        keeps talking the run is dropped (or, if it already logged something, kept and never repeated)."""
        if self.mode != "converse" or not SPECULATE or self.spec or self.hold_text or self.carry:
            return
        t = (text or "").strip()
        if not t or UNCLEAR in t or is_prompt_echo(t) or numeric_fragment(t) or classify_turn(t, self.follow) != "pass":
            return
        said, at = self.last_bot
        if said and is_echo(t, said, time.time() - at):
            return
        run = self._start_run(t, "hinglish" if is_hinglish(t) else "en", speculative=True)
        self.spec = {"norm": _norm(t), "run": run, "text": t}

    def _adopt_spec(self, text: str):
        sp, self.spec = self.spec, None
        if sp and sp["norm"] == _norm(text):
            return sp["run"]
        if sp:
            self._drop_run(sp)
        return None

    def _on_user_resumed(self):
        sp, self.spec = self.spec, None
        if sp:
            self._drop_run(sp)

    def _drop_run(self, sp: dict):
        run: ConvoRun = sp["run"]
        if run.wrote:                                                   # it already logged something: keep that, speak nothing, and never run this text again
            self.committed, self.committed_at = sp["text"], time.time()
            asyncio.create_task(self._drain_silently(run))
        else:
            run.cancel()

    async def _drain_silently(self, run: ConvoRun):
        try:
            while True:
                ev = await asyncio.wait_for(run.q.get(), timeout=CONVO_READ_TIMEOUT + 2)
                if ev is None:
                    break
        except Exception:                                               # noqa: BLE001
            run.cancel()
        await self._report_interrupt(run.turn_id, "")

    def _strip_committed(self, text: str) -> str:
        c, self.committed = self.committed, ""
        if not c or time.time() - self.committed_at > 25:
            return text
        k = len(_norm(c).split())
        if _norm(text).startswith(_norm(c)):
            return " ".join(text.split()[k:]).strip()
        return text

    # ------------------------------------------------------------------ interruption
    async def _on_interrupted(self, tap: bool = False):
        """The athlete started talking over the coach (or tapped the phone). Tell the Core what was actually heard, and stop working."""
        rec = self.rec
        if rec and not rec.reported and (self.bot_speaking or (self.work and not self.work.done())):
            rec.reported = True
            played = (time.time() - rec.started_at) if rec.started_at else 0.0
            spoken = spoken_prefix(rec.items, played) if rec.started_at else ""
            rest = unspoken_from(rec.items, played) if (rec.started_at and not tap) else ""
            self.resume = (rec.turn_id, rest, time.time()) if rest else None
            if self.resume:
                self._arm_resume_timer()
            self.last_bot = (spoken, time.time())
            if "?" not in spoken:
                self.follow = False
            if self.mode == "converse":
                asyncio.create_task(self._report_interrupt(rec.turn_id, spoken))
            print(f"[voice {self.cid}] interrupted after {played:.2f}s; heard: {spoken!r}", flush=True)
        if self.work and not self.work.done():
            self.work.cancel()

    async def _report_interrupt(self, turn_id: str, spoken: str):
        try:
            await self.http.post("/convo/interrupt", json={"conn_id": self.conn_id, "turn_id": turn_id, "spoken": spoken, "reason": "barge_in"}, timeout=3.0)
        except Exception as e:                                          # noqa: BLE001
            logger.warning(f"could not report the interruption: {e}")

    # the voice tells how long each piece really played (FirstAudio calls these)
    def tts_start(self):
        r = self.rec
        if r:
            r.tts_i += 1
            r.cur_bytes = 0

    def tts_audio(self, n: int, sr: int):
        r = self.rec
        if r:
            r.cur_bytes += n
            r.cur_sr = sr or OUT_RATE

    def tts_stop(self):
        r = self.rec
        if r and 0 <= r.tts_i < len(r.tts_map) and r.cur_bytes:
            r.items[r.tts_map[r.tts_i]][1] = r.cur_bytes / (2.0 * r.cur_sr)

    # ------------------------------------------------------------------ one turn
    async def answer(self, text: str, run: ConvoRun | None = None):
        said, at = self.last_bot
        if said and is_echo(text, said, time.time() - at):
            logger.warning(f"ignored the coach's own echo: {text!r}")
            if run:
                run.cancel()
            await say(self, t="state", s="listening")
            return
        self.clock.heard = time.time()
        self.clock.start_turn(text, self.mode)
        if UNCLEAR in text:                          # the recogniser heard speech but is not sure what: ask again (once in a row), never act on a guess
            self.carry = ""
            if run:
                run.cancel()
            if time.time() - self.last_unclear > 8.0:
                self.last_unclear = time.time()
                print(f"[voice {self.cid}] unclear speech -> asking to repeat (stt {STT_STATS['last']})", flush=True)
                await say(self, t="state", s="thinking")
                line = "Sorry, say that again?"
                self.last_bot = (line, time.time())
                await self.push_frame(TTSSpeakFrame(line))
            else:
                await say(self, t="state", s="listening")
            return
        full = (self.carry + " " + text).strip()
        kind = classify_turn(full, self.follow) if not self.carry else "pass"
        self.transcribing = False
        if kind in ("idle", "pass") and self.resume and is_backchannel(full, float((STT_STATS["last"] or {}).get("p") or 1.0)) and not (self.follow and "?" in self.resume[1]):
            if run:
                run.cancel()
            print(f"[voice {self.cid}] backchannel {full!r} cut the coach off: carrying on", flush=True)
            await self._resume("a backchannel")
            return
        if kind == "pass" or kind == "hello" or not is_backchannel(full, float((STT_STATS["last"] or {}).get("p") or 1.0)):
            self.resume = None                                          # a real question, "wait, stop" or narration: do not carry on with the old answer
        if kind != "pass":
            if run:
                run.cancel()
            print(f"[voice {self.cid}] ignored ({kind}): {full!r}", flush=True)
            if kind == "hello":
                await say(self, t="user", text=full)
                line = "I'm here. Go ahead."
                self.last_bot = (line, time.time())
                await self.push_frame(TTSSpeakFrame(line))
            else:
                await say(self, t="state", s="listening")
            return
        self.carry = ""
        lang = self.lang_pref or ("hinglish" if is_hinglish(full) else "en")
        print(f"[voice {self.cid}] heard: {full!r} ({lang}, stt {STT_STATS['last']}){' [speculated]' if run else ''}", flush=True)
        await say(self, t="user", text=full)
        await say(self, t="state", s="thinking")
        if self.mode == "converse":
            await self._converse(full, lang, run)
        else:
            await self._legacy(full, lang)

    def _start_run(self, full: str, lang: str, speculative: bool = False) -> ConvoRun:
        self.turn_n += 1
        asr = {"avg_logprob": (STT_STATS["last"] or {}).get("logprob"), "unclear": False}
        body = {"text": full, "turn_id": f"{self.conn_id}-{'s' if speculative else 'u'}{self.turn_n}", "conn_id": self.conn_id, "lang": lang,
                "screen": self.screen, "asr": asr, "ambient": False}
        if self.set_active is not None:
            body["set_active"] = self.set_active
        if self.rest_remaining is not None:
            body["rest_remaining"] = self.rest_remaining
        return ConvoRun(self.http, body, "converse")

    async def _play_ack(self, kind: str, rec: TurnRec):
        pcm = ACK_PCM.get(kind)
        if not pcm:
            return
        rec.add("", len(pcm) / (2.0 * OUT_RATE))
        await say(self, t="ack", kind=kind, ms=round(len(pcm) / (2.0 * OUT_RATE) * 1000))      # lets a test tell this clip from the composed answer (the app ignores it)
        fr = OutputAudioRawFrame(audio=pcm, sample_rate=OUT_RATE, num_channels=1)
        fr.metadata["ack"] = True
        await self.push_frame(fr)

    async def _speak(self, text: str, rec: TurnRec, first: bool = False):
        for part in (chunks(text, longest=7, shortest=3) if first else chunks(text)):
            rec.add(part, tts=True)
            await self.push_frame(TTSSpeakFrame(part))

    async def _converse(self, full: str, lang: str, run: ConvoRun | None):
        if run is None or run.text != full:
            if run:
                run.cancel()
            run = self._start_run(full, lang)
        rec = TurnRec(run.turn_id, full)
        self.rec = rec
        t0 = time.time()
        acked = False
        if SET_LIKE.search(full) and SET_UNITS.search(full) and "got_it" in ACK_PCM:      # a set being logged: "got it" at once, the real answer follows
            await self._play_ack("got_it", rec)
            acked = True
        spoke: list[str] = []
        done = None
        failure = None
        try:
            while True:
                ev = await run.q.get()
                if ev is None:
                    break
                k = ev.get("t")
                if k == "ack":
                    kind = ev.get("kind")
                    if kind == "one_sec" and not spoke:
                        await self._play_ack("one_sec", rec)
                    elif not acked and kind in ACK_PCM:
                        await self._play_ack(kind, rec)
                        acked = True
                elif k == "say":
                    s = (ev.get("text") or "").strip()
                    if s:
                        if not spoke:
                            self.clock.note("first_say", time.time())
                        spoke.append(s)
                        await self._speak(s, rec, first=len(spoke) == 1)
                        self.last_bot = ((" ".join(spoke)), time.time())
                elif k == "state":
                    if isinstance(ev.get("state"), dict):
                        await say(self, t="state", state=ev["state"])
                elif k == "done":
                    done = ev
                elif k in ("_exc", "_http_error"):
                    failure = ev
                elif k == "error":
                    print(f"[voice {self.cid}] core error: {ev.get('code')} {ev.get('message')}", flush=True)
        except asyncio.CancelledError:
            run.cancel()
            raise
        if done is None:
            run.cancel()
            print(f"[voice {self.cid}] Conversation Core unavailable ({(failure or {}).get('exc') or (failure or {}).get('status') or 'no answer'}); "
                  f"{'answering with the old brain' if not spoke else 'keeping what was said'}", flush=True)
            if not spoke:
                await self._legacy(full, lang)
            return
        self.clock.brain_ms = int((time.time() - t0) * 1000)
        result = done.get("result") or {}
        intent = done.get("intent") or result.get("intent")
        self.clock.extra.update(turn_id=run.turn_id, brain=done.get("brain"), fallback=bool(done.get("fallback")), intent=intent, model=done.get("model"),
                                model_calls=(done.get("ms") or {}).get("model_calls"), core_ms=done.get("ms"), tools=done.get("tools"))
        if intent == "fragment":                      # an unfinished sentence: say nothing, keep it, join it to the next words
            self.carry = full
            await say(self, t="state", s="listening")
            return
        self.follow = bool(done.get("expects"))
        reply = (done.get("said") or result.get("reply") or "").strip()
        if reply or result.get("actions"):
            await say(self, t="bot", text=reply, intent=intent, ms=self.clock.brain_ms, result=result or {"reply": reply, "intent": intent})
        else:
            await say(self, t="state", s="listening")
        if spoke:
            self.last_bot = (" ".join(spoke), time.time())

    async def _legacy(self, full: str, lang: str):
        t0 = time.time()
        spoke = ""
        if SET_LIKE.search(full) and SET_UNITS.search(full):          # a set being logged: say "got it" at once, the real answer follows (~1.5-3 s of model time)
            spoke = random.choice(ACKS)
            await self.push_frame(TTSSpeakFrame(spoke))
        filler = asyncio.create_task(self._filler()) if not spoke else None
        out = None
        try:
            for attempt in (0, 1):
                try:
                    r = await self.http.post("/voice/command", json={"transcript": full, "screen": self.screen, "day": None, "ambient": False, "followup": self.follow, "lang": lang})
                    r.raise_for_status()
                    out = r.json()
                    break
                except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError) as e:      # backend restarting / stale keep-alive connection: one quick retry
                    if attempt == 1:
                        raise
                    logger.warning(f"brain connection hiccup, retrying: {e}")
                    await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            print(f"[voice {self.cid}] brain timed out after {self.BRAIN_TIMEOUT_S:.0f}s for {full!r}", flush=True)
            await self._speak_error("That is taking too long. Say it again in a few words?")
            return
        except Exception as e:
            print(f"[voice {self.cid}] brain failed: {type(e).__name__}: {e}", flush=True)
            await self._speak_error("Sorry, I lost you for a second. Say that again?")
            return
        finally:
            if filler:
                filler.cancel()
        self.clock.brain_ms = int((time.time() - t0) * 1000)
        reply = (out.get("reply") or "").strip()
        if out.get("intent") == "fragment":        # an unfinished sentence: say nothing, keep it, join it to the next words
            self.carry = full
            await say(self, t="state", s="listening")
            return
        self.follow = bool(out.get("expects"))
        await say(self, t="bot", text=reply, intent=out.get("intent"), ms=self.clock.brain_ms, result=out)      # the app applies sets / navigation from the same result it always got
        if reply:
            line = spoken(reply)
            self.last_bot = ((spoke + " " + line).strip(), time.time())
            for part in chunks(line):
                await self.push_frame(TTSSpeakFrame(part))

    async def _filler(self):
        """The answer is slow (model time): say so once, instead of leaving the athlete in silence."""
        try:
            await asyncio.sleep(self.FILLER_AFTER_S)
            line = random.choice(["One sec.", "Let me think."])
            print(f"[voice {self.cid}] slow answer: saying {line!r}", flush=True)
            self.last_bot = (line, time.time())
            await self.push_frame(TTSSpeakFrame(line))
        except asyncio.CancelledError:
            pass

    async def _speak_error(self, line: str):
        self.last_bot = (line, time.time())
        await say(self, t="state", s="listening")
        await self.push_frame(TTSSpeakFrame(line))

    async def close(self):
        """Connection ended: stop what is running, tell the Core the session is over (it writes a short summary), close the HTTP client."""
        if self.work and not self.work.done():
            self.work.cancel()
        if self.spec:
            self.spec["run"].cancel()
        self._clear_hold()
        self.clock.flush()
        if self.mode == "converse" and self.turn_n:
            try:
                await self.http.post("/convo/event", json={"conn_id": self.conn_id, "event": "session_end", "data": {}, "turn_id": f"{self.conn_id}-end"}, timeout=3.0)
            except Exception:                                           # noqa: BLE001
                pass
        await self.http.aclose()


def chunks(line: str, longest: int = 9, shortest: int = 4) -> list[str]:
    """A long sentence is spoken in phrases: the first one is voiced while the rest is still being made (the voice model needs ~0.35 s for 6 words, ~1.2 s for 15)."""
    out: list[str] = []
    for sent in [x for x in re.split(r"(?<=[.!?])\s+", line.strip()) if x]:
        words = sent.split()
        if len(words) <= longest:
            out.append(sent)
            continue
        cur: list[str] = []
        for w in words:
            cur.append(w)
            if len(cur) >= shortest and (w.endswith((",", ":", ";")) or len(cur) >= longest + 2):
                out.append(" ".join(cur))
                cur = []
        if cur:
            if out and len(cur) < 3:
                out[-1] += " " + " ".join(cur)
            else:
                out.append(" ".join(cur))
    return out or [line]


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


_live_conns: set = set()
_STARTED = time.time()


@app.get("/health")
async def health():
    """ok = models are warm. Also: how long it has been up, how many phones are connected, which models, the last speech-recognition numbers, and whether the coach backend answers."""
    backend_ok = None
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            backend_ok = (await c.get(f"{BACKEND}/health")).status_code == 200
    except Exception:
        backend_ok = False
    return {"ok": _ready["warm_s"] is not None, "warm_s": _ready["warm_s"], "uptime_s": int(time.time() - _STARTED), "connections": len(_live_conns),
            "whisper": WHISPER_MODEL, "voice": VOICE, "backend": BACKEND, "brain": BRAIN_DEFAULT, "acks": sorted(ACK_PCM), "backend_ok": backend_ok, "last_stt": STT_STATS["last"]}


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
    cid = next(_conn_ids)
    clock = Clock(cid)
    mode = (websocket.query_params.get("brain") or BRAIN_DEFAULT).strip().lower()
    brain = Brain(token, backend, clock, cid, mode=mode)
    _live_conns.add(cid)
    print(f"[voice {cid}] connected (backend {backend}, brain {brain.mode})", flush=True)

    async def report():
        last = 0
        while True:
            await asyncio.sleep(3)
            rx = ser.rx if ser else {"bytes": 0, "frames": 0, "peak": 0}
            print(f"[voice {cid}] mic: {rx['frames']} frames, {rx['bytes'] // 1024} KB total, {(rx['bytes'] - last) // 1024} KB in the last 3 s, loudest sample {rx['peak']}/32767", flush=True)
            last, rx["peak"] = rx["bytes"], 0

    async def keepwarm():
        """While a phone is connected: a small silent transcription every ~0.8 s, unless the speech stage is busy. Holds the GPU at full clock."""
        loop = asyncio.get_event_loop()
        while True:
            await asyncio.sleep(0.8)
            w = next(iter(_whisper_cache.values()), None)
            if w is not None and time.time() - w.busy_until > 0.5:
                await loop.run_in_executor(None, w.ping)

    reporter = asyncio.create_task(report())
    warmer = asyncio.create_task(keepwarm()) if KEEPWARM else None
    gate = Gate()
    pipe = Pipeline([transport.input(), gate, Spy("in", clock), make_stt(), Spy("stt"), EarlyTurn(brain, clock), InterruptShield(gate, brain), pair.user(), brain, make_tts(), FirstAudio(clock, cid, brain), transport.output()])
    task = PipelineTask(pipe, params=PipelineParams(audio_in_sample_rate=IN_RATE, audio_out_sample_rate=OUT_RATE, allow_interruptions=True))

    @transport.event_handler("on_client_disconnected")
    async def _gone(_t, _c):
        await task.cancel()

    try:
        await PipelineRunner(handle_sigint=False).run(task)
    except asyncio.CancelledError:
        raise
    except Exception as e:                       # one bad connection must never take the others (or the server) down, and the reason must be in the log
        import traceback
        print(f"[voice {cid}] pipeline ERROR {type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}", flush=True)
    finally:
        _live_conns.discard(cid)
        reporter.cancel()
        if warmer:
            warmer.cancel()
        if CAPTURE and ser is not None and len(ser.capture) > 16000:
            try:
                import wave as _wave
                os.makedirs(CAPTURE_DIR, exist_ok=True)
                path = f"{CAPTURE_DIR}/conn_{int(time.time())}_{cid}.wav"
                with _wave.open(path, "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(IN_RATE); w.writeframes(bytes(ser.capture))
                print(f"[voice {cid}] saved the phone's audio: {path}", flush=True)
            except Exception as e:
                print(f"[voice {cid}] could not save audio: {e}", flush=True)
        print(f"[voice {cid}] closed", flush=True)
        await brain.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("VOICE_PORT", "8765")), log_level="warning")
