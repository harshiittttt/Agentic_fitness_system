// "Hey coach": a foreground-only wake phrase. Mounted once in App.tsx.
//
// How it works: continuous speech recognition (expo-speech-recognition) runs while the app is open on screen; every
// transcript goes through wakeWord.ts. Nothing is stored or sent until the phrase is heard; then the command (same
// breath, or the next utterance within ~6 s) goes to POST /voice/command (falls back to /live/say on an older
// server), the reply is spoken and shown, and the returned actions (open a tab, rest timer...) go to voiceBus. When the coach
// asks a question (expects set), a one-shot ~8 s window hears the answer without the wake phrase.
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
import { ApiError, getCoach, liveSay, voiceCommand } from './api';
import { emitVoiceActions, parseAction, VoiceAction } from './voiceBus';
import { getVoiceDay, getVoiceScreen } from './voiceScreen';
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
const FOLLOWUP_MS = 8000;         // after the coach asks something, the next utterance is the answer (no wake phrase)

type Phase = 'idle' | 'heard' | 'thinking' | 'speaking' | 'followup';

const TAB_NAME: Record<string, string> = { today: 'Today', coach: 'Coach', nutrition: 'Fuel', insights: 'Insights', setup: 'Setup' };
/** One short human line for what the phone just did, from the actions the server sent ('Opened Fuel'). */
export function describeActions(actions: VoiceAction[]): string {
  const out: string[] = [];
  for (const a of actions) {
    if (a.type === 'navigate') out.push(`Opened ${TAB_NAME[a.tab] ?? a.tab}`);
    else if (a.type === 'open') out.push(`Opened ${a.screen}`);
    else if (a.type === 'close') out.push('Closed');
    else if (a.type === 'rest') out.push(`Rest ${a.seconds}s`);
    else if (a.type === 'rest_add') out.push(`+${a.seconds}s rest`);
    else if (a.type === 'rest_skip') out.push('Rest skipped');
    else if (a.type === 'end') out.push('Workout ended');
    else if (a.type === 'nutrition_day') out.push(`Fuel: ${a.day}`);
    else if (a.type === 'setting') out.push(`${a.key} set to ${String(a.value)}`);
  }
  return out.slice(0, 2).join(', ');
}

