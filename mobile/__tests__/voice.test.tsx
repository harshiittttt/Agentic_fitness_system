import React from 'react';
import { act, render, waitFor } from '@testing-library/react-native';
import { makeBackend } from './fakeBackend';

jest.mock('../src/tts', () => ({ speak: jest.fn(async (_t: string, _c: any, done?: () => void) => { if (done) setTimeout(done, 0); }), stopSpeaking: jest.fn() }));
jest.mock('../src/haptics', () => ({ impactAsync: jest.fn(), notificationAsync: jest.fn(), selectionAsync: jest.fn(), ImpactFeedbackStyle: {}, NotificationFeedbackType: {}, loadHaptics: jest.fn(), hapticsEnabled: () => true, setHapticsEnabled: jest.fn() }));

const SR = require('expo-speech-recognition');
const TTS = require('../src/tts');
import WakeCoach from '../src/WakeCoach';
import { getTalkState, onVoiceReply, setTalkState, toggleTalk } from '../src/voiceTalk';
import { setVoiceOverlay } from '../src/voiceScreen';
import { tuning } from '../src/voiceEndpoint';
import { micOwner } from '../src/micLock';
import { getWakeState, setWakeEnabled } from '../src/wakeSettings';

// Real hold times are 0.7 s (finished sentence) / 1.6 s (maybe) / 3.8 s (unfinished); the tests run the same logic 5x faster.
const S = 0.2;
const HOLD_DONE = 700 * S, HOLD_MAYBE = 1600 * S, HOLD_OPEN = 3800 * S;

let backend: ReturnType<typeof makeBackend>;
const setup = (opts: Parameters<typeof makeBackend>[0] = {}) => { backend = makeBackend(opts); (global as any).fetch = backend.fetch; };
const defaultSpeak = async (_t: string, _c: any, done?: () => void) => { if (done) setTimeout(done, 0); };
beforeEach(() => { tuning.scale = S; setTalkState({ phase: 'idle', heard: '', note: '', error: false, session: false }); SR.__reset(); TTS.speak.mockReset(); TTS.speak.mockImplementation(defaultSpeak); TTS.stopSpeaking.mockReset(); setVoiceOverlay(null); setup(); });
afterAll(() => { tuning.scale = 1; });

const say = (text: string, isFinal = false) => act(() => { SR.__emit('result', { results: [{ transcript: text }], isFinal }); });
const wait = (ms: number) => act(async () => { await new Promise((r) => setTimeout(r, ms)); });
const tap = async () => { await act(async () => { toggleTalk(); }); };
const starts = () => SR.__state.starts.length;
const cmds = () => backend.calls.filter((c) => c.path === '/voice/command').map((c) => c.body);
const mounted = async () => { const r = render(<WakeCoach />); await waitFor(() => expect(SR.__listenerCount('result')).toBe(1)); return r; };
const startSession = async () => { await tap(); await waitFor(() => expect(starts()).toBe(1)); };
// a tap while the coach is still talking only skips its sentence, so stopping may take a second tap (that is the real behaviour)
const stopSession = async () => { for (let i = 0; i < 3 && getTalkState().session; i++) { await tap(); await wait(80); } await waitFor(() => expect(getTalkState().session).toBe(false)); };
const spokenTexts = () => TTS.speak.mock.calls.map((c: any[]) => c[0]);

test('REGRESSION: the recognizer listeners are attached through the module object, not the unbound package export', async () => {
  await mounted();
  expect(SR.__listenerCount('end')).toBe(1);
  expect(SR.__listenerCount('error')).toBe(1);
});

test('one tap: continuous listening in Indian English with gym vocabulary and a long phone-side silence allowance; the second tap stops it', async () => {
  await mounted();
  await startSession();
  expect(SR.__state.starts[0]).toMatchObject({ lang: 'en-IN', continuous: true, interimResults: true });
  expect(SR.__state.starts[0].androidIntentOptions.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS).toBeGreaterThanOrEqual(3000);
  expect(SR.__state.starts[0].contextualStrings).toEqual(expect.arrayContaining(['warm up', 'bench press']));
  expect(getTalkState()).toMatchObject({ phase: 'listening', session: true });
  await stopSession();
  expect(micOwner()).toBe(null);
  expect(SR.__state.aborts).toBeGreaterThan(0);
});

