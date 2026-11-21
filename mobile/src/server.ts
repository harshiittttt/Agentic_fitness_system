import * as SecureStore from 'expo-secure-store';
import { DEFAULT_API, API, getApiToken, notifyAuthFailure, setApiConfig } from './api';
import { cleanToken, makeAuthFetch, normalizeUrl } from './serverCore';

// The server address and access key are typed in the app (one APK serves every person), and kept in the phone's secure store
// (Android Keystore / iOS Keychain). Nothing saved => EXPO_PUBLIC_API_URL (local development, no key).
const KEY = 'coach_server_v1';

export type Server = { url: string; token: string; saved: boolean };

/** Read the saved server (if any) and make the app use it. Call once at startup, before the first request. */
export async function loadServer(): Promise<Server> {
  try {
    const raw = await SecureStore.getItemAsync(KEY);
    if (raw) {
      const j = JSON.parse(raw);
      const url = normalizeUrl(j?.url);
      if (url) { const token = cleanToken(j?.token); setApiConfig(url, token); return { url, token, saved: true }; }
    }
  } catch { /* secure store unavailable or unreadable: behave as "nothing saved" */ }
  setApiConfig('', '');
  return { url: DEFAULT_API, token: '', saved: false };
}

let savedFlag = false;
export const currentServer = (): Server => ({ url: API, token: getApiToken(), saved: savedFlag });

/** Save and switch to a new server. Throws if the address is unusable. If the secure store fails the choice still applies for this session. */
export async function saveServer(urlInput: string, tokenInput: string): Promise<Server> {
  const url = normalizeUrl(urlInput);
  if (!url) throw new Error('That does not look like a server address. Example: https://my-coach.example.com');
  const token = cleanToken(tokenInput);
  setApiConfig(url, token);
  savedFlag = true;
  await SecureStore.setItemAsync(KEY, JSON.stringify({ url, token })).catch(() => {});
  return { url, token, saved: true };
}

/** Forget the saved server and key; the app goes back to the development default. */
export async function clearServer(): Promise<void> {
  setApiConfig('', '');
  savedFlag = false;
  await SecureStore.deleteItemAsync(KEY).catch(() => {});
}

/** Call after loadServer so currentServer().saved is right. */
export const markLoaded = (s: Server) => { savedFlag = s.saved; };

// ---- the one authenticated fetch ---------------------------------------------------------------------------------------
const baseFetch: typeof fetch = globalThis.fetch.bind(globalThis);
let installed = false;

/** Wrap global fetch once so every request to the current server carries the bearer key. Other hosts never see it. */
export function installAuthFetch() {
  if (installed) return;
  installed = true;
  globalThis.fetch = makeAuthFetch(baseFetch as any, () => API, getApiToken, notifyAuthFailure) as any;
}

async function timed(url: string, init: RequestInit, ms = 8000) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), ms);
  try { return await baseFetch(url, { ...init, signal: ctl.signal }); } finally { clearTimeout(t); }
}

/** Check a candidate server without touching saved state: /health (open), then an endpoint that needs the key. */
export async function testConnection(urlInput: string, tokenInput: string): Promise<{ ok: boolean; message: string }> {
  const url = normalizeUrl(urlInput);
  if (!url) return { ok: false, message: 'That does not look like a server address. Example: https://my-coach.example.com' };
  const token = cleanToken(tokenInput);
  try {
    const h = await timed(`${url}/health`, {});
    if (!h.ok) return { ok: false, message: `The server answered ${h.status} on /health. Is that the coach address?` };
  } catch (e: any) {
    return { ok: false, message: e?.name === 'AbortError' ? `No reply from ${url} after 8 seconds.` : `Cannot reach ${url}. Check the address and that the server is on.` };
  }
  try {
    const r = await timed(`${url}/agents/status`, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
    if (r.status === 401) return { ok: false, message: token ? 'The server is there, but it rejected that access key.' : 'The server is there, but it needs an access key.' };
    if (!r.ok) return { ok: false, message: `The server is there, but answered ${r.status}.` };
    return { ok: true, message: 'Connected.' };
  } catch {
    return { ok: false, message: 'The server went quiet while checking the key. Try again.' };
  }
}
