import React from 'react';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';
import { makeBackend } from './fakeBackend';

jest.mock('../src/tts', () => ({ speak: jest.fn(async (_t: string, _c: any, done?: () => void) => { if (done) setTimeout(done, 0); }), stopSpeaking: jest.fn() }));
jest.mock('../src/haptics', () => ({ impactAsync: jest.fn(), notificationAsync: jest.fn(), selectionAsync: jest.fn(), ImpactFeedbackStyle: {}, NotificationFeedbackType: {}, loadHaptics: jest.fn(), hapticsEnabled: () => true, setHapticsEnabled: jest.fn() }));
jest.mock('../src/ux/WorkoutSummary', () => () => null);
jest.mock('../src/SwapSheet', () => () => null);

const SR = require('expo-speech-recognition');
import Live from '../src/screens/Live';

let backend: ReturnType<typeof makeBackend>;
beforeEach(() => { SR.__reset(); backend = makeBackend(); (global as any).fetch = backend.fetch; });
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

test('one tap on the mic starts a voice session: it keeps listening turn after turn, every sentence goes to the coach, and the next tap stops it', async () => {
  const api = render(<Live onClose={jest.fn()} />);
  await api.findByLabelText('Log set 1 of 3');
  await act(async () => { fireEvent.press(await micButton(api)); });
  expect(await api.findByText('Voice on · tap the mic to stop')).toBeTruthy();
  await waitFor(() => expect(SR.__state.starts.length).toBe(1), { timeout: 4000 });
  expect(SR.__state.starts[0].contextualStrings).toEqual(expect.arrayContaining(['warm up', 'bench press']));
  expect(SR.__state.starts[0].lang).toBe('en-IN');
  for (let i = 1; i <= 3; i++) {
    act(() => SR.__emit('result', { results: [{ transcript: `sentence ${i}` }], isFinal: true }));
    act(() => SR.__emit('end'));
    await waitFor(() => expect(backend.count('/live/say')).toBe(i));
    await waitFor(() => expect(SR.__state.starts.length).toBe(i + 1), { timeout: 6000 });   // back to listening with no tap
  }
  const said = backend.calls.filter((c) => c.path === '/live/say');
  expect(said.map((c) => c.body.transcript)).toEqual(['sentence 1', 'sentence 2', 'sentence 3']);
  expect(said.every((c) => c.body.ambient === false)).toBe(true);                       // you started it, so it is all for the coach
  await act(async () => { fireEvent.press(await micButton(api)); });                    // stop
  await waitFor(() => expect(api.queryByText('Voice on · tap the mic to stop')).toBeNull());
  const n = SR.__state.starts.length;
  await wait(2000);
  expect(SR.__state.starts.length).toBe(n);
});

test('silence in a voice session is fine (rest between sets): it keeps listening without any message or switching itself off', async () => {
  const api = render(<Live onClose={jest.fn()} />);
  await api.findByLabelText('Log set 1 of 3');
  await act(async () => { fireEvent.press(await micButton(api)); });
  for (let round = 1; round <= 4; round++) {
    await waitFor(() => expect(SR.__state.starts.length).toBe(round), { timeout: 4000 });
    act(() => SR.__emit('error', { error: 'no-speech', message: 'No speech was detected.' }));
    act(() => SR.__emit('end'));
  }
  expect(api.queryByText('Voice on · tap the mic to stop')).toBeTruthy();
  expect(api.queryByText(/Didn't hear anything/i)).toBeNull();
  await act(async () => { fireEvent.press(await micButton(api)); });
  await waitFor(() => expect(api.queryByText('Voice on · tap the mic to stop')).toBeNull());
});

test('the voice session does not start by itself: nothing listens until you tap the mic', async () => {
  const api = render(<Live onClose={jest.fn()} />);
  await api.findByLabelText('Log set 1 of 3');
  await wait(2000);
  expect(SR.__state.starts.length).toBe(0);
});
