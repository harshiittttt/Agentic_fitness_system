# The athlete memory layer ("Mem"): design and contracts

**Goal.** The longer someone uses the app, the better the system knows them: their body's real response to training, their habits, their
words, their risks. After a few weeks it should tell them things about themselves they had not noticed, and every agent (Training, Nutrition,
Recovery, the chat coach, the live coach, the roundtable) should be using that knowledge without being asked. "It knows me better than I do"
is the bar, and it has to be TRUE: every claim is backed by data and an honest statistical test, never by a model's guess.

## Why not "just embeddings"
Notes + vector search gives a coach that remembers sentences. Knowing someone needs five more things:

1. **Beliefs, not notes.** Each fact is a belief with a Beta(alpha, beta) posterior, a valid-time interval, provenance and a lifecycle. New evidence
   reinforces or contradicts it; a better value supersedes the old one without erasing it ("you used to ... now ...").
2. **Discoveries.** Patterns mined from the athlete's own time series (sleep -> next-day lifts, volume dose-response, weekday adherence, personal
   maintenance calories, RPE reporting bias ...), validated with permutation tests, FDR control and split-half replication. Only validated
   patterns are ever shown to the athlete or to an agent as "true about you".
3. **Prediction and surprise.** The system forecasts what the athlete will do/feel/lift next, logs it, and scores itself. Misses are the richest
   learning signal and the most convincing proof that it knows the person.
4. **Active learning.** It knows what it does not know, and asks the question whose answer would improve its decisions the most.
5. **A living model of the athlete (the dossier).** A versioned, structured synthesis that agents read first, with a "what changed about you" diff.

## Architecture (all in one SQLite file: relational + graph + FTS5 + vectors + time-versioned beliefs; the access layer is `store.py`, so any piece
## could be moved to a different engine later without touching the callers)

```
 raw logs (sets, checkins, meals, chat turns, decisions, nudges)               <- existing tables, never duplicated
        |                         |                              |
   frames.py (daily feature     ingest.py (words -> beliefs)   (events -> beliefs)
   matrix, deterministic)            |                              |
        |                            v                              v
   miner.py + stats.py          store.py :  mem_fact (beliefs)  mem_entity/mem_edge (graph)  mem_embedding  mem_fts
   (discoveries)  forecast.py        mem_discovery   mem_prediction   mem_question   mem_dossier   mem_audit
        |  (predictions)             ^        ^          ^
        +--------------> store ------+        |          |
                                     consolidate.py (the "sleep" pass)     embed.py (vectors, offline fallback)
                                              |
                       retrieve.py  (BM25 + dense + graph + temporal prior -> fused -> diversified)
                                              |
                       pack.py  (budgeted per-agent context: core + retrieved + situational)  ---> every agent prompt
                       dossier.py / questions.py / reveal.py  ---> API (/memory/*) ---> the "What I know about you" screen
```

## Ownership (STRICT: three agents work in parallel in one working tree)

| Owner | Files they create/edit |
|---|---|
| **me (frozen foundation)** | `mem/types.py`, `mem/store.py`, `mem/__init__.py`, `mem/jobs.py`, `mem/DESIGN.md`, `tests/test_mem_store.py`. NOBODY else edits these. Need a store change? Put raw SQL in your own module via `store._c()` (read-only SQL is fine; WRITES go through store functions), or add your own new table with your own `SCHEMA`/`_c()`, and list the request in your final report. |
| **memory-core** | `mem/embed.py`, `mem/retrieve.py`, `mem/pack.py`, `mem/consolidate.py`, `mem/migrate.py`, `fitness_os/memory.py` + `fitness_os/curator.py` (legacy: keep every existing public function and test green), tests `tests/test_mem_core_*.py`, `mem/bench_retrieval.py`, `requirements.txt` (numpy is already added) |
| **insight-engine** | `mem/frames.py`, `mem/stats.py`, `mem/miner.py`, `mem/forecast.py`, `mem/synth.py`, tests `tests/test_mem_insight_*.py`, `mem/bench_insight.py` |
| **athlete-model** | `mem/ingest.py`, `mem/dossier.py`, `mem/questions.py`, `mem/reveal.py`, `mem/api.py`, integration edits in `fitness_os/context.py`, `fitness_os/coach_context.py`, `fitness_os/coach_tools.py`, `fitness_os/conversation.py`, `fitness_os/learn.py` (keep API), `backend/main.py` (ONE include line for the router), `backend/coach/live.py` (only if needed), `mobile/src/**` (new screen + api.ts + one entry point), tests `tests/test_mem_athlete_*.py` |

