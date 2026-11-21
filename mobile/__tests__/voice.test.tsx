import React from 'react';
import { act, render, waitFor } from '@testing-library/react-native';
import { makeBackend } from './fakeBackend';

jest.mock('../src/tts', () => ({ speak: jest.fn(async (_t: string, _c: any, done?: () => void) => { if (done) setTimeout(done, 0); }), stopSpeaking: jest.fn() }));
jest.mock('../src/haptics', () => ({ impactAsync: jest.fn(), notificationAsync: jest.fn(), selectionAsync: jest.fn(), ImpactFeedbackStyle: {}, NotificationFeedbackType: {}, loadHaptics: jest.fn(), hapticsEnabled: () => true, setHapticsEnabled: jest.fn() }));

const SR = require('expo-speech-recognition');
import WakeCoach from '../src/WakeCoach';
import { getTalkState, setTalkState, toggleTalk } from '../src/voiceTalk';
import { micOwner } from '../src/micLock';

let backend: ReturnType<typeof makeBackend>;
beforeEach(() => { setTalkState({ phase: 'idle', heard: '', note: '', error: false }); SR.__reset(); backend = makeBackend(); (global as any).fetch = backend.fetch; });

const say = (text: string, isFinal: boolean) => act(() => { SR.__emit('result', { results: [{ transcript: text }], isFinal }); });
const startTurn = async () => { await act(async () => { toggleTalk(); }); await waitFor(() => expect(SR.__state.starts.length).toBeGreaterThan(0)); };
const wait = (ms: number) => act(async () => { await new Promise((r) => setTimeout(r, ms)); });

test('REGRESSION: the recognizer listeners are attached through the module object, not the unbound package export', async () => {
  render(<WakeCoach />);
  await waitFor(() => expect(SR.__listenerCount('result')).toBe(1));
  expect(SR.__listenerCount('end')).toBe(1);
  expect(SR.__listenerCount('error')).toBe(1);
});

test('tap to talk: listens in Indian English, shows live words, sends the final words once, speaks the reply, ends idle and frees the mic', async () => {
  render(<WakeCoach />);
  await waitFor(() => expect(SR.__listenerCount('result')).toBe(1));
  await startTurn();
  expect(SR.__state.starts[0].lang).toBe('en-IN');
  expect(SR.__state.starts[0].continuous).toBe(false);
  expect(getTalkState().phase).toBe('listening');
  say('what is my', false);
  expect(getTalkState().heard).toBe('what is my');
  say('what is my protein left', true);
  await waitFor(() => expect(backend.count('/voice/command')).toBe(1));
  expect(backend.calls.find((c) => c.path === '/voice/command')!.body.transcript).toBe('what is my protein left');
  await waitFor(() => expect(getTalkState().phase).toBe('idle'));
  expect(micOwner()).toBe(null);
  act(() => SR.__emit('end'));                                         // the recognizer's own 'end' after the final result must not send twice
  expect(backend.count('/voice/command')).toBe(1);
});

test('a turn that hears words but never gets a final result still sends them when the recognizer ends', async () => {
  render(<WakeCoach />);
  await waitFor(() => expect(SR.__listenerCount('result')).toBe(1));
  await startTurn();
  await wait(650);                                                     // past the stale-end guard
  say('open fuel', false);
  act(() => SR.__emit('end'));
  await waitFor(() => expect(backend.count('/voice/command')).toBe(1));
});

test('silence: says plainly that nothing was heard, sends nothing, and the next tap works', async () => {
  render(<WakeCoach />);
  await waitFor(() => expect(SR.__listenerCount('result')).toBe(1));
  await startTurn();
  await wait(650);
  act(() => SR.__emit('error', { error: 'no-speech', message: 'No speech was detected.' }));
  await waitFor(() => expect(getTalkState().phase).toBe('idle'));
  expect(backend.count('/voice/command')).toBe(0);
  expect(getTalkState().error).toBe(true);
  expect(micOwner()).toBe(null);
  SR.__state.starts = [];
  await startTurn();                                                   // not locked
  expect(SR.__state.starts.length).toBe(1);
});

test('a phone with no speech service gets an on-screen explanation instead of silence', async () => {
  SR.__state.available = false;
  const { findByText } = render(<WakeCoach />);
  await waitFor(() => expect(SR.__listenerCount('result')).toBe(1));
  await act(async () => { toggleTalk(); });
  expect(await findByText(/no speech-recognition service/i, { hidden: true } as any)).toBeTruthy();   // the overlay is pointerEvents=none
  expect(SR.__state.starts.length).toBe(0);
});

