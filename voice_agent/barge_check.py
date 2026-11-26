"""Interruption test for the voice agent: does a "yeah" / "mm-hm" / cough while the coach talks leave it talking, and how fast does a real "wait, stop" stop it?
    D:\\voice-proto\\venv\\Scripts\\python voice_agent\\barge_check.py [ws_url] [trials]
Use a THROWAWAY agent and backend (see voice_agent/README.md): it makes the coach give a long answer, then plays the sound at a fixed moment of that answer.
Prints one line per trial and a summary. A trial 'stopped' when the agent sent {"t":"clear"} (= stop the speaker) within 1.5 s of the sound."""
import asyncio
import json
import sys
import time

import numpy as np
import websockets

SR = 16000
CHUNK = SR // 50 * 2
MODELS = "D:/voice-proto/models"
URL = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8766/ws?token=testtoken&brain=converse"
TRIALS = int(sys.argv[2]) if len(sys.argv) > 2 else 10
ASK = "Explain to me how to brace my core before a heavy squat and why it matters."
BACKCHANNELS = ["Yeah.", "Mm-hm.", "Okay.", "Right.", "Uh-huh.", "Got it.", "Yeah, okay.", "Sure.", "Hmm.", "Mm."]
REAL = ["Wait, stop.", "Hold on a second.", "Stop, stop.", "No wait, I have a question.", "Coach, one moment."]


def render(lines):
    from kokoro_onnx import Kokoro
    k = Kokoro(f"{MODELS}/kokoro-v1.0.onnx", f"{MODELS}/voices-v1.0.bin")
    out = {}
    for t in lines:
        x, sr = k.create(t, voice="am_adam", speed=1.0, lang="en-us")
        y = np.interp(np.linspace(0, len(x) - 1, int(len(x) * SR / sr)), np.arange(len(x)), x)
        out[t] = (np.clip(y, -1, 1) * 32767).astype("<i2").tobytes()
    return out


def cough():
    rng = np.random.default_rng(3)
    n = int(SR * 0.28)
    env = np.exp(-np.linspace(0, 6, n)) * np.hanning(n)
    x = rng.standard_normal(n) * env * 0.5
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


async def trial(clips, sound_name, sound, play_at_s):
    """-> dict(first_audio, stopped, stop_ms, said_len_s)"""
    st = {"first": None, "clear": None, "sound_at": None, "audio_bytes": 0, "after_clear": 0}
    async with websockets.connect(URL, max_size=None) as ws:
        async def reader():
            async for m in ws:
                now = time.time()
                if isinstance(m, bytes):
                    if st["first"] is None:
                        st["first"] = now
                    st["audio_bytes"] += len(m) - 1
                    if st["clear"] is not None and now >= st["clear"] + 0.15:
                        st["after_clear"] += len(m) - 1
                else:
                    d = json.loads(m)
                    if d.get("t") == "clear" and st["sound_at"] and st["clear"] is None and now >= st["sound_at"]:
                        st["clear"] = now
        rd = asyncio.create_task(reader())
        await ws.send(json.dumps({"type": "screen", "screen": "live"}))
        await asyncio.sleep(0.6)

        async def stream(data):
            t = time.time()
            n = 0
            for i in range(0, len(data), CHUNK):
                await ws.send(data[i:i + CHUNK])
                n += 1
                await asyncio.sleep(max(0, t + n * 0.02 - time.time()))

        await stream(clips[ASK])
        # silence like a mic until the coach has been talking for play_at_s
        t_wait = time.time()
        while time.time() - t_wait < 20:
            await ws.send(b"\x00" * CHUNK)
            await asyncio.sleep(0.02)
            if st["first"] and time.time() - st["first"] >= play_at_s:
                break
        if st["first"] is None:
            rd.cancel()
            return {"first_audio": False}
        st["sound_at"] = time.time()
        await stream(sound)
        t_end = time.time()
        while time.time() - t_end < 1.5 and st["clear"] is None:
            await ws.send(b"\x00" * CHUNK)
            await asyncio.sleep(0.02)
        if st["clear"] is not None:                                  # did the coach carry on by itself (a "yeah" or a cough is no reason to drop the answer)?
            t_more = time.time()
            while time.time() - t_more < 5.0:
                await ws.send(b"\x00" * CHUNK)
                await asyncio.sleep(0.02)
        spoke_for = (st["audio_bytes"] / (16000 * 2))
        rd.cancel()
        return {"first_audio": True, "stopped": st["clear"] is not None, "stop_ms": int((st["clear"] - st["sound_at"]) * 1000) if st["clear"] else None,
                "len_ms": int(len(sound) / 2 / SR * 1000), "coach_audio_s": round(spoke_for, 1),
                "carried_on": st["after_clear"] > 16000 * 2 * 0.5}


async def main():
    clips = render([ASK] + BACKCHANNELS + REAL)
    results = {"backchannel": [], "cough": [], "real": []}
    for i in range(TRIALS):
        name = BACKCHANNELS[i % len(BACKCHANNELS)]
        r = await trial(clips, name, clips[name], 2.0)
        results["backchannel"].append(r)
        print(f"backchannel {name!r:14} -> {r}", flush=True)
    for i in range(max(3, TRIALS // 3)):
        r = await trial(clips, "cough", cough(), 2.0)
        results["cough"].append(r)
        print(f"cough {i + 1}               -> {r}", flush=True)
    for i, name in enumerate(REAL):
        r = await trial(clips, name, clips[name], 2.0)
        results["real"].append(r)
        print(f"real {name!r:30} -> {r}", flush=True)
    bc = [r for r in results["backchannel"] if r.get("first_audio")]
    co = [r for r in results["cough"] if r.get("first_audio")]
    re_ = [r for r in results["real"] if r.get("first_audio")]
    print("\nSUMMARY")
    print(f"  backchannel left alone: {sum(1 for r in bc if not r['stopped'])}/{len(bc)}   (target: left alone + carried on >= 9/10); stopped but carried on by itself: {sum(1 for r in bc if r['stopped'] and r.get('carried_on'))}")
    print(f"  cough left alone:       {sum(1 for r in co if not r['stopped'])}/{len(co)}; stopped but carried on by itself: {sum(1 for r in co if r['stopped'] and r.get('carried_on'))}")
    stops = sorted(r["stop_ms"] for r in re_ if r["stopped"])
    print(f"  real interruption stopped it: {len(stops)}/{len(re_)}; stop time after the athlete starts talking (ms): {stops}  (target <= 300)")


asyncio.run(main())
