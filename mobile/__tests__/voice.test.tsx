import React from 'react';
import { act, render, waitFor } from '@testing-library/react-native';
import { makeBackend } from './fakeBackend';

jest.mock('../src/tts', () => ({ speak: jest.fn(async (_t: string, _c: any, done?: () => void) => { if (done) setTimeout(done, 0); }), stopSpeaking: jest.fn() }));
jest.mock('../src/haptics', () => ({ impactAsync: jest.fn(), notificationAsync: jest.fn(), selectionAsync: jest.fn(), ImpactFeedbackStyle: {}, NotificationFeedbackType: {}, loadHaptics: jest.fn(), hapticsEnabled: () => true, setHapticsEnabled: jest.fn() }));

const SR = require('expo-speech-recognition');
import WakeCoach from '../src/WakeCoach';
import { getTalkState, setTalkState, toggleTalk } from '../src/voiceTalk';
import { micOwner } from '../src/micLock';
import { getWakeState, setWakeEnabled } from '../src/wakeSettings';

let backend: ReturnType<typeof makeBackend>;
beforeEach(() => { setTalkState({ phase: 'idle', heard: '', note: '', error: false, session: false }); SR.__reset(); backend = makeBackend(); (global as any).fetch = backend.fetch; });

const say = (text: string, isFinal: boolean) => act(() => { SR.__emit('result', { results: [{ transcript: text }], isFinal }); });
const tap = async () => { await act(async () => { toggleTalk(); }); };
const startSession = async () => { await tap(); await waitFor(() => expect(SR.__state.starts.length).toBeGreaterThan(0)); };
const starts = () => SR.__state.starts.length;
const wait = (ms: number) => act(async () => { await new Promise((r) => setTimeout(r, ms)); });
const mounted = async () => { const r = render(<WakeCoach />); await waitFor(() => expect(SR.__listenerCount('result')).toBe(1)); return r; };

test('REGRESSION: the recognizer listeners are attached through the module object, not the unbound package export', async () => {
  await mounted();
  expect(SR.__listenerCount('end')).toBe(1);
  expect(SR.__listenerCount('error')).toBe(1);
});

test('one tap starts a voice session: Indian English, live words, the final words are sent once, the reply is spoken and it goes straight back to listening', async () => {
  await mounted();
  await startSession();
  expect(SR.__state.starts[0].lang).toBe('en-IN');
  expect(SR.__state.starts[0].continuous).toBe(false);
  expect(getTalkState()).toMatchObject({ phase: 'listening', session: true });
  say('what is my', false);
  expect(getTalkState().heard).toBe('what is my');
  say('what is my protein left', true);
  await waitFor(() => expect(backend.count('/voice/command')).toBe(1));
  expect(backend.calls.find((c) => c.path === '/voice/command')!.body.transcript).toBe('what is my protein left');
  await waitFor(() => expect(starts()).toBe(2), { timeout: 5000 });                  // listening again with no second tap
  expect(getTalkState()).toMatchObject({ phase: 'listening', session: true });
  act(() => SR.__emit('end'));                                                       // the recognizer's own 'end' after a final result must not send twice
  expect(backend.count('/voice/command')).toBe(1);
  await tap();
});

test('a conversation: three things said in a row, one tap, nothing sent twice, and the second tap stops it', async () => {
  await mounted();
  await startSession();
  for (let i = 1; i <= 3; i++) {
    say(`command number ${i}`, true);
    await waitFor(() => expect(backend.count('/voice/command')).toBe(i));
    await waitFor(() => expect(starts()).toBe(i + 1), { timeout: 5000 });
  }
  expect(backend.calls.filter((c) => c.path === '/voice/command').map((c) => c.body.transcript)).toEqual(['command number 1', 'command number 2', 'command number 3']);
  await tap();                                                                       // stop
  await waitFor(() => expect(getTalkState()).toMatchObject({ phase: 'idle', session: false }));
  expect(micOwner()).toBe(null);
  const n = starts();
  await wait(1500);
  expect(starts()).toBe(n);                                                          // and it stays stopped
});

