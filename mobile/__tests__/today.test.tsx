import React from 'react';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';
import { makeBackend } from './fakeBackend';

jest.mock('../src/tts', () => ({ speak: jest.fn(async (_t: string, _c: any, done?: () => void) => { if (done) setTimeout(done, 0); }), stopSpeaking: jest.fn() }));
jest.mock('../src/haptics', () => ({ impactAsync: jest.fn(), notificationAsync: jest.fn(), selectionAsync: jest.fn(), ImpactFeedbackStyle: {}, NotificationFeedbackType: {}, loadHaptics: jest.fn(), hapticsEnabled: () => true, setHapticsEnabled: jest.fn() }));
jest.mock('../src/ux/WorkoutSummary', () => () => null);
jest.mock('../src/SwapSheet', () => () => null);
jest.mock('../src/ux/f_AnimBar', () => () => null);

const SR = require('expo-speech-recognition');
import Today from '../src/screens/Today';

let backend: ReturnType<typeof makeBackend>;
beforeEach(() => { SR.__reset(); backend = makeBackend(); (global as any).fetch = backend.fetch; });
const wait = (ms: number) => act(async () => { await new Promise((r) => setTimeout(r, ms)); });

test('Today lists the same exercises as the live session, and opening Train with coach starts it exactly once and keeps it open', async () => {
  const { findByLabelText, getAllByText, queryByLabelText } = render(<Today onOpenTeam={jest.fn()} onOpenProfile={jest.fn()} />);
  await waitFor(() => expect(getAllByText(/bench press/i).length).toBeGreaterThan(0), { timeout: 5000 });
  expect(getAllByText(/lat pulldown/i).length).toBeGreaterThan(0);
  fireEvent.press(await findByLabelText(/Start your session with/i));
  await findByLabelText('Log set 1 of 3');
  await wait(4000);                                                    // the window in which the real phone closed and re-opened it
  expect(backend.count('/live/start')).toBe(1);
  expect(queryByLabelText('Log set 1 of 3')).toBeTruthy();             // still open
  expect(SR.__state.starts.length).toBe(0);
});

test('skip in Train with coach shows up on Today as skipped, and the up-next pointer moves on', async () => {
  const { findByLabelText, findByText, getAllByLabelText } = render(<Today onOpenTeam={jest.fn()} onOpenProfile={jest.fn()} />);
  fireEvent.press(await findByLabelText(/Start your session with/i, {}, { timeout: 5000 }));
  await findByLabelText('Log set 1 of 3');
  fireEvent.press((await findByLabelText('Skip')));
  await waitFor(() => expect(backend.calls.some((c) => c.path === '/live/say' && /skip/.test(c.body.transcript))).toBe(true));
  fireEvent.press(await findByLabelText('Leave session'));            // back to Today: it refreshes from the same state
  expect(await findByText('SKIPPED TODAY', {}, { timeout: 5000 })).toBeTruthy();
  expect(getAllByLabelText(/lat pulldown/i).length).toBeGreaterThan(0);
});
