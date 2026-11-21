/** The live-voice client (src/streamVoice.ts): protocol, paced playback, interrupting. The last test talks to a REAL voice agent when VOICE_TEST_URL is set. */
import { LEAD_MS, VoiceStream, PcmDevice } from '../src/streamVoice';

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

class FakeWS {
  static last: FakeWS;
  readyState = 0; binaryType = ''; sent: any[] = [];
  onopen: any; onclose: any; onmessage: any; onerror: any;
  constructor(public url: string) { FakeWS.last = this; setTimeout(() => { this.readyState = 1; this.onopen?.(); }, 1); }
  send(d: any) { this.sent.push(d); }
  close() { this.readyState = 3; }
  serverSays(d: any) { this.onmessage?.({ data: d }); }
  texts() { return this.sent.filter((x) => typeof x === 'string').map((x) => JSON.parse(x)); }
  frames() { return this.sent.filter((x) => typeof x !== 'string'); }
}

function device() {
  const d = { played: [] as Uint8Array[], mic: null as null | ((p: Uint8Array) => void), started: 0, stopped: 0 };
  const dev: PcmDevice = { start: (cb) => { d.started++; d.mic = cb; }, stop: () => { d.stopped++; }, play: (p) => { d.played.push(p); } };
  return { d, dev };
}

const audioMsg = (ms: number) => { const b = new Uint8Array(1 + ms * 32); b[0] = 1; return b.buffer; };

test('opens, announces the screen, and forwards microphone audio (not while muted)', async () => {
  const { d, dev } = device();
  const v = new VoiceStream('ws://x/voice/ws?token=t', dev, {}, FakeWS as any);
  await v.open('live');
  expect(FakeWS.last.binaryType).toBe('arraybuffer');
  expect(FakeWS.last.texts()[0]).toEqual({ type: 'screen', screen: 'live' });
  d.mic!(new Uint8Array(640));
  expect(FakeWS.last.frames()).toHaveLength(1);
  v.mute(300);
  d.mic!(new Uint8Array(640));
  expect(FakeWS.last.frames()).toHaveLength(1);                       // muted: nothing sent
  await wait(330);
  d.mic!(new Uint8Array(640));
  expect(FakeWS.last.frames()).toHaveLength(2);
  v.close();
  expect(d.stopped).toBe(1);
});

test('the talk-over choice is sent to the server when the connection opens', async () => {
  const { dev } = device();
  const v = new VoiceStream('ws://x', dev, {}, FakeWS as any);
  await v.open('live', false);
  expect(FakeWS.last.texts()).toContainEqual({ type: 'config', barge_in: false });
  v.close();
});

test('speech is fed to the speaker a little at a time, so a "clear" can stop it within a fraction of a second', async () => {
  const { d, dev } = device();
  const phases: string[] = [];
  const v = new VoiceStream('ws://x', dev, { onPhase: (p) => phases.push(p) }, FakeWS as any);
  await v.open('live');
  FakeWS.last.serverSays(audioMsg(3000));                              // 3 s of coach speech arrives at once
  await wait(60);
  const fed = d.played.reduce((n, c) => n + c.length / 32, 0);
  expect(fed).toBeLessThanOrEqual(LEAD_MS + 130);                      // never much more than the lead is handed over
  expect(fed).toBeGreaterThan(0);
  expect(phases).toContain('speaking');
  FakeWS.last.serverSays(JSON.stringify({ t: 'clear' }));              // you started talking
  const before = d.played.length;
  await wait(500);
  expect(d.played.length).toBe(before);                                // nothing more is fed
  expect(v.speaking).toBe(false);
  expect(phases[phases.length - 1]).toBe('listening');
  v.close();
});

test('words spoken while the socket is still connecting are kept and sent first, in order', async () => {
  class SlowWS extends FakeWS { constructor(u: string) { super(u); this.readyState = 0; setTimeout(() => { this.readyState = 1; this.onopen?.(); }, 120); } }
  const { d, dev } = device();
  const v = new VoiceStream('ws://x', dev, {}, SlowWS as any);
  const opening = v.open('live');
  await wait(20);
  d.mic!(new Uint8Array([1, 1]));
  await wait(30);
  d.mic!(new Uint8Array([2, 2]));
  await opening;
  d.mic!(new Uint8Array([3, 3]));
  const audio = FakeWS.last.frames().map((f: any) => Array.from(new Uint8Array(f.buffer ? f.buffer.slice(f.byteOffset, f.byteOffset + f.byteLength) : f)));
  expect(audio).toEqual([[1, 1], [2, 2], [3, 3]]);
  v.close();
});