test('silence inside a session is not an error: no message, no flicker, it just keeps listening', async () => {
  await mounted();
  await startSession();
  for (let round = 1; round <= 4; round++) {
    await wait(650);
    act(() => SR.__emit('error', { error: 'no-speech', message: 'No speech was detected.' }));
    await waitFor(() => expect(starts()).toBe(round + 1), { timeout: 4000 });
    expect(getTalkState()).toMatchObject({ phase: 'listening', session: true, error: false });
  }
  expect(backend.count('/voice/command')).toBe(0);
  await tap();
  await waitFor(() => expect(getTalkState().session).toBe(false));
});

test('tapping stop while half a sentence is heard discards it', async () => {
  await mounted();
  await startSession();
  say('log my bench', false);
  await tap();
  await waitFor(() => expect(getTalkState().session).toBe(false));
  await wait(400);
  expect(backend.count('/voice/command')).toBe(0);
});

test('a turn that hears words but never gets a final result still sends them when the recognizer ends', async () => {
  await mounted();
  await startSession();
  await wait(650);                                                                   // past the stale-end guard
  say('open fuel', false);
  act(() => SR.__emit('end'));
  await waitFor(() => expect(backend.count('/voice/command')).toBe(1));
  await tap();
});

test('a phone with no speech service gets an on-screen explanation, and no session is left running', async () => {
  SR.__state.available = false;
  const { findByText } = await mounted();
  await tap();
  expect(await findByText(/no speech-recognition service/i, { hidden: true } as any)).toBeTruthy();
  expect(SR.__state.starts.length).toBe(0);
  expect(getTalkState().session).toBe(false);
});

test('a hung server can never lock the mic: tap to cancel, then start again', async () => {
  (global as any).fetch = jest.fn(async (url: any, init: any = {}) => (String(url).includes('/voice/command') ? new Promise(() => {}) : backend.fetch(url, init)));
  await mounted();
  await startSession();
  say('what should I eat', true);
  await waitFor(() => expect(getTalkState().phase).toBe('thinking'));
  await tap();                                                                       // cancel
  await waitFor(() => expect(getTalkState()).toMatchObject({ phase: 'idle', session: false }));
  SR.__state.starts = [];
  await startSession();                                                              // a fresh session starts
  expect(getTalkState().phase).toBe('listening');
  await tap();
});

test('when the coach asks a question the answer is heard next, as a follow-up', async () => {
  (global as any).fetch = jest.fn(async (url: any, init: any = {}) => {
    if (!String(url).includes('/voice/command')) return backend.fetch(url, init);
    const b = JSON.parse(init.body);
    return { ok: true, status: 200, json: async () => (b.followup ? { intent: 'food', reply: 'Logged four eggs.', actions: [], expects: null }
      : { intent: 'food', reply: 'How many eggs?', actions: [], expects: 'food_amount', moment: false }) };
  });
  await mounted();
  await startSession();
  say('I ate eggs', true);
  await waitFor(() => expect(starts()).toBe(2), { timeout: 5000 });
  say('four', true);
  await waitFor(() => expect((global as any).fetch.mock.calls.filter((c: any[]) => String(c[0]).includes('/voice/command')).length).toBe(2));
  const bodies = (global as any).fetch.mock.calls.filter((c: any[]) => String(c[0]).includes('/voice/command')).map((c: any[]) => JSON.parse(c[1].body));
  expect(bodies[0].followup).toBe(false);
  expect(bodies[1]).toMatchObject({ transcript: 'four', followup: true });          // the server is told this is the answer to its question
  await tap();
});

// ---------------------------------------------------------------- "Hey coach" (opt-in)
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

test('Hey coach: a voice session takes the microphone from the wake listener and gives it back when you stop', async () => {
  await setWakeEnabled(true);
  render(<WakeCoach />);
  await waitFor(() => expect(SR.__state.starts.length).toBe(1), { timeout: 5000 });
  await tap();
  await waitFor(() => expect(SR.__state.starts.length).toBe(2), { timeout: 5000 });
  expect(SR.__state.starts[1].continuous).toBe(false);
  say('what should I eat', true);
  await waitFor(() => expect(backend.count('/voice/command')).toBe(1));
  await waitFor(() => expect(starts()).toBe(3), { timeout: 6000 });                  // the session carries on
  expect(SR.__state.starts[2].continuous).toBe(false);
  await tap();                                                                       // stop
  await waitFor(() => expect(starts()).toBe(4), { timeout: 6000 });                  // wake listening resumes
  expect(SR.__state.starts[3].continuous).toBe(true);
  await setWakeEnabled(false);
});
