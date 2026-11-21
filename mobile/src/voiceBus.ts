// Voice control bus: the one place where "something the server wants the phone to DO" (open a screen, start a rest timer,
// flip a setting) is handed to the UI. Pure TypeScript, no React / native imports, so it can run under plain Node tests.
//
// Flow:  "Hey coach, open my calendar"  ->  POST /voice/command  ->  { reply, actions: [{type:'open', screen:'agenda'}] }
//        WakeCoach speaks the reply and calls emitVoiceActions(actions).  Whoever owns that part of the UI listens:
//          App.tsx   -> navigate / close           Setup.tsx -> open (agenda, intake, research, trace, knows, server, push), setting
//          Today.tsx -> open live, rest*, end      Nutrition -> nutrition_day, refresh     Insights -> insights_segment, refresh
//
// An action may arrive before its screen is mounted (e.g. "open my calendar" while on Today: App switches the tab first, then
// Setup mounts). So unhandled actions wait in a short queue; a screen drains what it owns when it mounts (takePendingVoiceActions).

export type Tab = 'today' | 'coach' | 'nutrition' | 'insights' | 'setup';
export type OpenScreen = 'live' | 'agenda' | 'intake' | 'research' | 'trace' | 'knows' | 'decisions' | 'roundtable' | 'scorecard' | 'review' | 'server' | 'push' | 'diet';
export type SettingKey = 'wake' | 'vibration' | 'voice_mode' | 'persona';

export type VoiceAction =
  | { type: 'navigate'; tab: Tab }
  | { type: 'open'; screen: OpenScreen }
  | { type: 'close' }                                            // close the full-screen / modal that is open
  | { type: 'refresh'; what?: Array<'today' | 'nutrition' | 'plan' | 'decisions' | 'profile' | 'all'> }
  | { type: 'nutrition_day'; day: string }                      // YYYY-MM-DD: show that day in Fuel
  | { type: 'insights_segment'; seg: string }
  | { type: 'rest'; seconds: number }                           // start the rest timer (same as the existing live action)
  | { type: 'rest_add'; seconds: number }
  | { type: 'rest_skip' }
  | { type: 'end' }                                             // session finished (existing live action)
  | { type: 'setting'; key: SettingKey; value: string | boolean };

export const TABS: Tab[] = ['today', 'coach', 'nutrition', 'insights', 'setup'];
export const SCREENS: OpenScreen[] = ['live', 'agenda', 'intake', 'research', 'trace', 'knows', 'decisions', 'roundtable', 'scorecard', 'review', 'server', 'push', 'diet'];

/** Server JSON -> a VoiceAction, or null when it is unknown/malformed (never throw on server data). */
export function parseAction(a: any): VoiceAction | null {
  if (!a || typeof a !== 'object' || typeof a.type !== 'string') return null;
  switch (a.type) {
    case 'navigate': return TABS.includes(a.tab) ? { type: 'navigate', tab: a.tab } : null;
    case 'open': return SCREENS.includes(a.screen) ? { type: 'open', screen: a.screen } : null;
    case 'close': return { type: 'close' };
    case 'refresh': return { type: 'refresh', what: Array.isArray(a.what) ? a.what.filter((x: any) => ['today', 'nutrition', 'plan', 'decisions', 'profile', 'all'].includes(x)) : ['all'] };
    case 'nutrition_day': return typeof a.day === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(a.day) ? { type: 'nutrition_day', day: a.day } : null;
    case 'insights_segment': return typeof a.seg === 'string' && a.seg ? { type: 'insights_segment', seg: a.seg } : null;
    case 'rest': return Number.isFinite(a.seconds) && a.seconds > 0 && a.seconds <= 900 ? { type: 'rest', seconds: Math.round(a.seconds) } : null;
    case 'rest_add': return Number.isFinite(a.seconds) && a.seconds > 0 && a.seconds <= 300 ? { type: 'rest_add', seconds: Math.round(a.seconds) } : null;
    case 'rest_skip': return { type: 'rest_skip' };
    case 'end': return { type: 'end' };
    case 'setting': return ['wake', 'vibration', 'voice_mode', 'persona'].includes(a.key) && (typeof a.value === 'string' || typeof a.value === 'boolean') ? { type: 'setting', key: a.key, value: a.value } : null;
    default: return null;
  }
}

type Handler = (a: VoiceAction) => boolean | void;          // return true when handled (stops the action waiting in the queue)
const handlers = new Set<Handler>();
let queue: Array<{ a: VoiceAction; at: number }> = [];
const TTL_MS = 12000;

const fresh = (now: number) => { queue = queue.filter((q) => now - q.at < TTL_MS); };

/** Subscribe to actions. Returns an unsubscribe function. */
export function onVoiceAction(fn: Handler): () => void {
  handlers.add(fn);
  return () => { handlers.delete(fn); };
}

/** Hand one action to the UI. Unhandled actions wait up to 12 s for a screen that mounts later. */
export function emitVoiceAction(a: VoiceAction, now: number = Date.now()): void {
  let handled = false;
  for (const h of Array.from(handlers)) {
    try { if (h(a) === true) handled = true; } catch { /* a broken listener must not block the others */ }
  }
  fresh(now);
  if (!handled) queue.push({ a, at: now });
}

/** Parse + emit a list straight from the server response, in order. Unknown entries are skipped. Returns how many were valid. */
export function emitVoiceActions(raw: any, now: number = Date.now()): number {
  let n = 0;
  for (const r of Array.isArray(raw) ? raw : []) {
    const a = parseAction(r);
    if (a) { emitVoiceAction(a, now); n++; }
  }
  return n;
}

/** A screen calls this when it mounts: removes and returns the waiting actions that match `want`. */
export function takePendingVoiceActions(want: (a: VoiceAction) => boolean, now: number = Date.now()): VoiceAction[] {
  fresh(now);
  const mine = queue.filter((q) => want(q.a)).map((q) => q.a);
  queue = queue.filter((q) => !want(q.a));
  return mine;
}

/** Test helper. */
export function _resetVoiceBus(): void { handlers.clear(); queue = []; }