Do not run the whole test suite while developing (three agents share the CPU and database files): run your own test files plus the existing
tests of files you touched. The lead runs the full suite at the end.

## Contracts (signatures are stable; behaviour may be improved behind them)

### Data you can rely on (existing tables, `backend/memory/db.py`)
`sets(ts, session, exercise, weight, reps, rpe, note)` where **`session` is the session DATE label (YYYY-MM-DD) and is the truth; `ts` is only the insert
time** (demo/imported data is back-dated). `checkins(ts, sleep_h, soreness, energy, bodyweight, note)`, `meals(day, name, grams, kcal, protein, carbs, fat, estimated)`,
`profile`, `plan`, `memory`, the decision ledger (`fitness_os/ledger.py`), `nudge` (`fitness_os/comms.py`), `conversation_message` (`fitness_os/conversation.py`),
`training_session` (`backend/coach/session_flow.py`). Time comes from `fitness_os/clock.py` (`FITNESS_NOW` overrides it in tests; ALWAYS use it, never `datetime.now()`).

### memory-core
* `embed.embed_texts(texts: list[str], task: str = "document") -> np.ndarray` shape `[n, d]`, L2-normalised. `embed.model_name() -> str`. Uses Gemini
  `gemini-embedding-001` through the OpenAI-compatible endpoint of `backend.agents.base._cfg("main")` (`POST {base_url}/embeddings`; request `dimensions` 768 if
  accepted else truncate + renormalise); batching, timeout, retry, `store.text_hash` cache. **Never raises**: with no key or any failure it falls back to a
  deterministic hashed word+char-n-gram vector (model name `"hash-512"`). Vectors from different models are never compared.
* `embed.reindex() -> dict`: embed active facts/discoveries whose text changed or that have no vector; returns counts. Used by `jobs.nightly`.
* `retrieve.search(query, k=8, *, kinds, item_types, as_of, min_confidence, max_sensitivity, entities) -> list[Hit]` (signature in the baseline file is the contract).
  Hybrid: BM25 + dense cosine + graph expansion from linked entities (personalised PageRank or 1-2 hops) + temporal prior + confidence/salience, fused (RRF or
  learned weights), then MMR for diversity. `Hit.why` explains every score. Superseded/retracted/denied items never appear unless `as_of` asks for the past.
* `pack.build(agent, query="", budget_tokens=900, live_context="") -> Pack`: core (always true: pinned/health constraints, goals, confirmed facts, top confirmed
  discoveries for this agent) + retrieved + situational (from `frames.today()` if it exists, else empty) rendered into `Pack.text` within the budget;
  per-agent profiles (training wants response/risk/preferences; nutrition wants diet/schedule/energy discoveries; recovery wants sleep/stress; adherence/nudges must NOT receive
  health-sensitive facts; the live coach wants the shortest block). Deduplicates, truncates gracefully, reports `dropped`.
* `consolidate.run() -> dict`: idempotent "sleep" pass: promote repeated episodes to semantic facts, merge near-duplicates (embedding + lexical), resolve `disputed`
  facts (never override `confirmed`/`corrected`; generate nothing the athlete denied), decay unreinforced beliefs (archive < 0.30), re-link entities/edges, re-embed. Never raises.
* `migrate.import_legacy() -> dict`: idempotent import of `memory_item` (fitness_os/memory.py) and the older `memory` table into `mem_fact` (source `imported`).
  `fitness_os/memory.py` keeps working exactly as before (its tests stay green) and mirrors new writes into `mem_fact`.

