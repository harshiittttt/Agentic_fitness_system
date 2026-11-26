# Conversational voice coach: research and build plan

Written 2026-11-25. Research lead report. Nothing in the code was changed; the only files written are this one and `docs/CONV_live_probe.py` (a measurement script, see section 2.3).

How to read this: section 1 is the whole story on one page. Sections 2-4 are the evidence and the design. Section 5 is the order of work. Sections 6-8 are tests, decisions for you, sources.

Labels used throughout:
- **[V]** verified in a source I read (URL in section 8) or measured by me on this PC today.
- **[M]** measured by me today (first-hand; small samples, see the caveats in 2.3).
- **[I]** my inference or judgement. Treat as a hypothesis until the test harness says otherwise.
- **[?]** could not verify.

Plain-language glossary (defined once):
- **VAD** (voice activity detection): a small model that says "someone is speaking right now".
- **Turn detection / end-of-turn**: deciding the person has finished their thought, not just paused. Different from VAD.
- **Barge-in**: you talk while the coach is talking and it stops.
- **Backchannel**: tiny listener sounds ("mm-hm", "go on") that are not a turn.
- **ASR / STT**: speech to text. **TTS**: text to speech.
- **Cascade**: ASR -> LLM -> TTS as three separate steps (what you have). **Speech-to-speech / native audio**: one model hears audio and speaks audio (Gemini Live, Moshi, GPT-realtime).
- **TTFT**: time to first token, how long an LLM takes before it starts answering.
- **Tool / function call**: the LLM emits a structured request ("log_set(squat, 60, 5)") instead of text; your code runs it and hands back the result.
- **Hinglish**: Hindi and English mixed, written in Roman letters.

---

## 1. Vision in one page

### What "conversational" means here

Today the coach is a command bot with a nice voice: you say a sentence, a pile of rules decides what you meant, a template or a one-shot model call produces a reply, and the pipeline forgets the exchange. Conversational means four things change:

1. **The model owns the dialogue.** One LLM sees the whole recent conversation plus an exact snapshot of the workout, writes the reply, and decides itself when a reply should also *do* something (log a set, start rest, skip, undo). Rules do not interpret; rules only *guard* (they can veto or ask, never invent).
2. **It starts talking before it has finished thinking.** A short acknowledgement within about half a second, the real sentence streaming to the speaker sentence by sentence.
3. **Turn-taking feels human.** It waits through "sixty... five", does not answer "mm-hm", stops within a third of a second when you cut in, and remembers only what you actually heard of its interrupted sentence.
4. **It has initiative and memory.** It speaks first at the right moment (rest over, PR, long idle) and brings up what it knows about you from earlier sessions, within a talking budget so it never nags.

What it is *not*: a chatbot that chats through your set. During a set it gives at most 3-6 word cues. Long talk belongs to rest and between exercises.

### What a great 30 seconds sounds like (timings are from the end of the athlete's last word, T=0)

**Dialogue 1: logging a set while talking naturally**

| T | Who | What happens |
|---|---|---|
| -3.0 s | Athlete | (finishing a squat set 2 of 4; the last set was 60 x 8) "...done. Sixty. Five." |
| +0.45 s | Coach | "Mm-hm." (a pre-recorded short acknowledgement, played because digits + a weight/rep word were heard; it is a sound, not a claim) |
| +0.9 s | System | tool call `log_set(Back squat, 60 kg, 5 reps)` returns `saved, set 2/4, 6 sets left today` (the app screen updates) |
| +1.6 s | Coach | "Five at sixty, three fewer than last set. Heavy today?" (first word of the *composed* sentence; it only quotes numbers that came back from the tool) |
| +4.0 s | Athlete | "Yeah, legs are dead." |
| +5.0 s | Coach | "Fair. Two minutes, I'm starting the clock. Same weight next, go for six." + `start_rest(120)` |

**Dialogue 2: a correction and an ambiguity (the real bug from the error ledger, E-001)**

| T | Who | What happens |
|---|---|---|
| 0 | Athlete | "We are not skipping anything. We are doing every exercise." |
| +1.5 s | Coach | "Good, everything stays. Romanian deadlift after this set." No tool call. (A skip guard would also have blocked a skip call here.) |
| +20 s | Athlete | "Let's move on." |
| +1.4 s | Coach | "Move on to Romanian deadlift, and drop the last two squat sets? Yes or no?" (ambiguous, so it asks; nothing is skipped yet) |
| +2.5 s | Athlete | "No no, just do sixty-five next set, not sixty." |
| +1.4 s | Coach | "Sixty-five next set. Got it." + `set_next_weight(65)`. It heard a *correction* ("not sixty") and the LLM resolved "no" against the question it had just asked. |

**Dialogue 3: an interruption**

| T | Who | What happens |
|---|---|---|
| 0 | Coach | (rest period) "Next up is Romanian deadlift. Keep your back flat, push your hips back, and bar close to your legs, and last time you..." |
| +2.1 s | Athlete | "Wait, what weight?" |
| +2.1 s + 0.3 s | System | audio stops within 300 ms; the dialogue history stores only what was *actually spoken* ("Next up is Romanian deadlift. Keep your back flat, push your hips back,") marked as interrupted |
| +1.4 s later | Coach | "Last time forty for eight. Today I'd try forty-five." (a *new* answer to the new question; it does not resume the lecture) |
| side case | Athlete | says "yeah" while the coach speaks | Coach keeps going: a short acknowledgement while the coach talks is not a barge-in. |

**Dialogue 4: the coach speaks first, after the rest timer**

| T | Who | What happens |
|---|---|---|
| 0 | App | rest timer ends (event `rest_over`) |
| +0.2 s | Policy | checks: the athlete is not mid-speech, the last coach line was more than 20 s ago, we are under the talking budget |
| +0.8 s | Coach | "Rest is up. Squats, set 3. Sixty again, go for six when you're ready." (LLM-composed from state + one fact from memory: "your left knee felt off last week" only if relevant) |
| +45 s | | nothing. If the athlete has still not logged or spoken by +90 s, one soft nudge, then silence for this rest. |
| Hinglish variant | Coach | "Rest khatam, bhai. Squat ka teesra set, sixty kilo, chhe reps maaro." |

### The recommendation in four sentences

1. Build a transport-independent **Conversation Core** (state snapshot + dialogue history + tools with guards + streaming reply). This is the real work and it is the same whichever voice engine sits on top.
2. Put it first on your **existing Pipecat cascade** (Whisper -> streaming Gemini text -> Kokoro), so the app keeps working and nothing is lost if a vendor changes.
3. Add **Gemini Live (native audio) as a second engine behind a flag**, fed by the same tools and state, because in my measurements it understood Hinglish, made the right tool calls, and answered in 1.3-2.0 s, and it is the only free option that can sound expressive. A/B them on the same recorded audio, and let the numbers plus your ears choose the default.
4. Keep today's regex brain alive as the **last-resort fallback** (it already works offline of the LLM), not as the first thing every sentence goes through.

---

## 2. Research findings

### 2.1 Systems, models and papers (what each gives us)

Free = can you use it at no cost right now. 4 GB = does it fit your GPU next to Whisper + Kokoro (about 1.7 GB used by your own estimate; `nvidia-smi` showed 3415 of 4096 MiB in use when I looked, with the voice agent running, so the real headroom is under 1 GB [M]).

