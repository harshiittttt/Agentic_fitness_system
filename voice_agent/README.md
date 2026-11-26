# Live voice agent (runs on the PC, uses the GPU)

```
phone mic  --PCM16 16 kHz (echo-cancelled)-->  gateway /voice/ws  -->  voice agent (127.0.0.1:8765)
                                                                         VAD (Silero) -> turn model (Smart Turn v3) -> Whisper -> [gates] -> coach brain -> Kokoro
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
(env: `BACKEND_URL` default `http://127.0.0.1:8001`, `VOICE_PORT`, `WHISPER_MODEL`, `VOICE_TURN_STOP_SECS` default 1.3, `VOICE_BRAIN_TIMEOUT` default 12 s,
`VOICE_FILLER_AFTER` default 1.4 s, `STT_LOGPROB_MIN`, `STT_NOSPEECH_MAX`, `STT_NOSPEECH_LOGPROB`).
`GET /health` -> `{ok, warm_s, uptime_s, connections, whisper, voice, backend, backend_ok, last_stt}` (`last_stt` = language, confidence and time of the last transcription).
Every turn logs one line: `[voice N] timing: stop->words .. | brain .. | words->first audio .. | TOTAL stop->first audio ..`. Per-connection errors
(`brain timed out`, `brain failed`, `pipeline ERROR` with a traceback) are logged with the connection number.

## What it does for a turn (2026-11-25 rebuild)
1. **Speech-to-text safety net** (`_PromptedWhisper`): Whisper small with a Hinglish prompt; language auto-detected but only English or Hindi accepted (anything else is
   re-run as English: short English clips came back as Welsh/Latin); known gym mishearings fixed ("sit mar dia" -> "set maar diya", "gobble squads" -> "goblet squat",
   "dead lift", "vazhan" ...); Whisper's silence hallucinations ("Thank you for watching", its own prompt repeated) dropped; **a segment is only dropped as
   silence when Whisper is unsure there was speech AND unsure of the words** (see "Bug found" below); if speech was heard but the words are low-confidence
   (mumble, garbled Hindi) the coach says "Sorry, say that again?" (once in a row) instead of acting on a guess.
2. **Not everything is a command** (`classify_turn`): a lone "copy" / "right" / "okay" is ignored, "hello" gets "I'm here. Go ahead.", narration like
   "Boxing, wipes hands, adjust straps" is ignored. Never dropped: anything with a number, a question mark, or anything said right after the coach asked something.
3. **Language**: the coach is told which language you spoke (`lang` = `en` | `hinglish`, sent to `/voice/command`); English in -> English out, Hinglish in -> Hinglish
   (Roman letters) out for the model's answers. Canned/state answers ("12 sets left...") stay English. The voice is the English Kokoro voice.
4. **Speed**: sets ("60 kilos 5 reps done") get an instant "Got it." while the real answer is composed; questions the session state can answer (what's the plan,
   how many sets left, what weight next) are answered by the backend without a model (`_session_q` in `backend/coach/voice_control.py`, ~25 ms); any other slow answer
   gets one "One sec." after 1.4 s; long sentences are voiced in phrases so the first one starts early; one shared HTTP connection pool to the model API
   (`backend/agents/base.py`, no new TLS handshake per call); brain calls retry once on a connection hiccup, time out after 12 s with a spoken apology.
5. **Interrupting**: talking over the coach stops it in ~0.35 s and your words are still heard (tested). The turn logic's "interruption" is no longer sent back to the
   speech-to-text stage when there is nothing to interrupt (`InterruptShield`).

## Measured on this PC (RTX 3050 4 GB, synthetic speech through the real Whisper + backend, throw-away demo data)
Last word you say -> first sound of the coach (client-measured, `play_client.py golu/hesitate/need/hinglish/missing`, same scenarios before and after):

| | before | after |
|---|---|---|
| median | 3.78 s | **2.29 s** |
| mean | 4.42 s | 2.46 s |
| 90th percentile | 4.64 s | 3.64 s |
| fastest / slowest | 1.63 / 17.3 s | 0.79 / 4.85 s |

