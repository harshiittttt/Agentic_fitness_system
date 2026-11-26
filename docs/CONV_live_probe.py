import re, json, time, base64, asyncio, sys, websockets
env = {}
for l in open(".env", encoding="utf-8"):
    m = re.match(r"\s*([A-Z_]+)=(.*)", l)
    if m: env[m.group(1)] = m.group(2).strip().strip('"')
SIL = int(sys.argv[3]) if len(sys.argv) > 3 else 500
KEY = env.get("GEMINI_API_KEY") or env.get("LLM_API_KEY")
URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=" + KEY
SYS = ("You are Ustaad, a warm, brief gym coach speaking aloud to an athlete mid-workout. Reply in one short sentence (max 15 words). "
       "Speak the language the athlete speaks (English, or Hinglish in simple words). Never invent numbers. "
       "WORKOUT STATE: exercise = Back squat, set 2 of 4. Last logged: 60 kg x 8. Next in plan: Romanian deadlift. "
       "Call log_set when the athlete tells you a completed set. Call skip_exercise ONLY if they clearly order a skip. Never skip when they say they are NOT skipping.")
TOOLS = [{"functionDeclarations": [
  {"name": "log_set", "description": "Log a completed set.", "parameters": {"type": "OBJECT", "properties": {"exercise": {"type": "STRING"}, "weight_kg": {"type": "NUMBER"}, "reps": {"type": "INTEGER"}}, "required": ["reps"]}},
  {"name": "skip_exercise", "description": "Skip an exercise for today (only on a clear order).", "parameters": {"type": "OBJECT", "properties": {"exercise": {"type": "STRING"}}, "required": ["exercise"]}}]}]
async def probe(model, name):
    pcm = open(f"D:/voice-proto/audio/t_{name}.pcm", "rb").read()
    out = {"model": model, "clip": name}
    t_open = time.time()
    try:
        async with websockets.connect(URL, max_size=None, open_timeout=15) as ws:
            setup = {"setup": {"model": "models/" + model, "generationConfig": {"responseModalities": ["AUDIO"]},
                     "systemInstruction": {"parts": [{"text": SYS}]}, "tools": TOOLS,
                     "inputAudioTranscription": {}, "outputAudioTranscription": {}, "realtimeInputConfig": {"automaticActivityDetection": {"silenceDurationMs": SIL}}}}
            await ws.send(json.dumps(setup))
            first = json.loads(await asyncio.wait_for(ws.recv(), 15))
            out["setup_ms"] = int((time.time() - t_open) * 1000)
            if "setupComplete" not in first: out["setup_resp"] = str(first)[:200]
            audio = pcm + b"\x00" * 32000   # 1 s of silence so server VAD ends the turn
            # send paced at 4x real time to keep test short
            t_end = None; t_send0 = time.time()
            for i in range(0, len(audio), 640):
                if i >= len(pcm) and not getattr(probe, 'marked', False):
                    probe.marked = True; t_mark = time.time()
                await ws.send(json.dumps({"realtimeInput": {"audio": {"data": base64.b64encode(audio[i:i+640]).decode(), "mimeType": "audio/pcm;rate=16000"}}}))
                await asyncio.sleep(0.02)
            t_end = t_mark; probe.marked = False
            in_tx, out_tx, calls, first_audio, first_call, n_audio = "", "", [], None, None, 0
            deadline = time.time() + 16
            while time.time() < deadline:
                try: raw = await asyncio.wait_for(ws.recv(), 6)
                except asyncio.TimeoutError: break
                m = json.loads(raw)
                if "toolCall" in m:
                    for fc in m["toolCall"].get("functionCalls", []):
                        calls.append((fc["name"], fc.get("args")))
                        if first_call is None: first_call = time.time() - t_end
                    await ws.send(json.dumps({"toolResponse": {"functionResponses": [{"id": fc.get("id"), "name": fc["name"], "response": {"result": "ok, logged"}} for fc in m["toolCall"]["functionCalls"]]}}))
                sc = m.get("serverContent", {})
                if sc.get("inputTranscription"): in_tx += sc["inputTranscription"].get("text", "")
                if sc.get("outputTranscription"): out_tx += sc["outputTranscription"].get("text", "")
                for p in (sc.get("modelTurn") or {}).get("parts", []):
                    if p.get("inlineData"):
                        n_audio += 1
                        if first_audio is None: first_audio = time.time() - t_end
                if sc.get("turnComplete") or sc.get("generationComplete"):
                    if first_audio is not None or calls: 
                        if sc.get("turnComplete") and not calls: break
            out.update(first_audio_s=None if first_audio is None else round(first_audio, 2), first_call_s=None if first_call is None else round(first_call, 2),
                       heard=in_tx.strip()[:90], said=out_tx.strip()[:110], calls=calls, audio_chunks=n_audio)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:150]}"
    print(json.dumps(out, ensure_ascii=True), flush=True)
async def main():
    for model in sys.argv[1].split(","):
        for clip in sys.argv[2].split(","):
            await probe(model, clip)
asyncio.run(main())