test('a tap interrupts: queue dropped and the server is told', async () => {
  const { d, dev } = device();
  const v = new VoiceStream('ws://x', dev, {}, FakeWS as any);
  await v.open('live');
  FakeWS.last.serverSays(audioMsg(2000));
  await wait(30);
  v.interrupt();
  expect(FakeWS.last.texts().some((m) => m.type === 'interrupt')).toBe(true);
  const n = d.played.length;
  await wait(400);
  expect(d.played.length).toBe(n);
  v.close();
});

test('transcripts and the coach result reach the app; the result keeps its actions', async () => {
  const { dev } = device();
  const users: string[] = []; const bots: any[] = [];
  const v = new VoiceStream('ws://x', dev, { onUser: (t) => users.push(t), onBot: (r) => bots.push(r) }, FakeWS as any);
  await v.open('live');
  FakeWS.last.serverSays(JSON.stringify({ t: 'user', text: 'I did squat 60 for 5' }));
  FakeWS.last.serverSays(JSON.stringify({ t: 'bot', text: 'Nice.', intent: 'set', result: { reply: 'Nice.', intent: 'set', actions: [{ type: 'refresh' }], expects: null } }));
  expect(users).toEqual(['I did squat 60 for 5']);
  expect(bots[0].actions).toEqual([{ type: 'refresh' }]);
  v.close();
});

test('an unreachable voice agent rejects and reports "unavailable" so the app can use its built-in voice', async () => {
  class DeadWS { onclose: any; onerror: any; constructor() { setTimeout(() => this.onclose?.({ code: 1006 }), 1); } send() {} close() {} }
  const { d, dev } = device();
  const closes: string[] = [];
  const v = new VoiceStream('ws://x', dev, { onClose: (r) => closes.push(r) }, DeadWS as any);
  await expect(v.open('live')).rejects.toThrow();
  expect(closes).toEqual(['unavailable']);
  expect(d.stopped).toBe(1);                                            // the microphone it had started for the connection is released again
});

test('a refused key (4401) after connecting is reported as denied', async () => {
  const { dev } = device();
  const closes: string[] = [];
  const v = new VoiceStream('ws://x', dev, { onClose: (r) => closes.push(r) }, FakeWS as any);
  await v.open('live');
  FakeWS.last.onclose({ code: 4401 });
  expect(closes).toEqual(['denied']);
});

const URL = process.env.VOICE_TEST_URL;
(URL ? test : test.skip)('REAL voice agent: spoken "I just did a set of squat" is understood and the coach asks for the numbers, out loud', async () => {
  const fs = require('fs');
  const WS = require('ws');
  const wav = fs.readFileSync('D:/voice-proto/audio/c_missing.wav').subarray(44);          // PCM16 mono 16 kHz
  const played: number[] = [];
  let micCb: ((p: Uint8Array) => void) | null = null;
  const dev: PcmDevice = { start: (cb) => { micCb = cb; }, stop: () => {}, play: (p) => { played.push(Date.now()); } };
  const bots: any[] = []; const heard: string[] = [];
  const v = new VoiceStream(URL as string, dev, { onBot: (r) => bots.push(r), onUser: (t) => heard.push(t) }, WS);
  await v.open('live');
  await wait(500);
  const chunk = 640;
  let ended = 0;
  for (let i = 0; i < wav.length; i += chunk) { micCb!(new Uint8Array(wav.subarray(i, i + chunk))); await wait(20); }
  ended = Date.now();
  const t = Date.now();
  while (!bots.length && Date.now() - t < 12000) { micCb!(new Uint8Array(chunk)); await wait(20); }
  while (!played.length && Date.now() - t < 12000) { micCb!(new Uint8Array(chunk)); await wait(20); }
  v.close();
  expect(heard[0].toLowerCase()).toContain('squat');
  expect(bots[0].intent).toBe('ask');
  expect(bots[0].reply).toMatch(/weight|reps|numbers/i);
  expect(played.length).toBeGreaterThan(0);
  console.log(`first coach audio ${(played[0] - ended)} ms after the last word`);
  expect(played[0] - ended).toBeLessThan(4500);
}, 40000);
