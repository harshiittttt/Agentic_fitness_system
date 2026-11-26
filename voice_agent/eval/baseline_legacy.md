# Baseline of TODAY's legacy brain (2026-11-26)

Measured with this harness (`python -m voice_agent.eval run`): 259 scenarios / 284 turns in text mode against two throwaway backends, plus a small audio-mode sample through a throwaway Pipecat voice agent.
Nothing here touches your real data. The numbers are a snapshot, not a verdict: every audio line is synthetic Kokoro speech, run once.

* **HEAD** = the committed code at `7fc175d`, extracted to `D:/voice-proto/headcopy` and run on :8012. This is "before this session's fixes".
* **Working tree** = the same repo with the uncommitted fixes of agents A (coach logic) and B (voice agent), run on :8011. This is "the legacy brain as it stands today".
* The conversational brain does not exist yet, so there is no third column. When `/convo/turn` exists: `--brain legacy=... --brain converse=...` gives the side-by-side.

## Scorecard (text mode, all 259 scenarios)

| Metric | Target | HEAD 7fc175d (committed, before the fixes) | working tree (agents A+B fixes, not committed) |
|---|---|---|---|
| Scenarios passing | all | 183/259 | 230/259 |
| Turns passing | all | 199/284 | 255/284 |
| Wrong actions on negative cases (count) | 0 | 28 FAIL | 1 FAIL |
| Wrong actions, all turns (count) | 0 | 53 FAIL | 6 FAIL |
| Set-logging accuracy (positive set turns) | >= 98% | 78.7 FAIL | 89.0 FAIL |
| Self-corrections resolved ('sixty, no, sixty-five') | >= 90% | 0.0 FAIL | 50.0 FAIL |
| Asks instead of acting on ambiguous cases | >= 90% | 50.0 FAIL | 50.0 FAIL |
| Replies claiming 'logged' when nothing was written (count) | 0 | 0 PASS | 7 FAIL |
| Replies with an untraceable number (count) | 0 | 0 PASS | 1 FAIL |
| Replies in Devanagari to a Roman-letter caller (count) | 0 | 0 PASS | 0 PASS |
| First composed word p50 (ms) | <= 1800 | 583 PASS | 954 PASS |
| First composed word p95 (ms) | <= 3000 | 9675 FAIL | 2508 PASS |
| Turns over 6 s (%) | < 1% | 31.7 FAIL | 0.0 PASS |
| Hinglish not mirrored (informational) | - | 51 | 47 |
| Crashed turns (errors) | 0 | 0 | 0 |

**Caveat about the HEAD column:** the throwaway backend built from HEAD got a different demo plan (a push day: bench press, barbell row, overhead press, curls) than the working tree (Lower A: squat first). The scenarios expect `squat`, so HEAD's exercise-specific numbers (set accuracy, `e002`, `skip_real`, `unskip`) are NOT comparable. What IS valid on HEAD: any skip at all on a negative turn is wrong whatever the exercise, and the latency. The harness now prints a WARNING when the plan differs from what the scenarios assume.

### What the working tree still gets wrong

| Failure | Cases | Example |
|---|---|---|
| **Wrong action: skips on a vague or unrelated sentence** | 3 | "Maybe skip this." and "Skip... or maybe not." skipped squat; **"I will skip dessert tonight." skipped squat** |
| **Wrong action: a self-correction logged the FIRST weight** | 3 | "sixty kilos. No wait, sixty five. five reps." logged 60x5 (and "sixty five kilos. No wait, sixty." logged 65x5 on `bench press`, an exercise that is not in the plan) |
| **The coach says "logged" but nothing was written** | 7 | "sixty two point five kilos, twelve reps, done." -> "Twelve clean reps at sixty-two point five logged, Golu." and the sets table has no new row. This is the E-003 class (a reply claiming an action that did not happen). |
| Spoken decimal weights are not understood | 11 | "sixty seven point five kilos six reps" -> no set; the coach asks "what weight?" or claims it logged (digits like "62.5" work) |
| Self-corrections | 6 of 12 pass | the 3 wrong actions above plus 3 where the corrected value was never logged (two of them reply "logged": "a hundred kilos. No wait, a hundred and five. three reps." -> no row) |
| Ambiguous sentences are not clarified | 10 of 20 pass | "I'm not sure about squats today" -> proposes a swap; "Let's see what's next" -> status line; "I'm done here" -> ends the session |
| An untraceable number in a reply | 1 | "Next set mein kitna weight lagaun?" -> "try karo 60 kg se start" (60 is in no state or history) |
| Hinglish answered in English | 47 turns (informational, not counted as failures) | every Hinglish caller gets an English reply |

