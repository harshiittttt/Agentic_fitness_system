/** The mic button with LIVE VOICE (streamed audio to the PC voice agent) through the real WakeCoach, with a fake audio module and a fake server socket. */
import React from 'react';
import { act, render, waitFor } from '@testing-library/react-native';
import { AppState } from 'react-native';
import { makeBackend } from './fakeBackend';

jest.mock('../src/tts', () => ({ speak: jest.fn(async (_t: string, _c: any, done?: () => void) => { if (done) setTimeout(done, 0); }), stopSpeaking: jest.fn() }));
jest.mock('../src/haptics', () => ({ impactAsync: jest.fn(), notificationAsync: jest.fn(), selectionAsync: jest.fn(), ImpactFeedbackStyle: {}, NotificationFeedbackType: {}, loadHaptics: jest.fn(), hapticsEnabled: () => true, setHapticsEnabled: jest.fn() }));
const mockAudio: any = { mic: null, err: null, played: [] as any[], recording: false, stops: 0, flushes: 0, silent: false, startError: '', asked: 0, needsAsk: false };
jest.mock('../src/pcmNative', () => ({
  pcmNative: () => ({
    getPermission: async () => ({ granted: !mockAudio.needsAsk }),
    requestPermission: async () => { mockAudio.asked++; mockAudio.needsAsk = false; return { granted: true }; },
    addListener: (name: string, cb: any) => { if (name === 'mic') mockAudio.mic = cb; else mockAudio.err = cb; return { remove: () => { if (name === 'mic') mockAudio.mic = null; } }; },
    start: () => {
      if (mockAudio.startError) return mockAudio.startError;
      mockAudio.recording = true;
      if (!mockAudio.silent) mockAudio.timer = setTimeout(() => mockAudio.mic?.({ b64: 'AAAA' }), 30);        // a real microphone delivers audio right away
      return '';
    },
    stop: () => { mockAudio.stops++; mockAudio.recording = false; },
    play: (b64: string) => { mockAudio.played.push(b64); },
    flush: () => { mockAudio.flushes++; },
    status: () => 'recording=1;frames=0;aec=1;ns=1;queued=0;error=',
  }),
}));

const SR = require('expo-speech-recognition');
import WakeCoach from '../src/WakeCoach';
import { getTalkState, onVoiceReply, setTalkState, stopTalk, toggleTalk } from '../src/voiceTalk';
import { setVoiceOverlay } from '../src/voiceScreen';
import { setApiConfig } from '../src/api';
import { setLiveVoice } from '../src/voiceSettings';
import { micOwner } from '../src/micLock';
import { tuning } from '../src/voiceEndpoint';

class FakeSocket {
  static all: FakeSocket[] = []; static failNext = false;
  readyState = 0; binaryType = ''; sent: any[] = [];
  onopen: any; onclose: any; onmessage: any; onerror: any;
  constructor(public url: string) {
    FakeSocket.all.push(this);
    setTimeout(() => { if (FakeSocket.failNext) { FakeSocket.failNext = false; this.onclose?.({ code: 4503 }); } else { this.readyState = 1; this.onopen?.(); } }, 1);
  }
  send(d: any) { this.sent.push(d); }
  close() { this.readyState = 3; }
  says(d: any) { act(() => { this.onmessage?.({ data: typeof d === 'string' ? d : d }); }); }
  texts() { return this.sent.filter((x) => typeof x === 'string').map((x) => JSON.parse(x)); }
}
const sock = () => FakeSocket.all[FakeSocket.all.length - 1];
const wait = (ms: number) => act(async () => { await new Promise((r) => setTimeout(r, ms)); });
const tap = async () => { await act(async () => { toggleTalk(); }); };
const mounted = async () => { const r = render(<WakeCoach />); await waitFor(() => expect(SR.__listenerCount('result')).toBe(1)); return r; };
// the session is 'on' while the socket is still connecting; a test acts once the microphone is really streaming
const openLive = async () => { (global as any).__dlog.length = 0; await tap(); await waitFor(() => expect(mockAudio.recording).toBe(true)); await waitFor(() => expect((global as any).__dlog.some((e: any[]) => e[0] === 's_open')).toBe(true)); };
const audio = (ms: number) => { const b = new Uint8Array(1 + ms * 32); b[0] = 1; return b.buffer; };

