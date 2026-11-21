// Pure helpers for the server address and the authenticated fetch. No React Native imports, so scripts/test_server.js can run them under Node.

const PRIVATE_HOST = /^(localhost|127(\.\d{1,3}){3}|10(\.\d{1,3}){3}|192\.168(\.\d{1,3}){2}|172\.(1[6-9]|2\d|3[01])(\.\d{1,3}){2}|169\.254(\.\d{1,3}){2}|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])(\.\d{1,3}){2}|10\.0\.2\.2|[^.]+\.local|\[?::1\]?)$/i;

/** Clean what a person typed into a base URL: trimmed, scheme added (http for localhost / LAN, https for anything else), no trailing slash. '' when unusable. */
export function normalizeUrl(input: string | null | undefined): string {
  let s = String(input ?? '').trim().replace(/\s+/g, '');
  if (!s) return '';
  const m = /^([a-z][a-z0-9+.-]*):\/\//i.exec(s);
  if (m) {
    if (!/^https?$/i.test(m[1])) return '';
    s = m[1].toLowerCase() + s.slice(m[1].length);
  } else {
    const host = s.split(/[/?#]/)[0].replace(/:\d+$/, '');
    s = (PRIVATE_HOST.test(host) ? 'http://' : 'https://') + s;
  }
  s = s.replace(/[?#].*$/, '').replace(/\/+$/, '');
  return /^https?:\/\/[^/]+/i.test(s) ? s : '';
}

/** A bearer token pasted from a chat often carries spaces or a trailing newline; header values may not. */
export const cleanToken = (t: string | null | undefined) => String(t ?? '').replace(/\s+/g, '');

/** True when `url` is the base itself or lives under it (so `https://a.com` never matches `https://a.com.evil.io`). */
export function isSameBase(url: string, base: string): boolean {
  if (!base || !url) return false;
  const u = url.toLowerCase(), b = base.toLowerCase();
  if (!u.startsWith(b)) return false;
  const next = u.charAt(b.length);
  return next === '' || next === '/' || next === '?' || next === '#';
}

type FetchFn = (input: any, init?: any) => Promise<any>;

/** Wrap a fetch so requests to the current server get `Authorization: Bearer <token>`; other hosts are passed through untouched. A 401 from the server calls on401. */
export function makeAuthFetch(inner: FetchFn, getBase: () => string, getToken: () => string, on401: () => void): FetchFn {
  return async (input, init) => {
    const url = typeof input === 'string' ? input : input && typeof input === 'object' && 'href' in input ? String(input.href) : String(input?.url ?? '');
    const mine = isSameBase(url, getBase());
    const token = getToken();
    let next = init;
    if (mine && token) {
      const h = new Headers(input && typeof input === 'object' && 'headers' in input ? input.headers : undefined);
      new Headers(init?.headers).forEach((v: string, k: string) => h.set(k, v));
      if (!h.has('authorization')) h.set('Authorization', `Bearer ${token}`);
      next = { ...(init ?? {}), headers: h };
    }
    const res = await inner(input, next);
    if (mine && res && res.status === 401) { try { on401(); } catch { /* listener errors must not break the request */ } }
    return res;
  };
}
