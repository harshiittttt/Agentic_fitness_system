// "Hey coach": a foreground-only wake phrase. Mounted once in App.tsx.
//
// How it works: continuous speech recognition (expo-speech-recognition) runs while the app is open on screen; every
// transcript goes through wakeWord.ts. Nothing is stored or sent until the phrase is heard; then the command (same
// breath, or the next utterance within ~6 s) goes to POST /live/say (ambient=false), the reply is spoken and shown.
//
// REAL PLATFORM LIMITS (be honest with users):
//  * Needs a development build / release build. Expo Go has no native speech-recognition module: this component then
//    does nothing and Setup says so.
//  * It is NOT a hardware wake word. It is always-on speech-to-text filtered for a phrase, so it costs battery
//    (microphone + recognizer running whenever the app is in front). Foreground only, by design.
//  * Android recognition sessions end on silence/after a time limit; we restart them with backoff. Many Android
//    versions play a system start/stop beep on every restart (not controllable from JS; on-device recognition on
//    Android 13+ is usually quieter). Continuous mode is not supported on Android 12 and below: sessions are shorter
//    and restart more often there.
//  * On-device recognition is requested (requiresOnDeviceRecognition) so ambient audio is not streamed to a server.
//    If the phone has no offline model for the language, we fall back to the normal (network) recognizer and say so
//    in Setup. Hindi offline models often need a one-time download.
//  * Accuracy in a loud gym is limited: music and neighbours cause misses and false wakes. Mishearings are tolerated
//    in wakeWord.ts ("hey couch"), but the push-to-talk mic and the Live screen remain the reliable paths.
//  * While the coach speaks, or another screen (Live, push-to-talk) holds the mic lock, or the app is in the
//    background, the listener is stopped, so it never hears the coach's own voice or fights another recognizer.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AppState, Platform, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { Ionicons } from '@expo/vector-icons';
import * as Haptics from './haptics';
import { getCoach, liveSay } from './api';
import { speak, stopSpeaking } from './tts';
import { shouldUsePremium, getRecognitionLang } from './voiceSettings';
import { acquireMic, micOwner, onMicChange, releaseMic } from './micLock';
import { createWakeTracker, matchWake, wakeNames, wakePhrases } from './wakeWord';
import { getWakeState, loadWakeSetting, onWakeState, patchWakeState } from './wakeSettings';
import { colors, font } from './theme';
import { Txt } from './ui';

let SR: any = null;
try { SR = require('expo-speech-recognition'); } catch { SR = null; }
const MOD = SR?.ExpoSpeechRecognitionModule;

const CMD_WINDOW_MS = 6000;       // a bare "hey coach" waits this long for the command
const STABLE_MS = 1600;           // phrase heard + no new words for this long = utterance finished (Android may not send isFinal)
const BUBBLE_MS = 6000;

type Phase = 'idle' | 'heard' | 'thinking' | 'speaking';

