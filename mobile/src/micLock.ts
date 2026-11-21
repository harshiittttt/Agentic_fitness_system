// A tiny single-owner microphone lock: only one speech recognizer may run at a time.
// Two kinds of owner:
//   'ui'   a screen the user is actively talking to (push-to-talk, Live). Always wins: it may take the lock from 'wake'.
//   'wake' the always-on "Hey coach" listener. Never takes the lock from anyone; it waits and resumes on release.
// Subscribers are told whenever the owner changes, so the wake listener can stop (or resume) at once.
export type MicOwner = 'ui' | 'wake';

let owner: MicOwner | null = null;
let count = 0;                       // 'ui' can be held by several hooks at once (Live + a button): count them
const subs = new Set<() => void>();
const emit = () => subs.forEach((f) => { try { f(); } catch { /* a bad subscriber must not break the lock */ } });

export const micOwner = () => owner;

/** Try to take the lock. 'ui' always succeeds (and evicts 'wake'); 'wake' only if nobody holds it. */
export function acquireMic(who: MicOwner): boolean {
  if (who === 'ui') { count++; if (owner !== 'ui') { owner = 'ui'; emit(); } return true; }
  if (owner === null) { owner = 'wake'; emit(); return true; }
  return owner === 'wake';
}

export function releaseMic(who: MicOwner) {
  if (who === 'ui') { if (count > 0) count--; if (count === 0 && owner === 'ui') { owner = null; emit(); } return; }
  if (owner === 'wake') { owner = null; emit(); }
}

export function onMicChange(fn: () => void): () => void {
  subs.add(fn);
  return () => { subs.delete(fn); };
}
