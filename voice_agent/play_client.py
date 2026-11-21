"""Plays recorded speech into the voice server like a phone would (PCM16 16 kHz, real time) and prints a timeline.
    python voice_agent/play_client.py <scenario> [ws_url]        ws_url default ws://127.0.0.1:8765/ws?token=testtoken
Latency is measured from the moment the last word was sent to the first byte of coach audio."""
import asyncio
import json
import sys
import time
import wave

import websockets

AUDIO = "D:/voice-proto/audio/"
SR = 16000
SCENARIOS = {
    "log": [("say", "c_log")],
    "missing": [("say", "c_missing"), ("say", "c_answer")],
    "question": [("say", "c_q")],
    "hesitate": [("say", "p_did_like"), ("gap", 2.5), ("say", "c_log")],
    "need": [("say", "p_need_a"), ("gap", 1.5), ("say", "p_need_b")],
    "h2": [("say", "h2")],
    "hinglish": [("say", "h1"), ("say", "h2"), ("say", "h4")],
}


def pcm(name):
    import os
    if os.path.exists(AUDIO + name + ".pcm"):
        return open(AUDIO + name + ".pcm", "rb").read()
    with wave.open(AUDIO + name + ".wav") as w:
        assert w.getframerate() == SR and w.getnchannels() == 1
        return w.readframes(w.getnframes())


async def main(name, url):
    T0 = time.time()
    state = {"sent_end": None, "audio_first": None, "audio_last": None, "bytes": 0}
    log = []

    def stamp(msg):
        rel = f"{time.time() - state['sent_end']:+6.2f}s" if state["sent_end"] else "      "
        print(f"  {time.time() - T0:6.2f}s {rel}  {msg}", flush=True)

    async with websockets.connect(url, max_size=None) as ws:
        async def reader():
            async for m in ws:
                now = time.time()
                if isinstance(m, bytes):
                    if state["audio_first"] is None or now - state["audio_last"] > 1.5:
                        stamp(">>> COACH AUDIO STARTS")
                        state["audio_first"] = now
                        state["first_after"] = (now - state["sent_end"]) if state["sent_end"] else None
                    state["audio_last"] = now
                    state["bytes"] += len(m) - 1
                else:
                    d = json.loads(m)
                    if d.get("t") == "user":
                        stamp(f"heard: {d['text']!r}")
                    elif d.get("t") == "bot":
                        stamp(f"brain [{d.get('intent')}, {d.get('ms')} ms]: {d['text']!r}")
                    elif d.get("t") == "clear":
                        stamp("(interrupt: stop playback)")
        rd = asyncio.create_task(reader())
        await ws.send(json.dumps({"type": "screen", "screen": "live"}))
        await asyncio.sleep(0.8)
        chunk = SR // 50 * 2
        steps = SCENARIOS[name]
        for idx, (kind, arg) in enumerate(steps):
            if kind == "gap":
                data = b"\x00" * int(SR * 2 * arg)
            else:
                data = pcm(arg)
                stamp(f"you say: {arg}")
            t = time.time()
            n = 0
            for i in range(0, len(data), chunk):
                await ws.send(data[i:i + chunk])
                n += 1
                await asyncio.sleep(max(0, t + n * 0.02 - time.time()))
            if kind == "say" and idx + 1 < len(steps) and steps[idx + 1][0] == "gap":
                continue                                     # a pause follows: the coach should NOT answer yet
            if kind == "say":
                state["sent_end"] = time.time()
                state["audio_first"] = None
                # silence like a real mic, until the coach has answered and finished talking
                waited = 0.0
                while waited < 14:
                    await ws.send(b"\x00" * chunk)
                    await asyncio.sleep(0.02)
                    waited += 0.02
                    if state["audio_first"] and time.time() - state["audio_last"] > 1.0:
                        break
                spoke = state["bytes"] / (24000 * 2)
                fa = state.get("first_after")
                stamp(f"turn done. coach audio started {fa:.2f}s after you stopped" if fa is not None else "turn done. NO AUDIO")
                state["bytes"] = 0
        await asyncio.sleep(0.5)
        rd.cancel()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "ws://127.0.0.1:8765/ws?token=testtoken"))
