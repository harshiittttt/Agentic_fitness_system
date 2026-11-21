# Fitness Agent OS (CLI-first control plane)

Implements the control plane from `docs/Fitness Agent OS.md`. Agents are **Claude Code subagents** (`.claude/agents/`), the loop is the `/coach-event` slash command, and everything deterministic is Python here. No Anthropic API key is needed.

```
event -> watchdogs -> agents propose (parallel) -> reconcile -> safety-critic -> policy gate -> apply / veto window / ask / escalate -> ledger
```

## Run it
Restart Claude Code in the project root once so it picks up `.claude/agents/*` and `.claude/commands/*`, then:

```
/coach-event cron.daily
/coach-event checkin.logged {"sleep_h":5,"soreness":4,"energy":2,"bodyweight":81.5,"note":"slept badly"}
/coach-event session.logged {"session":"2026-11-18","sets":[{"exercise":"squat","weight":100,"reps":5,"rpe":9}]}
```

Direct CLI (what the agents and the orchestrator call):

| Command | Does |
| --- | --- |
| `python -m fitness_os status` | plan, pending items, frozen domains, watchdog flags |
| `... context <agent>` | the context pack an agent receives |
| `... tool trends|readiness|training_signal|nutrition_signal|tdee|e1rm|alternatives` | read-only deterministic analysis |
| `... event <type> [--json ...]` | record event data, run watchdogs, say which agents wake |
| `... propose <json|file|-> [--dry-run]` | reconcile, policy-gate and (without `--dry-run`) apply proposals |
| `... ledger`, `why <id>` | audit trail and the "why did you change this?" answer |
| `... approve <id>`, `reject <id>`, `revert <id>`, `tick` | resolve pending items, undo an applied one, close veto windows |
| `... kill on|off`, `dial hands_on|balanced|autopilot`, `unfreeze <domain>` | athlete controls |
| `... demo seed|reset` | synthetic history for trying things out |

Set `FITNESS_DB=<path>` to use a scratch database and `FITNESS_NOW=<ISO time>` to simulate the clock.

## What is enforced in code (not prompts)
Agent scope (Nutrition cannot move a workout), calorie floor (1500 m / 1200 f), weekly load increase cap 10%, volume within 50%-125% of baseline, no volume increase under pain/injury, tiers (auto / notify-first with a 3 h veto window / approve / escalate), kill switch, frozen domains after escalation, autonomy dial, red-flag and disordered-eating watchdogs that run outside the agents.

## Tests
`python -m pytest fitness_os -q` (26 golden scenarios, offline, ~5 s).

## Memory, learning and scheduling (in-process, Gemini; all deterministic except wording)
| Module | Does |
| --- | --- |
| `memory.py` | episodic / semantic / procedural memory with salience, time-decay, pinning (injuries never fade), dedupe-by-reinforcement and ranked `recall()` |
| `curator.py` | nightly Curator: day summary -> episodic memory (LLM may only reword, never change a number), dedupe, decay, contradiction flags ("you said vegetarian but logged chicken") |
| `reflection.py` | closes the loop: 7 days after a volume change / 14 after a calorie change, scores readiness, strength or weight trend, stores the verdict on the decision and writes a lesson into procedural memory. Thin data waits, then is recorded as `insufficient_data` |
| `scheduler.py` | daily 05:30, Sunday 18:00 weekly review, 02:00 curator; due = "the most recent scheduled moment has not run yet", so a laptop that was off catches up, nothing double-runs, failures back off 20 min |

| `experiments.py` | plateau experiments + registry: detects a flat main lift, rules out confounders first (chronic short sleep, too few sessions), runs ONE change (5% more volume, or a 10% cut if tired) for 14 days through the normal policy gate, blocks other training changes meanwhile (safety always passes), then keeps it only if e1RM rose >= 1% and otherwise reverts. Verdict becomes a lesson; failed ideas are not retried for 60 days |

Agents receive `context_pack.memories` (top 6, relevant and fresh). CLI: `python -m fitness_os scheduler status|tick`, `memory list|curate`, `reflect`. HTTP: `/agents/memory`, `/agents/memory/curate`, `/agents/memory/{id}/resolve`, `/agents/scheduler`, `/agents/scheduler/tick`, `/agents/experiments`; CLI also `experiments list|detect|start|evaluate`.

**Not wired yet:** nothing calls `scheduler.run_due()` automatically. It needs a background tick (one startup hook in `backend/main.py`, or a cron/Task Scheduler call to `POST /agents/scheduler/tick`).

## Not built yet (from the roadmap)
embeddings for recall (ranking is keyword + salience today), Adherence / Sleep / Injury-Rehab / Periodization / Logistics / Form / Intake agents, wearables, comms (quiet hours), earned-trust tier tuning, replay harness for the LLM agents.