By kind of turn (server timing lines, after): a set being logged **~1.0 s** (instant "Got it."), a question the session state answers **~1.1-1.3 s**, a turn the
model must compose (start, ready, free questions) **~2.0-2.7 s** (model 0.8-2.5 s + "One sec." after 1.4 s). Of the ~1.1 s floor: ~0.6-0.8 s from the end of your
sentence to the words (VAD stop 0.25 s + turn model + Whisper small 0.4 s), ~0.4 s for the voice's first sound. The model call (Gemini lite) is 0.8-3.6 s and varies a lot
from minute to minute; it is the part that is not under this program's control.
Whisper small 0.42 s per clip; **medium 1.05 s per clip (+0.6 s on every turn) and needs 3.4 GB of the 4 GB with the other agent loaded**: it fixed one garbled Hindi
sentence of twelve and wrote another in Devanagari, so it is not used (`voice_agent/stt_bench.py small medium` reruns the comparison).
Hinglish (Kokoro Hindi voice speaking the app's phrases, 7 sentences + 6 English): all English clips exact; Hindi clips came back in readable Roman letters
(`Aaj ka plan kya hai?`, `Kitne set baki hain?`, `Ruko thoda, aaram karne do.`); "weight badhao" sometimes comes back half in Devanagari, which the backend still understands.

### Bug found and fixed in this pass: sentences were being thrown away
pipecat's Whisper service drops every segment whose `no_speech_prob >= 0.4`. Whisper reports 0.7-0.8 for real, clearly transcribed speech that is followed by silence
(e.g. the first half of "We are not skipping anything, <pause> we are doing every exercise"), so the first half of a sentence with a pause in it vanished and the coach
answered only the second half. The speech stage now uses Whisper's own rule (silence = unsure there was speech AND unsure of the words). `play_client.py pause` is the
regression test.

## Tests
```
python voice_agent\throwaway_backend.py            # throw-away backend on :8011 with demo data (never touches a real log)
set BACKEND_URL=http://127.0.0.1:8011&& set VOICE_PORT=8766&& set VOICE_CAPTURE=0&& D:\voice-proto\venv\Scripts\python voice_agent\server.py   # a second agent, not the real one
D:\voice-proto\venv\Scripts\python voice_agent\make_test_audio.py                       # makes the English and Hindi test sentences (Kokoro) once
D:\voice-proto\venv\Scripts\python voice_agent\play_client.py <scenario> ws://127.0.0.1:8766/ws?token=testtoken
D:\voice-proto\venv\Scripts\python -m pytest voice_agent\test_voice_logic.py -q         # pure logic: what is a command, STT safety net, Hinglish, phrases (40 tests)
python -m pytest backend\tests\test_voice_session_q.py -q                               # the no-model session questions
cd mobile && VOICE_TEST_URL="ws://127.0.0.1:8765/ws?token=testtoken" npx jest __tests__/stream.test.ts   # the app's own client against it
```
Scenarios: `golu` (the full workout talk incl. "not skipping"), `speed` (latency table), `hesitate` / `need` (pauses mid-sentence, joined by the brain), `pause`
(a pause inside one sentence: both halves must be heard), `hinglish`, `hindi` (Hindi speech), `missing`, `noise` (copy / stage direction / mumble / hello: no speech
from the coach), `barge` (talk over the coach: playback must clear), `skip_real`. NB: never pipe the restart script of a background process into `Out-Null`/`Select-Object`
in PowerShell: the children hold the pipe open and the command never returns.

## NOT verified
Any real phone: echo cancelling on the user's handset, mic/speaker latency over mobile data, real human Hinglish speech (every test uses synthesized speech;
real Haryanvi/Hinglish accents will score lower than Kokoro's clean Hindi), barge-in with the speaker playing (tests use recorded audio, no acoustic echo),
the 0.4 s "Got it." wording on a real set (the app still gets the same result object it always got), the log-confidence limits (`-1.05`) on real, noisy gym audio:
they were set from clean speech (-0.15..-0.65), so check `[voice N] unclear speech` lines in the first real session and loosen `STT_LOGPROB_MIN` if good speech is refused.

## Barge-in speed and carrying on (2026-11-26)
Speech must last `VOICE_BARGE_START_SECS` (default 0.15 s) while the coach talks before it counts as talking over it; the coach then stops about 0.3-0.33 s after you start (was 0.52-0.57 s with 0.4 s).
A sound that was not an interruption is put right afterwards: if what follows is a backchannel ("yeah", "mm-hm", "okay", a short doubtful sound) or no words at all, the coach carries on from the START of the sentence it was cut in (`unspoken_from`, `Brain._resume`);
"wait / stop / hold on" and real questions do not resume. `VOICE_RESUME_AFTER` (default 2.2 s) is how long it waits for words before carrying on. `python voice_agent/barge_check.py` measures it (10 backchannels, 3 coughs, 5 real interruptions).
Measured on synthetic speech (throwaway agent): stop 301-330 ms; 8/10 backchannels carried on by itself ("Hmm." and a Whisper mishearing like "Shim." are not recognised); coughs of 0.28 s are not heard as speech at all. NOT verified with a real speaker/phone echo or gym noise: if noise cuts the coach off too often, raise `VOICE_BARGE_START_SECS` to 0.25-0.4.
