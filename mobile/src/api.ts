import AsyncStorage from '@react-native-async-storage/async-storage';

export const API = process.env.EXPO_PUBLIC_API_URL ?? 'http://localhost:8000';
const QUEUE_KEY = 'offline_queue_v1';

type Queued = { path: string; body: unknown };

/** An error response from the backend. `message` is already plain language (the server's own `detail`), `status` is the HTTP code. */
export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.name = 'ApiError'; this.status = status; }
}

const nice = (s: string) => s.replace(/^value error,\s*/i, '').replace(/\s+/g, ' ').trim();
const fieldName = (loc: unknown) => {
  const parts = (Array.isArray(loc) ? loc : []).filter((x) => x !== 'body' && x !== 'query' && x !== 'path' && typeof x !== 'number');
  return parts.length ? String(parts[parts.length - 1]).replace(/_/g, ' ') : '';
};

/** Turn a FastAPI error body (`{detail: "text"}` or a 422 list of `{loc, msg}`) into one readable sentence. */
export function summarizeDetail(body: any): string | null {
  const d = body && typeof body === 'object' && 'detail' in body ? body.detail : body;
  if (typeof d === 'string') return nice(d) || null;
  if (Array.isArray(d)) {
    const lines = d.map((x) => {
      if (typeof x === 'string') return nice(x);
      const msg = nice(String(x?.msg ?? x?.message ?? ''));
      const f = fieldName(x?.loc);
      return msg ? (f ? `${f}: ${msg}` : msg) : '';
    }).filter(Boolean);
    const uniq = lines.filter((l, i) => lines.indexOf(l) === i);
    if (!uniq.length) return null;
    return uniq.length > 3 ? `${uniq.slice(0, 3).join('; ')} (and ${uniq.length - 3} more)` : uniq.join('; ');
  }
  if (d && typeof d === 'object') return nice(String((d as any).message ?? (d as any).msg ?? '')) || null;
  return null;
}

/** Read a failed response and build the error to throw. Never throws itself. */
export async function apiError(r: Response, what = ''): Promise<ApiError> {
  let detail: string | null = null;
  try { detail = summarizeDetail(await r.json()); } catch { /* body was not JSON */ }
  if (detail) return new ApiError(r.status, detail);
  const tail = what ? ` (${what})` : '';
  return new ApiError(r.status, r.status >= 500 ? `The server had a problem, code ${r.status}${tail}. Try again in a moment.` : `The server did not accept that, code ${r.status}${tail}.`);
}