test('a finished command is sent quickly, the microphone is NEVER restarted, and the next command works with no tap', async () => {
  await mounted();
  await startSession();
  say('what is my', false);
  expect(getTalkState().heard).toBe('what is my');
  say('what is my protein left', false);
  await wait(HOLD_DONE / 2);
  expect(cmds().length).toBe(0);                                       // not yet: it waits the short hold first
  await waitFor(() => expect(cmds().length).toBe(1), { timeout: 3000 });
  expect(cmds()[0].transcript).toBe('what is my protein left');
  await waitFor(() => expect(getTalkState()).toMatchObject({ phase: 'listening', session: true }));
  await wait(700 * S + 100);                                           // past the mute grace
  say('skip this', true);
  await waitFor(() => expect(cmds().length).toBe(2), { timeout: 3000 });
  expect(starts()).toBe(1);                                            // one recognizer start for the whole conversation: no restart gaps, no beeps
  await stopSession();
});

test('a thinking pause does not cut you off: "I did like ... uhh ... one set of bench 60 for 8" is ONE utterance, fillers removed', async () => {
  await mounted();
  await startSession();
  say('I did like');
  await wait(HOLD_OPEN / 2);                                           // you stop to think
  expect(cmds().length).toBe(0);
  say('I did like uhh');
  await wait(HOLD_OPEN / 2 - 40);
  expect(cmds().length).toBe(0);
  say('I did like uhh one set of bench 60 for 8 wraps');
  await waitFor(() => expect(cmds().length).toBe(1), { timeout: 3000 });
  expect(cmds()).toHaveLength(1);
  expect(cmds()[0].transcript).toBe('I did like one set of bench 60 for 8 reps');
  await stopSession();
});

test('an unfinished sentence the phone happened to finalize is still waited on, and segments are joined', async () => {
  await mounted();
  await startSession();
  say('I did the', true);                                              // the recognizer thinks this is a finished segment
  await wait(HOLD_OPEN * 0.45);
  expect(cmds().length).toBe(0);
  say('bench press', true);
  say('bench press 60 for 8 reps', false);
  await waitFor(() => expect(cmds().length).toBe(1), { timeout: 3000 });
  expect(cmds()[0].transcript).toBe('I did the bench press bench press 60 for 8 reps'.replace('bench press bench press', 'bench press bench press'));
  await stopSession();
});

test('the server can say a sentence is unfinished: nothing is spoken, it is held and joined to the next one', async () => {
  setup({ fragments: ['I did'] });
  await mounted();
  await startSession();
  say('I did');                                                        // looks unfinished: waits, and the server agrees
  await waitFor(() => expect(cmds().length).toBe(1), { timeout: 4000 });
  expect(cmds()[0].transcript).toBe('I did');
  expect(spokenTexts().filter((t: string) => !/sec|checking|moment|hold|see/i.test(t)).length).toBe(0);   // the coach said nothing
  expect(getTalkState().phase).toBe('listening');
  say('bench press 60 for 8 reps');
  await waitFor(() => expect(cmds().length).toBe(2), { timeout: 3000 });
  expect(cmds()[1].transcript).toBe('I did bench press 60 for 8 reps');
  await stopSession();
});

test('while the coach is thinking or speaking the microphone is ignored; right after, listening resumes', async () => {
  setup({ delayMs: 400 });
  await mounted();
  await startSession();
  say('what should I do next', false);
  await waitFor(() => expect(getTalkState().phase).toBe('thinking'), { timeout: 3000 });
  say('this is the coach talking to itself', true);                    // heard while the answer is pending
  await waitFor(() => expect(getTalkState().phase).toBe('listening'), { timeout: 4000 });
  await wait(700 * S + 120);
  expect(getTalkState().heard).toBe('');                               // that sentence never became the user's
  expect(cmds().length).toBe(1);
  say('skip this', true);
  await waitFor(() => expect(cmds().length).toBe(2), { timeout: 3000 });
  await stopSession();
});

test('a slow answer gets a spoken "one sec" at once, and the real answer replaces it', async () => {
  setup({ delayMs: 900 });
  await mounted();
  await startSession();
  say('what should I do next today');
  await waitFor(() => expect(getTalkState().phase).toBe('thinking'), { timeout: 3000 });
  await waitFor(() => expect(TTS.speak.mock.calls.length).toBeGreaterThan(0), { timeout: 3000 });
  expect(TTS.speak.mock.calls[0][3]).toMatchObject({ premium: false });
  await waitFor(() => expect(spokenTexts().some((t: string) => t.startsWith('You said'))).toBe(true), { timeout: 4000 });
  await stopSession();
});

test('a fast answer gets no filler', async () => {
  await mounted();
  await startSession();
  say('what is next', true);
  await waitFor(() => expect(spokenTexts().some((t: string) => t.startsWith('You said'))).toBe(true), { timeout: 3000 });
  expect(spokenTexts().length).toBe(1);
  await stopSession();
});

