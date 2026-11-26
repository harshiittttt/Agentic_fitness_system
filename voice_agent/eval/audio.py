"""Audio mode: the caller is a synthetic voice (Kokoro, rendered once and cached) streamed into a THROWAWAY voice agent over its websocket, in real time,
exactly like the phone does (PCM16 16 kHz, 20 ms frames). Measures: what the agent HEARD (WER vs what was said), time from the last sample sent to the first coach
sound, time to the brain's reply message, and how fast playback is cleared when the caller talks over the coach (barge-in).

Start the throwaway pair first (never :8765 / :8001):
    python voice_agent/throwaway_backend.py
    BACKEND_URL=http://127.0.0.1:8011 VOICE_PORT=8766 VOICE_CAPTURE=0 D:\\voice-proto\\venv\\Scripts\\python voice_agent\\server.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

import numpy as np
import websockets

from .scorers import wer

CACHE_DIR = Path(os.environ.get("EVAL_AUDIO_CACHE", "D:/voice-proto/audio/eval"))
VENV_PY = os.environ.get("VOICE_PY", "D:/voice-proto/venv/Scripts/python.exe")
RENDER = Path(__file__).with_name("render_tts.py")
SR = 16000
CHUNK = SR // 50 * 2          # 20 ms of PCM16
DEVA = __import__("re").compile(r"[\u0900-\u097F]")


# ---- rendering (cached) ----------------------------------------------------------------------------------------------
def _job(text: str, voice: str, speed: float) -> dict:
    hindi = bool(DEVA.search(text))
    v = "hm_psi" if hindi else voice
    lang = "hi" if hindi else "en-us"
    key = hashlib.sha1(f"{text}|{v}|{lang}|{speed}".encode("utf-8")).hexdigest()[:20]
    return {"key": key, "text": text, "voice": v, "lang": lang, "speed": speed, "out": str(CACHE_DIR / f"{key}.pcm")}


def render_all(texts: list[str], voice: str, speed: float) -> dict[str, bytes]:
    """-> {text: pcm}. Renders the missing ones in ONE venv subprocess (loading Kokoro takes a few seconds)."""
    jobs = {t: _job(t, voice, speed) for t in dict.fromkeys(texts)}
    missing = [j for j in jobs.values() if not Path(j["out"]).exists()]
    if missing:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(missing, f, ensure_ascii=False)
        print(f"  rendering {len(missing)} caller lines with Kokoro (cached afterwards)...", flush=True)
        r = subprocess.run([VENV_PY, str(RENDER), f.name], capture_output=True, text=True)
        os.unlink(f.name)
        if r.returncode != 0:
            raise SystemExit(f"TTS render failed:\n{r.stderr[-800:]}")
    return {t: Path(j["out"]).read_bytes() for t, j in jobs.items()}


# ---- noise / far-away variants ---------------------------------------------------------------------------------------
def _pink(n: int, rng: np.random.Generator) -> np.ndarray:
    w = rng.standard_normal(n)
    f = np.fft.rfft(w)
    f /= np.sqrt(np.maximum(np.arange(len(f)), 1))
    p = np.fft.irfft(f, n)
    return p / (np.std(p) + 1e-9)


def gym_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    """Pink noise + 50 Hz mains hum + random metal 'clanks' (short decaying bursts) + a faint music-like tone bed."""
    t = np.arange(n) / SR
    x = _pink(n, rng) * 0.8 + 0.3 * np.sin(2 * np.pi * 50 * t) + 0.15 * np.sin(2 * np.pi * (220 + 30 * np.sin(2 * np.pi * 0.2 * t)) * t)
    for _ in range(max(1, n // (SR * 3))):
        s = int(rng.integers(0, max(1, n - SR // 4)))
        m = min(SR // 4, n - s)
        x[s:s + m] += rng.standard_normal(m) * np.exp(-np.arange(m) / (SR * 0.03)) * 2.0
    return x / (np.std(x) + 1e-9)


def degrade(pcm: bytes, noise_db: float | None, lowpass: bool, seed: int = 0) -> bytes:
    if noise_db is None and not lowpass:
        return pcm
    rng = np.random.default_rng(seed)
    x = np.frombuffer(pcm, "<i2").astype(np.float32) / 32768
    if lowpass:                                         # phone in a pocket / far away: muffled and quiet
        k = np.ones(9) / 9
        x = np.convolve(x, k, mode="same") * 0.35
    if noise_db is not None:
        sp = np.sqrt(np.mean(x ** 2)) + 1e-9
        nz = gym_noise(len(x), rng)
        x = x + nz * sp / (10 ** (noise_db / 20))
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


# ---- the websocket session -------------------------------------------------------------------------------------------
class AudioExecutor:
    def __init__(self, brain, ws_url: str, noise_db: float | None = None, lowpass: bool = False, speed: float = 1.0, voice: str = "am_adam"):
        self.brain, self.ws_url, self.noise_db, self.lowpass, self.speed, self.voice = brain, ws_url, noise_db, lowpass, speed, voice
        if ":8765" in ws_url:
            raise SystemExit("refusing to run against :8765 (the REAL voice agent). Start a throwaway agent on :8766.")
        tag = ("+noise%gdB" % noise_db if noise_db is not None else "") + ("+lowpass" if lowpass else "")
        self.label = f"audio:{brain.name}{tag}"
        self.loop = asyncio.new_event_loop()
        self.ws = None
        self.msgs: asyncio.Queue | None = None
        self.state: dict = {}
        self._pcm: dict[str, bytes] = {}

    def prerender(self, scenarios: list[dict]) -> None:
        texts = []
        for s in scenarios:
            for t in s["turns"]:
                texts += t.get("say_parts") or [t["say"]]
        self._pcm = render_all(texts, self.voice, self.speed)

    # sync facade used by runner.run -----------------------------------------------------------------------------
    def open_scenario(self, scen: dict) -> None:
        ts = scen["turns"]
        for i, t in enumerate(ts):
            t["_fire"] = i + 1 < len(ts) and ts[i + 1].get("interrupt_at_ms") is not None
        self.loop.run_until_complete(self._open())

    def close_scenario(self) -> None:
        self.loop.run_until_complete(self._close())

    def turn(self, turn: dict, lang: str | None) -> dict:
        return self.loop.run_until_complete(self._turn(turn))

    async def _open(self) -> None:
        self.ws = await websockets.connect(self.ws_url, max_size=None)
        self.msgs = asyncio.Queue()
        self.state = {"audio_times": [], "clear_at": [], "bot": None, "user": None}
        self._reader = asyncio.create_task(self._read())
        await self.ws.send(json.dumps({"type": "screen", "screen": "live"}))
        await asyncio.sleep(0.8)

    async def _close(self) -> None:
        if self.ws:
            self._reader.cancel()
            await self.ws.close()
            self.ws = None

    async def _read(self) -> None:
        try:
            async for m in self.ws:
                now = time.time()
                if isinstance(m, bytes):
                    self.state["audio_times"].append(now)
                else:
                    d = json.loads(m)
                    if d.get("t") == "user":
                        self.state["user"] = (self.state["user"] + " " if self.state["user"] else "") + d["text"]
                    elif d.get("t") == "bot":
                        self.state["bot"] = {**d, "at": now}
                    elif d.get("t") == "ack":
                        self.state.setdefault("acks", []).append((now, float(d.get("ms") or 0) / 1000.0))
                    elif d.get("t") == "clear":
                        self.state["clear_at"].append(now)
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass

    async def _stream(self, data: bytes) -> float:
        t0 = time.time()
        for n, i in enumerate(range(0, len(data), CHUNK), 1):
            await self.ws.send(data[i:i + CHUNK])
            await asyncio.sleep(max(0.0, t0 + n * 0.02 - time.time()))
        return time.time()

    async def _silence(self, secs: float) -> None:
        end = time.time() + secs
        while time.time() < end:
            await self.ws.send(b"\x00" * CHUNK)
            await asyncio.sleep(0.02)

    async def _turn(self, turn: dict) -> dict:
        st = self.state
        parts = turn.get("say_parts") or [turn["say"]]
        barge_at = None
        if turn.get("interrupt_at_ms") is not None:                 # talk over the coach: wait until it has been speaking this long
            first = next(iter(st["audio_times"]), None) or time.time()
            while time.time() < first + turn["interrupt_at_ms"] / 1000:
                await self.ws.send(b"\x00" * CHUNK)
                await asyncio.sleep(0.02)
            barge_at = time.time()
            st.update(bot=None, user=None, clear_at=[])
        else:
            st.update(audio_times=[], bot=None, user=None, clear_at=[], acks=[])
            await self._silence(0.3)
        sent_end = barge_at or time.time()
        for i, p in enumerate(parts):
            pcm = degrade(self._pcm[p], self.noise_db, self.lowpass, seed=zlib.crc32(p.encode("utf-8")) & 0xFFFF)
            sent_end = await self._stream(pcm)
            if i + 1 < len(parts):
                await self._silence(turn.get("gap_s", 1.0))
                sent_end = time.time()

        def after() -> list[float]:
            return [t for t in st["audio_times"] if t >= sent_end - 0.05]

        t_wait = time.time()
        while time.time() - t_wait < 25:                           # silence like a real mic until the coach has answered and finished talking
            await self.ws.send(b"\x00" * CHUNK)
            await asyncio.sleep(0.02)
            a = after()
            if turn.get("_fire") and a:                              # the next turn will interrupt: return as soon as the coach has started speaking
                break
            if st["bot"] and a and time.time() - a[-1] > 1.0:
                break
            if st["bot"] and not a and time.time() - st["bot"]["at"] > 4:     # a silent answer (an ignored line): nothing more will come
                break
        bot = st["bot"] or {}
        heard = st["user"] or ""
        a = after()
        res = {"reply": bot.get("text") or "", "intent": bot.get("intent"), "actions": bot.get("actions") or [], "expects": bot.get("expects"),
               "ms": (bot["at"] - sent_end) * 1000 if bot else None, "first_word_ms": None,
               "first_sound_ms": (a[0] - sent_end) * 1000 if a else None, "heard": heard, "wer": wer(" ".join(parts), heard), "raw": bot}
        # first composed word = the first audio that is not the short acknowledgement clip (its chunks arrive in one burst right after the `ack` message)
        acks = [x for x in st.get("acks", []) if x[0] >= sent_end - 0.5]
        comp = [t for t in a if not any(t0 - 0.05 <= t <= t0 + 0.4 for t0, _ in acks)]
        res["first_word_ms"] = (comp[0] - sent_end) * 1000 if comp else None
        st["acks"] = []
        if barge_at is not None:
            c = [x for x in st["clear_at"] if x >= barge_at]
            res["barge_stop_ms"] = round((c[0] - barge_at) * 1000) if c else None
        return res
