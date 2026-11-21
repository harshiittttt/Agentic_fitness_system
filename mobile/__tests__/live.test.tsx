import React from 'react';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';
import { makeBackend } from './fakeBackend';

jest.mock('../src/tts', () => ({ speak: jest.fn(async (_t: string, _c: any, done?: () => void) => { if (done) setTimeout(done, 0); }), stopSpeaking: jest.fn() }));
jest.mock('../src/haptics', () => ({ impactAsync: jest.fn(), notificationAsync: jest.fn(), selectionAsync: jest.fn(), ImpactFeedbackStyle: {}, NotificationFeedbackType: {}, loadHaptics: jest.fn(), hapticsEnabled: () => true, setHapticsEnabled: jest.fn() }));
jest.mock('../src/ux/WorkoutSummary', () => () => null);
jest.mock('../src/SwapSheet', () => () => null);

const SR = require('expo-speech-recognition');
import Live from '../src/screens/Live';
import WakeCoach from '../src/WakeCoach';
import { tuning } from '../src/voiceEndpoint';
import { setTalkState } from '../src/voiceTalk';
import { setVoiceOverlay } from '../src/voiceScreen';

let backend: ReturnType<typeof makeBackend>;
beforeEach(() => { SR.__reset(); setTalkState({ phase: 'idle', heard: '', note: '', error: false, session: false }); setVoiceOverlay('live'); backend = makeBackend(); (global as any).fetch = backend.fetch; });
const wait = (ms: number) => act(async () => { await new Promise((r) => setTimeout(r, ms)); });

test('opens once: one /live/start, shows the exercise and the log button, does not restart itself or touch the microphone', async () => {
  const onClose = jest.fn();
  const { findByLabelText, getAllByText } = render(<Live onClose={onClose} />);
  await findByLabelText('Log set 1 of 3');
  await wait(1500);
  expect(backend.count('/live/start')).toBe(1);
  expect(onClose).not.toHaveBeenCalled();
  expect(getAllByText(/Bench Press/i).length).toBeGreaterThan(0);
  expect(SR.__state.starts.length).toBe(0);
});

test('logging a set: one tap saves it, the count moves on and the rest timer starts', async () => {
  const { findByLabelText, findByText } = render(<Live onClose={jest.fn()} />);
  fireEvent.press(await findByLabelText('Log set 1 of 3'));
  await waitFor(() => expect(backend.count('/live/log')).toBe(1));
  expect(backend.calls.find((c) => c.path === '/live/log')!.body).toMatchObject({ exercise: 'bench press', weight: 20, reps: 8 });
  await findByLabelText('Log set 2 of 3');
  expect(await findByText('Resting')).toBeTruthy();
});

const micButton = async (api: any) => (await api.findAllByLabelText(/Talk to your coach|Stop the voice conversation/, {}, { timeout: 3000 }))[0];
const sayLive = (text: string, isFinal = false) => act(() => { SR.__emit('result', { results: [{ transcript: text }], isFinal }); });
const voiceCalls = () => backend.calls.filter((c) => c.path === '/voice/command');

// The mic in the workout screen drives the app's voice engine (WakeCoach); both are mounted, as in the real app.
const mountBoth = async () => {
  const api = render(<><WakeCoach /><Live onClose={jest.fn()} /></>);
  await api.findByLabelText('Log set 1 of 3');
  await waitFor(() => expect(SR.__listenerCount('result')).toBe(1));
  return api;
};

test('one tap on the workout mic starts the voice session; a spoken set is logged through the server and the screen updates by itself', async () => {
  tuning.scale = 0.2;
  const api = await mountBoth();
  await act(async () => { fireEvent.press(await micButton(api)); });
  expect((await api.findAllByText('Voice on · tap the mic to stop')).length).toBeGreaterThan(0);
  await waitFor(() => expect(SR.__state.starts.length).toBe(1), { timeout: 4000 });
  expect(SR.__state.starts[0]).toMatchObject({ continuous: true, lang: 'en-IN' });
  expect(SR.__state.starts[0].contextualStrings).toEqual(expect.arrayContaining(['warm up', 'bench press']));
  await wait(900);                                                           // the coach's greeting just ended: a short echo-protection window
  sayLive('bench press 60 for 8 reps');
  await waitFor(() => expect(voiceCalls().length).toBe(1), { timeout: 4000 });
  expect(voiceCalls()[0].body).toMatchObject({ transcript: 'bench press 60 for 8 reps', screen: 'live' });
  await api.findByLabelText('Log set 2 of 3', {}, { timeout: 4000 });       // the set count moved: the screen applied the server's answer
  expect(await api.findByText('Resting')).toBeTruthy();                      // and the rest timer started
  expect(SR.__state.starts.length).toBe(1);                                  // the microphone never restarted
  await act(async () => { fireEvent.press(await micButton(api)); });         // stop
  await waitFor(() => expect(api.queryAllByText('Voice on · tap the mic to stop')).toHaveLength(0));
  tuning.scale = 1;
});

test('the words appear live while you speak, including through a thinking pause', async () => {
  tuning.scale = 0.2;
  const api = await mountBoth();
  await act(async () => { fireEvent.press(await micButton(api)); });
  await waitFor(() => expect(SR.__state.starts.length).toBe(1), { timeout: 4000 });
  await wait(900);
  sayLive('I did like');
  expect((await api.findAllByText(/“I did like”/)).length).toBeGreaterThan(0);
  await wait(300);
  expect(voiceCalls().length).toBe(0);                                       // still waiting for the rest
  sayLive('I did like one set of bench 60 for 8 reps');
  await waitFor(() => expect(voiceCalls().length).toBe(1), { timeout: 4000 });
  expect(voiceCalls()[0].body.transcript).toBe('I did like one set of bench 60 for 8 reps');
  await act(async () => { fireEvent.press(await micButton(api)); });
  tuning.scale = 1;
});

test('the voice session does not start by itself: nothing listens until you tap the mic', async () => {
  const api = await mountBoth();
  await wait(2000);
  expect(SR.__state.starts.length).toBe(0);
  expect(api.queryAllByText('Voice on · tap the mic to stop')).toHaveLength(0);
});