async function post(path: string, body: unknown) {
  const r = await fetch(`${API}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw await apiError(r, path);
  return r.json();
}

async function readQueue(): Promise<Queued[]> {
  try { return JSON.parse((await AsyncStorage.getItem(QUEUE_KEY)) ?? '[]'); } catch { return []; }
}

/** Replay anything logged while offline. Call on app start and after each success. */
export async function flushQueue(): Promise<number> {
  const q = await readQueue();
  const left: Queued[] = [];
  for (const item of q) {
    try { await post(item.path, item.body); } catch { left.push(item); }
  }
  await AsyncStorage.setItem(QUEUE_KEY, JSON.stringify(left));
  return q.length - left.length;
}

/** Logging must never be lost at the gym: queue on failure. */
export async function logVoice(session: string, transcript: string) {
  try {
    const res = await post('/log', { session, transcript });
    flushQueue();
    return { ...res, queued: false };
  } catch (e) {
    // A 4xx means the server read the request and refused it: retrying later would fail the same way, so tell the user why.
    if (e instanceof ApiError && e.status >= 400 && e.status < 500) throw e;
    const q = await readQueue();
    q.push({ path: '/log', body: { session, transcript } });
    await AsyncStorage.setItem(QUEUE_KEY, JSON.stringify(q));
    return { saved: [], next_set: null, safety_flag: null, queued: true };
  }
}

/** fetch that gives up after `ms` so a bad address shows an error instead of an endless spinner. */
export async function fetchTimeout(url: string, ms = 8000): Promise<Response> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms);
  try { return await fetch(url, { signal: ctl.signal }); }
  catch (e: any) { throw new Error(e?.name === 'AbortError' ? `timed out after ${ms / 1000}s (no reply from ${url})` : `${e?.message ?? e} (${url})`); }
  finally { clearTimeout(timer); }
}

export type Prefill = {
  last: { weight: number; reps: number; rpe: number | null; session: string; ts: string } | null;
  suggest: { weight: number; reps: number; reason: string } | null;
  increment: number;
};
export const getPrefill = (name: string, reps: number, rpe: number): Promise<Prefill> =>
  fetchTimeout(`${API}/exercise/prefill?name=${encodeURIComponent(name)}&target_reps=${reps}&target_rpe=${rpe}`).then(async (r) => { if (!r.ok) throw await apiError(r, 'prefill'); return r.json(); });

/** A set entered with the steppers. Same offline guarantee as voice logging: never lose a set at the gym. */
export async function logSet(session: string, exercise: string, weight: number, reps: number, rpe?: number) {
  const body = { session, exercise, weight, reps, ...(rpe != null ? { rpe } : {}) };
  try {
    const res = await post('/log/set', body);
    flushQueue();
    return { ...res, queued: false };
  } catch (e: any) {
    if (/\b4\d\d\b/.test(String(e?.message))) throw e;  // the server refused it (bad numbers): do not queue forever
    const q = await readQueue();
    q.push({ path: '/log/set', body });
    await AsyncStorage.setItem(QUEUE_KEY, JSON.stringify(q));
    return { saved: [], next_set: null, safety_flag: null, queued: true };
  }
}

export const getPlan = () => fetchTimeout(`${API}/plan`).then((r) => (r.ok ? r.json() : null));
export const getChanges = () => fetch(`${API}/changes`).then((r) => r.json());

// ---- Agent OS: decision feed (events -> agents -> critic -> policy gate -> ledger) ----
export type Decision = {
  id: number; ts: string; agent: string; kind: string; summary: string; why: string; evidence: string; tier: string; status: string;
  fired: string[]; veto_until: string | null; trigger: string; can_revert: boolean; pending: boolean;
};
export type AgentStatus = {
  llm: boolean; has_plan: boolean; designed_by: string | null; kill_switch: boolean; dial: string; frozen: string[];
  pending: Decision[]; watchdog_flags: { watchdog: string; evidence: string }[];
};
// ---- Roundtable, scorecard, trust, your data (backend: fitness_os/roundtable.py, metrics.py, trust.py, privacy.py) ----
export type RoundtableTurn = { agent: string; round: number; text: string; proposals: string[] };
export type RoundtableResult = {
  question: string; speakers: string[]; turns: RoundtableTurn[]; conflict: boolean; rebuttal_rounds?: number; notes?: string[]; verdict: string;
  results: { proposal: string; agent: string; status: string; tier?: string; explain?: string }[]; llm: boolean; care?: boolean;
};
export const askTeam = (question: string, addressed: string[] = []): Promise<RoundtableResult> => post('/agents/roundtable', { question, addressed });
export type Scorecard = {
  adherence: { planned_per_week: number; sessions_14d: number; planned_14d: number; rate_14d: number | null; sessions_28d: number; planned_28d: number; rate_28d: number | null; days_since_last_session: number | null };
  goal_progress: { available: boolean; lifts_trending?: { up: number; flat: number; down: number }; plateaus?: any[] };
  autonomy: { window_days: number; accepted: number; overridden: number; waiting: number; blocked_by_policy: number; acceptance_rate: number | null; override_rate: number | null };
  safety: { escalations_30d: number; frozen_domains: string[]; red_flags_without_freeze: any[]; false_negatives: number };
  disruptions: { count: number; avg_days_to_recover: number | null; active: { kind: string; since: string } | null };
  cost: { model_calls_since_start: number; by_role: Record<string, number> };
};
export const getScorecard = (): Promise<Scorecard> => fetchTimeout(`${API}/agents/metrics`).then(async (r) => { if (!r.ok) throw await apiError(r, 'metrics'); return r.json(); });
export type TrustRow = { type: string; accepted: number; pushed_back: number; recent_pushed_back: number; decisions: number; accept_rate: number | null; level: 'earned' | 'neutral' | 'tightened' | 'strict' };
export const getTrust = (): Promise<{ types: TrustRow[]; strict_new_types: boolean }> => fetchTimeout(`${API}/agents/trust`).then(async (r) => { if (!r.ok) throw await apiError(r, 'trust'); return r.json(); });
export const setStrictNewTypes = (on: boolean) => post('/agents/trust/strict-new-types', { on });
export const exportMyData = (): Promise<any> => fetchTimeout(`${API}/agents/export`, 30000).then(async (r) => { if (!r.ok) throw await apiError(r, 'export'); return r.json(); });
export const deleteAllData = () => post('/agents/data/delete', { confirm: 'DELETE EVERYTHING' });
export const getAccessLog = (): Promise<{ id: number; ts: string; action: string; detail: string }[]> => fetchTimeout(`${API}/agents/access-log?limit=5`).then((r) => (r.ok ? r.json() : []));

export type Nudge = { id: number; agent: string; text: string; ts: string };
export const getNudges = (): Promise<Nudge[]> => fetchTimeout(`${API}/agents/nudges`).then((r) => (r.ok ? r.json() : []));
export const readNudge = (id: number) => post(`/agents/nudges/${id}/read`, {});
export const getAgentStatus = (): Promise<AgentStatus> => fetchTimeout(`${API}/agents/status`).then(async (r) => { if (!r.ok) throw await apiError(r, 'status'); return r.json(); });
export const getDecisions = (limit = 40): Promise<Decision[]> => fetchTimeout(`${API}/agents/decisions?limit=${limit}`).then(async (r) => { if (!r.ok) throw await apiError(r, 'decisions'); return r.json(); });
export const decide = (id: number, action: 'approve' | 'reject' | 'revert') => post(`/agents/decisions/${id}/${action}`, {});
export const setDial = (mode: 'hands_on' | 'balanced' | 'autopilot') => post('/agents/dial', { mode });
export const setKill = (on: boolean) => post(`/agents/kill/${on ? 'on' : 'off'}`, {});
export const runAgents = (type = 'cron.daily') => post('/agents/event', { type, payload: {} });
export const runReview = () => post('/review', {});
export const sendChat = (message: string) => post('/chat', { message });
export type CoachAction = { kind: 'changed' | 'pushback' | 'refused' | 'undone'; tool?: string; decision_id?: number | null; summary?: string; can_undo?: boolean; structural?: boolean };
export const undoCoachChange = (decision_id?: number | null) => post('/coach/undo', decision_id != null ? { decision_id } : {});
export const sendCheckin = (c: object) => post('/checkin', c);
export const onboard = (p: object) => post('/onboard', p);
export const getReadiness = () => fetch(`${API}/readiness`).then((r) => r.json());
export const getSeries = () => fetch(`${API}/progress/series`).then((r) => r.json());
export const getProgress = () => fetch(`${API}/progress`).then((r) => r.json());
export const seedDemo = () => post('/demo/seed', {});
export const resetDemo = () => post('/demo/reset', {});
export const getNutrition = () => fetch(`${API}/nutrition/today`).then((r) => r.json());
export const logMeal = (transcript: string, day?: string) => post('/meal', { transcript, day });
export const deleteMeal = (id: number) => fetch(`${API}/meal/${id}`, { method: 'DELETE' }).then((r) => r.json());
export const resetAll = () => post('/reset-all', {});
export const getAlternatives = (name: string) => fetch(`${API}/exercises/alternatives?name=${encodeURIComponent(name)}`).then((r) => r.json());
export const swapExercise = (day_index: number, exercise: string, replacement: string, scope: 'today' | 'plan') =>
  post('/plan/swap', { day_index, exercise, replacement, scope });
export const fitSession = (minutes: number) => post('/session/fit', { minutes });
export const restoreSession = () => post('/session/restore', {});
export const getCoach = () => fetch(`${API}/coach`).then((r) => r.json());
export const setCoachSettings = (body: { persona?: string; name?: string }) => post('/coach', body);
export const getLiveState = () => fetch(`${API}/live/state`).then((r) => (r.ok ? r.json() : null));
export const liveStart = () => post('/live/start', {});
export const liveSay = (transcript: string, ambient: boolean) => post('/live/say', { transcript, ambient });
export const liveCue = (event: string) => post('/live/cue', { event });
export const liveEnd = () => post('/live/end', {});
export const getTtsStatus = (probe = false) => fetch(`${API}/tts/status${probe ? '?probe=true' : ''}`).then((r) => r.json());
export const getTtsTest = (persona: string) => fetch(`${API}/tts/test?persona=${encodeURIComponent(persona)}`).then((r) => r.json());
export const getNutritionDay = (day: string) => fetch(`${API}/nutrition/day?day=${encodeURIComponent(day)}`).then((r) => r.json());
export const getWeek = () => fetch(`${API}/nutrition/week`).then((r) => r.json());
export const getRecentMeals = () => fetch(`${API}/meal/recent`).then((r) => r.json());
export const repeatMeal = (id: number, day?: string) => post('/meal/repeat', { id, day });
export const getProfile = () => fetch(`${API}/profile`).then((r) => (r.ok ? r.json() : null));
export const updateProfile = (patch: object) => post('/profile', patch);

// ---- Session helpers (backend/routes_session.py): undo a set, workout summary, weigh-in ----
export type LoggedSet = { id: number; ts: string; session: string; exercise: string; weight: number | null; reps: number | null; rpe: number | null };
export type WorkoutSummaryData = {
  session: string; sets: number; volume_kg: number; prs: string[]; duration_min: number | null; day_name: string | null;
  planned_sets: number; done_planned_sets: number; finished: boolean;
  exercises: { name: string; sets: number; volume_kg: number; best: { weight: number | null; reps: number } | null }[];
  next: { name: string; exercises: number; est_minutes: number | null } | null;
};
export type CheckinSummary = {
  weights: { day: string; kg: number }[]; last_weight: { day: string; kg: number } | null;
  today: { sleep_h: number | null; soreness: number | null; energy: number | null } | null;
};
const okJson = async (r: Response) => { if (!r.ok) throw await apiError(r, r.url); return r.json(); };
export const getSessionSets = (session: string, exercise?: string): Promise<LoggedSet[]> =>
  fetchTimeout(`${API}/session/sets?session=${encodeURIComponent(session)}${exercise ? `&exercise=${encodeURIComponent(exercise)}` : ''}`).then(okJson);
export const deleteSet = (id: number): Promise<{ ok: boolean; deleted: LoggedSet }> =>
  fetch(`${API}/session/set/${id}`, { method: 'DELETE' }).then(okJson);
export const getWorkoutSummary = (session?: string): Promise<WorkoutSummaryData> =>
  fetchTimeout(`${API}/session/summary${session ? `?session=${encodeURIComponent(session)}` : ''}`).then(okJson);
export const postWeighIn = (bodyweight: number) => post('/session/weighin', { bodyweight });
export const getCheckinSummary = (): Promise<CheckinSummary> => fetchTimeout(`${API}/session/checkins`).then(okJson);

// ---- UX additions: connectivity probe, insights summary, today state, meal undo ----
export const getHealth = (): Promise<boolean> => fetchTimeout(`${API}/health`, 5000).then((r) => r.ok);
export type InsightsSummary = {
  prs: { lift: string; best_e1rm: number; best_date: string; heaviest: { weight: number; reps: number; date: string }; latest_e1rm: number; at_best: boolean; sessions: number }[];
  recent: { date: string; sets: number; tonnage: number; exercises: { name: string; sets: number; top: { weight: number; reps: number } | null }[] }[];
  week: { sessions: number; target: number; start: string };
  total_sessions: number; first_session: string | null;
};
export const getInsightsSummary = (): Promise<InsightsSummary> => fetchTimeout(`${API}/insights/summary`).then(async (r) => { if (!r.ok) throw await apiError(r, 'insights'); return r.json(); });
export type TodayState = {
  has_plan: boolean; trained_today?: boolean; sets_today?: number; tonnage_today?: number; week_done?: boolean; days_since_last?: number | null;
  next?: { name: string; lifts: number; sets: number; minutes: number | null; exercises: string[] };
  week?: { sessions: number; target: number };
};
export const getTodayState = (): Promise<TodayState> => fetchTimeout(`${API}/insights/today`).then(async (r) => { if (!r.ok) throw await apiError(r, 'today'); return r.json(); });
export const restoreMeal = (m: { day: string; name: string; grams: number | null; kcal: number; protein: number; carbs: number; fat: number; estimated: boolean }) => post('/meal/restore', m);

// ---- The athlete model: "What I know about you" (backend: fitness_os/mem/api.py, mounted at /memory) ----
export type MemSlot = { slot: string; label: string; weight: number; known: boolean; confidence: number; fact_id: number | null; partial: boolean };
export type MemOverview = {
  coverage: number; coverage_pct: number; slots_known: number; slots_total: number; slots: MemSlot[]; missing: string[]; narrative: string; version: number; changed: string;
  counts: { facts: number; discoveries: number; sensitive: number }; stage: 'new' | 'knowing'; pending_discoveries: number; open_questions: number;
  reveal: { ts: string | null; cards: number; age_days: number | null };
};
export type MemEvidence = { kind: string; text: string; ts?: string | null };
export type MemItem = {
  text: string; confidence: number; source: string; source_label: string; evidence: MemEvidence[]; fact_id?: number; discovery_id?: number; predicate?: string;
  user_state?: string; sensitive: boolean; sensitivity: string; since?: string; title?: string; recommendation?: string | null; weakening?: boolean;
};
export type MemDiff = { since: string | null; summary?: string; added?: { section: string; text: string }[]; removed?: { section: string; text: string }[]; used_to?: { before: string; now: string; sensitive?: boolean }[] };
export type MemDossier = {
  version: number; ts: string | null; sections: Record<string, MemItem[]>; section_titles: Record<string, string>; coverage: number; slots: MemSlot[]; missing: string[];
  narrative: string; diff: MemDiff; counts: { facts: number; discoveries: number; sensitive: number };
};
export type MemDiscovery = MemItem & { id: number; status: string; pending: boolean; chips: string[] };
export type MemQuestion = { id: number; topic: string; kind: string; question: string; why: string; options: string[]; value: number; status: string; free_text: boolean };
export type MemEvent = { ts: string; kind: string; action: string; text: string; sensitive: boolean; fact_id?: number | null };
const memGet = (path: string, ms = 10000) => fetchTimeout(`${API}/memory${path}`, ms).then(okJson);
const memPost = (path: string, body: unknown = {}) => post(`/memory${path}`, body);
export const getMemoryOverview = (): Promise<MemOverview> => memGet('/overview');
export const getMemoryDossier = (): Promise<MemDossier> => memGet('/dossier');
export const getMemoryDiscoveries = (): Promise<{ discoveries: MemDiscovery[] }> => memGet('/discoveries');
/** The question of the day (at most one is surfaced per day). Validated patterns are shown as cards, so their questions are skipped here. */
export const getMemoryQuestion = (): Promise<{ questions: MemQuestion[]; queued: number }> => memGet('/questions?limit=1&exclude=discovery');
export const getMemoryTimeline = (limit = 30): Promise<{ events: MemEvent[] }> => memGet(`/timeline?limit=${limit}`);
export const answerMemoryQuestion = (id: number, answer: string): Promise<{ ok: boolean; understood: boolean; learned: { body?: string; object?: string }[]; next: MemQuestion[] }> => memPost(`/questions/${id}/answer`, { answer });
export const dismissMemoryQuestion = (id: number) => memPost(`/questions/${id}/dismiss`);
export const confirmMemoryFact = (id: number) => memPost(`/facts/${id}/confirm`);
export type MemProfileSync = { changed: string[] | Record<string, unknown> | null; nutrition_updated: boolean; plan_rebuilt: boolean };
/** A 400 here carries a plain-language reason (ApiError.message) when the value is not valid for the profile. */
export const correctMemoryFact = (id: number, value: string): Promise<{ ok?: boolean; profile?: MemProfileSync }> => memPost(`/facts/${id}/correct`, { value });
export const forgetMemoryFact = (id: number): Promise<{ ok?: boolean; note?: string }> => memPost(`/facts/${id}/forget`);
export const confirmDiscovery = (id: number) => memPost(`/discoveries/${id}/confirm`);
export const dismissDiscovery = (id: number) => memPost(`/discoveries/${id}/dismiss`);
export const rememberAboutMe = (text: string): Promise<{ understood: boolean; learned: { body?: string; object?: string }[]; coverage: number | null }> => memPost('/remember', { text });

// ---- Agent OS: calendar agenda, intake interview, research (cited evidence) ----
export type AgendaKind = 'competition' | 'trip' | 'no_gym' | 'busy' | 'equipment';
export type AgendaEvent = { id: number; kind: AgendaKind; start: string; end: string; label: string; minutes?: number; equipment?: string[]; days_until: number; active: boolean; length_days: number };
export type AgendaInput = { kind: AgendaKind; start: string; end?: string; label?: string; minutes?: number; equipment?: string[] };
async function agentsJson(path: string, init?: RequestInit, what = path) {
  const r = await fetch(`${API}/agents${path}`, init);
  if (!r.ok) throw await apiError(r, what);
  return r.json();
}
export const getAgenda = (): Promise<{ events: AgendaEvent[] }> => agentsJson('/agenda');
export const addAgendaEvent = (e: AgendaInput): Promise<{ event: AgendaEvent; run: { summary?: string; decisions?: unknown[] } | null }> =>
  agentsJson('/agenda', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(e) });
export const removeAgendaEvent = (id: number): Promise<{ removed: number }> => agentsJson(`/agenda/${id}`, { method: 'DELETE' });

export type IntakeQuestion = { slot: string; question: string; why: string; chips: string[] | null; required: boolean };
export type IntakeStatus = { has_profile: boolean; missing_required: string[]; ask_once: string[]; memory: string[]; reprofile_due: boolean; complete: boolean; next_question: IntakeQuestion | null };
export type IntakeReply = { accepted: Record<string, unknown>; refused: { field: string; reason: string }[]; warnings: string[]; mode: string; care: string | null; reply: string; next_question: IntakeQuestion | null; complete: boolean; needs_rebuild?: boolean };
export const getIntake = (): Promise<IntakeStatus> => agentsJson('/intake');
export const answerIntake = (text: string, slot?: string | null): Promise<IntakeReply> =>
  agentsJson('/intake/answer', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text, slot: slot ?? null }) });

export type ResearchSource = { id: string; cite: string; kind: string };
export type ResearchAnswer = { question: string; answer: string; sources: ResearchSource[]; no_sources: boolean; mode: string; caveat: string; referred: boolean };
export type ScreenFinding = { claim?: string; verdict?: string; explanation?: string; sources?: ResearchSource[]; caveat?: string; [k: string]: unknown };
export const askResearch = (question: string): Promise<ResearchAnswer> =>
  agentsJson('/research/ask', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question }) });
export const screenClaim = (claim: string): Promise<ScreenFinding> =>
  agentsJson('/research/screen', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ claim }) });

// ---- wearables (provider-neutral ingest; manual check-ins always win over wearable values) ----
export type WearableMetric = 'sleep_h' | 'sleep_score' | 'resting_hr' | 'hrv_ms' | 'steps' | 'active_kcal';
export type WearableRecord = { date: string; metric: WearableMetric; value: number; unit?: string };
export type WearableWorkout = { start: string; kind: string; minutes: number; kcal?: number | null };
export type WearableIngestResult = { source: string; inserted: number; updated: number; unchanged: number; rejected: number; rejected_detail: { item: string; reason: string }[]; workouts_inserted: number; workouts_updated: number; notes?: string[] };
export type WearableStatus = { connected: boolean; sources: { source: string; n: number; first_day: string; last_day: string; last_import: string }[]; metrics: { metric: string; n: number }[]; workouts: number };
export const ingestWearables = (source: string, records: WearableRecord[], workouts: WearableWorkout[] = []): Promise<WearableIngestResult> =>
  post('/wearables/ingest', { source, records, workouts });
export const getWearableStatus = (): Promise<WearableStatus> => fetchTimeout(`${API}/wearables/status`).then(async (r) => { if (!r.ok) throw await apiError(r, 'wearables'); return r.json(); });
export const deleteWearableData = (source?: string): Promise<{ deleted: boolean; daily_rows: number; workout_rows: number }> =>
  fetch(`${API}/wearables/data${source ? `?source=${encodeURIComponent(source)}` : ''}`, { method: 'DELETE' }).then(async (r) => { if (!r.ok) throw await apiError(r, 'wearables'); return r.json(); });

// ---- Agent OS: tracing (read-only view of recent agent runs) ----
export type TraceRun = { run_id: string; name: string; kind: string; started: string; dur_ms: number | null; outcome: string; attrs?: Record<string, unknown>; spans: number; errors: number; tokens_in: number | null; tokens_out: number | null; cost: number | null };
export type TraceSpan = { id: string; parent_id: string | null; name: string; kind: string; started: string; dur_ms: number | null; tokens_in: number | null; tokens_out: number | null; cost: number | null; outcome: string; error: string | null; attrs?: Record<string, unknown>; children: TraceSpan[] };
export const getTraceRuns = (limit = 30): Promise<{ enabled: boolean; runs: TraceRun[] }> => agentsJson(`/trace?limit=${limit}`);
export const getTraceRun = (id: string): Promise<{ run_id: string; spans: number; errors: number; tokens_in: number | null; tokens_out: number | null; tree: TraceSpan[] }> => agentsJson(`/trace/${encodeURIComponent(id)}`);

// ---- Portion sheet: quantity scale for quick-add chips and logged rows ----
export type PortionInfo = { name: string; known: boolean; unit: string | null; unit_g: number | null; mode?: 'count' | 'weight'; measure?: 'g' | 'ml'; per100: { kcal: number; protein: number; carbs: number; fat: number } | null };
/** null when the lookup fails (older backend, offline): the sheet then falls back to proportional maths. */
export const getPortionInfo = async (name: string): Promise<PortionInfo | null> => {
  try {
    const r = await fetch(`${API}/foods/portion?name=${encodeURIComponent(name)}`);
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
};
/** Change a logged meal's portion in place. Throws ApiError on failure. */
export const setMealPortion = async (id: number, body: { grams: number } | { units: number }) => {
  const r = await fetch(`${API}/meal/${id}/portion`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!r.ok) throw await apiError(r, '/meal/portion');
  return r.json();
};
/** Log a food by exact grams. Throws ApiError (status 404 = food not in the local DB). */
export const logPortion = (name: string, grams: number, day?: string): Promise<{ id: number; item: any }> => post('/meal/portion', { name, grams, day });
