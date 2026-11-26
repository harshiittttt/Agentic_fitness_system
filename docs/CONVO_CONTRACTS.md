# Conversation Core: contracts (v1)

Written 2026-11-26 by Stream B (Conversation Core). The voice-pipeline stream (D), the mobile stream (E) and the Live-engine stream (F) build against this file.
It is the single source of truth for: the `/convo/*` HTTP API, the NDJSON event stream, the tool schemas and the scenario file format.
If the code and this file disagree, the code is wrong or this file is stale: fix whichever is later.

Status of each section is marked **[built]** (exists and tested) or **[planned]**. Section 7 lists any deviation found while building.

## 0. Ground rules

- The Core lives in the backend (FastAPI). It is engine independent: the Pipecat cascade calls it over HTTP; a speech-to-speech engine calls `/convo/tool` for the same guarded tools.
- Selecting the brain: env `VOICE_BRAIN=legacy|converse` (default `legacy`). Per request override: query `?brain=converse|legacy`. With `legacy`, `/convo/turn` still works and returns the SAME stream shape but the text comes from the old `voice_control.handle` (one `say` event, then `done`). So the voice agent can talk to `/convo/turn` unconditionally.
- The Core never trusts the model with an action. Every tool call passes `guards.py` (section 4). A vetoed tool returns `status: "needs_clarification"` or `"refused"` with a ready-to-speak `say`.
- Numbers in anything spoken are traceable: each sentence is checked (section 5) before it is emitted. Untraceable sentences are dropped, never spoken.
- No API keys in any response, log line or exception text.
- Auth: same as every other route. If the server has `API_TOKEN` set, send `Authorization: Bearer <token>` (the voice agent already does this for `/voice/command`). The `/convo/*` paths need nothing special.

## 1. `POST /convo/turn` (the main call)

Request, JSON:

