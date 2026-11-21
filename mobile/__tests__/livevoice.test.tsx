/** The mic button with LIVE VOICE (streamed audio to the PC voice agent) through the real WakeCoach, with a fake audio module and a fake server socket. */
import React from 'react';
import { act, render, waitFor } from '@testing-library/react-native';
import { makeBackend } from './fakeBackend';

jest.mock('../src/tts', () => ({ speak: jest.fn(async (_t: string, _c: any, done?: () => void) => { if (done) setTimeout(done, 0); }), stopSpeaking: jest.fn() }));
jest.mock('../src/haptics', () => ({ impactAsync: jest.fn(), notificationAsync: jest.fn(), selectionAsync: jest.fn(), ImpactFeedbackStyle: {}, NotificationFeedbackType: {}, loadHaptics: jest.fn(), hapticsEnabled: () => true, setHapticsEnabled: jest.fn() }));
const mockAudio: any = { mic: null, played: [] as any[], recording: false, torn: 0 };
jest.mock('@speechmatics/expo-two-way-audio', () => ({
  requestMicrophonePermissionsAsync: async () => ({ granted: true }),
  initialize: async () => true,
  addExpoTwoWayAudioEventListener: (_n: string, cb: any) => { mockAudio.mic = cb; return { remove: () => { mockAudio.mic = null; } }; },
  toggleRecording: (v: boolean) => { mockAudio.recording = v; return v; },
  tearDown: () => { mockAudio.torn++; mockAudio.recording = false; },
  playPCMData: (p: any) => { mockAudio.played.push(p); },
}), { virtual: true });

const SR = require('expo-speech-recognition');
import WakeCoach from '../src/WakeCoach';
import { getTalkState, onVoiceReply, setTalkState, toggleTalk } from '../src/voiceTalk';
import { setVoiceOverlay } from '../src/voiceScreen';
import { setApiConfig } from '../src/api';
import { setLiveVoice } from '../src/voiceSettings';
import { micOwner } from '../src/micLock';

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

beforeEach(() => {
  (global as any).WebSocket = FakeSocket; FakeSocket.all = []; FakeSocket.failNext = false;
  mockAudio.mic = null; mockAudio.played = []; mockAudio.recording = false; mockAudio.torn = 0;
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
  act(() => { mockAudio.mic({ data: new Uint8Array(640) }); });
  expect(sock().sent.some((x) => typeof x !== 'string')).toBe(true);               // microphone audio is streamed
  await tap();
  await waitFor(() => expect(getTalkState().session).toBe(false));
  expect(mockAudio.torn).toBeGreaterThan(0);                                       // the phone's normal audio mode is given back
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
  expect(getTalkState()).toMatchObject({ phase: 'listening', session: true });
  await tap();
  await waitFor(() => expect(getTalkState().session).toBe(false));
});

test('when the PC voice agent is not reachable the phone recogniser takes over by itself', async () => {
  FakeSocket.failNext = true;
  await mounted();
  await tap();
  await waitFor(() => expect(SR.__state.starts.length).toBe(1), { timeout: 4000 });
  expect(getTalkState()).toMatchObject({ session: true });
  await tap();
  await waitFor(() => expect(getTalkState().session).toBe(false));
});

test('switching Live voice off in Profile uses the phone recogniser straight away', async () => {
  await setLiveVoice(false);
  await mounted();
  await tap();
  await waitFor(() => expect(SR.__state.starts.length).toBe(1));
  expect(FakeSocket.all.length).toBe(0);
  await tap();
  await waitFor(() => expect(getTalkState().session).toBe(false));
});

test('the server dropping the connection mid-session ends it with a clear message', async () => {
  await mounted();
  await openLive();
  act(() => { sock().onclose({ code: 1006 }); });
  await waitFor(() => expect(getTalkState()).toMatchObject({ session: false, error: true }));
});