| Name | What it gives us | Free? | Fits 4 GB? | Latency | Languages | Verdict | Source (access date 2026-11-25) |
|---|---|---|---|---|---|---|---|
| **Gemini Live API** (`gemini-3.8-live`, `gemini-3.1-flash-live-preview`, `gemini-2.5-flash-native-audio-preview-12-2025`) | Native audio in/out, server VAD, barge-in, tool calls (3.8: asynchronous/non-blocking), transcripts, session resumption, 128k context | Pricing page lists input and output "free of charge" for all three [V]. Hard rate numbers not published [V]. | Cloud | **Measured by me: 1.3-2.0 s from end of speech to first audio** incl. tool round trip (2.5 native-audio: 3.0-4.7 s) [M] | ~99 incl. Hindi [V]; Hinglish replies worked [M] | **ADOPT as optional engine** (flag, A/B) | [live caps], [pricing], [probe in 2.3] |
| **Gemini text API** `gemini-3.5-flash-lite` (what the backend uses) | Cheap fast LLM with function calling; streaming | Free tier listed [V]; volume limits uncertain (below) | Cloud | **TTFT about 0.7 s steady (0.67-0.95 s over 6 calls), 1.4 s on the first call** [M] | Hindi/Hinglish ok [I] | **ADOPT as the cascade LLM** with a fallback chain | pricing page; my measurement |
| Gemini free-tier limits | Daily/minute caps | - | - | - | - | **RISK**: third-party reports (Oct 2026) say Flash 5 RPM / 20 RPD, Flash-Lite 15 RPM / 500 RPD; Google's own page shows no numbers; older guides say 1,500/day [V that they conflict, ? true values] | [rl-aggregators], [rate-limits page] |
| Gemini TTS (`gemini-3.8-flash-lite-tts`, `gemini-3.8-flash-tts`) | Expressive cloud voice, style-promptable, Hinglish-capable [I] | Listed free [V] | Cloud | Not measured; probably not streaming-first [?] | multi | **MAYBE**: test in the Hinglish TTS experiment | pricing page |
| **Moshi** (Kyutai, 7.7B) | The original full-duplex speech-to-speech model; listens and speaks at once | Open weights (CC-BY) | 4-bit about 4 GB per an HF post, 8-bit about 8 GB, bf16 about 16 GB; Kyutai docs say about 24 GB for the PyTorch path [V, sources disagree] | 160 ms theoretical, 200 ms practical [V] | English (French variants) | **SKIP**: no tools, no Hindi, would not leave room for anything else, and Full-Duplex-Bench found it takes the turn during pauses and handles interruptions poorly | arXiv 2410.00037; FD-Bench |
| **PersonaPlex-7B** (NVIDIA, Moshi-based) | Persona/voice control on top of Moshi | NVIDIA Open Model License, gated | 16-24 GB typical [V, sources vary 8-40] | n/a | English | **SKIP** (hardware) | HF card, tutorials |
| Qwen2.5-Omni-3B | Small omni model, speech out | Open | Q4 about 3.6 GB per a third-party calculator (unverified) | n/a | Hindi quality [?] | **SKIP** on this GPU | HF card, VentureBeat |
| Qwen3-Omni (30B-A3B) | Strong open omni model | Open | No (tens of GB) | n/a | multi | **SKIP** (cloud-only for you) | GitHub, vLLM blog |
| MiniCPM-o 4.5 | "Real-time full-duplex" omni on edge | Open | int4 about 11 GB (third-party) | n/a | multi | **SKIP** | arXiv 2604.27393 |
| Ultravox (Fixie) | Open audio-in LLM (audio -> text), no separate ASR | MIT, large backbones (8B-70B) | No | about 400 ms TTFT on 4xH100 [V, old card] | v0.6 trained for more Hindi | **SKIP** (idea reused: "give the LLM the audio") | GitHub, HF cards |
| Sesame CSM-1B | Very expressive conversational TTS | Open | 6-8 GB recommended | n/a | English | **SKIP** (hardware); the *idea* of expressive prosody is why Live is interesting | codersera blog |
| Orpheus 3B / Chatterbox / Dia | Expressive TTS | Open | No / non-streaming | Chatterbox-Turbo about 48 s on Picovoice's test box, non-streaming [V] | English mostly | **SKIP** | Picovoice benchmark |
| **Kokoro-82M** (current) | Fast small TTS | Apache-2.0 | Yes (about 0.2-0.3 s a sentence on your GPU [V in your README]) | good | English strong; Hindi voices graded C [V]; Roman-letter Hinglish behaviour [?] | **KEEP**, verify Hinglish | HF VOICES.md |
| IndicF5 (AI4Bharat, 330M) / Indic Parler-TTS | Open Indian-language TTS | Open | Probably (330M) [I] | RTF not published [?] | 11 / 20 Indian languages | **MAYBE** for the Hinglish TTS experiment; one author's tests say plain IndicF5 skips words and needs preprocessing | HF, harrrshall/hinglish-tts |
| Sarvam Bulbul v3/v4 + Saaras ASR | Indian-language TTS/ASR, Hindi-English mixed voices | Credits on signup (₹1,000 vs ₹100: sources disagree [?]) | Cloud | Not measured | 11 Indian languages | **MAYBE** as paid-later fallback | invideo, usagepricing (secondary) |
| Deepgram Nova-3 / Flux | Streaming ASR with real-time Hindi-English code-switching, eager end-of-turn | Free credit amount [?] | Cloud | Flux about 260 ms p50 end-of-turn; eager mode saves about 100-200 ms but 50-70% more LLM calls [V vendor] | `multi` includes Hindi | **SKIP now** (money), reuse the *eager end-of-turn* idea | Deepgram docs |
| AssemblyAI streaming | Streaming ASR | $50 credit then card | Cloud | n/a | Hindi code-switch [?] | SKIP | AssemblyAI docs |
| **Whisper large-v3-turbo (int8, faster-whisper)** | Better ASR than `small`, much cheaper than large-v3 | Open | About 1.5 GB peak per a faster-whisper benchmark (English) [V] | 19.6 s vs 52.6 s for 13 min of audio vs large-v3 [V] | 99 incl. Hindi | **MAYBE: benchmark on your real phone clips** (you already have `D:/voice-proto/captures/*.wav` and `stt_bench.py`) | faster-whisper README |
| Whisper-Hindi2Hinglish (Oriserve Prime/Apex/Swift) | Whisper fine-tunes that *output Roman-letter Hinglish* | Apache-2.0 | Prime/Apex are large-v3-sized (CT2 community build about 1.5 GB); Swift is base-sized [V] | n/a | Hinglish | **MAYBE: benchmark** | HF cards, oriserve blog |
| Moonshine v2 | Very fast streaming ASR | Open | Yes | 73 ms vs Whisper-small 1,940 ms on a MacBook (a vendor blog, metric unclear) | English-first [I] | **SKIP** for Hinglish | arXiv 2602.12241 |
| Parakeet / Nemotron streaming | NVIDIA ASR | Open | 2-7 GB | batch-oriented [V] | English-first | SKIP | arXiv 2604.14493 |
| Voxtral Mini 4B Realtime (Mistral) | Open streaming ASR, Hindi in the 13 languages | Apache-2.0; API $0.006/min | 4B model, does not fit with the rest [I] | configurable 80 ms-2.4 s; about 480 ms is the practical point [V] | Hindi yes | **WATCH** (cloud or a bigger GPU later) | arXiv 2602.11298 |
| IndicConformer / IndicWhisper | Best open *Hindi* ASR | Open | 600M | n/a | Hindi good, English and Hinglish poor [V one eval] | SKIP | shunyalabs, BharatASR |
| **Smart Turn v3.x** (Pipecat/Daily) | Audio end-of-turn model (current) | BSD | CPU, 8 MB | 9-12 ms on a fast cloud CPU, about 57-60 ms on a small one [V] | 23 languages incl. Hindi; a community Hinglish fine-tune scores 93.9% vs 92.8% for the official one on Hindi [V secondary] | **KEEP**; tune the fallback timeout; no fine-tune needed | Daily blog, HF |
| LiveKit turn detector (multilingual) | *Text* end-of-turn model using the transcript, lists `hi` | Weights under LiveKit Model License (not plain open source) | CPU, about 396 MB, 50-160 ms | per above | Hindi listed [V] | **MAYBE** as a second opinion on ambiguous endings | LiveKit docs/PyPI |
| VAP, TurnGPT, "Yeah, un, oh" (backchannel VAP) | Research models that *predict* turn shifts and backchannel timing from audio | Research code | n/a | n/a | Japanese/English corpora | **SKIP as code, USE as ideas** (backchannels belong at pauses, not mid-word) | arXiv 2410.15929, MDPI review |
| **Pipecat 1.x** (you run 0.0.108) | Turn strategies, classifier-based end-of-turn, early LLM start (`predictive_delegation`, alpha), async function calls, idle handling, `LatencyBreakdown`, Gemini Live service with non-blocking tools | BSD | n/a | n/a | n/a | **ADOPT (upgrade on a branch)** | pipecat releases page |
| LiveKit "async tools" pattern | How slow tools should speak, be cancelled, and never run twice | Docs | n/a | n/a | n/a | **ADOPT the ideas** | livekit.com blog |
| Free text-LLM backups: Groq (free 30 RPM / 1,000 RPD gpt-oss, per aggregators), OpenRouter `:free` (20 RPM / 50 RPD), Cerebras (free tier replaced by a card trial Jul 2026, per aggregators) | A second quota bucket | Yes | Cloud | Fast | Hinglish untested | **MAYBE** as fallback #3 (needs an account each; limits unverified) | OpenRouter blog + aggregators |
| Hume EVI | Emotion-aware speech-to-speech | 5 EVI min/month (one aggregator) | Cloud | n/a | English-first | SKIP | costbench/toolradar |
| OpenAI gpt-realtime(-mini) | Best tool pass@1 in Full-Duplex-Bench v3 | Paid only | Cloud | n/a | multi | SKIP (money) | OpenAI pricing via eesel |
| Gemma 4 E2B on-phone (AICore preview) | Audio-capable small model on the handset | Open | Phone | 0.3 s TTFT on a flagship GPU [V one card] | 140 languages | **WATCH** (later, offline mode) | Android Developers blog |
| Full-Duplex-Bench v1 / 1.5 / v2 / v3 | Turn-taking, overlap, multi-turn, **tool use with disfluent speech** | Open | n/a | n/a | English | **ADOPT scenarios and metrics** | GitHub + arXiv 2503.04721 / 2507.23159 / 2510.07838 / 2604.04847 |
| Talking Turns (ICLR 2025) | Audio models rarely backchannel, interrupt too aggressively, weak at predicting when to speak | Paper | n/a | n/a | English | **Reference** | arXiv 2503.01174 |
| EVA-Bench (ServiceNow) | Open framework: simulated caller over a live audio WebSocket, noise/accent/connection perturbations, accuracy + experience metrics | MIT | n/a | n/a | English | **BORROW the metric design**; it needs paid keys (ElevenLabs/OpenAI/Cartesia) so do not run it as is | GitHub, arXiv 2605.13841 |
| Impulse-Eval, NVIDIA NeMo voice-agent eval | Other open simulated-caller harnesses | Open | n/a | n/a | n/a | **MAYBE** later | GitHub |

### 2.2 Ten insights that change the design

1. **Your latency problem is the brain, not the audio.** Your own README measures Whisper-small at 0.21 s for 3 s of speech and Kokoro at 0.2-0.3 s a sentence [V]. A plain Gemini 3.5-flash-lite call has a TTFT of about 0.7 s [M]. Yet the live system takes 5-9 s from last word to first audio (E-006). The difference (3-6 s in "brain") is [I] several sequential model calls (router, up to 3 tool rounds in `run_agent`, a compose/polish step) plus non-streaming responses plus a tool catalogue prompt. It is architectural. A one-call, streaming design removes it; a faster model would not.

2. **Streaming only helps if the first sentence can start early.** Streaming a 15-word reply from a 0.7 s TTFT model gains almost nothing (measured: stream and non-stream both about 0.7 s for a one-line reply [M]). The gains come from (a) one model call per turn instead of several, (b) starting that call speculatively before the turn is confirmed, (c) saying a short acknowledgement while it runs, (d) tools returning ready-to-speak facts so the model does not need a second round to read them.

3. **Native audio is not faster, it is more natural.** Gemini Live's 1.3-2.0 s end-of-speech to first audio [M] is *equal to or slightly better than* a well-built cascade (my budget in 4.6 is about 1.8 s). Its real advantages are prosody, built-in barge-in and Hinglish in one model. Its weaknesses are privacy (insight 4), less control over what is said before a tool result exists, preview-model churn, and an unknown free quota.

4. **Free-tier terms matter more than free-tier limits.** Google's current terms (effective 2026-03-23) say that on the unpaid tier, inputs and outputs are used to improve products and "human reviewers may read, annotate, and process your API input and output", and "Do not submit sensitive, confidential, or personal information to the Unpaid Services" [V]. Today only *text* of what you said goes to Gemini. A Live engine would send raw voice plus workout and health context. This is a decision for you (section 7), not a technical detail.