What the fixes changed (HEAD -> working tree): wrong actions on negative cases 28 -> 1, negatives passing 58/86 -> 84/86, "we are not skipping anything" (English 1/12 -> 12/12, Hinglish 0/8 -> 8/8), the whole ledger 15/32 -> 32/32 (HEAD's ledger result is partly the plan mismatch, see the caveat), turns over 6 s 31.7% -> 0%, p95 reply time 9.7 s -> 2.5 s.

### Latency (text mode = request to full reply; the legacy brain does not stream, so this stands in for "first composed word")

| | HEAD | working tree | target |
|---|---|---|---|
| p50 | 583 ms | 954 ms | <= 1800 |
| p95 | 9675 ms | 2508 ms | <= 3000 |

The p50 is higher on the working tree than on HEAD (954 vs 583 ms); I did not investigate why. The runs happened at different times of day against the shared Gemini model and overlapped with the audio runs on the same PC, so read the p95 and the share over 6 s, not the p50.

## Audio mode (working tree, synthetic caller through the throwaway voice agent on :8766)

| Run | Turns passing | Wrong actions | Set accuracy | First sound p50 / p95 (ms) | Reply message p50 / p95 (ms) | WER | Barge-in stop (ms) |
|---|---|---|---|---|---|---|---|
| ledger, clean | 33/34 | 0 | 90% | 2355 / 3530 | 2434 / 3701 | 20.1% | 340 |
| 10 set reports, clean | 10/10 | 0 | 100% | 1712 / 2976 | 2530 / 3300 | 0.0% | n/a |
| ledger e001-e003, gym noise 5 dB | 22/22 | 0 | 100% | 2024 / 2860 | 1946 / 3526 | 3.7% | n/a |
| 6 set reports, phone far away | 6/6 | 0 | 100% | 1268 / 1772 | 2134 / 2710 | 0.0% | n/a |

* **First sound is 1.3-2.4 s at p50 and 2.9-3.5 s at p95** against the plan's targets of 0.8 s / 1.5 s. The reply message arrives at about the same time as the first sound, so no quick acknowledgement comes ahead of the real reply in these turns.
* **WER on set reports is 0-4%**: the agent hears "60 kilos 5 reps" correctly, also at 5 dB gym noise and through the muffled "phone far away" variant (small samples). The 20% on the ledger run comes from the deliberately garbled Hinglish lines ("Kike mech kya?" heard as "Kaik mech kaya") and short words ("No wait" heard as "No weight", "Haan" as "Haun").
* **Barge-in: playback was cleared 340 ms after the caller started talking** (one measurement; plan target 300 ms at the agent, 450 ms on the phone).
* **Turn-taking failure found by audio mode:** "Sixty kilos ... [1.2 s pause] ... five reps, done." was split into two turns. The agent's transcript has both halves, but the brain answered the second half alone ("Five solid reps on that squat, Golu. What weight was on the bar?") and logged nothing. Text mode cannot see this; it is the "cut-offs mid-thought" metric (target <= 3%). "We are not skipping anything ... [pause] ... We are doing every exercise" reached the brain whole and passed.

## Naturalness (LLM judge, 12 sampled replies from the working-tree run)

Sample: 12 turns (12 scored, 12 new judge requests, rest from cache). Target: mean >= 4.0 and no turn under 3.

| fits_moment | brevity | no_repetition | mirrors_language | warm_not_gushing | honest | overall |
|---|---|---|---|---|---|---|
| 4.92 | 3.83 | 5.0 | 4.67 | 4.58 | 5.0 | **4.67** |

Turns with a score under 3: 1
- said 'chalis kilo, chhe reps, ho gaya.' -> "That set at 40 for 6 was really steady, Golu. Let's hit 40 for 5 next, and take two minutes to rest while I watch the cl" (Too long for mid-workout; wrong language used.)

_Judge = Gemini through the project's fallback chain, temperature 0, transcripts only. It is a smoke detector: spot-check 20 turns by ear each release._

Judge limits: it scores one reply at a time, so "no repetition" is trivially high (it cannot see earlier turns), and the sample is mostly templated set-report scenarios. Read 4.67 as "nothing is badly broken in single replies", not as "it sounds natural".

## What is NOT verified

* **Real voices and phones.** All audio is synthetic Kokoro speech sent from this PC over loopback. Real accents, real mumbling, echo from the phone's own speaker, the Cloudflare tunnel and mobile data are in none of these numbers.
* **The audio runs are small samples** (34 + 10 + 22 + 6 turns, one run each). Latency varies minute to minute with the Gemini model; treat the audio p95 as indicative only. The full 259-scenario set was run in text mode only.
* **The text-mode "first word" is the complete reply time**, not a streamed first word.
* **HEAD's exercise-specific numbers are not comparable** (different demo plan, see the caveat above).
* **Interruption by a backchannel ("yeah", a cough)**, the 30-minute soak and memory growth have no scenario yet.
* The judge is one model reading text. After a real phone test, answer the 10 questions by hand (`python -m voice_agent.eval checklist`).
* The free-tier request budget is an estimate (0.5 request per turn for the legacy brain, a guess; the judge's requests are counted exactly).

Raw data: `voice_agent/eval/runs/baseline_head`, `baseline_tree`, `audio_*` (`report.md`, `turns.jsonl`, `summary.json`; the folder is git-ignored). Re-render any run with `python -m voice_agent.eval report <name>`.
