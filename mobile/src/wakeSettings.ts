// "Hey coach" (hands-free wake phrase) setting, plus a live status and diagnostics the Profile screen can show.
//
// DEFAULT IS OFF since v0.3: the phrase is matched on an always-on speech recognizer (not a hardware wake word), which is unreliable on many
// phones. Tap-to-talk (voiceTalk.ts) is the main way to use voice. The phrase stays as an opt-in extra, and the diagnostics show exactly what
// the recognizer hears, so when it does not trigger you can see why instead of guessing.
import AsyncStorage from '@react-native-async-storage/async-storage';

const KEY = 'wake_coach_v2';

export type WakeStatus =
  | 'off'          // switched off by the user (the default)
  | 'unavailable'  // no native speech module (Expo Go) or no recognizer on the phone
  | 'denied'       // microphone / speech permission refused
  | 'listening'    // waiting for the phrase
  | 'paused';      // app in background, coach talking, or another screen is using the mic
export type WakeDiag = { sessions: number; results: number; lastHeard: string; lastHeardAt: number; lastMatch: number; lastError: string };
export type WakeState = { enabled: boolean; status: WakeStatus; onDevice: boolean | null; note: string; diag: WakeDiag };

const EMPTY: WakeDiag = { sessions: 0, results: 0, lastHeard: '', lastHeardAt: 0, lastMatch: 0, lastError: '' };
let state: WakeState = { enabled: false, status: 'off', onDevice: null, note: '', diag: EMPTY };
const subs = new Set<(s: WakeState) => void>();
const emit = () => subs.forEach((f) => { try { f(state); } catch { /* ignore */ } });

export const getWakeState = () => state;
export const onWakeState = (fn: (s: WakeState) => void) => { subs.add(fn); return () => { subs.delete(fn); }; };
export function patchWakeState(p: Partial<WakeState>) {
  const next = { ...state, ...p };
  if (next.enabled === state.enabled && next.status === state.status && next.onDevice === state.onDevice && next.note === state.note && next.diag === state.diag) return;
  state = next; emit();
}
/** Update the diagnostics (cheap). */
export function patchDiag(p: Partial<WakeDiag>) { patchWakeState({ diag: { ...state.diag, ...p } }); }
export const resetDiag = () => patchWakeState({ diag: EMPTY });

export async function loadWakeSetting(): Promise<boolean> {
  try {
    const v = await AsyncStorage.getItem(KEY);
    if (v === '1') patchWakeState({ enabled: true, status: 'paused' });
  } catch { /* keep default (off) */ }
  return state.enabled;
}

export async function setWakeEnabled(on: boolean): Promise<void> {
  patchWakeState({ enabled: on, status: on ? 'paused' : 'off', ...(on ? { diag: EMPTY } : {}) });
  try { await AsyncStorage.setItem(KEY, on ? '1' : '0'); } catch { /* applies for this session */ }
}
