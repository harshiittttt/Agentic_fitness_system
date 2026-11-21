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

test('tapping the mic listens once, sends what you said (not as ambient) and does not loop', async () => {
  const { findByLabelText, findAllByLabelText } = render(<Live onClose={jest.fn()} />);
  await findByLabelText('Log set 1 of 3');
  const mic = (await findAllByLabelText('Talk to your coach', {}, { timeout: 3000 }))[0];
  await act(async () => { fireEvent.press(mic); });
  await waitFor(() => expect(SR.__state.starts.length).toBe(1));
  expect(SR.__state.starts[0].contextualStrings).toEqual(expect.arrayContaining(['warm up', 'bench press']));
  expect(SR.__state.starts[0].lang).toBe('en-IN');
  act(() => SR.__emit('result', { results: [{ transcript: 'bench press 60 for 8' }], isFinal: true }));
  act(() => SR.__emit('end'));
  await waitFor(() => expect(backend.count('/live/say')).toBe(1));
  expect(backend.calls.find((c) => c.path === '/live/say')!.body).toMatchObject({ transcript: 'bench press 60 for 8', ambient: false });
  await wait(2500);
  expect(SR.__state.starts.length).toBe(1);
});

test('hands-free: after three silent rounds it switches itself off instead of flickering forever', async () => {
  const { findByLabelText, findByText, queryByText } = render(<Live onClose={jest.fn()} />);
  await findByLabelText('Log set 1 of 3');
  await wait(300);
  await act(async () => { fireEvent.press(await findByLabelText('Hands-free listening')); });
  expect(await findByText('HANDS-FREE ON')).toBeTruthy();
  for (let round = 1; round <= 3; round++) {
    await waitFor(() => expect(SR.__state.starts.length).toBe(round), { timeout: 4000 });
    act(() => SR.__emit('error', { error: 'no-speech', message: 'No speech was detected.' }));
    act(() => SR.__emit('end'));
  }
  expect(await findByText('hands-free off', {}, { timeout: 4000 })).toBeTruthy();
  const n = SR.__state.starts.length;
  await wait(2000);
  expect(SR.__state.starts.length).toBe(n);
  expect(queryByText('HANDS-FREE ON')).toBeNull();
});
