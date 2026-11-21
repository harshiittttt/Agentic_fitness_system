Reference architecture · v1

# Fitness Agent OS

A highly autonomous multi-agent coaching system. Specialist agents think, a control plane decides, shared memory remembers, and MCP servers act. Every autonomous action is typed, bounded, logged and reversible.

[Layers](#layers)[Agents](#agents)[Shared memory](#memory)[MCP tools](#mcp) [Control loop](#loop)[Workflows](#workflows)[Simulator](#simulator)[Autonomy & safety](#autonomy) [Call mode](#callmode)[Runtime](#runtime)[Roadmap](#roadmap)

**Blackboard, not chatroom**Agents propose changes to shared state. They do not message each other to decide things.

**Events over prompts**Sleep data, a missed session or a pain report starts work. The user does not have to ask.

**LLMs judge, code computes**Load math, macros and limits live in deterministic tools.

**Autonomy is graded**Low-risk actions run alone. High-risk ones need a person.

01 · System map

## Seven layers, one direction of authority

Signals flow up from sources to the experience layer. Authority flows down: nothing in a lower layer changes state without passing the control plane.

L7**Experience**How the athlete meets the system

ChatRoundtable call (text / voice)Push nudgesWeekly reportPlan calendar"Why did you change this?" explainer

L6**Control plane**Decides, arbitrates, restrains

Orchestrator (Head Coach)ModeratorPolicy engine (autonomy tiers)Safety CriticWatchdogs (overtraining, disordered eating, injury)Kill switch

L5**Agent plane**Domain specialists, each with a narrow tool scope

TrainingNutritionProgress AnalystRecoverySleep & StressAdherenceInjury & RehabPeriodizationLogisticsForm (vision)IntakeResearch

L4**Shared memory**The blackboard every agent reads and proposes to

Athlete state (Postgres)Time series (TimescaleDB)Semantic and episodic memory (pgvector)Decision ledgerExperiment registryMemory Curator

L3**Tool plane (MCP)**Typed, scoped, rate-limited

state-mcppolicy-mcpwearables-mcpnutrition-mcptraining-mcpcalendar-mcpcomms-mcpvision-mcpresearch-mcplogistics-mcp

L2**Event plane**What wakes the agents

Event bus (NATS / Redis Streams)Durable scheduler (Temporal)WebhooksCron rhythmsAnomaly detectors

L1**Sources**Raw signals from the world

Wearables (Oura, Whoop, Garmin, Apple Health)Food logs and barcode scansWorkout logsBody metricsCalendarChat and voiceForm videos

↑ signals: L1 → L2 → L5 → L6 → L7↓ authority: L6 → L4 / L3 (only via policy-mcp)

02 · Agent roster

## Twelve agents plus a control plane

Start with your four core agents, then add the control agents (Orchestrator, Critic) and Adherence first. Those three change the system's behavior the most.

| Agent | Tier | Owns | Reads | Proposes | Default autonomy |
| --- | --- | --- | --- | --- | --- |
| **Orchestrator** | control | Goal hierarchy, routing, conflict resolution, weekly plan | Everything | Final merged plan changes | auto within policy |
| **Safety Critic** | control | Veto on unsafe or off-goal changes | Proposals, constraints, history | Approve / revise / block | Veto power |
| **Training** | core | Sessions, exercise selection, progression | Program, logs, readiness, injuries | Session and block edits | auto swaps · approve new blocks |
| **Nutrition** | core | Calories, macros, meal timing, supplements | Food logs, weight trend, training load | Target changes, meal plans | auto ≤5% · notify ≤10% |
| **Recovery** | core | Daily readiness score, deload triggers | HRV, RHR, sleep, soreness, load | Readiness verdict, deload flags | auto |
| **Progress Analyst** | core | Trends, plateaus, forecasts, reports | All time series, plans, adherence | Hypotheses, experiment proposals | Read-mostly |
| **Sleep & Stress** | ext | Sleep debt, bedtime plan, stress load | Sleep stages, calendar load, mood | Wind-down and schedule shifts | auto |
| **Adherence** | ext | Motivation, nudges, habit design, tone | Compliance, message sentiment, silence | Nudge timing, plan simplification | auto inside quiet hours |
| **Injury & Rehab** | ext | Pain triage, movement substitutions, return ramps | Pain reports, injury history | Restrictions, rehab blocks | approve · escalate |
| **Periodization** | ext | Macro and meso cycles, peaking, deloads | Goals, events, history | Block structure | approve |
| **Logistics** | ext | Grocery, meal prep, travel mode, gym access | Calendar, meal plan, location | Lists, bookings, schedules | auto · spending approve |
| **Form (vision)** | ext | Video technique analysis, cue generation | Uploaded clips, exercise library | Cues, load caps | auto |
| **Intake & Research** | ext | Onboarding, re-profiling, evidence lookup | Profile, guidelines corpus | Profile patches, citations | notify |

### Each agent is the same shape

A system prompt with a persona and boundaries, a scoped set of MCP tools, a typed output schema (a `Proposal`), and a reflection step that writes lessons back to memory. Adding agent number thirteen means adding a config file, not rewriting the system.

### Model tiering controls cost

Orchestrator, Critic and Periodization use the strongest model. Daily specialists use a mid-tier model. Extraction, logging and the Memory Curator use the cheapest. Deterministic work (macros, load progression, 1RM) uses no model at all.

03 · Shared memory

## One blackboard, five stores

Agents never hold the athlete's truth in their prompts. They query it, and they change it only by submitting patches that the policy layer validates and versions.

STORE 1 · POSTGRES

### Athlete state

- Profile, goals, constraints, injuries, preferences
- Plan tree: macro → meso → week → session
- Optimistic-locked versions, so two agents cannot silently overwrite each other

STORE 2 · TIMESCALEDB

### Time series

- HRV, RHR, sleep, steps, weight, loads, RPE, macros
- Every point carries `source` and `confidence`
- Rollups and anomaly detectors live here

STORE 3 · PGVECTOR

### Semantic and episodic memory

- Episodic: what happened, summarized weekly
- Semantic: durable facts ("knees hurt on deep squats")
- Procedural: learned rules ("responds to high frequency")

STORE 4 · APPEND-ONLY

### Decision ledger

- Who decided what, from which inputs, why, at which tier
- Outcome is attached later; any entry can be reverted
- Powers the "why did you change this?" answer

STORE 5 · POSTGRES

### Experiment registry

- Hypothesis, change, success metric, window, verdict
- Stops two experiments confounding each other
- Verdicts feed procedural memory

BACKGROUND JOB

### Memory Curator

- Summarizes, dedupes, decays stale facts
- Resolves contradictions ("vegetarian" vs. chicken logged)
- Scores salience so recall stays small and relevant

schema.sql (core tables)

```
-- Source of truth, versioned
CREATE TABLE athlete_state (
  athlete_id uuid PRIMARY KEY,
  profile jsonb, goals jsonb, constraints jsonb, injuries jsonb, preferences jsonb,
  version int NOT NULL, updated_at timestamptz
);
CREATE TABLE plan_node (
  id uuid PRIMARY KEY, athlete_id uuid, parent_id uuid,
  horizon text CHECK (horizon IN ('macro','meso','week','session')),
  payload jsonb, status text, owner_agent text, version int
);
-- Everything measured
CREATE TABLE metric (ts timestamptz, athlete_id uuid, name text, value double precision,
  source text, confidence real);  -- SELECT create_hypertable('metric','ts');
-- Remembered
CREATE TABLE memory (id uuid, athlete_id uuid, kind text CHECK (kind IN ('episodic','semantic','procedural')),
  body text, embedding vector(1024), salience real, expires_at timestamptz, source_decision uuid);
-- Every autonomous action, auditable and reversible
CREATE TABLE decision (
  id uuid PRIMARY KEY, ts timestamptz, athlete_id uuid, agent text, trigger_event text,
  inputs jsonb, rationale text, action jsonb,
  tier text CHECK (tier IN ('auto','notify_first','approve','escalate')),
  status text, outcome jsonb, reverted_by uuid
);
CREATE TABLE experiment (id uuid, athlete_id uuid, hypothesis text, change jsonb,
  metric text, starts date, ends date, verdict text, result jsonb);
```

### Write rule

An agent calls `propose_patch(path, value, rationale)`. The policy engine checks constraints, assigns a tier, applies or queues it, bumps the version, and appends a ledger row in one transaction. No agent holds a database write credential.

### Recall rule

Before each run, the orchestrator builds a context pack: current plan slice, last 14 days of relevant metrics, top-k memories by salience and similarity, active experiments, active restrictions. Agents get the pack, not the whole database.

04 · Tool plane

## MCP servers and who may call what

Each capability is its own MCP server with typed tools. Agents receive scoped capability tokens, so Nutrition cannot move a workout and Training cannot change calories.

| Server | Key tools | Notes | Callable by |
| --- | --- | --- | --- |
| `state-mcp` | `read_state` `query_metrics` `search_memory` `propose_patch` `append_decision` | The only door to memory. Writes are proposals, never direct. | All agents |
| `policy-mcp` | `check_action` `get_tier` `get_constraints` `revert` | Deterministic rules plus a Critic call. Returns tier, verdict, and which constraint fired. | Orchestrator, Critic |
| `wearables-mcp` | `get_sleep` `get_hrv` `get_activities` `subscribe` | Aggregates Oura, Whoop, Garmin and Apple Health. Webhooks publish events to the bus. | Recovery, Sleep, Progress |
| `training-mcp` | `get_program` `estimate_1rm` `next_load` `log_set` `swap_exercise` | Progression math and volume landmarks are deterministic code. | Training, Periodization, Injury |
| `nutrition-mcp` | `search_food` `log_meal` `compute_tdee` `macro_targets` `meal_plan` | Food database plus an adaptive TDEE estimator fed by weight and intake trends. | Nutrition, Logistics |
| `calendar-mcp` | `free_slots` `create_event` `detect_travel` `load_score` | Calendar density becomes a stress signal. | Training, Sleep, Logistics |
| `comms-mcp` | `send_message` `schedule_nudge` `start_call` `read_sentiment` | Enforces quiet hours, frequency caps and tone profile. | Orchestrator, Adherence |
| `vision-mcp` | `analyze_form` `count_reps` `flag_risk` | Pose estimation plus a multimodal model. | Form, Injury |
| `research-mcp` | `search_guidelines` `cite` | Curated evidence corpus. Answers carry citations. | Research, Critic, Nutrition |
| `logistics-mcp` | `grocery_list` `find_restaurant_options` `order` | `order` always requires user approval. | Logistics |

policy-mcp contract (the gate every write passes)

```
check_action(agent, action, context) -> {
  verdict:   "allow" | "allow_with_limits" | "block",
  tier:      "auto" | "notify_first" | "approve" | "escalate",
  fired:     ["max_weekly_load_increase", "min_calorie_floor", ...],
  veto_window_minutes?: number,
  explain:   string            // shown to the athlete on request
}
```

05 · Control loop

## What happens on every event

One loop handles chat messages, wearable syncs, cron ticks and anomalies. It is the same shape every time, which makes it testable.

```
on event e:                                                   # L2 → L6
  ctx       = recall(e.athlete, e)                           # plan slice, metrics, memories, active experiments
  route     = orchestrator.plan(e, ctx)                      # which agents, in what order, parallel or serial
  proposals = parallel(agent.propose(ctx) for agent in route)# L5: typed Proposal objects
  merged    = orchestrator.reconcile(proposals)              # conflicts → debate, max 2 rounds, then rule
  verdict   = critic.review(merged, ctx)                     # revise or block unsafe / off-goal changes
  for action in merged.actions:
      gate = policy.check(action)                            # deterministic limits first
      match gate.tier:
        auto         -> apply(action); ledger.log(action); notify(summary)
        notify_first -> schedule_apply(action, gate.veto_window); notify(preview)
        approve      -> ask_user(action, rationale)
        escalate     -> freeze(action.domain); refer_to_professional(); notify(care_message)
  schedule(reflect, at = e.ts + horizon(action))             # outcome check → lesson → procedural memory
```

### Reflection closes the learning loop

After a horizon passes (next session, next week, next block) the system compares what it predicted with what happened, writes a lesson, and updates the athlete's procedural memory. Over months the system becomes specific to this person.

### Failure is a first-class path

Missing wearable data lowers confidence and widens the conservative margin. A failed tool call retries with backoff, then degrades to a simpler plan and tells the athlete. The loop never blocks waiting on one agent.

06 · Autonomous workflows

## Eight loops that run without being asked

Each runs as a durable Temporal workflow, so a restart in the middle of a weekly review resumes where it stopped.

### Daily readiness

cron 05:30 local · or on wearable sync

1. **Recovery** scores readiness from HRV, RHR, sleep and yesterday's load.
2. **Orchestrator** compares the score with today's session.
3. **Training** keeps, trims or swaps the session. **Nutrition** re-times carbs.
4. **Critic + policy** check limits; tier decides apply or ask.
5. **Adherence** writes one short message in the athlete's tone.

**Output:** an adjusted day and a single message with the reason.

### Post-workout

event: session.logged

1. **Progress** compares reps, load and RPE with the prediction.
2. **Training** sets the next session's load through `next_load`.
3. **Nutrition** sets recovery meal targets from actual energy burned.
4. **Form** reviews any uploaded clip.

**Output:** next session pre-loaded; prediction error logged for calibration.

### Weekly review

cron Sunday 18:00

1. **Progress** builds trends, adherence and plateau checks.
2. Each specialist submits one proposal for the coming week.
3. **Orchestrator** reconciles; **Critic** reviews.
4. Low-risk changes apply; the rest queue for approval.
5. Report is written and sent.

**Output:** next week's plan and a report that cites its own data.

### Plateau experiment

detector: no trend for 3 weeks

1. **Progress** rules out water, sleep and adherence as causes.
2. Ranks hypotheses (intake adaptation, stale stimulus, sleep debt).
3. Registers an experiment with one change and one success metric.
4. Runs two weeks, measures, then keeps or reverts.
5. Verdict becomes procedural memory.

**Output:** the system learns what works for this athlete.

### Disruption mode

sick · travel · 3 missed days · injury

1. **Adherence** finds the cause from messages and calendar.
2. **Orchestrator** enters degraded mode and re-plans all domains together.
3. **Training** shrinks sessions; **Logistics** adapts food; **Sleep** adjusts for time zones.
4. A return ramp is scheduled so re-entry is gradual.

**Output:** a plan that fits real life this week, and a path back.

### Proactive check-in

silence · sentiment drop · streak at risk

1. **Adherence** detects a pattern, not a single missed log.
2. Chooses timing and tone from what worked before.
3. **comms-mcp** enforces quiet hours and frequency caps.
4. The reply, or its absence, updates the engagement model.

**Output:** outreach that feels timely, not spammy.

### Onboarding and re-profiling

first run · every 6 weeks

1. **Intake** collects goals, injuries, schedule, equipment, food preferences.
2. Runs baseline tests and a screening questionnaire.
3. **Periodization** drafts the macro plan; the athlete approves it.
4. Later, Intake revisits what drifted (goals, schedule, injuries).

**Output:** a profile that stays current instead of aging in place.

### Memory maintenance

cron nightly

1. **Curator** summarizes the day into episodic memory.
2. Merges duplicates, resolves contradictions, decays stale facts.
3. Re-scores salience; prunes recall candidates.
4. Flags suspicious memories for the athlete to confirm.

**Output:** memory that stays small, correct and cheap to recall.

07 · Event simulator

## Pick an event and watch the system respond

A walkthrough of how the loop routes six different signals, which agents wake up, and which tier each action lands in.

08 · Autonomy and safety

## Autonomy is earned per action, not granted per agent

The same agent can act alone on a rest-day swap and need approval to start a new block. The policy engine decides from the action, the size of the change, and the athlete's history.

TIER 1 · AUTO**Act, then tell**

Swap an exercise, move a rest day, shift a meal, trim a session by up to 30%, send a reminder.

TIER 2 · NOTIFY FIRST**Act unless vetoed**

Change calories by 5–10%, run an experiment, reorder a week. Applies after a veto window of a few hours.

TIER 3 · APPROVE**Ask, then act**

New training block, aggressive deficit, return-to-training ramp, supplements, any spending.

TIER 4 · ESCALATE**Stop and refer**

Chest pain, fainting, severe or lasting injury, disordered-eating signals, medical conditions. Domain is frozen.

### Hard constraints (code, not prompts)

- Calorie floor by sex, size and age
- Weekly load increase cap near 10%
- No new loaded movement through reported pain
- Minimum rest days per week
- Rate of weight change ceiling
- Red-flag phrase list that forces escalation

### Earned trust

- Tier thresholds loosen as the athlete accepts changes without override
- They tighten after overrides, reverts or injuries
- The athlete can set a global dial: hands-on, balanced, autopilot
- New action types always start one tier stricter

### Always-on watchdogs

- **Overtraining:** rising RHR, falling HRV, stalled performance
- **Disordered eating:** restriction language, extreme deficits, guilt cues
- **Injury:** repeated pain mentions across sessions
- Watchdogs run outside the agents, so one cannot talk another out of a flag

### Auditability

- Every change has a ledger row: inputs, rationale, tier, outcome
- One-tap revert for any non-escalated decision
- Global kill switch pauses all autonomous writes
- Prompt-injection defense: user text and web content are data, never instructions

09 · Call mode

## Talking to the whole team at once

The athlete joins a roundtable. A Moderator runs it. Behind the call, agents still work through proposals and shared state.

STEP 1

### Triage

The Moderator reads the question and invites only the 2–3 relevant specialists. The athlete can also address anyone by name.

STEP 2

### Parallel answers

Invited agents answer concurrently from their own domain and the shared state. Replies are short and tagged by speaker.

STEP 3

### Bounded debate

If they disagree, one or two rounds of evidence-based rebuttal. Then the Moderator rules; the athlete is not left to referee.

STEP 4

### One verdict

The Moderator gives a single recommendation. Anything that changes the plan goes through the Critic and policy tiers as usual.

STEP 5

### Commit

Decisions are written to state and the ledger, and the call transcript is summarized into episodic memory.

VOICE

### Realtime layer

Speech-to-speech or STT → moderator → TTS with a distinct voice per agent. Interruptible, with the Moderator owning turn-taking.

10 · Runtime and tooling

## A stack that fits the shape

### Orchestration

**LangGraph** for the per-event agent graph (parallel fan-out, reconcile, critic, gate). **Temporal** for durable cron workflows, veto timers and retries. The Claude Agent SDK is a good fit for subagents and MCP wiring.

### Data

**Postgres** with **TimescaleDB** and **pgvector** covers state, metrics and memory in one system. **NATS** or **Redis Streams** as the event bus. Object storage for videos and exports.

### Models

Tiered by role (see agents). Structured outputs for every `Proposal`. Prompt caching on the stable system prompt and athlete profile to cut cost per event.

### Observability

Trace every event end to end (OpenTelemetry plus an LLM tracing tool). Dashboards for intervention acceptance, override rate, escalations, cost per athlete per day.

### Evals

Replay harness: recorded athlete histories re-run against new prompts. Golden scenarios (poor sleep, injury, deficit stall). A red-team set for safety. No agent or prompt change ships without passing these.

### Security and privacy

Health data is sensitive. Encrypt at rest, scope tokens per agent, keep per-athlete isolation, log all access, and let the athlete export or delete everything.

**Wearable data (implemented):** `POST /wearables/ingest` (normalised batch), `POST /wearables/import?format=apple_health|csv` (raw-body file upload, streamed to a temp file that is deleted afterwards), `GET /wearables/status`, `DELETE /wearables/data[?source=]`. Every value keeps its source and import time; out-of-range values are rejected and reported, never clipped; a manual check-in always wins over a wearable value for the same day (merged at read time, nothing manual is overwritten). The tables are inside the same database, so export-everything and delete-everything cover them. No provider credentials or tokens are stored by this path. Consumers: `fitness_os/mem/frames.py` (fills `sleep_h` only when no manual value exists; adds `rhr`, `hrv_ms`, `steps`, `active_kcal`, `sleep_score`, `sleep_src` columns the miner can use) and `fitness_os/context.py` (`wearables_7d` for recovery, sleep, training and orchestrator).

### Success metrics

- Adherence rate (sessions done / planned)
- Goal progress against forecast
- Autonomous action acceptance vs. override
- Time to recover from disruptions
- Safety: escalations caught, false negatives (target zero)

### Biggest risks

- Overconfident advice on medical or injury questions
- Compounding errors across agents
- Notification fatigue
- Memory drift and wrong "facts"
- Cost growth from chatty loops

11 · Roadmap

## Build in layers, prove each one

PHASE 0

### Foundations

State schema, decision ledger, `state-mcp`, `policy-mcp` with hard constraints, tracing, and a replay harness.

ExitAny change can be explained, reverted and replayed.

PHASE 1

### Closed-loop coach

Orchestrator, Training, Nutrition, Critic. Manual log entry. Daily and post-workout loops. Tiers 1 and 3 only.

ExitPlans adapt to logs without human editing.

PHASE 2

### Senses

Wearables, Recovery, Sleep & Stress. Event bus and cron. Readiness-driven adjustments. Add tier 2 with veto windows.

ExitThe day's plan changes overnight with no prompt from the athlete.

PHASE 3

### Judgment

Progress Analyst, weekly review, plateau experiments, Injury & Rehab, watchdogs, escalation paths.

ExitThe system runs experiments and refuses unsafe requests reliably.

PHASE 4

### Relationship

Adherence, Intake, Memory Curator, procedural memory, proactive check-ins, tone adaptation.

ExitRetention and adherence measurably beat a static plan.

PHASE 5

### Team presence

Roundtable call mode, voice, Form (vision), Logistics, Periodization, earned-trust autonomy dial.

ExitThe athlete can talk to the team and trust it on autopilot.

Fitness Agent OS · reference architecture. The system provides coaching support, not medical care. Tier 4 paths must always route to qualified professionals.