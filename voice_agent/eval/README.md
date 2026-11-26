# Voice eval harness

One command replays scripted conversations against the coach and prints a pass/fail scorecard with latency numbers.
Built from section 6 of `docs/CONVERSATIONAL_AGENT_PLAN.md` and phase 0.8 (the error ledger promoted to replayable tests).
It only ever talks to a **throwaway** backend (demo data, temp database). It refuses ports 8001/8100 (real backend) and 8765 (real voice agent).

## The one-liner

```
python voice_agent/throwaway_backend.py                                   # terminal 1: throwaway backend on :8011 (demo data, token testtoken)
python -m voice_agent.eval run                                            # terminal 2: ALL scenarios, text mode, today's legacy brain
```
Everything runs on the normal project python (needs `pyyaml`, `httpx`, `websockets`, `numpy`; all already installed). Run from the project root.
About 10 minutes for the full set (about 340 turns); `--tags ledger` or `--limit 20` is the 1-minute smoke test. The exit code is 1 if ANY wrong action happened (plan 6.2: one wrong skip/log fails the build).

| You want | Command |
|---|---|
| Quick smoke (the ledger only) | `python -m voice_agent.eval run ledger.yaml` |
| Only the negative cases | `python -m voice_agent.eval run --tags negative` |
| Compare today's brain with the new one | `python -m voice_agent.eval run --brain legacy=http://127.0.0.1:8011 --brain converse=http://127.0.0.1:8013` (the converse brain is skipped with a message until `/convo/turn` exists) |
| Naturalness judge on 12 sampled replies | add `--judge 12` (uses ~12 free-tier requests, cached) |
| With audio: ears + timing + barge-in | see "Audio mode" below |
| List scenarios | `python -m voice_agent.eval list` |
| Rewrite the generated scenario files | `python -m voice_agent.eval generate` |
| The 10-question human checklist | `python -m voice_agent.eval checklist` (file: `human_checklist.md`) |
| Tests of the harness itself | `python -m pytest voice_agent/eval/test_eval.py -q` |

Results go to `voice_agent/eval/runs/<timestamp>/` (`report.md`, `summary.json`, `turns.jsonl` with every turn: what was said, the reply, DB effects, failed checks). `runs/` and `.cache/` are git-ignored.

## What is in `voice_agent/scenarios/`

| File | Content |
|---|---|
| `ledger.yaml` | every sentence of `docs/ERROR_LEDGER.md` E-001..E-008 plus the real phone test of 2026-11-25 (multi-turn sessions, un-skip, pauses, barge-in) |
| `conversations.yaml` | what a *conversational* coach should do (remembers the last weight, mirrors Hinglish, stays short, asks for the missing number). Expected to fail on the legacy brain. |
| `generated_positive.yaml` | 140 set reports: 84 English phrasings ("sixty, five", "sixty for five, felt heavy", "sixty-two point five times eight"), 30 Hinglish number words ("saath kilo paanch reps"), 10 with an exercise name, 12 self-corrections ("sixty... no, sixty-five"), 4 Devanagari |
| `generated_negative.yaml` | 71 cases that must change NOTHING: "we are not skipping anything" (English and Hinglish), questions ("what is the next exercise"), chatter, stage directions, mumbles/foreign script, "I skipped breakfast", numbers that are not sets |
| `generated_ambiguous.yaml` | 20 cases where the coach must ASK ("next exercise please", "I did sixty") |
| `generated_skip.yaml` | 8 clear skip orders that must skip the right exercise |

The generated files come from `generate.py` (fixed seed, do not edit by hand). The hand-written ones are plain YAML:

```yaml
- name: e001-real-skip-then-put-back
  tags: [ledger, skip_real]
  lang: en                       # en | hinglish | hi : the language the caller speaks (the reply is checked against it)
  turns:
    - {say: "Skip this exercise, my shoulder hurts.", expect: {skipped: [squat]}}
    - {say: "No wait, put it back, I will do it.",    expect: {unskipped: [squat]}}
    - {say: "Sixty kilos, five reps, done.",          expect: {sets: [{exercise: squat, weight: 60, reps: 5}]}}
    - say_parts: ["Sixty kilos", "five reps"]         # audio: said as two parts with a pause (gap_s) between them
      gap_s: 1.2
    - {say: "Wait, stop.", interrupt_at_ms: 1200}     # audio: starts talking over the coach 1.2 s after it began
```
`expect` keys (full list at the top of `scorers.py`): `sets`, `skipped`, `unskipped`, `asks`, `intent_in`, `reply_max_words`, `reply_contains_any`, `reply_not_contains_any`, `lang`, `grounded`, `first_sound_ms_max`, `first_word_ms_max`, `barge_stop_ms_max`.
**A turn with no `sets`/`skipped` key must change nothing**: that is how "wrong action" is defined. A set with the wrong weight, reps or exercise is also a wrong action.
Assumption: the throwaway backend's demo plan (Lower A: squat, romanian deadlift, leg press, leg curl, calf raise; squat is current). If the demo plan changes, update the exercise names.