| field | type | required | meaning |
|---|---|---|---|
| `text` | string | yes | what the athlete said (final ASR transcript, Roman letters; Devanagari is accepted and normalised) |
| `turn_id` | string | yes | unique per utterance, chosen by the caller (uuid or counter). The same `turn_id` sent twice is **idempotent**: the cached event list is replayed, tools are not run again |
| `conn_id` | string | yes | one per voice connection / session. Dialogue history is kept per `conn_id` (idle 30 min => dropped) |
| `lang` | `"en"` \| `"hinglish"` \| `"hi"` \| null | no | language the athlete just spoke. The coach mirrors it (English in -> English out; Hinglish in -> Hinglish in Roman letters out). null = decide from the text |
| `screen` | string | no | app screen (`live`, `today`...), default `live` |
| `asr` | object | no | `{avg_logprob: float\|null, no_speech_prob: float\|null, unclear: bool, audio_ms: int\|null}`. `unclear=true` (the voice agent's `[[unclear]]` marker) or a very low `avg_logprob` (< -1.05) => the Core never acts, it asks "say that again?" |
| `set_active` | bool \| null | no | the app/agent knows the athlete is mid-set. true => replies are at most 6 words and tools still work |
| `rest_remaining` | int \| null | no | seconds of rest left on the app's timer, if known |
| `ambient` | bool | no | the mic is always open and the athlete did not address the coach (gym chatter). Non-commands then return `intent: "ignored"` with no `say` |

Response: `Content-Type: application/x-ndjson`. One JSON object per line, `\n` terminated, **flushed as produced** (do not buffer; disable proxy buffering). HTTP status is always 200 once the stream starts; failures are in-band `error` events. A caller that disconnects mid-stream cancels the model call.

Every line has `"t"` (type) and `"turn_id"`. Order guarantee: `ack`? then any number of `tool` / `action` / `state` / `say` interleaved in causal order, then exactly one `done` (always last, always present, even after `error`).

Consuming it from the voice agent (httpx):
```python
async with client.stream("POST", "/convo/turn", json=req, timeout=httpx.Timeout(15.0, read=10.0)) as r:
    async for line in r.aiter_lines():
        if line:
            ev = json.loads(line)          # route by ev["t"]; send each `say` to TTS the moment it arrives
```
Silence on the stream is normal for up to ~6 s (model thinking; an `ack` of kind `one_sec` arrives after 1.6 s). After 6 s with nothing spoken the Core answers through the legacy brain by itself, so the caller needs no timer of its own beyond a read timeout of about 10 s.
Cancel by closing the response: the Core stops the model call. The turn is then stored as spoken in full unless `/convo/interrupt` says otherwise.

### Events

```json
{"t":"ack","turn_id":"u12","kind":"got_it","text":"Got it."}
```
- Optional, at most one per turn, emitted at once (target < 150 ms after the request) before any model call.
- `kind` is one of `mm_hm`, `got_it`, `okay`, `one_sec`. The voice agent should play its **pre-rendered clip for `kind`** (fast) and may ignore `text`. `text` is the fallback wording if no clip exists.
- `one_sec` is emitted when nothing has been spoken after 1.6 s (slow model).
- Acks are **not** coach speech: never add them to the dialogue history, never count them as the spoken answer.

```json
{"t":"say","turn_id":"u12","i":0,"text":"Five at sixty, three fewer than last set.","final":false}
```
- One complete sentence (or phrase) per event, in speaking order, `i` = 0,1,2... The caller sends each straight to TTS as it arrives. Sentences are already grounded and cleaned for speech (no markdown, no emoji, numbers as the coach should say them: digits are fine, Kokoro reads them).
- `final: true` only on the last `say` of the turn (may be omitted until `done`; do not rely on it, rely on `done`).
- Language: Roman letters only (English or Hinglish-Roman). Never Devanagari (the English Kokoro voice cannot read it).

```json
{"t":"tool","turn_id":"u12","name":"log_set","status":"ok","facts":{"exercise":"back squat","weight":60,"reps":5,"set_no":2,"of":4,"sets_left_today":6,"pr":false}}
```
- Informational (for the harness, the app's debug view and logs). `status` is one of `ok`, `needs_clarification`, `refused`, `invalid`, `error`. The voice agent can ignore these.

```json
{"t":"action","turn_id":"u12","action":{"type":"rest","seconds":120}}
```
- An app action in the **existing** contract (`backend/coach/voice_control.valid_action`, mirrored by `parseAction` in `mobile/src/voiceBus.ts`): `rest`, `rest_add`, `rest_skip`, `end`, `navigate`, `open`, `refresh`, `setting`... The voice agent forwards it unchanged inside the `{t:"bot", result:{actions:[...]}}` message to the phone. Emitted when the tool ran (before the matching `say`).

```json
{"t":"state","turn_id":"u12","state":{ ...same shape as GET /live/state... }}
```
- The workout state after a tool changed it. Forward as `{t:"state", state}` to the phone.

```json
{"t":"done","turn_id":"u12","brain":"converse","intent":"set","said":"Five at sixty, three fewer than last set. Heavy today?",
 "expects":null,"interrupted":false,"fallback":false,
 "result":{ "reply":"...", "intent":"set", "actions":[...], "expects":null, "moment":false, "pr":false, "safety_flag":null, "state":{...}, "saved":[...], "next_set":null },
 "ms":{"ack":120,"first_token":690,"first_say":1130,"total":1710,"model_calls":1,"tool_ms":14},
 "model":"gemini-3.5-flash-lite","tools":["log_set"],"dropped_sentences":0}
```
- Always last. `result` has **exactly the legacy `/voice/command` response shape**, so the voice agent forwards `{t:"bot", result}` to the phone without changes. `result.reply` = the full spoken text.
- `said` = the full text emitted as `say` (joined by a space). `intent` is the legacy intent vocabulary where one applies (`set`, `skip`, `unskip`, `status`, `rest`, `question`, `ignored`, `noop`, `fragment`, `care`, `pain`, `fallback`, `clarify`, `start`, `end`, `chat`).
- `expects` (the legacy meaning): `"yes_no"` after the coach asked a yes/no question, `"set_details"` when it asked for missing numbers, `"answer"` for a fragment, else null. The voice agent uses it to hold the turn open / mark the next utterance a follow-up.
- `fallback: true` when the legacy brain produced this turn (the Core errored or took more than 6 s).
- `brain` is `converse` or `legacy`.

```json
{"t":"error","turn_id":"u12","code":"llm_unavailable","message":"model chain exhausted","fallback":true}
```
- Informational. A `done` still follows. `fallback:true` means the legacy brain answered instead (its text arrives as `say` events). Codes: `llm_unavailable`, `llm_timeout`, `bad_request`, `internal`.

### Sequences

A set logged by voice (`"sixty kilos five reps done"`):
```
ack(got_it)  tool(log_set ok)  action(rest 120)  state  say("Five at sixty. Same again, go for six.")  done
```
A negated skip (`"we are not skipping anything"`): no tool, no action:
```
say("Good, everything stays. Romanian deadlift after this set.")  done(intent="status")
```
An ambiguous skip (`"let's move on"`):
```
tool(skip_exercise needs_clarification)  say("Move on from squat and skip it for today? Yes or no?")  done(expects="yes_no")
```
Garbage / low confidence: `say("Sorry, say that again?")  done(intent="clarify")`.
Gym chatter / stage directions: `done(intent="ignored", said="")` (no `say`).
A fragment (`"sixty ..."`): `done(intent="fragment", expects="answer", said="")`.

## 2. `POST /convo/interrupt`

Called by the voice agent when the athlete cut the coach off (barge-in), so the dialogue history keeps **only what was actually heard**.

Request: `{"conn_id":"c1","turn_id":"u12","spoken":"Next up is Romanian deadlift. Keep your back flat,","reason":"barge_in"}`
- `spoken` = the text of the `say` sentences that were played to the speaker, truncated at the interruption point (whole sentences played + the partial one if known; if only whole sentences are known, send those). Empty string = nothing was heard.
- The Core replaces that turn's assistant message with `spoken`, marks it `interrupted: true`, drops unspoken sentences, and clears any pending yes/no question the unspoken part contained.
- Response: `{"ok":true,"turn_id":"u12","stored":"<spoken text>"}`. Unknown turn_id -> `{"ok":false,"error":"unknown_turn"}` (HTTP 200).
- If the caller never sends `/convo/interrupt`, every `say` of a turn is assumed fully spoken.

## 3. `POST /convo/event` (proactive coach and app state)

Request: `{"conn_id":"c1","event":"rest_over","data":{...},"turn_id":"e7"}`.

| event | `data` | what the Core does |
|---|---|---|
| `session_start` | `{}` | resets the dialogue for this `conn_id`, loads 3-5 memory facts. May stream a greeting (`say`) |
| `session_end` | `{}` | writes a 3-line summary to memory, drops the dialogue |
| `set_active` / `set_inactive` | `{}` | state only. During `set_active` the coach never speaks first and caps replies at 6 words |
| `rest_started` | `{"seconds":120}` | state only (the app timer started) |
| `rest_over` | `{}` | proactive: asks the policy; if allowed streams one short line ("Rest is up. Squats, set 3...") |
| `idle` | `{"seconds":90}` | proactive: one soft nudge max per rest |
| `set_logged` | `{"pr":true}` | state only (a manual log in the app); PR flag feeds the next proactive line |

Response: the **same NDJSON stream** as `/convo/turn` (so one parser handles both). When the policy says no (athlete speaking, quiet period, over the talking budget) or the event is state-only: a single line `{"t":"done","turn_id":"e7","brain":"converse","intent":"noop","said":"","proactive":true,"skipped":"budget|speaking|quiet|state_only|already_said"}`.
Policy (all in code, no model): a line is allowed only if the athlete is not mid-set/mid-speech, at least 20 s since the athlete last spoke, at most 1 unsolicited line per 90 s and 6 per 10 min, not repeated within the same rest. Lines are at most 15 words; a template from `lines.py` is used when the model is slower than 1.5 s.
Proactive lines are stored in the dialogue as coach messages.

## 4. `POST /convo/tool` (for speech-to-speech engines, e.g. Gemini Live)

Request: `{"conn_id":"c1","turn_id":"u12","name":"log_set","args":{"weight":60,"reps":5},"utterance":"sixty kilos five reps"}`.
`utterance` is the transcript of what the athlete said (the engine usually has it from input transcription); the guards use it. Without it, write tools are limited to class 1 and skip/swap return `needs_clarification`.
Response (one JSON object, not a stream): `{"status":"ok","say":"Five at sixty. Same again.","facts":{...},"actions":[...],"state":{...},"expects":null}`.
Same executor and same guards as the converse brain. `say` is ready to speak; the engine should speak it verbatim (blocking tool call: the engine waits for this response before it talks, so words follow results).

## 5. Tool schemas (what the model sees)

All tool args are JSON objects. Write tools accept an optional `reaction`: one short spoken sentence in the coach's voice, **without digits/number words the model could not know** (it may reuse numbers from the state block or from what the athlete said). The Core appends nothing numeric to it, and runs the grounding check on it. If missing or ungrounded, the Core speaks its own factual line instead.

Action classes: 0 read only, 1 reversible log (executes at once, plausibility-checked), 2 reversible but surprising (needs a clear order, else asks), 3 destructive/plan-changing (not available by voice here: handled by the text coach; `handoff` is not a tool, the model just says it is on the screen).

```json
[
 {"name":"log_set","class":1,"description":"Save a set the athlete just did. Use the numbers they SAID. exercise defaults to the current exercise.",
  "parameters":{"type":"object","properties":{
    "exercise":{"type":"string","description":"only if they named a different exercise from today's plan"},
    "weight":{"type":"number","description":"kg as said; 0 for bodyweight"},
    "reps":{"type":"integer"},
    "rpe":{"type":"number","description":"1-10 only if they said how hard it felt in numbers"},
    "sets":{"type":"integer","description":"only when they say several identical sets, e.g. '3 sets of 8'"},
    "reaction":{"type":"string"}},
   "required":["weight","reps"]}},
 {"name":"correct_last_set","class":1,"description":"The athlete corrects the set they just logged ('no, sixty-five', 'it was six reps'). Changes the most recent set; never adds one.",
  "parameters":{"type":"object","properties":{"weight":{"type":"number"},"reps":{"type":"integer"},"reaction":{"type":"string"}}}},
 {"name":"control_rest","class":1,"description":"Start, extend or skip the rest timer on the screen.",
  "parameters":{"type":"object","properties":{"action":{"type":"string","enum":["start","add","skip"]},"seconds":{"type":"integer","description":"for start/add"},"reaction":{"type":"string"}},"required":["action"]}},
 {"name":"set_next_weight","class":1,"description":"The athlete decided the weight for their next set of an exercise ('sixty-five next set'). Remembered for this workout and shown in the state; it does not log anything.",
  "parameters":{"type":"object","properties":{"weight":{"type":"number"},"exercise":{"type":"string"},"reaction":{"type":"string"}},"required":["weight"]}},
 {"name":"skip_exercise","class":2,"description":"Skip an exercise for today. ONLY when the athlete clearly orders it ('skip this', 'skip bench', 'machine's taken, skip it'). Never for 'not skipping', questions ('should I skip?', 'what's next?'), or vague 'move on' (ask first).",
  "parameters":{"type":"object","properties":{"exercise":{"type":"string","description":"default: the current one"},"confirmed":{"type":"boolean","description":"true only if the athlete just said yes to your question 'skip X?'"},"reason":{"type":"string"},"reaction":{"type":"string"}}}},
 {"name":"unskip_exercise","class":2,"description":"Put a skipped exercise back ('put it back', 'I'll do it', 'we're not skipping anything' after a wrong skip).",
  "parameters":{"type":"object","properties":{"exercise":{"type":"string"},"everything":{"type":"boolean"},"reaction":{"type":"string"}}}},
 {"name":"swap_exercise","class":2,"description":"Swap an exercise for a safe alternative for today (equipment taken, discomfort). Only on an explicit swap request.",
  "parameters":{"type":"object","properties":{"exercise":{"type":"string"},"replacement":{"type":"string"},"reaction":{"type":"string"}}}},
 {"name":"get_status","class":0,"description":"Exact workout status from the log: current exercise and set, sets left, last sets, next exercise, rest remaining. Use when the athlete asks where they are or what's next, and you need exact numbers.",
  "parameters":{"type":"object","properties":{}}},
 {"name":"recall_about_me","class":0,"description":"What the coach already knows about this athlete (injuries, preferences, patterns). Use before asking them something you may know.",
  "parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}},
 {"name":"log_note","class":1,"description":"Record how the athlete feels or hurts ('knee feels off', 'slept badly'). Pain always stops the movement: never advise treatment.",
  "parameters":{"type":"object","properties":{"text":{"type":"string"},"kind":{"type":"string","enum":["pain","feeling","preference","other"]},"reaction":{"type":"string"}},"required":["text"]}},
 {"name":"end_workout","class":2,"description":"Finish the workout ('wrap up', 'I'm done for today'). Only on a clear order.",
  "parameters":{"type":"object","properties":{"reaction":{"type":"string"}}}}
]
```

Tool result (internal and `/convo/tool` response): `{"status":"ok|needs_clarification|refused|invalid|error","say":"<ready to speak>|null","facts":{...},"actions":[...],"expects":null|"yes_no"|"set_details"}`.
- `ok` + write tool + `say` present: the turn ends after speaking `say` (**no second model round**).
- `needs_clarification`: `say` is the question; `expects` is `yes_no` or `set_details`; the dialogue remembers the pending question so a plain "yes"/"haan" on the next turn resolves it (the model sees it in history and calls the tool again with `confirmed:true`; the guard checks a matching pending question exists).
- Read tools (class 0) return `facts` and no `say`: the model gets one more (streamed) round to compose the answer from the facts.

Guards (all deterministic, applied before executing; they can veto or ask, never invent):
1. `unclear` ASR / very low `avg_logprob` / a script that is neither Latin nor Devanagari (the Korean-on-noise case): no tool runs; the Core answers "Sorry, say that again?".
2. Duplicate `turn_id` (or the same tool+args twice in one turn): the second is not executed.
3. `log_set`/`correct_last_set`: the weight and reps must be numbers the athlete actually said (digits or number words in `text`), except explicit repeats ("same again" -> the last set's values). Plausibility: reps > 30, weight < 0, weight > 500, or weight differing by more than 40% from the previous set of that exercise (this session or last time) -> `needs_clarification` ("Sixty or six hundred?"). Names that are not in today's plan (`unknown`, `stretch`...) are re-pinned to the current exercise or asked about.
4. `skip_exercise`: `set_slots.skip_intent(text)` must say `kind=="skip"`, or (`kind not in negated/question`) with a matching pending confirmation and `confirmed:true`. Anything else -> clarification or silent veto.
5. `unskip_exercise`: something must be skipped today.
6. `swap_exercise`/`end_workout`: the utterance must contain an explicit swap / wrap-up word, else ask.
7. Pain / eating-disorder / crisis language: `care.handle` answers deterministically before any model (as today). Pain words also auto-record the note whatever the model does.
8. Any model sentence containing a number that is not in {state block, tool facts, athlete's utterance, differences/sums of those} is dropped before it is spoken.

## 6. Scenario file format (replay harness)

JSON files in `backend/tests/convo_data/*.json`; one file = a list of scenarios (a scenario = one short conversation against a fresh demo workout).

```json
[
 {"id":"neg-skip-1",
  "tags":["negative","skip","en"],
  "setup":{"persona":"hype","sets_done":{"back squat":1},"skipped":[],"language":"en"},
  "turns":[
    {"say":"We are not skipping anything, we are doing every exercise.",
     "asr":{"avg_logprob":-0.3},
     "expect":{
        "no_write_tools":true,
        "tools_not":["skip_exercise"],
        "skipped":[],
        "say_not_matches":["\\bskipp?ed\\b"],
        "sets_logged_delta":0}}]},
 {"id":"pos-set-1",
  "tags":["positive","set"],
  "turns":[
    {"say":"sixty kilos five reps done",
     "expect":{"tools":["log_set"],"last_set":{"weight":60,"reps":5},"sets_logged_delta":1,"say_matches":["\\b(60|sixty)\\b"]}}]}
]
```
`expect` keys (all optional): `tools` (must all be called), `tools_not`, `no_write_tools`, `skipped` (exact list of skipped exercise names after the turn), `last_set` `{exercise?,weight,reps}`, `sets_logged_delta` (rows added in `sets` this turn), `say_matches` / `say_not_matches` (regexes over the full spoken text), `expects`, `intent`.
Global checks the runner always applies: every number in the spoken text is traceable (0 untraceable), no Devanagari in `say`, no API key in output, `done` is last.

## 7. Deviations from the plan found while building (as built, 2026-11-26)

Everything in sections 0-6 is **built** and tested unless listed here.

1. **Extra endpoints** (not in the plan): `POST /convo/reset {conn_id}` (forget a dialogue) and `GET /convo/status` (brain mode, model chain, dead models, open dialogues).
2. **`ack` kinds**: the Core only emits `got_it` (at once, when the sentence has a number plus kg/reps) and `one_sec` (nothing spoken after 1.6 s). `mm_hm` and `okay` are reserved for the voice agent's own use. The ack text is English or Hinglish by the detected language.
3. **`say.final` is always `false`**. Rely on `done` (always last) to know the turn ended.
4. **Spoken line for write tools** = the model's own one-sentence `reaction` if every number in it is traceable, else a plain factual line the code builds (e.g. `Squat: 60 for 5. Set 1 of 4.`), plus a factual tail when the exercise is done (`Squat is done. Next up: Romanian Deadlift.`) or a PR happened. No second model round. Read tools (`get_status`, `recall_about_me`) and guard vetoes that carry a reason (`refused`) do get a second streamed round (so `model_calls` can be 2-3).
5. **Guard rewrites**: the guard can swap near-identical tools: `correct_last_set` with nothing to correct but both numbers heard runs as `log_set`; a `log_set` that starts with a correction cue ("no, ...", "make that...") within 3 minutes of the last set runs as `correct_last_set`. The `tool` event shows the tool that really ran.
6. **Pain**: whatever the model does, a pain sentence is written to memory (`injury` + check-in) unless the model's own `log_note` / `skip_exercise` already did.
7. **`set_next_weight` is session memory only** (kept in the dialogue and shown in the state block). It is NOT written to the database and the app's logger pre-fill does not use it (that would need an edit in `backend/routes_insights.py` / `plan_edit.py`, outside this stream).
8. **Persona**: the five personas from `backend/coach/persona.py` are used (name, tone, hard limits); the sentences that demand Devanagari are removed for voice and replaced by one rule: mirror the athlete, Hinglish in Roman letters.
9. **Dangling sentences** ("I did like", "and then") return `intent: fragment`, `expects: answer`, no `say`. The rule is narrower than `speech_fix.is_fragment`, which also held complete sentences ("I want to move on").
10. **Proactive** (`/convo/event`) is implemented with the policy and templates, plus a model line (max 15 words, 1.5 s timeout, template fallback). The app does not send these events yet (Phase 3, mobile).
11. **Not built in Phase 1** (per plan Phase 2+): speculative LLM start at VAD stop, barge-in thresholds, backchannels, session-end summaries beyond one memory line, long-term memory write-back beyond that, the `/convo/tool` blocking contract for Gemini Live beyond the executor shown in section 4.
12. **Long-term facts** are loaded in a background thread at the first turn of a connection (`fitness_os.coach_tools.recall_about_me`), so the very first turn may not have them.
13. **Debug log**: every turn is appended as one JSON line to `<data dir>/convo/<conn_id>.jsonl` (disable with `CONVO_LOG=0`). It contains the athlete's words and the coach's words, never keys.
14. **Models**: chain = `CONVO_MODELS` if set, else the configured compose model, its fallbacks, then `gemini-3.5-flash-lite, gemini-3.1-flash-lite, gemini-3.5-flash, gemini-flash-lite-latest, gemini-flash-latest`, filtered by the live `GET /models` list; a 404 marks a name dead for 6 h; a 429 uses the shared cooldown in `backend/agents/base.py`. Reasoning effort is `minimal` (`CONVO_REASONING` overrides); `none` is rejected by 3.5-flash-lite (HTTP 400).