5. **The free text quota is the main operational risk of an LLM-first design.** If third-party reports are right (Flash-Lite about 500 requests/day, Flash about 20/day) [?], a 60-set workout where every turn is a model call burns the Flash-Lite budget in about 250 turns. Mitigations: one call per turn (not 3-4); deterministic fast paths for pure status questions; a fallback chain across models and, if needed, a second Google project; the Live path (which is billed in tokens-per-minute terms rather than requests, per forum posts [?]). Model churn is real: `gemini-2.5-flash-lite` returned HTTP 404 "no longer available to new users" for your key today [M], and the pricing page says `gemini-2.5-flash` is routed to a different model on 2026-12-01 [V]. Your fallback list in `backend/agents/base.py` still names `gemini-2.5-flash`.

6. **Even the best speech-to-speech models fail tool use on messy speech.** Full-Duplex-Bench v3 (real disfluent speech: fillers, self-corrections, false starts; tool chains) found the best system (GPT-Realtime) at Pass@1 of 0.60; Gemini Live 3.1 had the lowest latency but the lowest turn-take rate (78%); a Whisper -> GPT-4o -> TTS cascade had perfect turn-taking but 10 s latency; **self-correction handling was the most consistent failure** [V, abstract]. Lesson: "sixty, no, sixty-five" is a hard case for everyone. You need guards, confirmations for risky actions, and your own tests of exactly this.

