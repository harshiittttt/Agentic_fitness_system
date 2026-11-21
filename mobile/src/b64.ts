/** Base64 <-> bytes, no dependencies (the native audio module carries PCM as base64 text). */
const ABC = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
const LOOKUP: Record<string, number> = {};
for (let i = 0; i < ABC.length; i++) LOOKUP[ABC[i]] = i;

export function b64ToBytes(s: string): Uint8Array {
  const clean = (s || '').replace(/[^A-Za-z0-9+/]/g, '');
  const out = new Uint8Array(Math.floor((clean.length * 3) / 4));
  let o = 0;
  for (let i = 0; i < clean.length; i += 4) {
    const a = LOOKUP[clean[i]], b = LOOKUP[clean[i + 1]], c = clean[i + 2] !== undefined ? LOOKUP[clean[i + 2]] : -1, d = clean[i + 3] !== undefined ? LOOKUP[clean[i + 3]] : -1;
    if (o < out.length) out[o++] = (a << 2) | (b >> 4);
    if (c >= 0 && o < out.length) out[o++] = ((b & 15) << 4) | (c >> 2);
    if (d >= 0 && o < out.length) out[o++] = ((c & 3) << 6) | d;
  }
  return out;
}

export function bytesToB64(bytes: Uint8Array): string {
  let s = '';
  for (let i = 0; i < bytes.length; i += 3) {
    const a = bytes[i], b = i + 1 < bytes.length ? bytes[i + 1] : 0, c = i + 2 < bytes.length ? bytes[i + 2] : 0;
    s += ABC[a >> 2] + ABC[((a & 3) << 4) | (b >> 4)] + (i + 1 < bytes.length ? ABC[((b & 15) << 2) | (c >> 6)] : '=') + (i + 2 < bytes.length ? ABC[c & 63] : '=');
  }
  return s;
}
