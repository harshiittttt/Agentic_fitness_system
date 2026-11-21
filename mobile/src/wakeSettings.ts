// "Hey coach" setting (persisted, default ON) plus a live status the Setup screen can show.
import AsyncStorage from '@react-native-async-storage/async-storage';

const KEY = 'wake_coach_v1';

export type WakeStatus =
  | 'off'          // switched off by the user
  | 'unavailable'  // no native speech module (Expo Go) or no recognizer on the phone
  | 'denied'       // microphone / speech permission refused
  | 'listening'    // waiting for the phrase
  | 'paused';      // app in background, coach talking, or another screen is using the mic
export type WakeState = { enabled: boolean; status: WakeStatus; onDevice: boolean | null; note: string };

let state: WakeState = { enabled: true, status: 'paused', onDevice: null, note: '' };
const subs = new Set<(s: WakeState) => void>();
const emit = () => subs.forEach((f) => { try { f(state); } catch { /* ignore */ } });

export const getWakeState = () => state;
export const onWakeState = (fn: (s: WakeState) => void) => { subs.add(fn); return () => { subs.delete(fn); }; };
export function patchWakeState(p: Partial<WakeState>) {
  const next = { ...state, ...p };
  if (next.enabled === state.enabled && next.status === state.status && next.onDevice === state.onDevice && next.note === state.note) return;
  state = next; emit();
}

export async function loadWakeSetting(): Promise<boolean> {
  try {
    const v = await AsyncStorage.getItem(KEY);
    if (v === '0') patchWakeState({ enabled: false });
    else if (v === '1') patchWakeState({ enabled: true });
  } catch { /* keep default (on) */ }
  return state.enabled;
}

export async function setWakeEnabled(on: boolean): Promise<void> {
  patchWakeState({ enabled: on, ...(on ? {} : { status: 'off' as WakeStatus }) });
  try { await AsyncStorage.setItem(KEY, on ? '1' : '0'); } catch { /* applies for this session */ }
}