test('the recognizer ending by itself mid-session is restarted at once and keeps what you had said', async () => {
  await mounted();
  await startSession();
  await wait(650);
  say('bench press 60 for');
  act(() => SR.__emit('end'));                                          // the phone's service gave up mid-sentence
  await waitFor(() => expect(starts()).toBe(2), { timeout: 3000 });
  say('8 reps');                                                        // a restarted recognizer only reports the NEW words
  await waitFor(() => expect(cmds().length).toBe(1), { timeout: 3000 });
  expect(cmds()[0].transcript).toBe('bench press 60 for 8 reps');
  await stopSession();
});

test('silence and no-speech errors are normal: it restarts quietly, never shows an error', async () => {
  await mounted();
  await startSession();
  for (let i = 1; i <= 3; i++) {
    await wait(650);
    act(() => SR.__emit('error', { error: 'no-speech', message: 'No speech was detected.' }));
    await waitFor(() => expect(starts()).toBe(i + 1), { timeout: 3000 });
    expect(getTalkState()).toMatchObject({ phase: 'listening', session: true, error: false });
  }
  expect(cmds().length).toBe(0);
  await stopSession();
});

test('a phone without continuous recognition (restarts again and again without hearing anything) is switched to one-utterance mode', async () => {
  await mounted();
  await startSession();
  for (let i = 1; i <= 4; i++) {
    await wait(620);
    act(() => SR.__emit('end'));
    await waitFor(() => expect(starts()).toBe(i + 1), { timeout: 3000 });
  }
  expect(SR.__state.starts[SR.__state.starts.length - 1].continuous).toBe(false);
  await stopSession();
});

test('tapping stop discards a half-said sentence and frees the microphone', async () => {
  await mounted();
  await startSession();
  say('log my bench');
  await stopSession();
  await wait(HOLD_MAYBE + 100);
  expect(cmds().length).toBe(0);
  expect(micOwner()).toBe(null);
});

