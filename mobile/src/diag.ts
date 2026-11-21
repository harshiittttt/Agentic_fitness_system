// The phone's black-box recorder: short event records (voice recognizer events, screen open/close, crashes) sent to the user's own server
// (POST /diag/log) so what happens on the device can be read from the server log. Text only, no audio, bounded, never throws, never blocks.
import { Platform } from 'react-native';
import Constants from 'expo-constants';
import { API } from './api';

const buf: { t: number; kind: string; data?: any }[] = [];
let timer: ReturnType<typeof setTimeout> | null = null;
let sending = false;
const APP = `${Constants.expoConfig?.version ?? '?'}/${Platform.OS}${Platform.Version ? ' ' + Platform.Version : ''}`;

const clip = (d: any) => {
  try {
    if (d == null) return d;
    if (typeof d === 'string') return d.slice(0, 300);
    const s = JSON.stringify(d);
    return s.length > 300 ? s.slice(0, 300) : d;
  } catch { return String(d).slice(0, 200); }
};

async function flush() {
  timer = null;
  if (sending || !buf.length) return;
  sending = true;
  const batch = buf.splice(0, 100);
  try {
    await fetch(`${API}/diag/log`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ app: APP, events: batch }) });
  } catch { /* offline: drop it; diagnostics must never get in the way */ }
  sending = false;
  if (buf.length) timer = setTimeout(flush, 1500);
}

/** Record one event. `data` is clipped; sent within ~1.5 s. */
export function dlog(kind: string, data?: any) {
  try {
    buf.push({ t: Date.now(), kind: kind.slice(0, 40), data: clip(data) });
    if (buf.length > 300) buf.splice(0, buf.length - 300);
    if (!timer) timer = setTimeout(flush, 1500);
  } catch { /* never throw */ }
}

let installed = false;
/** Report uncaught JS errors (a crash that "restarts" the app is one of these), then let the normal handler run. */
export function installCrashLog() {
  if (installed) return;
  installed = true;
  dlog('app_start', APP);
  try {
    const EU = (globalThis as any).ErrorUtils;
    const prev = EU?.getGlobalHandler?.();
    EU?.setGlobalHandler?.((e: any, fatal?: boolean) => {
      dlog('js_error', { fatal: !!fatal, msg: String(e?.message ?? e).slice(0, 160), stack: String(e?.stack ?? '').split('\n').slice(0, 4).join(' | ').slice(0, 220) });
      if (timer) clearTimeout(timer);
      flush();
      prev?.(e, fatal);
    });
  } catch { /* no ErrorUtils: nothing to hook */ }
}