## What is scored

| Scorer | How |
|---|---|
| Wrong actions | database diff per turn: unexpected set / skip, or a set with wrong values or exercise. **Target 0.** |
| Set-logging accuracy | turns that expect a set and got exactly that set (right exercise, weight, reps) |
| Self-correction | the `correction` scenarios ("sixty... no, sixty-five" must log 65) |
| Clarification | the `ambiguous` scenarios: no action AND the reply asks a question |
| Grounding | every number in the reply must be traceable to what was said, the session state, logged sets, action values, or plain arithmetic around them (done+1, weight +/- 2.5/5, seconds -> minutes). A bare "one" is ignored. |
| Language | Devanagari reply to a Roman-letter caller fails; an English caller getting 3+ Hinglish words fails; "Hinglish not mirrored" is informational |
| Latency | text mode: request -> full reply (a stand-in for "first composed word", the legacy brain does not stream). Audio mode: last sample sent -> first coach sound, and -> the brain's reply message. p50/p95, share over 6 s. |
| WER | what the agent heard vs what was said (audio mode) |
| Barge-in | time from the caller starting to talk over the coach to the agent's `clear` (stop playback) message |
| Naturalness | `--judge N`: Gemini scores 6 dimensions 1-5 on transcripts (fits the moment, brevity, no repetition, mirrors language, warm not gushing, honest). Smoke detector only: spot-check by ear. |
| Request budget | the harness counts its own model requests in `.cache/budget.json` (default 400/day, `EVAL_DAILY_REQUEST_BUDGET`) and refuses to start a run that would exceed it (`--force` overrides). The free quota is not published; 500/day is a guess. |

## Audio mode (ears, timing, barge-in)

Needs a THROWAWAY voice agent next to the throwaway backend (the real one on :8765 is refused):

```
python voice_agent/throwaway_backend.py
set BACKEND_URL=http://127.0.0.1:8011 & set VOICE_PORT=8766 & set VOICE_CAPTURE=0 & D:\voice-proto\venv\Scripts\python voice_agent\server.py
python -m voice_agent.eval run ledger.yaml --mode audio                      # clean
python -m voice_agent.eval run ledger.yaml --mode audio --noise 5            # gym noise at 5 dB SNR (also 0, 10)
python -m voice_agent.eval run ledger.yaml --mode audio --lowpass            # phone far away / in a pocket
```
Caller lines are rendered once with Kokoro (in the voice venv, a subprocess) and cached in `D:/voice-proto/audio/eval/`; later runs replay the cache. They are SYNTHETIC speech: they prove the logic and timing, not how your real voice and accent are heard (record the 30 real phrases from section 7 of the plan for that).

## Adding the new brain later

`brains.ConverseBrain` already speaks the planned contract (`POST /convo/turn`, NDJSON events `ack|say|action|state|done`). When Phase 1 exists, run both on identical scenarios and read the two columns side by side:
`python -m voice_agent.eval run --brain legacy=http://127.0.0.1:8011 --brain converse=http://127.0.0.1:8011`. If the real contract differs, only `brains.py` changes.

## Not covered (be honest)

* Real voices: everything audio is synthetic Kokoro speech. No accent, mumble or gym-noise recording from a real phone is included yet.
* Acoustic echo, phone speaker -> phone mic, Cloudflare tunnel latency and mobile data are not measured (the agent is reached over loopback).
* The naturalness judge reads text only, ONE reply at a time, and is one model: it cannot hear prosody, timing or interruptions, and cannot see repetition across turns.
* A false 'logged' claim (the reply says a set was logged, the database has no new row) IS scored (`no_false_log_claim`): the E-003 class of failure.
* The HEAD baseline in `baseline_legacy.md` came from `git archive HEAD` extracted to `D:/voice-proto/headcopy` and run with `TEST_PORT=8012 TEST_DATA_DIR=D:/voice-proto/testdata_head python voice_agent/throwaway_backend.py` (copy `deploy/shared.env` in for the model key and delete the copy afterwards), then `--brain legacy=http://127.0.0.1:8012 --db D:/voice-proto/testdata_head/fitness.db`.
* Interruption "false barge-in by yeah/cough" (plan 6.2) has no scenario yet: it needs recorded backchannel audio.
* 30-minute soak and memory-growth checks are not in the harness.