### insight-engine
* `frames.build(days: int = 120, end: str | None = None) -> list[dict]`: one dict per calendar day, ascending, keys (None when unknown, never invented): `date, dow,
  sleep_h, soreness, energy, bodyweight, kcal, protein_g, carbs_g, fat_g, trained (bool), n_sets, volume_kg, rpe_mean, readiness, pain_note (bool),
  e1rm_<lift> (best that day, by `backend.tools.calculators.rpe_adjusted_e1rm`)`. Pure, deterministic, offline.
* `frames.today() -> dict`: `{"frame": ..., "baseline": {metric: {"mean","sd","n"}}, "z": {metric: z}, "deviations": [sentence with numbers computed by code]}`.
* `stats.py`: numpy-only (no scipy): `spearman`, `pearson`, `permutation_p` (BLOCK permutation, because daily series are autocorrelated), `bootstrap_ci`, `bh_fdr`,
  `detrend`, `split_half_agree`, `cohens_d`, `min_n` rules. Every test returns effect, CI, p, n.
* `miner.run(force: bool = False) -> dict` and `miner.run_full()`: skip when the frames' `data_hash` is unchanged (unless forced); test the hypothesis library; apply
  Benjamini-Hochberg across ALL hypotheses tested in the run; a discovery is `confirmed` only if q < 0.10, n >= its minimum, |effect| above its practical-significance floor,
  CI excludes 0, and the direction replicates in both halves of the data; `candidate` if it passes the test but not replication; `weakening` when a previously confirmed pattern stops
  holding; `retired` after repeated failure. `store.upsert_discovery(key, ...)` with a **stable key** (family + variables + lag). The `statement` and `recommendation` are generated from the numbers
  by code (no LLM), in plain second person ("On days after you sleep 7.5 h or more, your squat is about 4% stronger (31 sessions, holds in both halves of your history).").
  Also writes trait facts (`response_profile`, `recovery_profile`, `calibration_bias`, `adherence_pattern`, ... `source="derived"`, `value_num` set) so retrieval and the dossier see them.
  Minimum hypothesis families: sleep -> performance (lag 0 and 1); soreness/energy -> performance; weekly-volume dose-response (personal sweet spot, diminishing returns);
  training frequency response; weekday effects on adherence and on performance; rest-gap effect; RPE reporting bias; protein -> next-day recovery; adaptive maintenance calories from
  the weight trend and intake (the athlete's REAL TDEE vs the formula); late meals -> sleep; bodyweight -> strength; deload response; what preceded PRs.
* `forecast.predict(target, subject=None, **kw) -> dict` (`target` in `e1rm` (subject=lift), `readiness`, `adherence`, `bodyweight`): `{value, lo, hi, horizon, model, basis[...]}`,
  robust local trend (Theil-Sen or similar) adjusted by validated discoveries; logs via `store.log_prediction`. `forecast.resolve_due() -> dict` resolves predictions against frames;
  `forecast.calibration(target) -> {n, mae, naive_mae, skill, coverage}`; `forecast.surprises(limit) -> list` (largest standardised errors, with plain explanations).
* `synth.make_athlete(seed, days, effects={...}, noise=...) -> truth dict`: writes realistic rows into the SCRATCH database with planted effects (e.g. +4% squat after >=7.5 h sleep,
  Monday adherence dip, volume sweet spot at X sets, maintenance 300 kcal above formula). Used by every agent's integration tests. Deliver it FIRST (the athlete-model agent depends on it).

### athlete-model
* `ingest.observe(text, source="chat", channel="chat", ts=None) -> list[dict]` (facts written, via `store.add_fact`, with entity links and evidence refs). Deterministic extraction first (reuse `learn.extract`),
  then guarded LLM extraction through a forced tool call when a key exists; negation and change ("I don't mind lunges anymore" disputes/supersedes), temporal phrases, polarity, sensitivity,
  idempotent. Athlete text is DATA: stored as a short normalised belief plus an evidence quote, never executed, length-capped, control characters stripped, imperative "ignore previous
  instructions" content produces NO belief. Also `ingest.from_event(kind, payload)` for pain reports, skipped exercises, repeated meals, check-in notes.
* `dossier.build(refresh=False) -> dict` / `dossier.refresh() -> dict`: sections `goals, training, recovery, nutrition, behavior, risk, calibration, track_record`, each a list of
  `{text, confidence, source, evidence, fact_id|discovery_id}`; `coverage` (0..1: how many key slots are known with confidence >= 0.6); `narrative` (LLM optional, every number guarded
  against the inputs, deterministic fallback); `diff` against the previous version ("what changed about you"); saved with `store.save_dossier` only when the data hash changed.
* `questions.generate() -> dict` / `questions.next(limit) -> list` / `questions.answer(id, text) -> dict`: value-of-information ranking (uncertainty x decision relevance x recency),
  from low-confidence/disputed facts, empty dossier slots that matter, discoveries awaiting confirmation, contradictions; at most one question surfaced per day; answers go through `ingest`.
* `reveal.weekly() -> dict`: the "what I learned about you this week" cards (only discoveries not yet `surfaced_ts`, prediction hits/surprises, belief changes), numbers from code, then marks them surfaced.
* `api.py` router mounted at `/memory`: `GET /memory/overview`, `GET /memory/dossier`, `GET /memory/facts`, `POST /memory/facts/{id}/(confirm|deny|correct)`, `GET /memory/discoveries`,
  `POST /memory/discoveries/{id}/(confirm|dismiss)`, `GET /memory/questions`, `POST /memory/questions/{id}/(answer|dismiss)`, `GET /memory/timeline`, `POST /memory/search`,
  `POST /memory/remember`, `GET /memory/reveal`. Health-sensitive items are flagged so the screen can collapse them.
* Agent integration (this is what makes it matter): `context.pack()` gets `athlete_model` (the dossier digest) + `memories` from `mem.pack`; the coach (`coach_context`, `coach_tools`) gets
  tools to recall, remember, correct and forget; the live coach and chat use the pack; `conversation.record` calls `mem.observe`.
* Mobile: a "What I know about you" screen (entry from Profile): coverage ring, "I noticed ..." discovery cards with evidence chips and Yes/No, the open question with answer chips,
  dossier sections, "changed recently", facts with confirm / correct / forget, health items collapsed. One hero number, each fact once, 16 px gutters, tap targets >= 44 px.

## Quality bars (these are acceptance tests, not aspirations)
* **No hallucinated insight.** On 30 synthetic NULL athletes (no planted effects, autocorrelated noise) the miner must produce < 10% confirmed discoveries on average across all families.
* **Recovery of planted truth.** On synthetic athletes with planted effects the miner must confirm them with the right sign and an effect within the CI/ +- 35% of truth, in >= 85% of seeds.
* **Forecasts beat the naive baseline** (last value) on synthetic athletes: skill > 0 for `e1rm` and `readiness`; interval coverage within 0.15 of nominal.
* **Retrieval quality.** On a labelled paraphrase benchmark hybrid retrieval beats BM25-only and dense-only on recall@5 and MRR; temporal queries never return superseded beliefs as current;
  the offline hash fallback still passes a lower threshold.
* **Governance.** A denied fact never returns; a confirmed fact is never overridden by data; every belief shown has provenance; deleting everything (privacy.delete_all) leaves working indexes.
* **Safety.** Memory text is data, never instructions (injection test). Health-sensitive beliefs never reach outreach/nudge copy. No number shown to the athlete is invented (guard + test).
* **Budget.** `pack()` respects `budget_tokens` (+-5%), runs in < 100 ms for 2,000 beliefs offline. Everything works with NO LLM key and NO network (deterministic paths); the LLM only improves wording/extraction.
* **Background steps never raise** and never block the app; jobs are idempotent.

## Conventions
Python 3.10, type hints, `from __future__ import annotations`, no new heavy dependencies (numpy only), tests offline (monkeypatch the LLM/embeddings), `PYTHONIOENCODING=utf-8` for Devanagari,
Windows paths, never print or log keys, use the Write tool for scripts that contain apostrophes (Git Bash heredocs break), use `fitness_os/clock.py` for time, rows are plain dicts.