export default function WakeCoach() {
  const insets = useSafeAreaInsets();
  const [phase, setPhase] = useState<Phase>('idle');
  const [bubble, setBubble] = useState('');
  const [ws, setWs] = useState(getWakeState());
  const [heard, setHeard] = useState('');
  const [note, setNote] = useState('');          // what happened ('Opened Fuel'), shown in the pill
  const [isErr, setIsErr] = useState(false);
  const followT = useRef<ReturnType<typeof setTimeout> | null>(null);
  const errAt = useRef(0);

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
    setBubble(t); setIsErr(false);
    if (bubbleT.current) clearTimeout(bubbleT.current);
    bubbleT.current = setTimeout(() => { setBubble(''); setHeard(''); setNote(''); setIsErr(false); }, BUBBLE_MS);
  };

  const refreshCoach = async () => {
    if (Date.now() - coachAt.current < 60000) return;
    coachAt.current = Date.now();
    try {
      const c = await getCoach();
      persona.current = c.persona ?? persona.current; coachName.current = c.coach_name ?? coachName.current;
      ttsCfg.current = c.tts ?? ttsCfg.current;
      const nn = wakeNames(coachName.current);
      if (nn.join('|') !== names.current.join('|')) { names.current = nn; tracker.current = createWakeTracker(nn, CMD_WINDOW_MS); }   // keep the tracker otherwise (it may hold a follow-up window)
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

  const showError = (t: string) => { showBubble(t); setIsErr(true); setNote(''); };

  const handleCommand = useCallback(async (command: string, followup = false) => {
    if (busy.current) return;
    busy.current = true; setPhase('thinking'); setHeard(command); setNote(''); showBubble('');
    if (followT.current) { clearTimeout(followT.current); followT.current = null; }
    stopRun();
    let expects: string | null = null;
    try {
      let r: any;
      try {
        r = await voiceCommand(command, getVoiceScreen(), getVoiceDay(), followup);
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) r = await liveSay(command, false);   // older server without /voice/command
        else throw e;
      }
      const reply: string = r?.reply ?? '';
      const acts = (Array.isArray(r?.actions) ? r.actions : []).map(parseAction).filter(Boolean) as VoiceAction[];
      if (acts.length) { try { emitVoiceActions(r.actions); } catch { /* a UI listener bug must not stop the reply */ } }
      const did = describeActions(acts);
      if (r?.pr) Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
      else if (acts.length) Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light);
      if (reply && r?.intent !== 'ignored') {
        expects = r?.expects ?? null;
        setNote(did); showBubble(reply); setPhase('speaking'); speaking.current = true;
        await new Promise<void>((done) => {
          speak(reply, ttsCfg.current, () => done(), { persona: persona.current, premium: shouldUsePremium(!!r.moment) }).catch(() => done());
        });
      } else if (did) { setNote(did); showBubble(did); }
      else { showBubble("I didn't catch a command in that. Try again."); }
    } catch (e) {
      expects = null;
      const m = e instanceof ApiError && e.status !== 404 && e.message ? e.message : "Couldn't reach your coach. Check the connection and try again.";
      showError(m);
    }
    speaking.current = false; busy.current = false;
    if (!alive.current) return;
    if (expects) {
      // one-shot window: the next thing said is the answer, no "Hey coach" needed
      tracker.current.expectFollowup(Date.now(), FOLLOWUP_MS);
      setPhase('followup'); setHeard('');
      followT.current = setTimeout(() => {
        followT.current = null;
        if (busy.current) return;
        tracker.current.cancelFollowup(); setPhase((p) => (p === 'followup' ? 'idle' : p));
      }, FOLLOWUP_MS + 1200);
    } else setPhase('idle');
    backoff.current = 500; reconcile();
  }, [reconcile, stopRun]);

  const dispatch = useCallback((events: ReturnType<typeof tracker.current.feed>) => {
    for (const e of events) {
      if (e.kind === 'wake') { Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium); setPhase('heard'); }
      else if (e.kind === 'command') handleCommand(e.command, !!e.followup);
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
        if (!e.isFinal && (matchWake(t, names.current).hit || tracker.current.followingUp(Date.now()))) {
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
        if (code === 'not-allowed') { perm.current = 'denied'; running.current = false; releaseMic('wake'); status(); showError('Microphone or speech permission was refused. Allow it in your phone settings to use Hey coach.'); return; }
        if (code === 'language-not-supported' && onDevicePref.current) { onDevicePref.current = false; }
        if (code === 'no-speech' || code === 'aborted' || code === 'speech-timeout') return;   // normal; 'end' follows
        backoff.current = Math.min(backoff.current * 2, 10000);
        if (Date.now() - errAt.current > 30000) { errAt.current = Date.now(); showError('Voice is having trouble hearing right now. Retrying. The mic button still works.'); }
      }),
    ];
    return () => {
      alive.current = false; un(); unMic(); app.remove(); subs.forEach((s) => s.remove());
      clearTimers(); if (followT.current) clearTimeout(followT.current); if (bubbleT.current) clearTimeout(bubbleT.current);
      if (running.current) { running.current = false; try { MOD.abort?.(); } catch { /* ignore */ } }
      releaseMic('wake'); stopSpeaking();
    };
  }, []);

  const bubbleView = () => (
    <View accessibilityLiveRegion="polite" style={{ maxWidth: '88%', paddingHorizontal: 14, paddingVertical: 10, borderRadius: 16, backgroundColor: isErr ? '#3A1D22' : '#222838', borderWidth: 1, borderColor: isErr ? 'rgba(255,107,107,0.6)' : 'rgba(198,255,61,0.35)', shadowColor: '#000', shadowOpacity: 0.45, shadowRadius: 12, shadowOffset: { width: 0, height: 6 }, elevation: 8 }}>
      <Txt style={{ fontSize: 14, lineHeight: 19 }} numberOfLines={4}>{bubble}</Txt>
    </View>
  );
  const top = { position: 'absolute' as const, top: insets.top + 6, left: 0, right: 0, alignItems: 'center' as const, gap: 8, zIndex: 50, elevation: 50 };

  // Not available / off / denied: Setup explains it; only a failure bubble (never silent) may still be showing here.
  if (!MOD || !ws.enabled || ws.status === 'denied') return bubble ? <View pointerEvents="none" style={top}>{bubbleView()}</View> : null;
  const show = phase !== 'idle' || ws.status === 'listening';
  if (!show && !bubble && !heard && !note) return null;
  const label = phase === 'followup' ? 'Listening for your answer' : phase === 'heard' ? 'Listening…' : phase === 'thinking' ? 'On it…'
    : phase === 'speaking' ? (note || 'Coach') : note ? note : ws.onDevice === false ? 'Say "Hey coach" (online)' : 'Say "Hey coach"';
  const hot = phase !== 'idle' || !!note;
  const ink = hot ? colors.lime : '#C5CCDA';
  return (
    <View pointerEvents="none" style={top}>
      {ws.status === 'listening' || phase !== 'idle' || !!note ? (
        <View accessibilityLabel={label} accessibilityLiveRegion="polite" style={{ flexDirection: 'row', alignItems: 'center', gap: 6, paddingHorizontal: 14, paddingVertical: 7, borderRadius: 18, maxWidth: '90%', backgroundColor: hot ? 'rgba(24,32,8,0.92)' : 'rgba(20,24,36,0.92)', borderWidth: 1, borderColor: hot ? colors.lime : colors.border }}>
          <Ionicons name={hot ? 'mic' : 'mic-outline'} size={15} color={ink} />
          <Txt numberOfLines={1} style={{ fontFamily: font.semibold, fontSize: 13, color: ink, flexShrink: 1 }}>{label}</Txt>
        </View>
      ) : null}
      {!!heard && (
        <Txt numberOfLines={2} style={{ maxWidth: '88%', fontSize: 12, color: '#C5CCDA', backgroundColor: 'rgba(20,24,36,0.85)', paddingHorizontal: 10, paddingVertical: 4, borderRadius: 10, overflow: 'hidden' }}>{`Heard: "${heard}"`}</Txt>
      )}
      {!!bubble && bubbleView()}
    </View>
  );
}
