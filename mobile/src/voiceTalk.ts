// Tap-to-talk, the reliable way to use the coach by voice. A button (in the tab bar) asks WakeCoach to listen ONCE; WakeCoach reports what is
// happening here so the button and a small "what I heard" bubble can show it. Pure TypeScript (no React / native imports) so it runs under plain Node.
//
//   idle -> listening (live transcript while you speak) -> thinking (sent to the coach) -> speaking (the answer) -> idle
//   If the coach asks a question the cycle runs again by itself ('followup'), so a conversation needs one tap, not a wake phrase.
export type TalkPhase = 'idle' | 'listening' | 'thinking' | 'speaking' | 'followup';
export type TalkState = { phase: TalkPhase; heard: string; note: string; error: boolean };

let state: TalkState = { phase: 'idle', heard: '', note: '', error: false };
const subs = new Set<(s: TalkState) => void>();
let starter: (() => void) | null = null;
let stopper: (() => void) | null = null;

export const getTalkState = () => state;
export function setTalkState(p: Partial<TalkState>) {
  const next = { ...state, ...p };
  if (next.phase === state.phase && next.heard === state.heard && next.note === state.note && next.error === state.error) return;
  state = next;
  subs.forEach((f) => { try { f(state); } catch { /* a bad subscriber must not break talking */ } });
}
export const onTalkState = (fn: (s: TalkState) => void) => { subs.add(fn); return () => { subs.delete(fn); }; };

/** WakeCoach registers how to start / stop a listening turn. */
export function registerTalk(start: () => void, stop: () => void) {
  starter = start; stopper = stop;
  return () => { if (starter === start) { starter = null; stopper = null; } };
}
/** The button: starts a turn when idle, stops the current one otherwise. Returns false when nothing is registered (e.g. Expo Go). */
export function toggleTalk(): boolean {
  if (!starter) return false;
  if (state.phase === 'idle') starter(); else stopper?.();
  return true;
}
export const talkAvailable = () => !!starter;