test('a hung server can never lock the mic: tap to cancel, then talk again', async () => {
  (global as any).fetch = jest.fn(async (url: any, init: any = {}) => (String(url).includes('/voice/command') ? new Promise(() => {}) : backend.fetch(url, init)));
  render(<WakeCoach />);
  await waitFor(() => expect(SR.__listenerCount('result')).toBe(1));
  await startTurn();
  say('what should I eat', true);
  await waitFor(() => expect(getTalkState().phase).toBe('thinking'));
  await act(async () => { toggleTalk(); });                            // tap = cancel
  await waitFor(() => expect(getTalkState().phase).toBe('idle'));
  SR.__state.starts = [];
  await startTurn();                                                   // a fresh turn starts
  expect(getTalkState().phase).toBe('listening');
});

test('the coach asking a question opens a follow-up listening turn by itself', async () => {
  (global as any).fetch = jest.fn(async (url: any, init: any = {}) => (String(url).includes('/voice/command')
    ? { ok: true, status: 200, json: async () => ({ intent: 'food', reply: 'How many eggs?', actions: [], expects: 'food_amount', moment: false }) }
    : backend.fetch(url, init)));
  render(<WakeCoach />);
  await waitFor(() => expect(SR.__listenerCount('result')).toBe(1));
  await startTurn();
  say('I ate eggs', true);
  await waitFor(() => expect(SR.__state.starts.length).toBe(2), { timeout: 5000 });   // the second start is the follow-up turn
});

// ---------------------------------------------------------------- "Hey coach" (opt-in)
import { getWakeState, patchWakeState, setWakeEnabled } from '../src/wakeSettings';
import { AppState } from 'react-native';

test('Hey coach: off by default, nothing listens until it is switched on', async () => {
  expect(getWakeState().enabled).toBe(false);
  render(<WakeCoach />);
  await wait(1200);
  expect(SR.__state.starts.length).toBe(0);
});

test('Hey coach: switched on, it listens continuously, ignores ordinary talk, and runs the command after the phrase', async () => {
  await setWakeEnabled(true);
  render(<WakeCoach />);
  await waitFor(() => expect(SR.__state.starts.length).toBe(1), { timeout: 5000 });
  expect(SR.__state.starts[0]).toMatchObject({ continuous: true, interimResults: true, lang: 'en-IN' });
  say('this gym is so crowded today', true);
  await wait(300);
  expect(backend.count('/voice/command')).toBe(0);
  say('hey coach what is my protein left', true);
  await waitFor(() => expect(backend.count('/voice/command')).toBe(1));
  expect(backend.calls.find((c) => c.path === '/voice/command')!.body.transcript).toBe('what is my protein left');
  expect(getWakeState().diag.results).toBeGreaterThan(0);
  expect(getWakeState().diag.lastMatch).toBeGreaterThan(0);
  await setWakeEnabled(false);
});

test('Hey coach: a bare phrase waits for the next sentence as the command', async () => {
  await setWakeEnabled(true);
  render(<WakeCoach />);
  await waitFor(() => expect(SR.__state.starts.length).toBe(1), { timeout: 5000 });
  say('hey coach', true);
  await wait(200);
  expect(backend.count('/voice/command')).toBe(0);
  say('open fuel', true);
  await waitFor(() => expect(backend.count('/voice/command')).toBe(1));
  await setWakeEnabled(false);
});

test('Hey coach: a tap-to-talk turn takes over the microphone from the wake listener and gives it back', async () => {
  await setWakeEnabled(true);
  render(<WakeCoach />);
  await waitFor(() => expect(SR.__state.starts.length).toBe(1), { timeout: 5000 });
  await act(async () => { toggleTalk(); });
  await waitFor(() => expect(SR.__state.starts.length).toBe(2), { timeout: 5000 });
  expect(SR.__state.starts[1].continuous).toBe(false);
  say('what should I eat', true);
  await waitFor(() => expect(backend.count('/voice/command')).toBe(1));
  await waitFor(() => expect(getTalkState().phase).toBe('idle'));
  await waitFor(() => expect(SR.__state.starts.length).toBe(3), { timeout: 6000 });   // wake listening resumes
  expect(SR.__state.starts[2].continuous).toBe(true);
  await setWakeEnabled(false);
});
