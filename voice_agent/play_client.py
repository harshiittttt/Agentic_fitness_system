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
    # regression for ERROR_LEDGER E-001/E-002: "not skipping" must not skip, sets must land on the current exercise
    "golu": [("say", "t_start"), ("say", "t_plan"), ("say", "t_set1"), ("say", "t_set2"), ("say", "t_no_skip"), ("say", "t_no_skip2"),
             ("say", "t_nextset"), ("say", "t_next_ex"), ("say", "t_left")],
    "skip_real": [("say", "t_real_skip"), ("say", "t_next_ex")],
    # speed: the questions the coach answers from the session state, plus a set; latency table in README
    "speed": [("say", "t_start"), ("say", "t_plan"), ("say", "t_set1"), ("say", "t_left"), ("say", "t_weight_q"), ("say", "t_sets60")],
    # things that are not commands: the coach must stay quiet (or say one short line), never give a speech
    "noise": [("say", "t_copy"), ("say", "t_stage"), ("say", "t_mumble"), ("say", "t_hello")],
    # Hindi speech (Kokoro Hindi voice) through the real Whisper + backend
    "hindi": [("say", "t_hi_plan"), ("say", "t_hi_set"), ("say", "t_hi_left"), ("say", "t_hi_weight"), ("say", "t_hi_next"), ("say", "t_hi_wait")],
    # barge-in: talk over the coach while it is speaking; its audio must stop
    "barge": [("fire", "t_plan"), ("over", "t_left")],
    # a pause inside a sentence: both halves must be heard (regression: the first half used to be lost)
    "pause": [("say", "t_no_skip"), ("say", "t_no_skip"), ("say", "t_no_skip")],
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
                        state["heard_at"] = now
                        stamp(f"heard: {d['text']!r}")
                    elif d.get("t") == "bot":
                        state["bot_at"] = now
                        stamp(f"brain [{d.get('intent')}, {d.get('ms')} ms]: {d['text']!r}")
                    elif d.get("t") == "state" and d.get("s") == "listening":
                        state["listening_at"] = now
                    elif d.get("t") == "clear":
                        state["clear_at"] = now
                        stamp("(interrupt: stop playback)")
        rd = asyncio.create_task(reader())
        await ws.send(json.dumps({"type": "screen", "screen": "live"}))
        await asyncio.sleep(0.8)
        chunk = SR // 50 * 2
        steps = SCENARIOS[name]
        for idx, (kind, arg) in enumerate(steps):
            if kind == "over":                                   # wait until the coach is speaking, then talk over it
                t_wait = time.time()
                while not state["audio_first"] and time.time() - t_wait < 30:
                    await ws.send(b"\x00" * chunk)
                    await asyncio.sleep(0.02)
                await asyncio.sleep(0.6)
                state["over_at"] = time.time()
                state["clear_at"] = None
                stamp(f"BARGE-IN: you start talking over the coach: {arg}")
                data = pcm(arg)
            elif kind == "gap":
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
            if kind == "fire":                                   # said, and not waiting for the answer (the next step talks over it)
                state["sent_end"] = time.time()
                state["audio_first"] = None
                state["clear_at"] = None
                continue
            if kind == "say" and idx + 1 < len(steps) and steps[idx + 1][0] == "gap":
                continue                                     # a pause follows: the coach should NOT answer yet
            if kind == "over":
                ca = state.get("clear_at")
                stamp(f"barge-in result: playback cleared {ca - state['over_at']:.2f}s after you started talking" if ca else "barge-in result: NOT interrupted")
            if kind in ("say", "over"):
                state["sent_end"] = time.time()
                state["audio_first"] = None
                state["bot_at"] = state["listening_at"] = state["heard_at"] = None
                # silence like a real mic, until the coach has answered and finished talking
                waited = 0.0
                while waited < 20:
                    await ws.send(b"\x00" * chunk)
                    await asyncio.sleep(0.02)
                    waited += 0.02
                    now_ = time.time()
                    answered = state["bot_at"] or state["listening_at"] or (state["heard_at"] and now_ - state["heard_at"] > 10.0)   # the coach has said its piece (or stayed silent on purpose)
                    ref = max(state["audio_last"] or 0, state["bot_at"] or 0, state["listening_at"] or 0, state["heard_at"] or 0)
                    if answered and now_ - ref > 1.0:
                        break
                spoke = state["bytes"] / (24000 * 2)
                fa = state.get("first_after")
                stamp(f"turn done. coach audio started {fa:.2f}s after you stopped" if fa is not None else "turn done. NO AUDIO")
                state["bytes"] = 0
        await asyncio.sleep(0.5)
        rd.cancel()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "ws://127.0.0.1:8765/ws?token=testtoken"))
