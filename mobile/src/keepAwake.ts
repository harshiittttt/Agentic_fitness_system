// Keeps the screen on while the workout screen is open: a sleeping screen takes the app to the background, which ends the voice session (E-007).
// expo-keep-awake is a native module: an older installed build may not have it, so everything here is guarded and silent.
let KA: any = null;
try { KA = require('expo-keep-awake'); } catch { KA = null; }

export function keepScreenOn(on: boolean, tag = 'live-workout'): boolean {
  if (!KA) return false;
  try {
    if (on) { const p = KA.activateKeepAwakeAsync?.(tag); if (p && typeof p.catch === 'function') p.catch(() => {}); }
    else KA.deactivateKeepAwake?.(tag);
    return true;
  } catch { return false; }
}
