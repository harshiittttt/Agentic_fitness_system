// Rules that keep ONE live-voice session alive through a workout (ERROR_LEDGER E-007 / E-008). Pure TypeScript, no React / native imports, so it
// runs under plain Node (scripts/test_livesession.js).
//
// What was found on the real phone (backend DIAG log, 2026-11-25):
//   live_unmount -> session_stop -> (the user taps the tab-bar mic again) -> a NEW session that talks to the coach as if on the Today tab.
// The workout screen is a full-screen Modal. It was being closed by things the user never meant:
//   * Android Back (button or the edge-swipe gesture, easy to hit while switching apps) -> Modal.onRequestClose -> closed at once;
//   * a voice "navigate" / "open" action (a misheard sentence) -> the tab changed under the Modal -> Today unmounted -> Live unmounted.
// And going to another app / the screen sleeping ended the voice session SILENTLY (no reason, no way back).

export type SessionEnd = 'user' | 'background' | 'quiet' | 'dropped' | 'denied' | 'error' | 'screen';

/** Auto-resume the voice when the app comes back within this long; later than that, show a one-tap "Resume voice" instead. */
export const AUTO_RESUME_MS = 3 * 60 * 1000;
/** After this long away the workout voice is not offered back at all (the workout is over for practical purposes). */
export const RESUME_GIVE_UP_MS = 45 * 60 * 1000;

/** What a change of the app's state does to a running voice session. 'inactive' (a permission dialog, the notification shade, a system screen) is not leaving. */
export function appStateEffect(s: string, ctx: { session: boolean; stream: boolean; starting: boolean }): 'keep' | 'pause' {
  if (!ctx.session) return 'keep';
  if (s === 'background') return 'pause';
  if (s !== 'active' && !ctx.stream && !ctx.starting) return 'pause';   // the phone-recogniser session keeps its stricter rule
  return 'keep';
}

/** When the app comes back to the front: carry on by itself, ask for a tap, or forget it. */
export function resumePlan(pausedAt: number | null, now: number): 'none' | 'auto' | 'manual' {
  if (pausedAt == null) return 'none';
  const gone = now - pausedAt;
  if (gone < 0) return 'auto';
  if (gone <= AUTO_RESUME_MS) return 'auto';
  if (gone <= RESUME_GIVE_UP_MS) return 'manual';
  return 'none';
}

/** While the workout screen is open it covers the tab bar: a navigate / open-another-screen action would swap the tab UNDER it and tear the workout down
 *  (and the voice session with it). Those are dropped while the workout is open; the coach's spoken reply still plays. rest / end / refresh are fine. */
export function blockedWhileLive(screen: string, a: { type: string; screen?: string }): boolean {
  if (screen !== 'live') return false;
  if (a.type === 'navigate') return true;
  if (a.type === 'open') return a.screen !== 'live';
  return false;
}

/** Leaving the workout screen (X, or Android Back / the back gesture): ask first when it would cost something. */
export function leaveNeedsConfirm(ctx: { voiceOn: boolean; ended: boolean; setsDone: number; finished: boolean }): boolean {
  if (ctx.ended || ctx.finished) return false;
  return ctx.voiceOn || ctx.setsDone > 0;
}

export type Banner = { kind: 'connecting' | 'listening' | 'thinking' | 'speaking' | 'ended' | 'off'; text: string };

/** The one line that says what the voice is doing. `ended` carries the REASON, so a dead session is never silent. */
export function voiceBanner(t: { session: boolean; phase: string; endReason: string; connecting?: boolean }): Banner {
  if (t.session && t.connecting) return { kind: 'connecting', text: 'Connecting voice…' };
  if (t.session) {
    if (t.phase === 'thinking') return { kind: 'thinking', text: 'Coach is thinking…' };
    if (t.phase === 'speaking') return { kind: 'speaking', text: 'Coach is talking' };
    return { kind: 'listening', text: 'Listening… say your set' };
  }
  if (t.endReason) return { kind: 'ended', text: t.endReason };
  return { kind: 'off', text: '' };
}

/** Plain-words reason for each way a session can end (shown in the app, never just logged). */
export function endText(kind: SessionEnd, detail = ''): string {
  switch (kind) {
    case 'background': return 'Voice paused because you left the app. It comes back when you return.';
    case 'quiet': return 'Voice paused after a few quiet minutes. Tap the mic to carry on.';
    case 'dropped': return 'Voice connection dropped. Tap the mic to reconnect.';
    case 'denied': return 'The voice connection was refused. Check your access key in Profile.';
    case 'screen': return 'Voice stopped because the workout screen closed.';
    case 'error': return detail || "Voice stopped because of an error. Tap the mic to try again.";
    default: return '';
  }
}

/** A dropped connection while the app is in front is retried a couple of times before bothering the user. Returns the wait before the next try, or null = give up. */
export function reconnectDelay(attemptsInLastMinute: number): number | null {
  if (attemptsInLastMinute >= 2) return null;
  return attemptsInLastMinute === 0 ? 700 : 2500;
}
