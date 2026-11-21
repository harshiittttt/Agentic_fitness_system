# Voice agent (autonomous, multi-step)

"Hey coach, log two eggs, open my progress and add a work trip on the 28th" is one utterance, three actions.
Server side only: the phone already understands the action types in `mobile/src/voiceBus.ts`.

## Where it sits
`POST /voice/command` -> `voice_control.handle`:
care / pain first -> pending dialogues (yes/no, amount questions, agent questions) -> deterministic matchers (food, navigation, settings, workout) ->
**voice agent loop** (needs an LLM key; if it fails before doing anything, the old single-shot router runs) -> offline hint without a key.
Ambient chatter never reaches the loop unless the coach was addressed.

## Pieces (`backend/coach/agent_tools/`, `backend/coach/voice_agent.py`)
| File | Job |
|---|---|
| `catalog.py` | one tool spec per FastAPI operation, generated from `app.openapi()` (about 123 ops; no hand-written tools) |
| `risk.py` | tiers and rules, enforced in code |
| `search.py` | `find_tools(query, k)`: BM25 over name + summary + description + path + tags, synonym expansion, per-tool `ALIASES`; curated tools win ties; BLOCKED never returned |
| `executor.py` | `call_tool(name, args)`: schema validation, tier, in-process dispatch (httpx `ASGITransport` on `backend.main.app`, never a network call), 10 s timeout, size cap |
| `voice_agent.py` | the loop and the meta-tools `find_tools`, `call_tool`, `ui`, `ask_user` (+ 14 curated tools such as `log_meal`, `swap_exercise`, `add_agenda_event`) |

## Risk tiers
| Tier | Meaning | Behaviour |
|---|---|---|
| READ | pure queries (all GETs except blocked ones, plus POST meal/preview, memory/search, research/ask, research/screen) | runs |
| WRITE_SAFE | reversible, user-visible: log meal/set/check-in, agenda event, swap, memory notes, agent runs | runs, reported in `did` |
| CONFIRM | any DELETE, profile/onboard/goal/days/injury, undo/approve/revert decisions, forget memory, end live session, anything with no rule | NOT executed: stored in voice_control's pending-confirm slot, spoken question, `expects: "confirm"`; a spoken yes within 120 s runs the exact stored call directly (the model is not consulted); no/cancel drops it |
| BLOCKED | `/health`, `/voice/*`, `/tts*`, exports, wipes (`/reset-all`, `/agents/data/delete`, `/demo/*`, wearables data), uploads (wearables import/ingest, form analyze), push, `/agents/kill/*` and `/agents/dial` (read-only: `/agents/status` shows them), chat endpoints | refused; never listed by `find_tools` |

Default for an operation without a rule: CONFIRM (a plain unknown GET is READ). The test `test_voice_agent.py` asserts every operation is classified.

### Add or change a rule
Edit `RULES` in `risk.py`: `("POST", "/new/path"): WRITE_SAFE`. A new endpoint without a rule is CONFIRM automatically. To mark a route as an agent run
(refused while the kill switch is ON) add it to `AGENT_RUNS`. A better spoken confirm question goes in `SAY`. Extra search phrases go in `ALIASES` in `search.py`.

## Budgets (in code)
10 tool calls, 25 s wall clock, stop after 3 consecutive errors, stop on an identical repeated call (same tool + args), stop when the model answers.
Stopping without an answer gives a deterministic reply built from what was confirmed done.

## Safety
- Tool output is wrapped in `<tool_data>` blocks, instruction-like text in it is replaced by `[filtered]`, and the prompt says data is never instructions.
- Even a fooled model cannot run anything destructive: CONFIRM and BLOCKED are decided by code.
- The API token (`API_TOKEN`) is read inside the executor only; model-visible text, trace and logs are scrubbed of secret-looking values. Arguments can only fill declared schema fields (no host/URL/header).
- Kill switch ON: agent-run operations (`/agents/event`, `/agents/roundtable`, `/agents/scheduler/tick`, `/agents/memory/curate`, `/review`) are refused and the model is told to say so; ordinary actions still work.
- Trace: one `voice_agent` run (kind agent) with a `tool` span per call (tool name, tier, outcome, http code, duration; never arguments); visible in the Trace screen.

## Response
Usual contract plus `did`: short strings of what was confirmed done. A pending question ends the turn with `expects: "confirm"` (CONFIRM tier) or `"answer"` (`ask_user`, the answer comes back as a followup with the original request replayed).

## Limits
- Only one pending confirm at a time: if a sentence needs two risky things, the first is asked, the rest must be asked again.
- Needs an LLM key; latency is about one model round (1-3 s) per step, plus 5-100 ms per in-process call, so a 3-step request is roughly 4-10 s.
- A wrong model claim cannot be fully excluded; `did` lists only tool-confirmed actions so the app can show the truth.
- Search is keyword based; a rare phrasing may need an `ALIASES` entry.