afterAll(() => { tuning.scale = 1; });
beforeEach(() => {
  clearTimeout(mockAudio.timer);                                                      // a frame scheduled by the previous test must not leak into this one
  tuning.scale = 0.02;                                                              // the real 1.8 s connecting guard would slow every test
  (global as any).WebSocket = FakeSocket; FakeSocket.all = []; FakeSocket.failNext = false;
  mockAudio.mic = null; mockAudio.err = null; mockAudio.played = []; mockAudio.recording = false; mockAudio.stops = 0; mockAudio.flushes = 0; mockAudio.silent = false; mockAudio.startError = ''; mockAudio.asked = 0; mockAudio.needsAsk = false;
  setTalkState({ phase: 'idle', heard: '', note: '', error: false, session: false }); SR.__reset(); setVoiceOverlay(null);
  (global as any).fetch = makeBackend().fetch;
  setApiConfig('https://coach.example', 'tok');
  return setLiveVoice(true);
});

test('one tap opens the live voice: a socket to the PC with the access key, the echo-cancelling mic running, and the phone recogniser left alone', async () => {
  await mounted();
  await tap();
  await waitFor(() => expect(getTalkState()).toMatchObject({ phase: 'listening', session: true }));
  expect(sock().url).toBe('wss://coach.example/voice/ws?token=tok');
  expect(sock().texts()[0]).toMatchObject({ type: 'screen' });
  expect(mockAudio.recording).toBe(true);
  expect(SR.__state.starts.length).toBe(0);
  act(() => { mockAudio.mic({ b64: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' }); });
  expect(sock().sent.some((x) => typeof x !== 'string')).toBe(true);               // microphone audio is streamed
  await wait(70);                                                                   // past the (scaled) connecting guard
  await tap();
  await waitFor(() => expect(getTalkState().session).toBe(false));
  expect(mockAudio.stops).toBeGreaterThan(0);                                       // the phone's normal audio mode is given back
  expect(micOwner()).toBe(null);
});

test('what you said shows at once, the coach result reaches the workout screen, and the audio drives the speaking state', async () => {
  await mounted();
  setVoiceOverlay('live');
  const replies: any[] = [];
  const off = onVoiceReply((r: any) => replies.push(r));
  await openLive();
  sock().says(JSON.stringify({ t: 'user', text: 'I did squat 60 for 5' }));
  expect(getTalkState()).toMatchObject({ phase: 'thinking', heard: 'I did squat 60 for 5' });
  sock().says(JSON.stringify({ t: 'bot', text: 'Nice set.', intent: 'set', result: { reply: 'Nice set.', intent: 'set', actions: [], expects: null, state: { x: 1 } } }));
  expect(replies[0]).toMatchObject({ intent: 'set', reply: 'Nice set.' });
  sock().says(audio(600));
  await waitFor(() => expect(getTalkState().phase).toBe('speaking'));
  expect(mockAudio.played.length).toBeGreaterThan(0);
  await waitFor(() => expect(getTalkState().phase).toBe('listening'), { timeout: 3000 });
  off();
  await wait(70);                                                                   // past the (scaled) connecting guard
  await tap();
  await waitFor(() => expect(getTalkState().session).toBe(false));
});

test('a tap while the coach is talking cuts the sentence and keeps the session', async () => {
  await mounted();
  await openLive();
  sock().says(audio(3000));
  await waitFor(() => expect(getTalkState().phase).toBe('speaking'));
  await tap();
  expect(sock().texts().some((m) => m.type === 'interrupt')).toBe(true);
  expect(mockAudio.flushes).toBeGreaterThan(0);                                    // the speaker is emptied at once, not after its buffer drains
  expect(getTalkState()).toMatchObject({ phase: 'listening', session: true });
  await wait(70);                                                                   // past the (scaled) connecting guard
  await tap();
  await waitFor(() => expect(getTalkState().session).toBe(false));
});

test('when the PC voice agent is not reachable the phone recogniser takes over by itself', async () => {
  FakeSocket.failNext = true;
  await mounted();
  await tap();
  await waitFor(() => expect(SR.__state.starts.length).toBe(1), { timeout: 4000 });
  expect(getTalkState()).toMatchObject({ session: true });
  await wait(70);                                                                   // past the (scaled) connecting guard
  await tap();
  await waitFor(() => expect(getTalkState().session).toBe(false));
});

test('switching Live voice off in Profile uses the phone recogniser straight away', async () => {
  await setLiveVoice(false);
  await mounted();
  await tap();
  await waitFor(() => expect(SR.__state.starts.length).toBe(1));
  expect(FakeSocket.all.length).toBe(0);
  await wait(70);                                                                   // past the (scaled) connecting guard
  await tap();
  await waitFor(() => expect(getTalkState().session).toBe(false));
});

test('a dropped connection is retried by itself (twice); after that the session ends with a clear reason', async () => {
  await mounted();
  await openLive();
  const n0 = FakeSocket.all.length;
  act(() => { sock().onclose({ code: 1006 }); });
  await waitFor(() => expect(FakeSocket.all.length).toBe(n0 + 1), { timeout: 3000 });         // reconnected on its own
  await waitFor(() => expect((global as any).__dlog.filter((e: any[]) => e[0] === 's_open').length).toBe(2), { timeout: 3000 });
  act(() => { sock().onclose({ code: 1006 }); });
  await waitFor(() => expect(FakeSocket.all.length).toBe(n0 + 2), { timeout: 5000 });         // and a second time
  await waitFor(() => expect((global as any).__dlog.filter((e: any[]) => e[0] === 's_open').length).toBe(3), { timeout: 3000 });
  act(() => { sock().onclose({ code: 1006 }); });                                              // a third drop within a minute: stop trying and say so
  await waitFor(() => expect(getTalkState()).toMatchObject({ session: false, error: true }));
  expect(getTalkState().endReason).toMatch(/dropped/i);
});

test('connected but the phone delivers NO microphone audio: it is noticed within seconds and the phone recogniser takes over by itself', async () => {
  mockAudio.silent = true;
  await mounted();
  await tap();
  await waitFor(() => expect(SR.__state.starts.length).toBe(1), { timeout: 6000 });
  expect((global as any).__dlog.some((e: any[]) => e[0] === 's_nomic')).toBe(true);
  expect(getTalkState()).toMatchObject({ session: true });
  expect(mockAudio.stops).toBeGreaterThan(0);                                      // the dead audio engine was released
  await wait(70);                                                                   // past the (scaled) connecting guard
  await tap();
  await waitFor(() => expect(getTalkState().session).toBe(false));
}, 15000);

test('when the audio engine cannot start, the exact reason is logged and the phone recogniser is used', async () => {
  mockAudio.startError = 'the microphone could not be opened (permission refused, or another app is using it)';
  await mounted();
  await tap();
  await waitFor(() => expect(SR.__state.starts.length).toBe(1), { timeout: 4000 });
  const why = (global as any).__dlog.find((e: any[]) => e[0] === 's_unavailable');
  expect(why[1]).toContain('microphone could not be opened');
  await wait(70);                                                                   // past the (scaled) connecting guard
  await tap();
  await waitFor(() => expect(getTalkState().session).toBe(false));
});

test('the first microphone audio is logged with its delay, so a silent phone is visible in the server log', async () => {
  await mounted();
  await tap();
  await waitFor(() => expect((global as any).__dlog.some((e: any[]) => e[0] === 's_mic')).toBe(true));
  await wait(70);                                                                   // past the (scaled) connecting guard
  await tap();
  await waitFor(() => expect(getTalkState().session).toBe(false));
});

test('extra taps while live voice is still connecting do not cancel it', async () => {
  tuning.scale = 1;
  await mounted();
  await tap();
  await tap();                                                                      // an impatient second tap
  await tap();
  await waitFor(() => expect((global as any).__dlog.some((e: any[]) => e[0] === 's_open')).toBe(true));
  expect(getTalkState()).toMatchObject({ session: true });
  expect(mockAudio.recording).toBe(true);
  await wait(1900);                                                                 // after the connecting window a tap stops it as usual
  await wait(70);                                                                   // past the (scaled) connecting guard
  await tap();
  await waitFor(() => expect(getTalkState().session).toBe(false));
});

test('REGRESSION (found on the real phone): an already-granted microphone permission is NOT asked for again, so no permission screen flashes over the app at every tap', async () => {
  await mounted();
  await openLive();
  expect(mockAudio.asked).toBe(0);
  await wait(70);
  await tap();
  await waitFor(() => expect(getTalkState().session).toBe(false));
  mockAudio.needsAsk = true;                                                        // first ever use: it IS asked, once
  await openLive();
  expect(mockAudio.asked).toBe(1);
});

test('REGRESSION: the app flickering to "inactive" (a system screen over it) does not end a live-voice session; really going to the background does', async () => {
  const listeners: ((s: string) => void)[] = [];
  const spy = jest.spyOn(AppState, 'addEventListener').mockImplementation(((_t: string, cb: any) => { listeners.push(cb); return { remove: () => {} }; }) as any);
  try {
    await mounted();
    await openLive();
    act(() => { listeners.forEach((f) => f('inactive')); });
    act(() => { listeners.forEach((f) => f('active')); });
    expect(getTalkState()).toMatchObject({ session: true });
    expect(mockAudio.recording).toBe(true);
    act(() => { listeners.forEach((f) => f('background')); });
    await waitFor(() => expect(getTalkState().session).toBe(false));
    expect(getTalkState().endReason).toMatch(/left the app/i);                                  // never silent: the reason is in the state (the workout screen shows it)
    const n0 = FakeSocket.all.length;
    act(() => { listeners.forEach((f) => f('active')); });                                       // back within a few minutes: the voice carries on by itself
    await waitFor(() => expect(FakeSocket.all.length).toBe(n0 + 1), { timeout: 3000 });
    await waitFor(() => expect(getTalkState()).toMatchObject({ session: true, endReason: '' }));
  } finally { spy.mockRestore(); }
});

test('away for longer than a few minutes: no surprise microphone, a one-tap resume is offered instead', async () => {
  const listeners: ((s: string) => void)[] = [];
  const spy = jest.spyOn(AppState, 'addEventListener').mockImplementation(((_t: string, cb: any) => { listeners.push(cb); return { remove: () => {} }; }) as any);
  const realNow = Date.now;
  try {
    await mounted();
    await openLive();
    act(() => { listeners.forEach((f) => f('background')); });
    await waitFor(() => expect(getTalkState().session).toBe(false));
    const n0 = FakeSocket.all.length;
    Date.now = () => realNow() + 10 * 60 * 1000;
    act(() => { listeners.forEach((f) => f('active')); });
    await wait(900);
    expect(FakeSocket.all.length).toBe(n0);
    expect(getTalkState()).toMatchObject({ session: false });
    expect(getTalkState().endReason).toMatch(/Tap the mic to resume/i);
  } finally { Date.now = realNow; spy.mockRestore(); }
});

test('a stop by the user (tap) leaves no reason and never resumes by itself; closing the workout screen says why', async () => {
  const listeners: ((s: string) => void)[] = [];
  const spy = jest.spyOn(AppState, 'addEventListener').mockImplementation(((_t: string, cb: any) => { listeners.push(cb); return { remove: () => {} }; }) as any);
  try {
    await mounted();
    await openLive();
    await wait(70);
    await tap();
    await waitFor(() => expect(getTalkState().session).toBe(false));
    expect(getTalkState().endReason).toBe('');
    const n0 = FakeSocket.all.length;
    act(() => { listeners.forEach((f) => f('background')); listeners.forEach((f) => f('active')); });
    await wait(900);
    expect(FakeSocket.all.length).toBe(n0);                                                      // stopped on purpose: stays stopped
    await openLive();
    await wait(70);
    act(() => { stopTalk('screen'); });
    await waitFor(() => expect(getTalkState().session).toBe(false));
    expect(getTalkState().endReason).toMatch(/workout screen closed/i);
  } finally { spy.mockRestore(); }
});
