# Error ledger

Known problems, newest first. Add an entry when something goes wrong; mark it fixed (with the commit) when it is.
Status: OPEN / FIXED / WORKAROUND.

---

## E-001 · Exercises skipped without the user asking (live session) · FIXED IN CODE (2026-11-25, workstream A, backend; not deployed: the running container still has the old code until rebuilt)
- **Fix:** `SKIP_RE` is gone. `set_slots.skip_intent()` reads the sentence clause by clause: a skip needs a skip verb that is NOT negated (not/no/never/don't/nahi/mat/na/नहीं/मत, in the same clause), NOT a question ("should I skip bench?", "what is the next exercise?") and clearly an order ("skip this", "let's skip bench", "skip kar do", "yeh wala skip kardo", a bare "skip"). "Skipping/skip anything|everything" is never an order. Unclear phrases ("next exercise please", "let's move on", "machine's taken", "I can't do this") now ASK ("Do you want to skip X? yes or no", `intent: skip_ask`, `expects: yes_no`); only a clear "yes" skips, a "no" or any other talk drops the question. "What is the next exercise?" / "agla exercise kaun sa hai" is answered ("You're on X now, and Y comes right after it."), not skipped. "Skip this, my shoulder hurts" still skips AND writes the pain down. A second skip of the same exercise writes no second record.
- **Un-skip (new):** voice ("put it back", "bring it back", "undo skip", "I will do it", "wapas laga do", "put <exercise> back") and `POST /live/unskip {exercise?, everything?}` (`routes_session.py`; result `{intent:'unskip', reply, restored:[names], state}`). It deletes today's skip row(s) and retracts the matching "skipped X on <day>" belief in the memory layer (`status=retracted`, not 'denied', so a real skip later the same day is still recorded; an inferred "dislikes X" that rested on fewer than 3 skip days is withdrawn too). Saying "we are not skipping anything" / "I'm not skipping X" / "don't skip it" AFTER a wrong skip also puts the exercise(s) back. A vague "put it back" only undoes a skip made in the last 10 minutes.
- **Tests:** `backend/tests/test_live_skip.py` (all the sentences above, the Hindi forms, un-skip, the endpoint, memory retraction).
- (Original report below.)
- **Found:** 2026-11-25
- **What happened:** During a live session, 5 exercises of today's workout were marked skipped: goblet squat, deadlift, leg extension, leg curl, hanging leg raise. The user did not mean to skip them. Rows were written to the `memory` table (`kind='skip'`, text `2026-11-25|<exercise>`) between 16:31 and 16:37 UTC, by the skip path in `backend/coach/live.py` (around line 542).
- **Why it hurt:** The app has no way to reverse an exercise skip. `restore_session` only clears a whole-session override, and the coach Undo button only covers plan changes. The Today screen showed the exercises as skipped.
- **Cause (CONFIRMED from the 2026-11-25 test log):** `SKIP_RE` in `backend/coach/live.py:24` matches the bare words `skip|skipping|move on|next exercise` with no check for negation. "We are not skipping anything", "we are not skipping goblet squat", "Skipping anything." and "Haan, next set krte hain" each triggered a real skip, one exercise per sentence (deadlift, leg extension, leg curl, hanging leg raise). The coach then said "It looks like you've skipped everything on your list today".
- **Workaround used:** Deleted the five `skip` rows for 2026-11-25 straight from the container database (`/data/fitness.db`, table `memory`). The 4 logged sets were untouched. The skip also sent an `exercise.skipped` event to the memory layer, which was NOT reverted.
- **To fix later:**
  1. Find what triggered the skips: check `deploy/local/logs/voice.*.log` and the saved phone audio for 16:31-16:37 UTC.
  2. Make skip need confirmation, or at least not fire on a low-confidence match.
  3. Add an "un-skip" action (app button and voice command) that removes the day's `skip` row.
  4. When un-skipping, also reverse the `exercise.skipped` memory event.
  5. Add a test: a skipped exercise can be restored and comes back as the current one.

---

## E-002 · Spoken sets saved under the wrong exercise name, so the deadlift was never logged · FIXED IN CODE (2026-11-25, workstream A; not deployed)
- **Fix (`live._pin_to_plan`):** a spoken set is saved under a name the coach knows (a plan exercise, or a real exercise from the exercise list). A name it cannot place ('unknown', 'stretch', 'cable exercise', 'exercise', anything made up) becomes the plan exercise named in the sentence, else the exercise you are on now. With none (all sets done): the coach ASKS which exercise (`ask_which`) and saves nothing. A parser result that repeats the same set (same exercise, weight, reps) is saved once unless the sentence asked for several ("2 sets of 12", "twice"). Warm-up sets keep their own name. Tests: `test_live_skip.py` (junk names, name that fits a plan exercise, nothing left to do, repeated parse result, two sets asked for).
- **Decision / NOT done:** a real exercise that is NOT in today's plan ("lat pulldown" on a leg day) is still saved as said (accessory work is legitimate and existing tests rely on it); only made-up names are re-pinned. Identical sets sent in separate requests are NOT collapsed: the logger cannot tell a double-sent sentence from two real identical sets, and the existing tests and simulation log identical sets milliseconds apart. If the app double-sends, dedupe there (or add a request id).
- **Found:** 2026-11-25 (voice test). User said they did deadlifts; nothing was logged for `deadlift`.
- **Evidence (table `sets`, session 2026-11-25):** id 411 `stretch` 60x8; id 413 `unknown` 60x5; ids 415 and 416 `cable exercise` 30x12 (same timestamp, a duplicate). The 30x12 sets were meant for leg extension. Two more sets (ids 412, 414) were deleted from the app by the user during the session.
- **Effect:** The coach said "You're on deadlift, set 2 of 2, with 9 sets still to go" while 0 deadlift sets existed, and later "Looks like you're done with deadlifts".
- **Likely cause (NOT confirmed):** the set logger does not tie a spoken set to the exercise the coach is currently on. It guesses a name from the sentence, or falls back to `unknown`.
- **To fix later:** log a set against the CURRENT exercise by default; reject or ask when the name is not in today's plan; stop duplicate inserts (same timestamp and values); add tests.

## E-003 · Coach reply does not match what the user said · FIXED IN CODE (2026-11-25, workstream A; not deployed)
- **Fix:** (1) with one set in a sentence, reps and weight said WITH a unit ("five reps", "60 kilos") overrule the model's guess (`live._ground_numbers`), so "five reps said, 8 saved" cannot happen; the reaction line is built from the saved values. (2) The state the reply and the model see (`live_context`) now always carries "sets logged today (exact, from the log): N" and, when 0, "none yet, so never say an exercise is done". (3) Because sets are now saved under the right exercise (E-002), "set 2 of 2 / N sets left / done with deadlifts" come from the real counts. Tests: `test_the_numbers_said_win_over_the_models_guess`, `test_reply_matches_the_log`, `test_the_model_is_told_exactly_what_is_logged`.
- **NOT verified:** that the live model (Gemini) no longer says "you're done with X" in open chat when it is given this context; it was only checked that the context says so.
- **Found:** 2026-11-25. User said "five reps" and the reply said "those 8 reps at 60". The "next set, kitna weight" question got a generic answer. Coach also said "Looks like you're done with deadlifts" with none logged.
- **To fix later:** build the reply from the values that were actually logged, not from the model's guess.

## E-004 · Hindi/Hinglish speech recognition garbles input · OPEN
- **Found:** 2026-11-25. Heard "Kike mech kya?", "Heavy top.", "बस तो ना लेस चाही.", "अगले सेट में वेद बराव क्या?" (meant "weight badhau kya?"). Hinglish came out as Devanagari mixed with garbage.
- **To fix later:** Whisper language/prompt setting for Hinglish, or a post-fix list of common gym words.

## E-005 · Test setup note · INFO (backend side mitigated 2026-11-25: a line that is ONLY stage directions, e.g. "Boxing, wipes hands, adjust straps.", "*wipes hands*", and pure filler ("Hmm", "Copy", "Thanks") now returns `intent: ignored` with an empty reply; "Hello." gets one short "where you are" line instead of a model paragraph; a command inside such a line is still heard. Tests in `test_live_skip.py`.)
- The role-play assistant speaks long lines with stage directions ("Boxing, wipes hands, adjust straps"), and several of those lines were heard by the coach as user speech. Tell the test assistant to speak ONLY what Golu says, one short line, no narration.

---

## E-001 / E-006 · Reproduced with synthetic speech (2026-11-25, throwaway backend, nothing real touched) · OPEN
Test: `voice_agent/make_test_audio.py` makes Golu's lines with Kokoro, `python voice_agent/play_client.py golu ws://127.0.0.1:8766/ws?token=testtoken` plays them into a second voice agent whose brain is the throwaway backend (:8011, demo data).
- Speech recognition was exact for all English lines, so the skip bug is NOT a mishearing problem. It is the skip rule.
- "We are not skipping anything. We are doing every exercise." -> coach: "Okay, we'll leave squat for today." (SKIPPED)
- "I am not skipping the goblet squat." -> "Skip romanian deadlift, and we continue" (SKIPPED a different exercise)
- "What is the next exercise?" -> "Okay, we'll leave leg press for today." (SKIPPED: `next exercise` is in `SKIP_RE`, so a plain question skips)
- "Okay, next set." and "How many sets are left?" were handled correctly.
- **E-006 latency:** last word to first coach audio was 5.2-8.7 s in this run (brain 3.2-6.0 s). The README claims 1.0-2.9 s. Possibly the Gemini model is slow or rate limited right now; not investigated.
- Fix still to do: negation check ("not", "don't", "nahi", "mat") before `SKIP_RE`; make `next exercise` a question, not a skip; add these three lines as tests.

---

## E-001 confirmed on the real phone (2026-11-25 17:03 UTC) + E-007 · OPEN
PC speakers played the Golu lines, the real app (build 0.4.2) heard them through the phone mic:
- "You're not skipping anything. We are doing every exercise." -> skipped goblet squat.
- "I'm not skipping the goblet squat." -> skipped leg extension. Same bug as the synthetic test, now proven end to end.
- Cleaned up afterwards: deleted today's skip rows (goblet squat, leg extension, deadlift). No sets were logged.
- **E-007 (new): the live session ends itself.** `session_stop` + `wake_error aborted` appeared 4 times in 7 minutes with nobody pressing stop (17:03:14, 17:04:07, 17:04:24, 16:58:47). One stop fired the same millisecond a line was heard ("Okay, next set."). Many played lines were never heard or answered (plan, both set lines). Could be the earlier "do not re-ask mic permission / ignore inactive app state" area (commit 6176e37), or the app stopping on a coach reply.
- Also: at 16:58 the coach said "You skipped your deadlifts" (a skip left over from the earlier bug) and answered "Samadhi bhiya, phir kabhi continue karhainge" (Hinglish garble) by ending the conversation with "take care".

**E-007 update:** the user says the session stops when they switch apps or the phone sleeps. So the stops are the app leaving the foreground, not a random crash. Status: WORKAROUND for tests = keep the app open, screen on (disable screen timeout while testing). Open question: should a workout keep listening with the screen locked? (Needs a foreground service + wake lock in the native module.)

---

## E-008 · Voice inside workout mode does not work · OPEN
- **Reported:** 2026-11-25 by the user ("voice inside workout mode not working"). No details yet: not known which screen, what was said, or what happened (no audio, no reply, no logging).
- **To find out:** reproduce on the phone, then read the backend DIAG lines (`docker logs ... | findstr DIAG`) and the voice agent log for that moment. Ask: does the mic start (`s_mic`), is speech heard (`s_user`), does the coach answer (`s_bot`)?
- **First clue (17:12:55 UTC log):** `live_unmount` fired, then `session_stop` + `wake_error aborted`, then a new `s_open` / `s_mic` right away. So the session is torn down when the Live screen unmounts, and a new one starts. Whatever re-mounts or navigates the screen mid-workout kills the live voice. Then at 17:13 speech was heard and a set was logged, so voice can work between those restarts. Not confirmed which action triggers the unmount.

---

## E-007 / E-008 · Live voice inside the workout screen — workstream C (mobile) result · FIXED IN CODE, NEEDS A NEW APK BUILD
- **Root cause (from the DIAG log: `live_unmount` -> `session_stop` -> the user taps the tab-bar mic -> a NEW session):** the workout screen is a full-screen Modal and it was being closed by things the user never meant, and closing it stops the voice (`stopTalk()` in its unmount):
  1. Android Back (button or the edge-swipe back gesture, easy to hit while switching apps) -> `Modal.onRequestClose` closed it at once.
  2. A voice `navigate` / `open <other screen>` action (a misheard sentence) switched the tab UNDER the Modal -> Today unmounted -> Live unmounted.
  After that the user restarted voice from the tab bar, which talks to the coach as if on the Today tab, not the workout: "voice inside workout mode not working".
- **E-007 (switching apps / screen sleep):** going to the background ended the session SILENTLY (no reason, no way back). Not a crash: the phone takes the microphone from an app that is not in front.
- **Fixes (mobile/):** Back now asks "Leave your workout?" (Keep going / Leave) when voice is on or sets are logged (`src/liveBack.ts`, `Live.tsx`, `Today.tsx`); navigation actions are dropped while the workout is open (`App.tsx`, `liveSession.blockedWhileLive`); the screen stays on during the workout (`expo-keep-awake`, `src/keepAwake.ts`); going to the background pauses the voice WITH a visible reason, and coming back within 3 min resumes by itself, later shows a one-tap "Resume voice" (`WakeCoach.tsx`); a dropped connection is retried twice before telling the user; every ended session shows its reason in the workout screen (`endReason` in `voiceTalk.ts`); "Connecting voice…" state; `session_stop` diag now says WHY (`tap`, `screen_closed`, `background:<state>`, `quiet`, `dropped`); un-skip UI ("Put back" per skipped exercise + "Undo skip" button) calling `POST /live/unskip {exercise?}`; the app sends `lang: 'en'|'hi'` in the voice config message.
- **Needs a new APK:** all of it (no OTA updates in this app) plus `expo-keep-awake` is a new native dependency.
- **NOT done: voice with the screen LOCKED.** Needs an Android foreground service of type `microphone` in `modules/pcm-audio` (Kotlin `Service` + notification channel + `FOREGROUND_SERVICE` and `FOREGROUND_SERVICE_MICROPHONE` permissions + `startForeground` before the mic starts, start/stop from `PcmAudioModule`). Could not be compiled or tried here (no Android build allowed), so it was not written blind. Until then: keep-awake keeps the screen on, and a lock/app switch pauses the voice with a message and resumes on return.
- **Tests:** `mobile/scripts/test_livesession.js` (pure rules), and jest: Back confirm, put-back, ended-reason + Resume voice, background -> auto resume, long absence -> manual resume, dropped connection retried.

---

## Voice agent pass 2026-11-26 (workstream B) · results
- **E-004 Hinglish/Hindi speech recognition · MOSTLY FIXED (synthetic speech only).** Whisper small + a better Hinglish prompt + language limited to English/Hindi (a wrong
  language is re-run as English) + known gym mishearings repaired + low-confidence guard ("Sorry, say that again?" instead of acting on garbage). Kokoro-Hindi test sentences:
  all came back readable in Roman letters ("Aaj ka plan kya hai?", "Kitne set baki hain?"). Also fixed in `backend/coach/speech_fix.py`: "saath kilo" = 60 kg (it was ignored,
  so "saath kilo paanch bar ho gaya" asked for the weight again) and "hogaya" without a space. NOT verified on real human Hinglish / Haryanvi speech; the confidence limits are
  tuned on clean synthetic speech: watch `[voice N] unclear speech` in the first real session.
- **E-006 Latency · FIXED for most turns, model-bound for the rest.** Last word -> first coach sound, same scenarios before/after: median 3.78 s -> 2.29 s, worst 17.3 s -> 4.85 s;
  sets ~1.0 s (instant "Got it."), session questions ~1.2 s (answered without a model), model-composed turns ~2-2.7 s (the model call itself is 0.8-3.6 s and not under our control).
  Details and how to re-measure: `voice_agent/README.md`.
- **E-009 Whole sentences silently lost before the coach heard them · FIXED.** pipecat drops every Whisper segment with `no_speech_prob >= 0.4`; real speech followed by silence scores
  0.7-0.8, so "We are not skipping anything. <pause> We are doing every exercise." reached the coach as only the second half. Very likely a cause of the "lines never heard" in the first
  real-phone test (E-007 note). Regression test: `play_client.py pause`.
- **E-010 Non-commands got speeches · FIXED.** "Copy.", "Right.", "Okay." (when the coach asked nothing), "Boxing, wipes hands, adjust straps." are ignored; "Hello." gets "I'm here. Go ahead."
- **Not mine, found on the way:** (a) the skip detector on the live path (E-001) is handled by the other workstream; in my run "We are not skipping anything" no longer skipped.
  (b) `test_auth.py::test_preflight_and_cors_on_401` failed once in a full-suite run (RuntimeError) and passes alone and with its neighbours: probably parallel pytest runs by other
  workstreams sharing the machine; re-run the full suite once the other workstreams are done.

---

## Conversation Core, Phase 1 (2026-11-26, Stream B) · BUILT, NOT DEPLOYED, default stays `VOICE_BRAIN=legacy`
Code: `backend/convo/*`, `backend/routes_convo.py` (one include in `main.py`), tests `backend/tests/test_convo_core.py` (62), contracts `docs/CONVO_CONTRACTS.md`, replay `python -m backend.convo.replay <cases.json>` with cases in `backend/tests/convo_data/` (65 negatives, 108 positives, 22 action scenarios, 12 chat). Live results: `docs/CONVO_replay_*.json`.
Things learned (for the other streams and for later):
- **Legacy bug, not fixed (not my files):** `speech_fix.is_fragment("I want to move on")` is True, so the legacy brain holds a complete sentence and answers nothing (dead air). The Core uses its own narrower `_dangling()`.
- **Gemini free tier, this key, today:** `gemini-2.5-flash-lite` is 404 (never tried now: the chain reads `GET /models`). `reasoning_effort: "none"` is rejected by `gemini-3.5-flash-lite` (HTTP 400): `minimal` works and is fastest (about 0.55 s plain). Gemini 3 sends a `thought_signature` with each tool call that must go back with it (kept as `extra_content`). Short replies arrive as ONE stream chunk, so sentence streaming only helps long replies.
- **The model tried things the guards stopped** (all in the live runs): swapped an exercise on "machine's busy", set a "next weight" on "sixty kilos", wrote a note for "I did some squats", started a rest timer on "I'm not skipping, let's go", called `correct_last_set` with nothing to correct. Each is now a guard or prompt rule with a unit test.
- Grounding: allowing sums/differences of any two known numbers let "two more sets" through; now only inside sentences that compare, and minute/second conversion only in sentences that name a unit.
