// The voice session, seen from the screens. One engine (WakeCoach) owns the microphone; screens only start / stop it, read its state, give it hints
// and receive the coach's replies. Pure TypeScript (no React / native imports) so it runs under plain Node.
//
//   tap -> SESSION on: the microphone stays on for the whole conversation (no restarts, no beeps).
//     listening  your words appear live; the app (not the phone's recognizer) decides when you are finished, waiting longer after "uhh" / "like"
//     thinking   sent to the coach (a short "one sec" is spoken if the answer is slow)
//     speaking   the answer; the microphone is muted meanwhile so the coach never hears itself
//     back to listening, by itself.  Tap again to stop.
export type TalkPhase = 'idle' | 'listening' | 'thinking' | 'speaking' | 'followup';
/** `endReason`: why the LAST session ended ('' = the user stopped it, or none yet) so a dead session is never silent. `connecting`: live voice is still opening its connection. */
export type TalkState = { phase: TalkPhase; heard: string; note: string; error: boolean; session: boolean; endReason: string; connecting: boolean };

let state: TalkState = { phase: 'idle', heard: '', note: '', error: false, session: false, endReason: '', connecting: false };
const subs = new Set<(s: TalkState) => void>();
let starter: (() => void) | null = null;
let stopper: ((why?: string) => void) | null = null;
let muter: ((ms: number, exact?: boolean) => void) | null = null;
let skipper: (() => void) | null = null;
const replySubs = new Set<(r: any) => void>();

export const getTalkState = () => state;
export function setTalkState(p: Partial<TalkState>) {
  const next = { ...state, ...p };
  if (next.phase === state.phase && next.heard === state.heard && next.note === state.note && next.error === state.error && next.session === state.session && next.endReason === state.endReason && next.connecting === state.connecting) return;
  state = next;
  subs.forEach((f) => { try { f(state); } catch { /* a bad subscriber must not break talking */ } });
}
export const onTalkState = (fn: (s: TalkState) => void) => { subs.add(fn); return () => { subs.delete(fn); }; };

/** WakeCoach registers how to start / stop a voice session and how to mute the microphone for a while. */
export function registerTalk(start: () => void, stop: (why?: string) => void, mute?: (ms: number, exact?: boolean) => void, skip?: () => void) {
  starter = start; stopper = stop; muter = mute ?? null; skipper = skip ?? null;
  return () => { if (starter === start) { starter = null; stopper = null; muter = null; skipper = null; } };
}
/** The button: one tap starts a voice SESSION, the next tap stops it. Returns false when nothing is registered (e.g. Expo Go). */
export function toggleTalk(): boolean {
  if (!starter) return false;
  if (state.session && state.phase === 'speaking' && skipper) skipper();          // the coach is talking: the tap cuts the sentence short and keeps listening
  else if (state.session || state.phase !== 'idle') stopper?.('tap'); else starter();
  return true;
}
export const talkAvailable = () => !!starter;
/** `why` goes to the diagnostics log: 'screen' = the workout screen closed (a leave the user may not have meant), 'tap' = the mic button. */
export const stopTalk = (why = 'screen') => { if (state.session || state.phase !== 'idle') stopper?.(why); };
/** Start a session again (the one-tap "Resume voice"). false when no engine is registered (Expo Go). */
export const resumeTalk = (): boolean => { if (!starter) return false; if (!state.session) starter(); return true; };
/** A screen that speaks by itself (a button press) tells the engine to ignore the microphone, so the coach's own voice is not heard as the user's. Call it with a
 *  generous `ms` when speech starts, and with a short `ms` and exact=true when it ends: the mute then ends exactly there (not at the generous estimate). */
export const muteVoice = (ms: number, exact = false) => { muter?.(ms, exact); };

/** Every answer from the server for a voice turn, so the screen the user is on (the workout) can update itself (sets, rest timer) while the engine speaks. */
export const onVoiceReply = (fn: (r: any) => void) => { replySubs.add(fn); return () => { replySubs.delete(fn); }; };
export const hasReplyListener = () => replySubs.size > 0;
export function emitVoiceReply(r: any) { replySubs.forEach((f) => { try { f(r); } catch { /* ignore */ } }); }

/** Words that help the recognizer (exercise names, gym vocabulary). Screens set them; the engine passes them to the speech service. */
const DEFAULT_HINTS = ['warm up', 'bench press', 'squat', 'deadlift', 'overhead press', 'lat pulldown', 'barbell row', 'reps', 'kilos', 'set', 'skip', 'next exercise', 'rest', 'protein', 'calories'];
let hints: string[] = DEFAULT_HINTS;
export const setVoiceHints = (w: string[]) => { hints = Array.from(new Set([...DEFAULT_HINTS, ...w.map((x) => x.toLowerCase())])).slice(0, 80); };
export const getVoiceHints = () => hints;