export default function WakeCoach() {
  const insets = useSafeAreaInsets();
  const [phase, setPhase] = useState<Phase>('idle');
  const [bubble, setBubble] = useState('');
  const [ws, setWs] = useState(getWakeState());

  const enabled = useRef(ws.enabled);
  const appActive = useRef(AppState.currentState === 'active');
  const speaking = useRef(false);
  const busy = useRef(false);                       // a command is being sent / spoken
  const running = useRef(false);
  const perm = useRef<'unknown' | 'granted' | 'denied'>('unknown');
  const onDevicePref = useRef(true);
  const backoff = useRef(500);
  const startedAt = useRef(0);
  const restartT = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stableT = useRef<ReturnType<typeof setTimeout> | null>(null);
  const bubbleT = useRef<ReturnType<typeof setTimeout> | null>(null);
  const names = useRef<string[]>([]);
  const persona = useRef('hype');
  const coachName = useRef('Coach');
  const ttsCfg = useRef({ rate: 1, pitch: 1 } as any);
  const coachAt = useRef(0);
  const tracker = useRef(createWakeTracker([], CMD_WINDOW_MS));
  const alive = useRef(true);

  const shouldRun = () => !!MOD && enabled.current && appActive.current && !speaking.current && !busy.current && micOwner() !== 'ui' && perm.current !== 'denied';

  const showBubble = (t: string) => {
    setBubble(t);
    if (bubbleT.current) clearTimeout(bubbleT.current);
    bubbleT.current = setTimeout(() => { setBubble(''); }, BUBBLE_MS);
  };

  const refreshCoach = async () => {
    if (Date.now() - coachAt.current < 60000) return;
    coachAt.current = Date.now();
    try {
      const c = await getCoach();
      persona.current = c.persona ?? persona.current; coachName.current = c.coach_name ?? coachName.current;
      ttsCfg.current = c.tts ?? ttsCfg.current;
      names.current = wakeNames(coachName.current);
      tracker.current = createWakeTracker(names.current, CMD_WINDOW_MS);
    } catch { /* keep the last known persona */ }
  };

  const status = useCallback(() => {
    if (!MOD) return patchWakeState({ status: 'unavailable', note: 'Not available here. "Hey coach" needs a development build of the app (Expo Go cannot listen continuously).' });
    if (!enabled.current) return patchWakeState({ status: 'off' });
    if (perm.current === 'denied') return patchWakeState({ status: 'denied', note: 'Microphone or speech permission was refused. Allow it in your phone settings.' });
    patchWakeState({ status: running.current ? 'listening' : 'paused' });
  }, []);

  const clearTimers = () => {
    if (restartT.current) { clearTimeout(restartT.current); restartT.current = null; }
    if (stableT.current) { clearTimeout(stableT.current); stableT.current = null; }
  };

  const stopRun = useCallback(() => {
    if (stableT.current) { clearTimeout(stableT.current); stableT.current = null; }
    if (running.current) { running.current = false; try { MOD?.abort?.(); } catch { /* not running */ } }
    tracker.current.reset();
    releaseMic('wake');
    status();
  }, [status]);

  const begin = useCallback(async () => {
    restartT.current = null;
    if (!alive.current || running.current || !shouldRun()) { status(); return; }
    try {
      if (perm.current === 'unknown') {
        const p = await MOD.requestPermissionsAsync();
        perm.current = p.granted ? 'granted' : 'denied';
        if (!p.granted) { status(); return; }
      }
      if (!shouldRun() || running.current) { status(); return; }
      if (!acquireMic('wake')) { status(); return; }
      refreshCoach();
      const lang = getRecognitionLang();
      let onDevice = false;
      try { onDevice = !!MOD.supportsOnDeviceRecognition?.() && onDevicePref.current; } catch { onDevice = false; }
      patchWakeState({
        onDevice,
        note: onDevice ? 'Using on-device recognition: audio stays on your phone.'
          : 'This phone has no offline speech model for your language, so the normal online recognizer is used while listening. Install offline speech (Settings, Language, Google speech services) to keep it on the phone.',
      });
      running.current = true; startedAt.current = Date.now();
      MOD.start({
        lang, interimResults: true, continuous: true, addsPunctuation: true, maxAlternatives: 1,
        requiresOnDeviceRecognition: onDevice,
        contextualStrings: wakePhrases(coachName.current),
      });
      status();
    } catch {
      running.current = false; releaseMic('wake');
      scheduleRestart();
    }
  }, [status]);

  const scheduleRestart = useCallback((ms?: number) => {
    if (restartT.current || !alive.current) return;
    const wait = ms ?? backoff.current;
    restartT.current = setTimeout(() => { restartT.current = null; begin(); }, wait);
  }, [begin]);

  /** Re-evaluate whether we should be listening. Called whenever any input to shouldRun changes. */
  const reconcile = useCallback(() => {
    if (!MOD) { status(); return; }
    if (shouldRun()) { if (!running.current) scheduleRestart(micOwner() === 'wake' ? 300 : 700); }
    else if (running.current) stopRun();
    else status();
  }, [scheduleRestart, stopRun, status]);

  const handleCommand = useCallback(async (command: string) => {
    if (busy.current) return;
    busy.current = true; setPhase('thinking'); showBubble(`"${command}"`);
    stopRun();
    try {
      const r = await liveSay(command, false);
      const reply: string = r?.reply ?? '';
      if (reply && r?.intent !== 'ignored') {
        if (r.pr) Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
        showBubble(reply); setPhase('speaking'); speaking.current = true;
        await new Promise<void>((done) => {
          speak(reply, ttsCfg.current, () => done(), { persona: persona.current, premium: shouldUsePremium(!!r.moment) }).catch(() => done());
        });
      } else setBubble('');
    } catch {
      showBubble("Couldn't reach your coach. Try again.");
    }
    speaking.current = false; busy.current = false;
    if (alive.current) { setPhase('idle'); backoff.current = 500; reconcile(); }
  }, [reconcile, stopRun]);

  const dispatch = useCallback((events: ReturnType<typeof tracker.current.feed>) => {
    for (const e of events) {
      if (e.kind === 'wake') { Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium); setPhase('heard'); }
      else if (e.kind === 'command') handleCommand(e.command);
    }
    if (events.length === 0) return;
    // a bare "hey coach": leave the indicator on for the command window, then drop it
    if (tracker.current.awaiting(Date.now())) setTimeout(() => { if (!busy.current) setPhase((p) => (p === 'heard' ? 'idle' : p)); }, CMD_WINDOW_MS + 200);
  }, [handleCommand]);

  // ---- bootstrap + native events
  useEffect(() => {
    alive.current = true;
    loadWakeSetting().then((on) => { enabled.current = on; reconcile(); });
    const un = onWakeState((s) => { setWs(s); if (s.enabled !== enabled.current) { enabled.current = s.enabled; reconcile(); } });
    const unMic = onMicChange(reconcile);
    const app = AppState.addEventListener('change', (s) => { appActive.current = s === 'active'; reconcile(); });
    if (!MOD) { status(); return () => { alive.current = false; un(); unMic(); app.remove(); }; }

    const subs = [
      SR.addSpeechRecognitionListener('result', (e: any) => {
        if (!running.current || micOwner() !== 'wake' || busy.current) return;
        const t: string = e.results?.[0]?.transcript ?? '';
        if (!t) return;
        dispatch(tracker.current.feed(t, !!e.isFinal, Date.now()));
        if (stableT.current) clearTimeout(stableT.current);
        if (!e.isFinal && matchWake(t, names.current).hit) {
          stableT.current = setTimeout(() => { stableT.current = null; dispatch(tracker.current.flush(Date.now())); }, STABLE_MS);
        }
      }),
      SR.addSpeechRecognitionListener('end', () => {
        if (!running.current) return;                      // ours was stopped on purpose (or a stale end from another session)
        running.current = false; releaseMic('wake');
        dispatch(tracker.current.flush(Date.now()));
        // sessions that lived a while are normal (silence timeout): restart quickly. Immediate deaths back off.
        backoff.current = Date.now() - startedAt.current > 4000 ? 400 : Math.min(backoff.current * 2, 10000);
        status(); reconcile();
      }),
      SR.addSpeechRecognitionListener('error', (e: any) => {
        const code = e?.error;
        if (code === 'not-allowed') { perm.current = 'denied'; running.current = false; releaseMic('wake'); status(); return; }
        if (code === 'language-not-supported' && onDevicePref.current) { onDevicePref.current = false; }
        if (code === 'no-speech' || code === 'aborted' || code === 'speech-timeout') return;   // normal; 'end' follows
        backoff.current = Math.min(backoff.current * 2, 10000);
      }),
    ];
    return () => {
      alive.current = false; un(); unMic(); app.remove(); subs.forEach((s) => s.remove());
      clearTimers(); if (bubbleT.current) clearTimeout(bubbleT.current);
      if (running.current) { running.current = false; try { MOD.abort?.(); } catch { /* ignore */ } }
      releaseMic('wake'); stopSpeaking();
    };
  }, []);

  if (!MOD || !ws.enabled || ws.status === 'denied') return null;
  const show = phase !== 'idle' || ws.status === 'listening';
  if (!show && !bubble) return null;
  const label = phase === 'heard' ? 'Listening…' : phase === 'thinking' ? 'On it…' : phase === 'speaking' ? 'Coach' : ws.onDevice === false ? 'Say "Hey coach" (online)' : 'Say "Hey coach"';
  const hot = phase !== 'idle';
  return (
    <View pointerEvents="none" style={{ position: 'absolute', top: insets.top + 6, left: 0, right: 0, alignItems: 'center', gap: 8, zIndex: 50, elevation: 50 }}>
      {ws.status === 'listening' || phase !== 'idle' ? (
        <View accessibilityLabel={label} accessibilityLiveRegion="polite" style={{ flexDirection: 'row', alignItems: 'center', gap: 6, paddingHorizontal: 12, paddingVertical: 6, borderRadius: 16, maxWidth: '88%', backgroundColor: hot ? 'rgba(198,255,61,0.22)' : 'rgba(20,24,36,0.82)', borderWidth: 1, borderColor: hot ? colors.lime : colors.border }}>
          <Ionicons name={hot ? 'mic' : 'mic-outline'} size={14} color={hot ? colors.lime : colors.faint} />
          <Txt numberOfLines={1} style={{ fontFamily: font.semibold, fontSize: 12, color: hot ? colors.lime : colors.faint, flexShrink: 1 }}>{label}</Txt>
        </View>
      ) : null}
      {!!bubble && (
        <View accessibilityLiveRegion="polite" style={{ maxWidth: '88%', paddingHorizontal: 14, paddingVertical: 10, borderRadius: 16, backgroundColor: '#222838', borderWidth: 1, borderColor: 'rgba(198,255,61,0.35)', shadowColor: '#000', shadowOpacity: 0.45, shadowRadius: 12, shadowOffset: { width: 0, height: 6 }, elevation: 8 }}>
          <Txt style={{ fontSize: 14, lineHeight: 19 }} numberOfLines={4}>{bubble}</Txt>
        </View>
      )}
    </View>
  );
}
