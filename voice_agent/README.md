# Live voice agent (runs on the PC, uses the GPU)

```
phone mic  --PCM16 16 kHz (echo-cancelled)-->  gateway /voice/ws  -->  voice agent (127.0.0.1:8765)
                                                                         VAD (Silero) -> turn model (Smart Turn v3) -> Whisper -> coach brain -> Kokoro
phone speaker  <--PCM16 16 kHz--------------------------------------------------------------------------------------------'
```

The **coach brain is not replaced**: every finished sentence goes to the backend's own `POST /voice/command`, so set logging, the
"what weight / how many reps?" questions, the no-invented-numbers rule and the diet planner all stay in `backend/`. The agent only adds
streaming audio, end-of-turn detection, speech-to-text, the voice, and interrupting.

If the agent is off, the app notices and uses the phone's own recogniser (Profile > Talking to your coach > Live voice switch forces that).

## Setup (once, Windows, NVIDIA GPU)
```
python -m venv D:\voice-proto\venv
D:\voice-proto\venv\Scripts\python -m pip install "pipecat-ai[silero,websocket,openai,local-smart-turn-v3,whisper,kokoro]==0.0.108" uvicorn
D:\voice-proto\venv\Scripts\python -m pip install -r voice_agent\requirements.txt      # exact versions that were tested (CUDA libs come as pip wheels)
# Kokoro voice files -> D:\voice-proto\models\  (kokoro-v1.0.onnx, voices-v1.0.bin from github.com/thewh1teagle/kokoro-onnx releases, model-files-v1.0)
```
Whisper `small` downloads itself on first start into `D:\voice-proto\hf`. Everything is kept on D: (the C: drive is nearly full).

## Run
`START_COACH.bat` starts it with the rest (deploy/local/start_all.ps1, step 3b). By hand: `D:\voice-proto\venv\Scripts\python voice_agent\server.py`
(env: `BACKEND_URL` default `http://127.0.0.1:8001`, `VOICE_PORT`, `WHISPER_MODEL`, `VOICE_TURN_STOP_SECS` default 1.3).

## Test without a phone
```
python voice_agent\throwaway_backend.py            # throw-away backend on :8011 with demo data (never touches a real log)
BACKEND_URL=http://127.0.0.1:8011 D:\voice-proto\venv\Scripts\python voice_agent\server.py
D:\voice-proto\venv\Scripts\python voice_agent\play_client.py missing    # plays recorded speech in real time, prints the timeline
cd mobile && VOICE_TEST_URL="ws://127.0.0.1:8765/ws?token=testtoken" npx jest __tests__/stream.test.ts   # the app's own client against it
```

## Measured on this PC (RTX 3050 4 GB, recorded speech, local network)
* Whisper small on GPU 0.21 s for 3 s of audio (CPU: 4.2 s). Kokoro on GPU 0.2-0.3 s per sentence (CPU 1.2-2 s).
* Last word -> first coach audio: 1.0-2.9 s, dominated by the brain's model call (Gemini, 0.7-2.5 s). First turn after a cold start is ~5 s.
* Hinglish: Whisper writes Hindi in Devanagari unless prompted; with the Hinglish prompt in `server.py` it writes Roman letters
  ("Chhe hai reps huye, vazhan pachas kilo tha." -> logged 50 kg x 6). English is unchanged.

## NOT verified
Any real phone: echo cancelling on the user's handset, mic/speaker latency over mobile data, real human Hinglish speech
(all tests use synthesized speech), barge-in with the speaker playing (tests use recorded audio, no acoustic echo).