7. **Cascades are better at interruption control; end-to-end is better at feel.** Full-Duplex-Bench v1: cascaded architectures did best on interruption management, Moshi-style models respond quickly but take the turn during pauses, Gemini Live was best at pause management (takeover 0.26-0.31) [V, from the paper's abstract/snippets]. So keep control of turn-taking in your own code where you can (Smart Turn, interruption rules), and use native audio where its naturalness matters.

8. **Interruptions are mostly a *context* problem, not an audio problem.** Stopping audio is easy; the hard part is that the model must only "remember" what the user heard. Pipecat does this natively (text is released to the context in step with playback; an interrupted turn commits the partial text) [V]. Your current pipeline throws the Pipecat context away (`frame.context.set_messages([])` in `Brain.process_frame`) and keeps a 6-turn, 240 s memory in the backend instead (`voice_control._turns`), so it cannot do this today.

9. **Hinglish fails at the ears first, then at the mouth.** E-004 shows Whisper-small garbling Hinglish. Vendor evidence says vanilla Whisper large-v3 has WER around 28-34 on code-switched sets while Hinglish fine-tunes are around 10-14 (vendor-reported, custom normaliser) [V weak]. For the mouth, Kokoro is a single English voice in `server.py` (`VOICE = af_heart`), so Roman-letter Hinglish gets English pronunciation [I]. A cheap, high-value experiment: send the raw audio clip to Gemini *together with* the Whisper transcript and let it resolve disagreements, because the model hears Hinglish far better than a 244M Whisper does [I, to test]. My Live probe shows Gemini understanding Hindi speech (transcribing "साठ किलो पांच बार हो गया" correctly) [M].

10. **Proactive speech is mostly a policy problem.** Current research (Jarvis, LLAMAPIE, MM-When2Speak) agrees that models predicting "a turn change is possible" is not the same as deciding "should I speak"; a proactive line consumes shared airtime and has a helpfulness-vs-intrusiveness trade-off, and papers are early-stage [V]. Practical stance: event triggers + a hard talking budget + LLM composing only the words.

11. **Fillers must not enter the model's memory, and thinking-aloud has a ceiling.** One open implementation found filler lines spoken through the same channel as real replies were added to the LLM history and the model then echoed them; they fix it by marking such audio "do not append to context". Over-narrating a slow tool past about 8 s reads as performative; first filler at about 2 s, a second about 6 s later, then stop [V, protoVoice docs]. LiveKit's rule for slow tools: acknowledge immediately, narrate real progress, never run a non-reversible write twice (reject duplicates) [V].

12. **Conversation quality decays; plan to prune.** An AWS talk (AI Engineer World's Fair 2026) reports model behaviour degrading after about 15-20 turns (verbose, ignoring prompt rules) and recommends pruning or resetting [V, talk summary]. A forum thread reports Gemini Live latency creeping up in long sessions, fixed by restarting the session [V, user report]. Design: rolling summary + session rotation at natural breaks.

### 2.3 My own measurements today (first-hand, small, treat as indications)

All with your `.env` key, from this PC directly to Google (no phone, no tunnel), on the synthetic Kokoro test clips in `D:/voice-proto/audio/` (`t_set1`, `t_no_skip`, `t_hi_set`, `t_mumble`, `t_hi_weight`, `t_real_skip`, `t_hi_skip`), single runs unless noted. The script is `docs/CONV_live_probe.py` (reads the key from `.env`, never prints it): `python docs/CONV_live_probe.py <model> <clips> <silence_ms>`.

**Gemini text, `gemini-3.5-flash-lite`, 15-word reply with a 60-token system prompt, OpenAI-compatible endpoint:** TTFT 1.37 s (cold), 0.67 s, 0.70 s; non-streaming 0.68, 0.95, 0.68 s. `gemini-2.5-flash`: 1.1-1.2 s streaming (and it began with filler "Alright,"). `gemini-2.5-flash-lite`: HTTP 404 for this key.

**Gemini Live, `gemini-3.8-live`, server VAD with `silenceDurationMs=400`, system prompt + 2 tools (`log_set`, `skip_exercise`) + workout state, audio streamed in real time at 16 kHz:**

| Clip (what was said) | Heard (Live's own transcript) | Tool call (time from end of speech) | First audio |
|---|---|---|---|
| "sixty kilos, five reps done" | "60 kilos, five reps done." | `log_set(reps 5, Back squat, 60 kg)` at 1.35 s | 1.98 s |
| "We are not skipping anything. We are doing every exercise." | exact | **none (correct)** | 1.32 s |
| Hindi (Devanagari clip) "60 किलो पांच बार हो गया" | exact | `log_set(5, back squat, 60)` at 1.41 s | 2.04 s, replied "Badhiya, 60 kilos ke 5 reps log ho gaye." (Roman Hinglish) |
| "Skip this exercise. My shoulder hurts." | exact | `skip_exercise(Back squat)` at 1.38 s | 2.00 s |
| Hindi "यह वाला स्किप कर, दो कंधा दुख रहा है" | close | `skip_exercise` at 1.37 s | 1.89 s (replied in Devanagari: need a script rule in the prompt) |
| noise/mumble clip | **heard as Korean** | none | 1.55 s, **replied in Korean** (needs a language lock and a "was that speech?" guard) |
| Hindi "अगले सेट में वजन बढ़ाऊं क्या?" | exact | none | 1.42 s, a vague non-answer (grounding gap, fix with state + tools) |

`gemini-3.1-flash-live-preview`: similar latency (1.4-2.2 s), tool calls correct, but answered a Hindi utterance in English. `gemini-2.5-flash-native-audio-preview-12-2025`: slower (3.0-4.7 s to first audio) and noisier transcripts ("SIXTY KLOS FIVE REP DON"). Setup (WebSocket handshake to `setupComplete`) took 0.3-1.5 s. Default VAD (no setting) delayed the end of speech by about 1.2 s, so tune `silenceDurationMs`; Google recommends 500-800 ms [V], I used 400 ms.

**Caveats:** synthetic speech, one run each, wall-clock includes my 400 ms silence setting, not through the Cloudflare tunnel or a phone, free-tier quota state unknown at that moment, 12 or so Live sessions and about 20 text calls were made. Do not treat these as p50/p95; the harness in section 6 should.

**Hardware facts I read from this PC [M]:** Ryzen 7 4800H (8 cores), **7.4 GB RAM total**, RTX 3050 Laptop 4 GB (3.4 GB in use). A local LLM for fallback does not fit in a useful way (RAM and VRAM are both nearly spent), so the fallback ladder in 4.9 is cloud models then your existing deterministic brain.

---

## 3. Gap analysis: the current code against the vision

Honest list; file and function names are what I read today (the working tree has uncommitted edits by other agents, so line numbers may drift).

| # | Vision requirement | What the code does now | Evidence | Gap |
|---|---|---|---|---|
| 1 | LLM understands the whole dialogue | The pipeline **discards** the Pipecat conversation: `frame.context.set_messages([])` after reading the last user message; the backend keeps `_turns` = last 6 (said, answered) pairs for 240 s | `voice_agent/server.py` `Brain.process_frame`; `backend/coach/voice_control.py` `_remember_turn`, `_history`, `TURN_TTL` | No true dialogue memory; interrupted text is not recorded at all |
| 2 | LLM decides actions | Order of decision in `voice_control.handle()`: care -> wake strip -> pending-confirm -> pending-food -> pending-set -> `_rest`, `_settings`, `_nav`, `_meal_queries`, `_food`, `_session_q` -> `live.handle` (regex/slot filler) -> `_diet` regex -> **LLM router last** | `voice_control.py` lines ~1033-1122, `_router` ~960 | Roughly 10 rule stages run before the model; each is a place to misread (E-001, E-002 came from here). The model is the last resort, which is backwards |
| 3 | Rules only as safety guards | Skip is now clause-aware (`set_slots.skip_intent`, E-001 fixed in code, not deployed); set naming pinned to the plan (`live._pin_to_plan`); numbers grounded (`_ground_numbers`) | `docs/ERROR_LEDGER.md` E-001..E-003, `backend/coach/live.py`, `set_slots.py` | These are good *guards*; they are currently used as *interpreters*. Reuse them in the guard layer |
| 4 | Fast, streaming reply | `run_agent` posts to `/chat/completions` without streaming (no `stream` anywhere in `backend/agents/base.py`), up to 3 tool rounds, `max_tokens` 400, `ROUTER_DEADLINE_S = 12`; `_post_fast` hedges two requests | `backend/agents/base.py` `run_agent`, `_post`, `_post_fast`; `voice_control._router` | 5-9 s measured (E-006); no sentence streaming; the voice agent waits for the whole HTTP response |
| 5 | Natural length and voice | `spoken()` cuts every reply to the first sentence or two (about 100 characters); `_system()` says "ONE short sentence of about 15 words" | `server.py` `spoken`; `voice_control._system` | Brevity is forced by truncation and prompt rather than by a coach who reads the moment. No room for a real exchange during rest |
| 6 | Backchannels, thinking cues | `ACKS = ["Got it.", "Okay.", "Mm-hm."]` played only if the sentence has a digit and a unit; `_filler` says "One sec."/"Let me think." after 1.4 s; no rule keeps these out of history (history is empty anyway) | `server.py` `Brain.answer`, `_filler` | Canned and random, not tied to state; no backchannel while the user talks; cannot tell backchannel from interruption |
| 7 | Turn-taking | Silero VAD `stop_secs` 0.25 + Smart Turn v3 with a 1.3 s fallback; fragments handled later by the backend (`speech_fix.is_fragment` -> `intent: fragment` -> agent `carry`) | `server.py` `VAD_STOP_SECS`, `TURN_STOP_SECS`, `Brain.carry` | Reasonable base, but "sixty... five" hold-logic lives in the brain after a full round trip; no speculative start |
| 8 | Barge-in | Pipecat `InterruptionFrame` cancels `brain.work` and flushes TTS. `InterruptShield` suppresses the upstream interruption to keep STT alive unless the coach is busy; `Gate` mutes the mic while the coach speaks if the phone's AEC is weak | `server.py` `InterruptShield`, `Gate` | Workable; but no "is this a backchannel" test, no truncated-history commit, and `InterruptShield` is a workaround for how the aggregator is used |
| 9 | Proactive speech | `/live/cue {event: rest_over}` returns a template line (`lines.py` `rest_over`, `rest_done`) fired by the app timer (`mobile/src/api.ts` `liveCue`) | `backend/coach/live.py` `cue()` | One event, one template. No budget, no state awareness, no PR/idle/memory triggers, not spoken by the agent on its own initiative |
| 10 | Long-term memory | `fitness_os/mem` (beliefs, hybrid retrieval, dossier) exists; the router has `recall_about_me` among tools | `fitness_os/mem/*`, `voice_control._CORE_TOOLS` | Not injected into the voice conversation at session start; reachable only if the model calls the tool |
| 11 | Hinglish ears | One Whisper-small with an initial Hinglish prompt plus about 14 regex fixes (`_STT_FIX`), a language guesser (`is_hinglish`) from a word list, and `[[unclear]]` handling | `server.py` `_PromptedWhisper`, `PROMPT`, `_STT_FIX`, `_HINGLISH_WORDS` | A fragile pile of patches around one weak ASR; E-004 still open |
| 12 | Hinglish mouth, expressive voice | One English voice, `VOICE = "af_heart"` for every reply | `server.py` `make_tts` | Hinglish pronounced with English phonemes [I]; no emotion control |
| 13 | Grounded numbers | `numbers_preserved`, `_ground_numbers`, "exact from the log" context line exist and are tested | `live.py` | Good. Needs to wrap *every* model-written sentence, including the new open conversation |
| 14 | Session robustness | Live session ends when the screen unmounts; locked-screen listening not built; foreground service missing | E-007/E-008 in the ledger | A coach "in your pocket" needs the Android foreground service |
| 15 | Framework | Pipecat 0.0.108 (pinned); current is v1.17 (22 Nov per the releases page) | `voice_agent/requirements.txt` | Missing `LatencyBreakdown`, classifier turn strategies, async function calls, idle handling, early LLM start, backchannel-aware interruption options |
| 16 | Measurement | One timing line per turn (`FirstAudio`, `Clock`), `play_client.py`, `make_test_audio.py`, `stt_bench.py` | `voice_agent/` | Good seeds; no scenario scorer, no noise, no percentiles, no judge |
| 17 | Transport | WebSocket through a Cloudflare quick tunnel (the docs say quick tunnels are test-only with no uptime guarantee and no SSE) | Cloudflare docs; `deploy/local/gateway.py` | TCP head-of-line blocking and no jitter buffer or packet-loss concealment; not measured on the phone. Pipecat's docs recommend WebRTC for client-to-server audio, WebSocket for server-to-server |

What is good and must be kept: the audio stack (Silero + Smart Turn + faster-whisper + Kokoro all measured and warm), the app's action contract (`{t:"bot", result:{actions,...}}` applied by the app), the guards from workstream A, the exact-state context (`live_context`), the structured entry points that already exist and are ideal tool surfaces (`live.log_set(exercise, weight, reps, rpe, sets)`, `live.unskip_now`, `live._do_skip`, `/live/cue`), and the memory layer.

---

## 4. Target architecture

### 4.1 Three options

| | **A. Improved cascade (Pipecat, streaming LLM)** | **B. Gemini Live (speech-to-speech)** | **C. Hybrid (recommended)** |
|---|---|---|---|
| Shape | Whisper -> one streaming Gemini text call with tools -> sentence streamer -> Kokoro | Phone audio -> Pipecat `GeminiLiveLLMService` (or direct WebSocket) -> audio back; tools executed by your backend | One Conversation Core; two interchangeable engines (A default and fallback, B optional); flag per session |
| First audio | Budget about 1.8 s content, first *sound* about 0.5 s with an ack [I, 4.6] | 1.3-2.0 s measured with `silenceDurationMs` 400 [M] | Whichever engine is on |
| Naturalness | Limited by Kokoro (no emotion) and canned acks | Best available free (native prosody, Hinglish speech, built-in barge-in) [I from listening to design, not yet listened by me] | A baseline, B for "wow" |
| Control (guards before speech) | Full: you see tool results before any sentence quotes them | Weaker: the model can say "set logged" while the tool call is pending; guard can only veto the action, not the words already spoken [V async tools; I] | Full on A; B uses synchronous (blocking) tool calls on critical writes to keep order |
| Privacy | Text only goes to Google | Raw voice + context go to Google; free tier = human review possible [V] | User chooses per mode |
| Free quota | Requests/day matter (uncertain, possibly 500/day) | Token-per-minute based; limits unpublished [?] | Two separate buckets = resilience |
| Offline-ish | Deterministic fallback exists | None | Fallback ladder works for both |
| Hinglish | Needs ASR + TTS work | Works out of the box (measured), needs script/language rules | Both paths get an experiment |
| Effort | Highest (it is the core) | Low *after* the core exists | Core once, adapter small |
| Main risks | Quota; latency tail; Hinglish voice quality; more moving parts | Preview-model churn; privacy; session limits (15 min audio-only without compression, connection about 10 min, resumption handles) [V]; reported latency creep [V user reports]; Korean-on-noise behaviour [M] | Two engines to evaluate |

**Recommendation: C, built in the order A -> harness -> B.** Reasons: (1) the Conversation Core is the part you cannot buy; (2) A is under your control and keeps working if Google changes a free tier; (3) B is cheap to add once tools/state/guards are engine-independent, and the A/B harness (6) decides with data instead of enthusiasm; (4) the two engines draw on different quotas and fail differently, which is resilience for free.

### 4.2 Diagram

```
 PHONE (React Native app)                                     PC
 ------------------------                          ----------------------------------------------------------------
 mic PCM16 16 kHz --ws--> Cloudflare --> gateway --> VOICE AGENT (Pipecat, GPU)                      BACKEND (FastAPI, Docker)
 speaker <-PCM16---------<--------------<----------   [Gate] -> [VAD Silero] -> [Smart Turn v3]      +--------------------------+
 UI events: screen, set_active,                          |                         |                  | CONVERSATION CORE (new)  |
   rest_over, tap-interrupt, manual log                  |        [STT: Whisper small|turbo|Hinglish] |  session store (history, |
 applies result.actions from the "bot" message           |                         |                  |   spoken-text truncation)|
                                                         |           [TURN CONTROLLER]  <-- speculative start at VAD stop
                                                         |                         |                  |  state snapshot builder  |
                                                         |   engine = "cascade"    |   engine="live"  |   (live_context + clock  |
                                                         |   [Turn client] --NDJSON stream-->         |    + rest timer + memory)|
                                                         |                                            |  prompt assembler        |
                                                         |                                            |  LLM stream (Gemini) <-- fallback chain
                                                         |                                            |  tool router + GUARDS    |
                                                         |      <-- events: ack | say(sentence) | action | state | done
                                                         |                                            |  grounding checker       |
                                                         |   [Sentence streamer] -> [Kokoro TTS] -> [FirstAudio/Latency log]   |  proactive policy        |
                                                         |                                            |  memory (fitness_os/mem) |
                                                         |   OR  GeminiLiveLLMService (audio in/out)--+  /convo/tool (guarded)   |
                                                         |       tools -> HTTP -> backend /convo/tool |  legacy brain (fallback) |
                                                         +--------------------------------------------+--------------------------+
```

- The **voice agent holds no API key** in engine A; the backend calls Gemini (as today). In engine B the agent needs the key; keep it in the agent's own `.env` and make tool execution go back through the backend so guards and the database stay in one place.
- The app contract stays: the agent keeps sending `{t:"user"}`, `{t:"bot", result:{reply, actions, ...}}`, `{t:"state"}`, `{t:"clear"}`.

### 4.3 What stays, what is replaced

| Stays | Replaced / demoted |
|---|---|
| Transport, gateway, app audio module, wire protocol, Silero, Smart Turn, faster-whisper, Kokoro, `FirstAudio` timing, `play_client.py` | `Brain.answer` and its regex pre-filter (`classify_turn`, `_IDLE`, `_HELLO`, `_STAGE`) become a *cheap pre-classifier* that only decides ack/ignore; the LLM decides the rest |
| `live.py` entry points as tools; `set_slots.skip_intent`, `_pin_to_plan`, `_ground_numbers`, `care.py`, `numbers_preserved` as **guards**; `_gate`/`GATED` confirmation for destructive tools | `voice_control.handle()` waterfall demoted to engine `legacy` (fallback), no longer the default path |
| `fitness_os/mem` dossier and `recall_about_me` | `run_agent` multi-round JSON loop (kept for text chat), replaced for voice by a single streaming call |
| `_system()` persona text (`P.system_prompt`) | `spoken()` truncation: replaced by a model-chosen length with a hard cap from the *situation* |

### 4.4 How the LLM calls tools safely

**Tool surface for voice (small, typed, one purpose each).** Do not expose the 45-tool catalogue (that is why the router needed `tools_for`). Voice needs about 10:

`log_set(exercise?, weight, reps, rpe?, sets?)` · `correct_last_set(weight?, reps?)` · `start_rest(seconds)` · `set_next_weight(weight)` · `skip_exercise(exercise)` · `unskip_exercise(exercise|all)` · `swap_exercise(exercise, replacement?)` (existing) · `get_status()` (read-only: current, sets left, last set, rest remaining) · `recall_about_me(topic)` (existing, read-only) · `log_note(text, kind)` (pain/feeling; routes to `care`). Everything else (profile, plan rebuilds, diet planning, calendar) stays in the existing text-chat agent and is reached by `handoff_to_text_coach(request)` which tells the user "I've put that on your screen".

**Action classes and policy:**

| Class | Examples | Rule |
|---|---|---|
| 0 read-only | `get_status`, `recall_about_me` | no confirmation; result is the *only* source of numbers in the reply |
| 1 reversible log | `log_set`, `start_rest`, `set_next_weight`, `correct_last_set` | execute at once, speak the result from the tool output, always offer/accept "undo" (undo = another tool); **plausibility guard**: weight differs from the last set by more than 40% or reps over 30 -> ask "Six hundred or sixty?" instead of saving |
| 2 reversible but surprising | `skip_exercise`, `unskip_exercise`, `swap_exercise` | execute only if the **guard** agrees the utterance contains a clear, non-negated, non-question order (reuse `set_slots.skip_intent`); otherwise the tool returns `needs_clarification` and the model asks one short question; every one is undoable by voice and button (already built) |
| 3 destructive or plan-changing | `GATED` set (rebuild plan, set goal, forget...) | existing spoken-confirm gate (`_ask_confirm`); the LLM never calls the executor twice |
| Safety | pain/medical words | deterministic `care.handle` first, before any model: stays exactly as today |
| Low ASR confidence | `[[unclear]]`, Whisper avg-logprob under the existing limit, non-Latin/Devanagari script that is neither Hindi nor English (the Korean case) | **never act**; ask "say that again?" |

**Grounding (no invented numbers).** Three layers: (1) the prompt receives the exact state block and the instruction that every number must come from the state or a tool result; (2) a post-check on each sentence before it is spoken: any number not in {state, tool results, user's utterance} is dropped from the sentence or the sentence is regenerated once (`live.numbers_preserved`/`_ground_numbers` already do half of this); (3) writes are idempotent per turn id (LiveKit's "duplicates are rejected" lesson).

**Tool latency while speaking.** Fast tools (under 0.5 s: all of the above are local DB calls) need no filler: the model gets the result inside the same stream and speaks it. For slow ones (diet planning, evidence lookups) use the LiveKit pattern: say a 3-word acknowledgement, narrate at most one progress line at about 2 s and one at about 8 s, then stop; fillers are marked `append_to_context=False` so the model never echoes them. Gemini does not reliably produce a spoken preamble *before* a function call (forum thread), so in engine A do the acknowledgement in code, not in the prompt.

### 4.5 Conversation state and memory

Five layers, assembled by the Core on every turn (all engine-independent):

1. **Static system prompt** (persona, language policy, brevity-by-situation, tool rules, safety lines), about 500-700 tokens, never changes within a session, so it is cacheable.
2. **State block**, rebuilt each turn from `live.live_context()` plus: seconds since the last set, rest timer remaining, `set_active` flag from the app, last 3 sets with exact values, skipped list, the PR flag, local time, and what the coach said last (spoken text).
3. **Dialogue window**: last 12-16 messages verbatim (about 1.5 minutes of talk), then one rolling summary paragraph for older turns. Assistant messages are stored **as spoken** (truncated at the interruption), tagged `[interrupted]`; tool calls and results are stored as structured messages, not prose.
4. **Long-term**: at session start, 3-5 relevant facts from `fitness_os/mem` (injury beliefs, preferences, plateau notes) go in a "what you know about this person" block; mid-session the model may call `recall_about_me`. At the end of the session the Core writes a 3-line summary back to memory.
5. **Language policy**: mirror the athlete's last utterance (English / Hinglish in Roman letters), never switch script; this is where the Korean/Devanagari drift I measured gets fixed (explicit "RESPOND IN ..." line as Google recommends).

Prune rule: when the dialogue window passes about 4,000 tokens or 20 turns, summarise and restart the model context (or rotate the Live session at a rest period), per insight 12.

### 4.6 Latency budget (cascade, common path: a set is logged)

Common path = a short utterance with numbers, a fast tool, a one-sentence reply. Numbers marked [V] come from your README or my probes; the rest are my estimates.

| Stage | Budget | Basis |
|---|---|---|
| Phone -> tunnel -> agent (audio already streaming; this is only the *last frame's* delay) | 0.05-0.12 s | [?] not measured; mobile data |
| VAD end-of-speech (`stop_secs`) | 0.20-0.25 s | currently 0.25 |
| Turn decision (Smart Turn inference + rules) | 0.02-0.07 s | Daily: 12-60 ms [V]. The *incomplete-turn* case adds up to the fallback timeout (1.3 s now): lower to about 0.9 s and add "numeric fragment" hold rules |
| ASR (Whisper small on GPU, about 3 s of speech) | 0.21 s | your README [V]; turbo int8 may differ |
| **Speculative LLM start** | -0.20 to -0.30 s | start the LLM request at VAD stop on the partial transcript; cancel and restart if the user resumes (Deepgram reports similar eager-turn savings of 100-200 ms for 50-70% more calls [V vendor]; we only do this speculatively for utterances that look complete) |
| LLM TTFT, prompt about 1.5-2.5k tokens + 10 tools | 0.8-1.1 s | measured 0.7 s on a tiny prompt [M]; larger prompt and tools add [I]; prefix caching of the static layers helps if the provider caches implicitly [?] |
| Tool execution (local DB) | 0.05-0.15 s | [I] |
| First sentence to TTS -> first Kokoro audio | 0.25-0.35 s | README [V] |
| Agent -> phone | 0.05-0.12 s | [?] |
| **Total to first composed word** | **about 1.6-2.0 s** | sum [I] |
| **First sound (acknowledgement)** | **about 0.5-0.8 s** | pre-rendered "Mm-hm"/"Got it" cached as PCM at startup; plays at VAD stop + ASR + classify |

Honest answer to "under about 1.5 s": the **first sound** gets under 1 s (0.5-0.8 s). The **first composed word** is about 1.6-2.0 s with this LLM. Getting the composed word under 1.5 s needs the speculative start to pay off and a smaller prompt; if it does not, the acknowledgement-then-sentence pattern is what real coaches do anyway. The Live engine measured 1.3-2.0 s *including* tool calls [M]. Targets in section 6 are p50 first sound at or under 0.8 s and p50 composed word at or under 1.8 s, p95 at or under 3 s.

### 4.7 Turn-taking and interruption design

- **End of turn**: Silero (0.2-0.25 s) then Smart Turn v3 (kept). Add gym-specific holds in the *voice agent*, before the model: if the transcript ends in a bare number, "and", "for", "at" or a weight word without the matching rep/weight, wait up to 0.9 s more ("sixty ... five"). Upgrade to Pipecat 1.x `ClassifierUserTurnStopStrategy` or the LiveKit text turn detector (has `hi`) as a *second opinion* only for ambiguous cases, after the harness shows Smart Turn misses.
- **Speculative reply**: begin the LLM call at VAD stop; if the user resumes within 0.4 s, cancel (Pipecat has `UserTurnResponseCancelFrame` for voiding an early answer [V]).
- **Barge-in**: stop audio when the user's speech lasts at least 0.35 s *and* has at least 2 recognised words or any digit; shorter sounds ("yeah", "mm", cough) do not interrupt. Pipecat has `MinWordsUserTurnStartStrategy(min_words=3)`, and the Krisp VIVA interruption-prediction strategy exists for acknowledgements [V] (licence unchecked). On interruption: flush TTS, commit **only the spoken text** to history, mark interrupted, answer the new utterance.
- **Mid-set mode**: when the app reports `set_active=true`, the coach's maximum is 6 words and it does not speak first. During rest the maximum is about 2 sentences unless asked to explain.
- **Backchannel (user -> coach)**: ignored by the LLM (no reply), kept in the log. **Backchannel (coach -> user)**: only a pre-rendered "mm-hm" at a pause of at least 0.8 s inside a *long* user utterance (over 6 s, e.g. telling a story about knee pain). Research says systems almost never backchannel and when they do timing matters (ICLR 2025 "When should the system backchannel", Talking Turns [V]); keep it rare, off by default, an A/B toggle in the harness.
- **Silence**: after the coach asks a question, wait 6 s, then at most one repeat ("Still with me? Sixty or sixty-five?"), then drop it.

### 4.8 Proactive speech design

Events from the app and the Core: `rest_over`, `set_logged` (with RPE/PR flags), `exercise_done`, `idle_after_rest(45s, 90s)`, `session_start`, `session_end`, `plateau_note` (from memory), `pain_mentioned_earlier`.

`ProactivePolicy` (plain code, no LLM): allow a line only if (a) the athlete is not speaking or mid-set, (b) at least 20 s since the user last spoke, (c) at most 1 unsolicited line per 90 s and 6 per 10 minutes, (d) not repeated in the same rest, (e) quiet mode off. If allowed, the Core asks the LLM for a line of at most 15 words given event + state + at most one memory fact, with the existing template (`lines.py`) as the fallback when the model is slow (over 1.5 s) or unavailable. These lines are stored in history as coach messages. In engine B, initiative needs a client trigger (text turn sent into the session); Gemini's "proactive audio" only lets the model *decline* to answer [V], it does not speak first.

### 4.9 Fallback ladder (cost = free analysis)

1. `gemini-3.5-flash-lite` (stream) 2. `gemini-3.1-flash-lite` / `gemini-3.5-flash` (different quota bucket per model [I]) 3. optional second Google project key (free limits are per project [V per aggregators]) 4. optional Groq free (`gpt-oss`) for English only [?] 5. **legacy deterministic brain** (`voice_control.handle`, regex, templates: already built, tested, no LLM) 6. a recorded "I can't think right now, tap to log your sets". No local LLM: 7.4 GB RAM and a full 4 GB GPU leave no useful room [M, I]. Models are config (read `GET /v1beta/models` at start and prune dead names), never constants; today's 404 on `gemini-2.5-flash-lite` and the 2026-12-01 reroute are the reason.

### 4.10 Hinglish and the voice

- **Ears (experiments, ordered by cost):** (E1) re-run `stt_bench.py` on the real phone captures with `small` vs `large-v3-turbo` int8 vs an Oriserve Hinglish fine-tune; (E2) **audio-to-LLM**: send the 16 kHz clip (about 75 audio tokens for 3 s, at 25 tokens/s [V]) to Gemini beside the Whisper text and tell it to trust the audio; (E3) Voxtral/Deepgram only if both fail and you want to spend.
- **Mouth (experiments):** (E4) listening test of the same 20 coach lines with (a) Kokoro `af_heart` on Roman Hinglish, (b) Kokoro `hf_alpha` on the Devanagari form of the same line, (c) Gemini flash-lite-tts, (d) Live's own voice. You decide by ear. If (b) wins, the Core emits `display_text` (Roman) and `tts_text` (Devanagari for the Hindi parts), which is a small, testable transform. Kokoro's own Hindi voices are graded C [V], so expect a compromise.
- **Brain:** the system prompt carries a language policy and 6-8 worked Hinglish examples of *how a gym coach talks* ("Badhiya, ek aur set maar de"), because there is no public benchmark for Hinglish voice assistants (searched; nothing found) [V], so your own scenarios are the benchmark.

### 4.11 Transport and phone audio

Keep WebSocket for now. Reasons to move to WebRTC later (SmallWebRTC/Daily/LiveKit): jitter buffer, packet-loss concealment, built-in echo cancellation, and mobile network switching; Pipecat's docs recommend it for mobile clients [V]. Costs: native WebRTC in your custom `pcm-audio` module and a TURN story for the tunnel; Quick Tunnels have no SLA and no published latency, and I found no measured numbers for tunnel vs Tailscale [V gap]. Do the cheap thing first: **measure p50/p95 RTT and jitter on the phone through the tunnel** (a 2-minute echo test in the harness), and use a **named Cloudflare tunnel or Tailscale** if the quick-tunnel tail is bad. For echo: capture with Android's `VOICE_COMMUNICATION` source and check `AcousticEchoCanceler.isAvailable()` at runtime; AEC can also suppress the user's voice on some devices (Samsung forum report) so barge-in must be verified on *your* phone [V anecdotal].

---

## 5. Phased build plan

Efforts are rough working-day estimates for one focused agent/builder, [I]. "Owner files" are what that phase touches, so parallel agents do not collide. `voice_agent/server.py` and `backend/coach/voice_control.py` are conflict magnets: **one phase at a time may edit them**.

### Phase 0: quick wins on today's system (2-4 days; makes it feel better without a rewrite)

| # | Task | Files | Acceptance |
|---|---|---|---|
| 0.1 | **Profile the 5-9 s** end to end: add per-stage timing (stop->words, LLM calls count and each duration, tool time, TTS) to the existing timing line; keep a JSONL of every turn | `voice_agent/server.py` (`Clock`, `FirstAudio`), `backend/coach/voice_control.py` (count model calls per turn) | A table showing where time goes over 30 turns; this decides Phase 1 priorities |
| 0.2 | **Kill second model rounds**: write tools return a ready `say` line from the DB result (exactly as `log_set` already does) and `run_agent` stops after a write tool with a `say` (skip the "read the result" round) | `backend/agents/base.py` `run_agent`, `coach_tools.py` | Mean model calls per set-log turn drops from N to 1; p50 stop->first audio down by at least 1 s on the replay set |
| 0.3 | **Use the cheapest working model first, fix the fallback list**: remove dead/rerouted names, read `/v1beta/models` at start | `backend/agents/base.py` (`_DEFAULT_MODEL`, `_ROLE_FALLBACKS`, `_DEFAULT_FALLBACKS`) | No 404/401 in the log over a 30-minute session; fallback order printed at start |
| 0.4 | **Acknowledgement in 0.5 s**: pre-render 6 short acks (Hindi and English) at startup, play on "digits + unit" and on any non-trivial question; mark them not-in-history | `voice_agent/server.py` `ACKS` | p50 first *sound* at or under 0.8 s on the replay set |
| 0.5 | **Send the audio clip to the LLM too** (experiment E2) for turns where Whisper confidence is low or Hinglish is detected | new `voice_agent/audio_assist.py` + a call in the backend voice route | On the 30 real captures, wrong-numbers rate falls vs transcript-only (report the numbers either way) |
| 0.6 | **ASR bake-off** E1 on real captures | `voice_agent/stt_bench.py` (extend, do not rewrite) | Table: WER and VRAM and latency for small / turbo-int8 / Hinglish fine-tune; pick one or keep small |
| 0.7 | **Hinglish TTS listening test** E4 (20 lines x 4 voices), you choose | scratch script in `voice_agent/` | Your pick recorded in section 7 |
| 0.8 | **Freeze the guards as tests**: promote every sentence from `ERROR_LEDGER.md` E-001..E-005 into a replay file (the Golu lines) | `voice_agent/make_test_audio.py`, new `voice_agent/scenarios/ledger.yaml` | Replay runs in one command and prints pass/fail |
| 0.9 | Dialogue memory stopgap: lengthen `_turns` from 6 to 16 and TTL 240 s to 600 s; store the *spoken* text | `voice_control.py` | "what weight was that?" two minutes later works |
| 0.10 | Deploy the already-written E-001..E-003 fixes (container rebuild) | `deploy/local` | Ledger items marked deployed |

### Phase 1: Conversation Core in the backend (5-8 days)

New package `backend/convo/` (new files only, so it conflicts with nobody) plus one include line in `backend/main.py`:

- `convo/session.py` dialogue store (window, rolling summary, spoken-truncation, tool messages), keyed by connection id; persisted to `data/` for debugging.
- `convo/state.py` state snapshot builder (wraps `live.live_context`, rest timer, `set_active`, last sets, memory facts).
- `convo/prompt.py` static prompt + language policy + Hinglish examples (persona from `P.system_prompt`).
- `convo/tools.py` the ~10 voice tools with JSON schemas, each wrapping an existing function; returns `{status, say, facts, actions}`.
- `convo/guards.py` the action-class policy (4.4): skip guard (reuses `set_slots.skip_intent`), plausibility, ASR confidence, duplicate-turn id, care pre-check.
- `convo/ground.py` per-sentence number check.
- `convo/llm.py` streaming Gemini client with the fallback chain; emits events.
- `backend/routes_convo.py`: `POST /convo/turn` (NDJSON stream of `ack|say|action|state|done`), `POST /convo/event` (proactive triggers), `POST /convo/interrupt` (what was actually spoken), `POST /convo/tool` (engine B).
- Feature flag `VOICE_BRAIN=legacy|converse` (env, default `legacy`).

Acceptance: against the scenario replay with *text* input (no audio): 0 wrong actions on the 60 negative cases; set-logging accuracy at least 98% on 100 positive cases; first token latency p50 at or under 1.2 s from the request; every reply number traceable (grounding checker reports 0 untraceable numbers in 200 turns); fallback to `legacy` works when the key is removed.

### Phase 2: Pipecat integration, streaming, turn control (5-7 days; owner of `voice_agent/server.py`)

- Replace `Brain` with `ConverseBrain` that opens `/convo/turn`, pushes each `say` sentence as a `TTSSpeakFrame` as it arrives, forwards `action` frames as the existing `{t:"bot"}` message, honours interruption by posting the spoken prefix to `/convo/interrupt`.
- Speculative start at VAD stop and cancel-on-resume; numeric-fragment hold; barge-in thresholds; backchannel ignore.
- Upgrade Pipecat 0.0.108 -> current 1.x **on a branch** (`LatencyBreakdown`, turn strategies); run the replay before and after; roll back if worse.
- Acceptance: p50 first sound at or under 0.8 s, p50 first composed word at or under 1.8 s, p95 at or under 3.0 s on the 100-utterance replay over loopback; barge-in stop time at most 300 ms; "yeah" during coach speech does not stop it in at least 9/10 runs.

### Phase 3: Proactive coach (3-4 days; new files plus app hooks)

`convo/proactive.py` (policy, budget, quiet modes) and app events `set_active`, `rest_over`, `idle` through the existing config channel (mobile owner adds sends in `liveSession.ts` or `streamVoice.ts`; agent owner adds forwarding). Acceptance: scripted day with 5 triggers produces at most the allowed lines, none while the user speaks, templates used when the LLM is slower than 1.5 s.

### Phase 4: Gemini Live engine (3-5 days; new `voice_agent/engine_live.py`)

Pipecat `GeminiLiveLLMService` (or the direct WebSocket from `docs/CONV_live_probe.py`) with the same tools (blocking for class 1-3 writes so words follow results), `silenceDurationMs` 500-700, context-window compression and resumption handling for the 15-minute limit, a language lock and an "is this speech?" pre-check on the transcript (the Korean case), transcripts mirrored back as `{t:"user"}`. Flag `VOICE_ENGINE=cascade|live`. Acceptance: same replay scorecard, side by side with the cascade, 30-minute soak with one forced reconnect without losing state.

### Phase 5: Depth (ongoing, parallel)

Hinglish ears/mouth decisions from Phase 0 productised; long-term memory injection and end-of-session summaries; foreground service so voice works with the screen locked (E-007/E-008); transport measurements and named tunnel/Tailscale (or WebRTC if the tail is bad); optional paid tier decision.

### Who can work in parallel (no shared files)

| Stream | Files | Can run beside |
|---|---|---|
| A. Eval harness (Phase 0.8 then section 6) | `voice_agent/eval/*`, `voice_agent/scenarios/*` (new) | everything |
| B. Conversation Core (Phase 1) | `backend/convo/*`, `backend/routes_convo.py`, 1 line in `main.py` | A, C |
| C. ASR/TTS experiments (0.5-0.7) | `voice_agent/audio_assist.py`, `stt_bench.py`, scratch scripts | A, B |
| D. Pipecat integration (Phase 2) | `voice_agent/server.py` **only this stream edits it** | needs B's endpoint contract (agree the NDJSON schema first); can stub it |
| E. Mobile (events, foreground service, UI state) | `mobile/*`, `mobile/modules/pcm-audio/*` | A-D |
| F. Live engine (Phase 4) | `voice_agent/engine_live.py` | after B's `/convo/tool` exists |

Agree the three contracts first (cost: one hour, saves days): the `/convo/turn` event schema, the tool schemas, the scenario file format.

### Migration so the app keeps working

1. `VOICE_BRAIN=legacy` stays the default until the harness shows `converse` is at least as good on the negative set and not worse on set-logging; flip per environment variable, per connection (`?brain=` query on `/ws` for testing from one phone).
2. A/B on identical audio: the harness plays the same WAV scenarios through both brains (and both engines) and prints one table.
3. Both brains emit the same app contract, so the app needs no release for Phase 1-2. Only new events (Phase 3) and the foreground service (Phase 5) need an APK.
4. If `converse` errors or exceeds 6 s on a turn, the agent calls `legacy` for that turn (never silence).
5. Roll-out order: you alone on the phone for a week with a visible "brain: converse" tag in the Live screen and the per-turn JSONL, then default.

---

## 6. Evaluation plan

### 6.1 Harness (runs on this PC, all free)

Build on what exists: `make_test_audio.py` (Kokoro lines to PCM16), `play_client.py` (real-time playback into the agent), `throwaway_backend.py` (demo data), `stt_bench.py`.

- **Scenario files** (`voice_agent/scenarios/*.yaml`): a list of user turns with expected outcomes: `expect_tool: log_set{weight:60,reps:5}`, `expect_no_tool: skip_exercise`, `expect_reply_numbers_subset`, `expect_lang: hinglish`, `expect_first_sound_ms <= 800`, plus timing of interruptions (`interrupt_at_ms: 2100`).
- **Synthetic caller**: scripted turns rendered with Kokoro (English) and the Hindi voice (Devanagari form) at varied speed, plus **noise mixing** (add gym noise or music at 0, 5, 10 dB SNR, and a low-pass "phone far away" variant), **disfluency** variants (Full-Duplex-Bench v3 style: "sixty... no, sixty-five", "um, five reps", false starts), and a **simulated LLM caller** for open-ended chat (a Gemini flash-lite instance given a persona and a goal; used only for the naturalness runs).
- **Real audio**: 30 clips you record in the gym (script of 30 phrases in section 7) plus the existing `D:/voice-proto/captures/*.wav` phone captures. Synthetic proves logic; only real clips prove ears.
- **Runner**: `python -m voice_agent.eval run scenarios/ledger.yaml --brain converse --engine cascade --noise 5dB`, output a table and a JSONL; `--compare` runs two configs on identical audio.
- **Judge**: Gemini flash-lite with a fixed rubric (below), temperature 0, run on transcripts (not audio) to stay within quota; a 20-turn human spot check each release keeps the judge honest.
- **What I would borrow**: EVA's metric split (accuracy vs experience) and perturbation list; Full-Duplex-Bench's scenario families (pause, backchannel, smooth turn-taking, interruption, v2 corrections and entity tracking, v3 disfluent tool use). Not as a dependency: EVA needs paid keys (ElevenLabs/OpenAI/Cartesia) [V].

### 6.2 Metrics and targets (my proposals [I], to be tightened after the first baseline run)

| Area | Metric | Target |
|---|---|---|
| Latency | first sound, p50 / p95 | at most 0.8 s / 1.5 s |
| Latency | first composed word, p50 / p95 | at most 1.8 s / 3.0 s |
| Latency | turns over 6 s | under 1% (and each falls back, never silent) |
| ASR | WER on the English gym-phrase set (synthetic, then real) | at most 8% / 15% |
| ASR | Hinglish *numbers extracted correctly* (weight and reps) | at least 90% real, 95% synthetic |
| Actions | set-logging accuracy (right exercise, weight, reps) over 100 positive cases | at least 98% |
| Actions | **wrong-action rate** over 60 negative cases ("not skipping", questions, chatter, stage directions, noise, foreign-script transcripts) | **0** (any single wrong skip/log fails the build) |
| Actions | correct clarification rate on 20 ambiguous cases (asks instead of acts) | at least 90% |
| Grounding | replies containing a number not in state/tool/user text over 200 turns | 0 |
| Corrections | "sixty, no, sixty-five" resolves to 65 | at least 90% |
| Interruption | audio stops after user speech starts | at most 300 ms (agent) / 450 ms (phone, measured with the loopback test) |
| Interruption | false barge-in (stopped by "yeah"/cough/noise) | at most 1 per 10 min; at least 9/10 backchannel trials keep talking |
| Interruption | context after barge-in contains only spoken text | 100% |
| Turn-taking | cut-offs mid-thought ("sixty ... five" answered after "sixty") | at most 3% |
| Naturalness | LLM-judge rubric (1-5): fits the moment, brevity matches the situation, no repetition, mirrors language, warm not gushing, honest about not knowing | mean at least 4.0, no turn under 3 |
| Naturalness | your human checklist (7.4) | 8 of 10 yes |
| Robustness | 30-min soak: no dead session, memory growth bounded, model fallback exercised once | pass |

### 6.3 Running it

All local: Kokoro for caller voices (GPU is shared with the agent, so render the audio **once** and cache it), the agent on loopback, the throwaway backend for demo data (never your real DB), Gemini only for the judge and the LLM brain (quota-aware: nightly full run, quick 20-scenario smoke before every merge). Because a free quota may be about 500 requests a day [?], the harness counts its own requests and refuses to start a full run if the budget is short.

### 6.4 Human checklist for you (10 yes/no after a 15-minute test with the phone)

1. Did it ever answer something I did not say? 2. Did it ever change my workout without me asking? 3. Did it wait when I said "sixty... five"? 4. Could I cut it off and did it drop what it was saying? 5. Did "yeah" or a cough stop it? 6. Did it speak at a good moment after rest, and not twice? 7. Did it stay short during a set? 8. Did Hinglish sound natural, both ways? 9. Did it remember something from earlier in this session and from a previous one? 10. Did it ever sound like a form-filler instead of a coach?

---

## 7. Open questions and decisions for you (with my recommendation)

1. **Privacy on the free Google tier.** Voice and workout data on the unpaid tier may be read by human reviewers and used to improve Google products; Google asks you not to submit sensitive personal information [V, terms effective 2026-03-23]. *Recommendation:* the cascade (text only to Google) is the default; Live is an opt-in mode you turn on knowingly; if you ever want Live as the daily driver, pay for the lowest tier (roughly $0.005/min audio in and $0.018/min audio out on the pricing page for `gemini-3.8-live`, so on the order of tens of cents per workout; I did not measure real billing) [V price, I estimate].
2. **Which engine becomes default?** *Recommendation:* decide after the Phase 4 head-to-head, using the section 6 scorecard plus your checklist; do not decide on enthusiasm.
3. **Can I upgrade Pipecat 0.0.108 -> 1.x?** *Recommendation:* yes, on a branch, behind the replay test, after Phase 1.
4. **Speaking style: English default that mirrors you into Hinglish, or always Hinglish?** *Recommendation:* mirror your last utterance (one persona, one rule).
5. **Should I build the Android foreground service (voice with the screen locked)?** *Recommendation:* yes, in Phase 5; a coach that dies when the phone sleeps is not "standing next to you". It needs a native build, so you or an agent with a compile environment must test it.
6. **Real recordings.** Please record 30 short phrases at the gym (script: 10 set reports like "sixty, five", "sixty for five, felt heavy", "sixty-two point five times eight"; 6 corrections ("no, sixty-five"); 6 negatives ("we're not skipping anything", "should I skip?", "next exercise?"); 4 Hinglish; 4 mumbles/noisy), with the phone in your pocket and in your hand. *Recommendation:* start with 10 this week; they are worth more than any synthetic test.
7. **A second Google account/project for a spare quota bucket?** *Recommendation:* only if the Phase 0.1 profile shows you run out of requests; do not add it speculatively.
8. **Persona.** Keep Ustaad. *Recommendation:* one named voice, one persona prompt, a "how a coach talks" example block; no emotional-voice cloning (hardware, and no free model fits).

---

## 8. Sources and what I could not verify

Access date for every URL below: **2026-11-25**. "Pub." is the best publication date I could establish (arXiv IDs encode year-month). Pages marked (summary) were read through a fetch tool that summarises with a small model, so exact quotes could be off; the facts I rely on are cross-checked against my own measurements where possible.

Official / primary:
- Gemini API pricing (free-tier list, Live and Flash-Lite model ids; current, no date shown): https://aistudio.google.com/docs/pricing (redirect of ai.google.dev/gemini-api/docs/pricing) (summary)
- Gemini rate-limits page (shows no free-tier numbers for the models I asked about; "can be viewed in AI Studio"): https://aistudio.google.com/docs/rate-limits (summary)
- Gemini Live API capabilities (models, 16 kHz in / 24 kHz out, Hindi, async function calling, VAD settings, barge-in, session limits 15 min audio-only, 128k context, affective dialog, proactive audio, transcription): https://aistudio.google.com/docs/live-api/capabilities (summary)
- Live API best practices (system-prompt order, one function call at a time works best, 20-40 ms chunks, `interrupted` flag, compression, resumption, GoAway, cost: about 25 tokens/s of audio, context re-billed each turn): https://aistudio.google.com/docs/live-api/best-practices (summary)
- Live API session management: https://ai.google.dev/gemini-api/docs/live-api/session-management
- Gemini API terms (unpaid-tier data use, human review; effective 2026-03-23): https://aistudio.google.com/docs/terms (summary)
- Pipecat interruptions (spoken-text context truncation, start/stop/mute strategies, backchannel and Krisp): https://docs.pipecat.ai/pipecat/fundamentals/interruptions (summary)
- Pipecat Gemini Live service (settings, non-blocking tools, VAD, resumption): https://docs.pipecat.ai/server/services/s2s/gemini (summary)
- Pipecat releases (v1.9-v1.17 notes: LatencyBreakdown, classifier turn strategies, early delegation, async function cancel, Gemini 3.8 Live; v1.17 dated 22 Nov of this year): https://github.com/pipecat-ai/pipecat/releases (summary, truncated)
- Pipecat transports: https://docs.pipecat.ai/client/concepts/choosing-a-transport
- Smart Turn v3 (Daily; 23 languages, 12 ms CPU; pub. 2025): https://www.daily.co/blog/announcing-smart-turn-v3-with-cpu-inference-in-just-12ms/ ; v3.1: https://www.daily.co/blog/improved-accuracy-in-smart-turn-v3-1/ ; repo: https://github.com/pipecat-ai/smart-turn
- Community Hinglish Smart Turn fine-tune: https://huggingface.co/abhishek-040010/smart-turn-hinglish
- LiveKit turn detector docs: https://docs.livekit.io/agents/logic/turns/turn-detector.md ; PyPI: https://pypi.org/project/livekit-plugins-turn-detector
- LiveKit async tools pattern: https://livekit.com/blog/async-tools-voice-agents
- LiveKit "why WebRTC beats WebSockets": https://livekit.com/blog/why-webrtc-beats-websockets-for-voice-ai-agents ; Daily: https://www.daily.co/blog/you-dont-need-a-webrtc-server-for-your-voice-agents/
- Deepgram Flux eager end-of-turn / speculative replies (vendor): https://developers.deepgram.com/docs/flux/voice-agent-eager-eot ; interrupt handling/`text_spoken`: https://developers.deepgram.com/docs/flux-tts/interrupt-handling
- Cloudflare Quick Tunnels (test-only, no SLA, 200 in-flight, no SSE): https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/
- Android AcousticEchoCanceler: https://developer.android.com/reference/android/media/audiofx/AcousticEchoCanceler ; AOSP pre-processing: https://source.android.com/docs/core/audio/implement-pre-processing
- Kokoro voices (Hindi hf_alpha/hf_beta grade C): https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md
- Voxtral Realtime (arXiv 2602.11298, Feb 2026): https://arxiv.org/html/2602.11298v1 ; Mistral announcement: https://mistral.ai/news/voxtral-transcribe-2/
- Moshi paper (arXiv 2410.00037, Oct 2024): https://arxiv.org/abs/2410.00037 ; quantised builds: https://huggingface.co/kyutai/moshiko-mlx-q8
- Kyutai Unmute (cascade with semantic VAD; 16 GB+ per a secondary source): https://kyutai.org/unmute/
- NVIDIA PersonaPlex: https://huggingface.co/nvidia/personaplex-7b-v1
- Ultravox: https://github.com/fixie-ai/ultravox ; v0.6: https://huggingface.co/fixie-ai/ultravox-v0_6-qwen-3-32b
- Qwen2.5-Omni-3B: https://huggingface.co/Qwen/Qwen2.5-Omni-3B ; Qwen3-Omni: https://github.com/QwenLM/Qwen3-Omni
- MiniCPM-o 4.5 (arXiv 2604.27393, Apr 2026): https://arxiv.org/abs/2604.27393
- Oriserve Whisper-Hindi2Hinglish: https://huggingface.co/Oriserve/Whisper-Hindi2Hinglish-Prime ; Swift: https://huggingface.co/Oriserve/Whisper-Hindi2Hinglish-Swift
- IndicF5: https://huggingface.co/ai4bharat/IndicF5 ; arXiv 2505.20693
- Google Android blog, Gemma 4 in AICore (Apr 2026): https://developer.android.com/blog/posts/announcing-gemma-4-in-the-ai-core-developer-preview

Benchmarks and papers:
- Full-Duplex-Bench v1 (arXiv 2503.04721, Mar 2025; ASRU 2025): https://arxiv.org/abs/2503.04721 ; repo with v1.5 (arXiv 2507.23159), v2 (arXiv 2510.07838, Oct 2025: corrections, entity tracking, safety, multi-turn with an automated examiner), v3 (arXiv 2604.04847, Apr 2026: tool use with disfluent speech; GPT-Realtime Pass@1 0.60, Gemini Live 3.1 lowest latency 4.25 s and turn-take 78%, cascade 10.12 s): https://github.com/DanielLin94144/Full-Duplex-Bench , https://arxiv.org/abs/2604.04847 , https://arxiv.org/abs/2510.07838
- Talking Turns (arXiv 2503.01174, ICLR 2025): https://arxiv.org/abs/2503.01174
- "Yeah, Un, Oh": continuous backchannel prediction with VAP (arXiv 2410.15929): https://arxiv.org/pdf/2410.15929 ; turn-taking review (MDPI Technologies 2025): https://www.mdpi.com/2227-7080/13/12/591
- Backchannel and dialogue-context alignment (arXiv 2604.16622, Apr 2026): https://arxiv.org/html/2604.16622v1
- Proactive speech: Jarvis (arXiv 2610.07506): https://arxiv.org/pdf/2610.07506 ; LLAMAPIE (arXiv 2505.04066): https://arxiv.org/pdf/2505.04066 ; MM-When2Speak (arXiv 2505.14654): https://arxiv.org/html/2505.14654v1
- EVA-Bench (arXiv 2605.13841, May 2026; MIT): https://arxiv.org/html/2605.13841v1 , https://github.com/ServiceNow/eva ; NVIDIA NeMo voice-agent eval: https://github.com/NVIDIA-NeMo/labs-Voice-Agent ; Impulse-Eval: https://github.com/Impulse-Eval/impulse-eval
- Health/fitness coaching with LLMs: GPTCoach (CHI 2025, arXiv 2405.06061): https://arxiv.org/pdf/2405.06061 ; Bloom (arXiv 2510.05449): https://arxiv.org/pdf/2510.05449 ; MIcha RCT (IJHCS 2025): https://dl.acm.org/doi/10.1016/j.ijhcs.2025.103514 ; empathy in PA coaching (arXiv 2606.26641): https://arxiv.org/pdf/2606.26641. I found **no** trial of a voice-based LLM coach.
- Moonshine v2 (arXiv 2602.12241): https://arxiv.org/html/2602.12241 ; streaming ASR on-device (arXiv 2604.14493): https://arxiv.org/html/2604.14493v2
- Hinglish: Hinglish-Bench (HF): https://huggingface.co/datasets/saidutta69/hinglish-bench ; COMI-LINGUA (arXiv 2503.21670): https://arxiv.org/html/2503.21670v3 ; Hinglish chatbot (arXiv 2504.19070): https://arxiv.org/pdf/2504.19070v1

Secondary / vendor / aggregator (weaker; used only as stated):
- Free-tier rate aggregators (conflicting, Oct-Nov 2026): https://www.aifreeapi.com/en/posts/gemini-api-rate-limits-per-tier , https://imisofts.com/blog/gemini-api-free-tier-limits/ , https://tinkerllm.com/blog/gemini-api-free-tier-limits-rate-quotas/ , https://www.developersdigest.tech/rate-limits/google
- Free LLM APIs: https://openrouter.ai/blog/tutorials/free-llm-apis-compared/ , https://plugsky.com/free-llm-api , https://klymentiev.com/blog/free-llm-api
- Gemini Live forum reports: latency https://discuss.ai.google.dev/t/live-api-5-6-second-response-latency/123254 , growth in long sessions https://discuss.ai.google.dev/t/gemini-live-api-native-audio-response-latency-gradually-increases-during-long-sessions/115508 , accent drift https://discuss.ai.google.dev/t/gemini-live-3-8-follows-british-english-accent-instructions-less-consistently-than-3-1-flash-live/184558 , speak-before-tool-call https://discuss.ai.google.dev/t/anybody-succeeding-in-making-gemini-say-something-before-calling-functions/169087
- Hinglish ASR comparisons (vendor): https://www.shunyalabs.ai/blog/best-hinglish-speech-to-text-models-0yw1 , https://github.com/Arhanpg/BharatASR ; Hinglish TTS comparison (one author, 30 sentences): https://github.com/harrrshall/hinglish-tts
- Voice filler design: https://protolabsai.github.io/protoVoice/explanation/natural-fillers.html
- AI Engineer World's Fair 2026, AWS talk on conversation drift, turn policy: https://www.ai.engineer/talks/hMlLw1LeIK8-designing-voice-agents-real-conversations
- Sarvam pricing (blogs): https://www.usagepricing.com/blueprint/sarvam-ai ; Hume pricing: https://costbench.com/software/voice-apis/hume-ai/free-plan/ ; OpenAI realtime pricing: https://www.eesel.ai/blog/gpt-realtime-mini-pricing

### What I could not verify (be honest about these)

- **The real Gemini free-tier request limits** for text and for Live. Google's pages give no numbers; third-party pages disagree (1,500/day vs 20/day for Flash). My probes made about 20 text calls and 12 Live sessions without a rate error, which proves nothing about a full workout day. The Phase 0.1 profile and the harness request counter will.
- **Hinglish/Hindi accuracy of Gemini Live and of any ASR on real human speech.** My clips were synthetic Kokoro speech, which is clean. Real gym audio is the test.
- **Kokoro's treatment of Roman-letter Hinglish** (no source I found says; experiment E4 decides).
- **Latency through the phone and the Cloudflare tunnel**: unmeasured. All my numbers are PC-to-Google or in-process.
- **Moshi, Qwen-Omni, MiniCPM-o VRAM**: conflicting third-party numbers; the verdict (does not fit with Whisper and Kokoro on 4 GB) is robust to the spread.
- **Voxtral Realtime, Deepgram, Sarvam, IndicF5 speed or Hindi accuracy**: vendor claims only.
- **Whether Gemini's implicit prompt caching reduces TTFT** for the static prompt layers.
- **The Krisp VIVA interruption strategy's licence and whether it runs offline.**
- **SmallWebRTC on React Native** (no mobile-specific source found); WebRTC advice comes from Pipecat/LiveKit/Daily docs written for their own stacks.
- **AEC behaviour on your phone** while the coach speaks.
- **Fetch-tool summaries**: several Google pages were summarised by a small model; one page returned odd model names for a rate-limit table. I relied on `GET /v1beta/models` (first-hand, your key) to confirm which Live and Flash models exist today: `gemini-3.8-live`, `gemini-3.8-live-extended-thinking`, `gemini-3.1-flash-live-preview`, `gemini-2.5-flash-native-audio-*`, `gemini-3.5-flash(-lite)`, `gemini-3.1-flash-lite`, and the TTS models are all present; `gemini-2.5-flash-lite` is listed there but returned a 404 on calls.