test('a server error ends the session with a plain message (never a silent mic)', async () => {
  setup({ failVoice: true });
  const { findByText } = await mounted();
  await startSession();
  say('what is next', true);
  expect(await findByText(/server had a problem|Couldn't reach|boom/i, { hidden: true } as any, { timeout: 4000 })).toBeTruthy();
  await waitFor(() => expect(getTalkState().session).toBe(false));
});

test('a phone with no speech service gets an on-screen explanation, and no session is left running', async () => {
  SR.__state.available = false;
  const { findByText } = await mounted();
  await tap();
  expect(await findByText(/no speech-recognition service/i, { hidden: true } as any)).toBeTruthy();
  expect(starts()).toBe(0);
  expect(getTalkState().session).toBe(false);
});

test('a hung server can never lock the mic: tap to cancel, then start again', async () => {
  (global as any).fetch = jest.fn(async (url: any, init: any = {}) => (String(url).includes('/voice/command') ? new Promise(() => {}) : backend.fetch(url, init)));
  await mounted();
  await startSession();
  say('what should I eat tonight', true);
  await waitFor(() => expect(getTalkState().phase).toBe('thinking'), { timeout: 3000 });
  await tap();
  await waitFor(() => expect(getTalkState()).toMatchObject({ phase: 'idle', session: false }));
  SR.__state.starts = [];
  await startSession();
  expect(getTalkState().phase).toBe('listening');
  await stopSession();
});

test('when the coach asks a question the answer is sent as a follow-up', async () => {
  (global as any).fetch = jest.fn(async (url: any, init: any = {}) => {
    if (!String(url).includes('/voice/command')) return backend.fetch(url, init);
    const b = JSON.parse(init.body);
    return { ok: true, status: 200, json: async () => (b.followup ? { intent: 'food', reply: 'Logged four eggs.', actions: [], expects: null }
      : { intent: 'food', reply: 'How many eggs?', actions: [], expects: 'food_amount', moment: false }) };
  });
  await mounted();
  await startSession();
  say('I ate eggs please', true);
  await waitFor(() => expect(TTS.speak.mock.calls.some((c: any[]) => c[0] === 'How many eggs?')).toBe(true), { timeout: 4000 });
  await wait(700 * S + 150);
  say('four', true);
  await waitFor(() => expect((global as any).fetch.mock.calls.filter((c: any[]) => String(c[0]).includes('/voice/command')).length).toBe(2), { timeout: 4000 });
  const bodies = (global as any).fetch.mock.calls.filter((c: any[]) => String(c[0]).includes('/voice/command')).map((c: any[]) => JSON.parse(c[1].body));
  expect(bodies[0].followup).toBe(false);
  expect(bodies[1]).toMatchObject({ transcript: 'four', followup: true });
  await stopSession();
});

test('on the workout screen every voice answer is also handed to the screen (sets, rest timer), and the app does not close it', async () => {
  setVoiceOverlay('live');
  const got: any[] = [];
  const off = onVoiceReply((r) => got.push(r));
  await mounted();
  await startSession();
  say('bench press 60 for 8 reps', true);
  await waitFor(() => expect(got.length).toBe(1), { timeout: 4000 });
  expect(got[0]).toMatchObject({ intent: 'set', actions: [{ type: 'rest', seconds: 90 }] });
  expect(cmds()[0]).toMatchObject({ screen: 'live' });
  off();
  await stopSession();
});

// ---------------------------------------------------------------- "Hey coach" (opt-in)
test('Hey coach: off by default, nothing listens until it is switched on', async () => {
  expect(getWakeState().enabled).toBe(false);
  render(<WakeCoach />);
  await wait(1200);
  expect(starts()).toBe(0);
});

test('Hey coach: switched on, it listens continuously, ignores ordinary talk, and runs the command after the phrase', async () => {
  await setWakeEnabled(true);
  render(<WakeCoach />);
  await waitFor(() => expect(starts()).toBe(1), { timeout: 5000 });
  expect(SR.__state.starts[0]).toMatchObject({ continuous: true, interimResults: true, lang: 'en-IN' });
  say('this gym is so crowded today', true);
  await wait(300);
  expect(cmds().length).toBe(0);
  say('hey coach what is my protein left', true);
  await waitFor(() => expect(cmds().length).toBe(1));
  expect(cmds()[0].transcript).toBe('what is my protein left');
  expect(getWakeState().diag.results).toBeGreaterThan(0);
  expect(getWakeState().diag.lastMatch).toBeGreaterThan(0);
  await setWakeEnabled(false);
});

test('Hey coach: a bare phrase waits for the next sentence as the command', async () => {
  await setWakeEnabled(true);
  render(<WakeCoach />);
  await waitFor(() => expect(starts()).toBe(1), { timeout: 5000 });
  say('hey coach', true);
  await wait(200);
  expect(cmds().length).toBe(0);
  say('open fuel', true);
  await waitFor(() => expect(cmds().length).toBe(1));
  await setWakeEnabled(false);
});

test('Hey coach: a voice session takes the microphone from the wake listener and gives it back when you stop', async () => {
  await setWakeEnabled(true);
  render(<WakeCoach />);
  await waitFor(() => expect(starts()).toBe(1), { timeout: 5000 });
  await tap();
  await waitFor(() => expect(starts()).toBe(2), { timeout: 5000 });
  expect(SR.__state.starts[1].continuous).toBe(true);                    // the session's recognizer (not the phrase-matching one: punctuation off)
  expect(SR.__state.starts[1].addsPunctuation).toBe(false);
  await tap();                                                           // stop
  await waitFor(() => expect(starts()).toBe(3), { timeout: 6000 });      // wake listening resumes
  expect(SR.__state.starts[2].addsPunctuation).toBe(true);
  await setWakeEnabled(false);
});


test('a long answer can be cut short with one tap: the sentence stops, the session carries on, and you are heard at once', async () => {
  let finish: (() => void) | null = null;
  TTS.speak.mockImplementation(async (t: string, _c: any, done?: () => void) => { if (t.startsWith('You said')) finish = done ?? null; else if (done) setTimeout(done, 0); });   // the answer "talks" until stopped
  TTS.stopSpeaking.mockImplementation(() => { const f = finish; finish = null; f?.(); });
  await mounted();
  await startSession();
  say('what should I do next', true);
  await waitFor(() => expect(getTalkState().phase).toBe('speaking'), { timeout: 3000 });
  await tap();                                                         // the coach is talking: this tap only skips the rest of the sentence
  await waitFor(() => expect(getTalkState()).toMatchObject({ phase: 'listening', session: true }), { timeout: 3000 });
  expect(TTS.stopSpeaking).toHaveBeenCalled();
  await wait(220 * S + 60);                                            // only a very short echo guard after a skip
  say('skip this', true);
  await waitFor(() => expect(cmds().length).toBe(2), { timeout: 3000 });
  expect(starts()).toBe(1);
  await waitFor(() => expect(getTalkState().phase).toBe('speaking'), { timeout: 3000 });
  await tap();                                                         // skip that answer too, then stop the session
  await waitFor(() => expect(getTalkState().phase).toBe('listening'), { timeout: 3000 });
  await stopSession();
});

test('in a session the coach talks a little faster than the narration voice', async () => {
  await mounted();
  await startSession();
  say('what is next', true);
  await waitFor(() => expect(spokenTexts().some((t: string) => t.startsWith('You said'))).toBe(true), { timeout: 3000 });
  const call = TTS.speak.mock.calls.find((c: any[]) => String(c[0]).startsWith('You said'));
  expect(call[1].rate).toBeGreaterThan(1.1);
  await stopSession();
});
