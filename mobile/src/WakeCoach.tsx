// The voice layer, mounted once in App.tsx. Two ways in, one brain (POST /voice/command, falling back to /live/say on an older server):
//
//  1. TAP TO TALK (the main way; works on every phone that can do speech-to-text). The mic button in the tab bar (voiceTalk.ts) starts ONE
//     listening turn: you see your words appear live, they are sent when you stop, the coach answers out loud. If the coach asks a question
//     the next turn starts by itself, so a conversation needs one tap and no wake phrase.
//  2. "HEY COACH" (opt-in, OFF by default; Profile > Hands-free). Always-on speech-to-text filtered for the phrase. It is NOT a hardware wake
//     word, which is why it is unreliable on many phones (the recognizer restarts constantly, some phones beep, loud gyms break it). Free
//     hardware wake words do not exist for a custom phrase any more (Picovoice ended its free keys on 2026-06-30), so this stays an extra.
//     Profile shows live diagnostics: sessions started, results received, the last thing the recognizer heard, the last error.
//
// REAL PLATFORM LIMITS (be honest with users):
//  * Needs the installed app build. Expo Go has no native speech module: this component then does nothing and the mic button says so.
//  * Foreground only. While the coach speaks, or a screen holds the mic lock (Live, a text field's mic), the wake listener is stopped.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AppState, View } from 'react-native';
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
import { getWakeState, loadWakeSetting, onWakeState, patchDiag, patchWakeState } from './wakeSettings';
import { getTalkState, onTalkState, registerTalk, setTalkState, TalkState } from './voiceTalk';
import { voiceErrorMessage } from './useVoice';
import { colors, font } from './theme';
import { Txt } from './ui';

let SR: any = null;
try { SR = require('expo-speech-recognition'); } catch { SR = null; }
const MOD = SR?.ExpoSpeechRecognitionModule;

const CMD_WINDOW_MS = 6000;       // wake mode: a bare "hey coach" waits this long for the command
const STABLE_MS = 1600;           // wake mode: phrase heard + no new words for this long = utterance finished
const BUBBLE_MS = 7000;
const TALK_MAX_MS = 15000;        // a talk turn never listens longer than this
const TALK_SILENCE_MS = 1700;     // ... and ends this long after the last new word (recognizers on some phones never send a final result)

const TAB_NAME: Record<string, string> = { today: 'Today', coach: 'Coach', nutrition: 'Fuel', insights: 'Insights', setup: 'Profile' };
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
  const [talk, setTalk] = useState<TalkState>(getTalkState());
  const [bubble, setBubble] = useState('');
  const [wakeOn, setWakeOn] = useState(getWakeState().status === 'listening');
  const bubbleT = useRef<ReturnType<typeof setTimeout> | null>(null);
  const errAt = useRef(0);

  const enabled = useRef(getWakeState().enabled);
  const appActive = useRef(AppState.currentState === 'active');
  const speaking = useRef(false);
  const busy = useRef(false);                       // a command is being sent / spoken
  const running = useRef(false);                    // the WAKE recognizer session is running
  const talking = useRef(false);                    // a TALK turn owns the recognizer
  const talkText = useRef('');                      // latest words of the talk turn
  const talkSent = useRef(false);
  const talkLive = useRef(false);                   // the talk recognizer session has really been started (events before that are stale ones from the wake session)
  const talkStartedAt = useRef(0);
  const talkFollow = useRef(false);
  const talkT = useRef<ReturnType<typeof setTimeout> | null>(null);       // hard stop
  const talkQuiet = useRef<ReturnType<typeof setTimeout> | null>(null);   // silence stop
  const perm = useRef<'unknown' | 'granted' | 'denied'>('unknown');
  const onDevicePref = useRef(true);
  const backoff = useRef(500);
  const startedAt = useRef(0);
  const restartT = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stableT = useRef<ReturnType<typeof setTimeout> | null>(null);
  const names = useRef<string[]>([]);
  const persona = useRef('hype');
  const coachName = useRef('Coach');
  const ttsCfg = useRef({ rate: 1, pitch: 1 } as any);
  const coachAt = useRef(0);
  const tracker = useRef(createWakeTracker([], CMD_WINDOW_MS));
  const alive = useRef(true);
  const diagAt = useRef(0);

  const shouldRun = () => !!MOD && enabled.current && appActive.current && !speaking.current && !busy.current && !talking.current && micOwner() !== 'ui' && perm.current !== 'denied';

  const showBubble = (t: string) => {
    setBubble(t);
    if (bubbleT.current) clearTimeout(bubbleT.current);
    bubbleT.current = t ? setTimeout(() => { setBubble(''); if (!talking.current && !busy.current) setTalkState({ heard: '', note: '', error: false }); }, BUBBLE_MS) : null;
  };

  const refreshCoach = async () => {
    if (Date.now() - coachAt.current < 60000) return;
    coachAt.current = Date.now();
    try {
      const c = await getCoach();
      persona.current = c.persona ?? persona.current; coachName.current = c.coach_name ?? coachName.current;
      ttsCfg.current = c.tts ?? ttsCfg.current;
      const nn = wakeNames(coachName.current);
      if (nn.join('|') !== names.current.join('|')) { names.current = nn; tracker.current = createWakeTracker(nn, CMD_WINDOW_MS); }
    } catch { /* keep the last known persona */ }
  };

  const status = useCallback(() => {
    if (!MOD) return patchWakeState({ status: 'unavailable', note: 'Not available here. Voice needs the installed app, not Expo Go.' });
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
    if (micOwner() === 'wake') releaseMic('wake');
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
      patchWakeState({ onDevice, note: onDevice ? 'Using on-device recognition: audio stays on your phone.' : 'Using the normal online recognizer while listening (no offline speech model for your language).' });
      running.current = true; startedAt.current = Date.now();
      MOD.start({
        lang, interimResults: true, continuous: true, addsPunctuation: true, maxAlternatives: 1,
        requiresOnDeviceRecognition: onDevice,
        contextualStrings: wakePhrases(coachName.current),
      });
      patchDiag({ sessions: getWakeState().diag.sessions + 1 });
      status();
    } catch (e: any) {
      running.current = false; if (micOwner() === 'wake') releaseMic('wake');
      patchDiag({ lastError: String(e?.message ?? e).slice(0, 80) });
      scheduleRestart();
    }
  }, [status]);

  const scheduleRestart = useCallback((ms?: number) => {
    if (restartT.current || !alive.current) return;
    const wait = ms ?? backoff.current;
    restartT.current = setTimeout(() => { restartT.current = null; begin(); }, wait);
  }, [begin]);

  /** Re-evaluate whether the WAKE listener should be running. Called whenever any input to shouldRun changes. */
  const reconcile = useCallback(() => {
    if (!MOD) { status(); return; }
    if (shouldRun()) { if (!running.current) scheduleRestart(micOwner() === 'wake' ? 300 : 700); }
    else if (running.current) stopRun();
    else status();
  }, [scheduleRestart, stopRun, status]);

  const showError = (t: string) => { showBubble(t); setTalkState({ error: true, note: '' }); };

  // ---------------------------------------------------------------- one command to the coach
  const startTalkRef = useRef<(followup?: boolean) => void>(() => {});

  const handleCommand = useCallback(async (command: string, followup = false) => {
    if (busy.current) return;
    busy.current = true; setTalkState({ phase: 'thinking', heard: command, note: '', error: false }); showBubble('');
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
        setTalkState({ note: did }); showBubble(reply); setTalkState({ phase: 'speaking' }); speaking.current = true;
        await new Promise<void>((done) => {
          speak(reply, ttsCfg.current, () => done(), { persona: persona.current, premium: shouldUsePremium(!!r.moment) }).catch(() => done());
        });
      } else if (did) { setTalkState({ note: did }); showBubble(did); }
      else showBubble("I didn't catch a command in that. Tap the mic and try again.");
    } catch (e) {
      expects = null;
      const m = e instanceof ApiError && e.status !== 404 && e.message ? e.message : "Couldn't reach your coach. Check the connection and try again.";
      showError(m);
    }
    speaking.current = false; busy.current = false;
    if (!alive.current) return;
    setTalkState({ phase: 'idle' });
    backoff.current = 500;
    if (expects) setTimeout(() => { if (alive.current && !busy.current && !talking.current) startTalkRef.current(true); }, 350);   // the coach asked something: listen for the answer
    else reconcile();
  }, [reconcile, stopRun]);

  // ---------------------------------------------------------------- tap to talk
  const endTalk = useCallback((abort: boolean) => {
    if (talkT.current) { clearTimeout(talkT.current); talkT.current = null; }
    if (talkQuiet.current) { clearTimeout(talkQuiet.current); talkQuiet.current = null; }
    const was = talking.current;
    talking.current = false; talkLive.current = false;
    if (was) { try { abort ? MOD?.abort?.() : MOD?.stop?.(); } catch { /* not running */ } releaseMic('ui'); }
  }, []);

  /** The turn is over: send what was heard, or say plainly that nothing was heard. */
  const finishTalk = useCallback((why: 'final' | 'silence' | 'end' | 'timeout' | 'tap', errCode?: string) => {
    if (talkSent.current) return;
    const text = talkText.current.trim();
    const follow = talkFollow.current;
    talkSent.current = true;
    endTalk(false);
    if (text) { setTalkState({ heard: text }); handleCommand(text, follow); return; }
    setTalkState({ phase: 'idle', heard: '' });
    if (follow && why !== 'tap') { reconcile(); return; }          // a follow-up window that nobody answered: end quietly
    if (why === 'tap') { reconcile(); return; }
    showError(errCode ? voiceErrorMessage(errCode) || "Didn't hear anything. Tap the mic and try again." : "Didn't hear anything. Tap the mic and try again.");
    reconcile();
  }, [endTalk, handleCommand, reconcile]);

  const startTalk = useCallback(async (followup = false) => {
    if (!MOD) { setTalkState({ phase: 'idle', error: true }); showBubble('Voice needs the installed app build, not Expo Go. Type to your coach instead.'); return; }
    if (talking.current || busy.current) return;
    if (micOwner() === 'ui') { showBubble('The microphone is in use on this screen right now.'); return; }
    stopSpeaking(); speaking.current = false;
    try {
      if (perm.current !== 'granted') {
        const p = await MOD.requestPermissionsAsync();
        perm.current = p.granted ? 'granted' : 'denied';
        if (!p.granted) { status(); showError('Microphone permission is off. Allow it in your phone settings.'); return; }
      }
    } catch { showError("Couldn't ask for microphone permission."); return; }
    const wakeWasRunning = running.current;
    talking.current = true; talkSent.current = false; talkText.current = ''; talkFollow.current = followup;
    acquireMic('ui');                                              // takes the mic from the wake listener (reconcile stops it)
    if (running.current) stopRun();
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium);
    setBubble(''); setTalkState({ phase: followup ? 'followup' : 'listening', heard: '', note: '', error: false });
    await new Promise((r) => setTimeout(r, wakeWasRunning ? 600 : 120));   // let a running session wind down; its late 'end' event is ignored (talking is set)
    if (!talking.current) return;
    try {
      MOD.start({ lang: getRecognitionLang(), interimResults: true, continuous: false, addsPunctuation: false, maxAlternatives: 1 });
    } catch (e: any) { endTalk(true); setTalkState({ phase: 'idle' }); showError(voiceErrorMessage(e?.code || e?.error, e?.message) || "Couldn't start the microphone."); reconcile(); return; }
    talkLive.current = true; talkStartedAt.current = Date.now();
    talkT.current = setTimeout(() => finishTalk('timeout'), followup ? 9000 : TALK_MAX_MS);
  }, [endTalk, finishTalk, reconcile, status, stopRun]);
  startTalkRef.current = (f) => { startTalk(!!f); };

  const stopTalkOrAnswer = useCallback(() => {
    if (talking.current) { finishTalk('tap'); return; }
    if (speaking.current) { stopSpeaking(); return; }                // cut the coach off
    setTalkState({ phase: 'idle' });
  }, [finishTalk]);

  // ---------------------------------------------------------------- wake mode: an utterance, a hit, a command
  const dispatch = useCallback((events: ReturnType<typeof tracker.current.feed>) => {
    for (const e of events) {
      if (e.kind === 'wake') { patchDiag({ lastMatch: Date.now() }); Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium); setTalkState({ phase: 'listening', note: 'Heard "Hey coach"' }); }
      else if (e.kind === 'command') handleCommand(e.command, !!e.followup);
    }
    if (events.length === 0) return;
    if (tracker.current.awaiting(Date.now())) setTimeout(() => { if (!busy.current && !talking.current) setTalkState({ phase: 'idle', note: '' }); }, CMD_WINDOW_MS + 200);
  }, [handleCommand]);

  // ---------------------------------------------------------------- bootstrap + native events
  useEffect(() => {
    alive.current = true;
    const unTalk = onTalkState(setTalk);
    const unReg = registerTalk(() => { startTalk(false); }, stopTalkOrAnswer);
    loadWakeSetting().then((on) => { enabled.current = on; reconcile(); });
    const un = onWakeState((s) => { setWakeOn(s.status === 'listening'); if (s.enabled !== enabled.current) { enabled.current = s.enabled; reconcile(); } });
    const unMic = onMicChange(reconcile);
    const app = AppState.addEventListener('change', (s) => { appActive.current = s === 'active'; if (s !== 'active' && talking.current) finishTalk('tap'); reconcile(); });
    if (!MOD) { status(); return () => { alive.current = false; unTalk(); unReg(); un(); unMic(); app.remove(); }; }

    const subs = [
      SR.addSpeechRecognitionListener('result', (e: any) => {
        const t: string = e.results?.[0]?.transcript ?? '';
        if (!t) return;
        if (talking.current) {                                   // a tap-to-talk turn: show the words as they come, send when finished
          if (!talkLive.current) return;
          talkText.current = t; setTalkState({ heard: t });
          if (talkQuiet.current) clearTimeout(talkQuiet.current);
          if (e.isFinal) { finishTalk('final'); return; }
          talkQuiet.current = setTimeout(() => finishTalk('silence'), TALK_SILENCE_MS);
          return;
        }
        if (!running.current || micOwner() !== 'wake' || busy.current) return;
        const now = Date.now();
        if (now - diagAt.current > 300 || e.isFinal) { diagAt.current = now; patchDiag({ results: getWakeState().diag.results + 1, lastHeard: t.slice(-90), lastHeardAt: now }); }
        dispatch(tracker.current.feed(t, !!e.isFinal, now));
        if (stableT.current) clearTimeout(stableT.current);
        if (!e.isFinal && (matchWake(t, names.current).hit || tracker.current.followingUp(now))) {
          stableT.current = setTimeout(() => { stableT.current = null; dispatch(tracker.current.flush(Date.now())); }, STABLE_MS);
        }
      }),
      SR.addSpeechRecognitionListener('end', () => {
        if (talking.current) {
          if (!talkLive.current || (Date.now() - talkStartedAt.current < 500 && !talkText.current)) return;   // the wake session winding down, not ours
          finishTalk('end'); return;
        }
        if (!running.current) return;                            // ours was stopped on purpose (or a stale end from another session)
        running.current = false; if (micOwner() === 'wake') releaseMic('wake');
        dispatch(tracker.current.flush(Date.now()));
        backoff.current = Date.now() - startedAt.current > 4000 ? 400 : Math.min(backoff.current * 2, 10000);
        status(); reconcile();
      }),
      SR.addSpeechRecognitionListener('error', (e: any) => {
        const code = e?.error;
        if (talking.current) {
          if (!talkLive.current) return;
          if (code === 'not-allowed') { perm.current = 'denied'; status(); }
          if (code === 'no-speech' || code === 'speech-timeout' || code === 'aborted') { finishTalk('end'); return; }
          finishTalk('end', code);
          return;
        }
        patchDiag({ lastError: String(code || e?.message || 'error').slice(0, 60) });
        if (code === 'not-allowed') { perm.current = 'denied'; running.current = false; if (micOwner() === 'wake') releaseMic('wake'); status(); showError('Microphone or speech permission was refused. Allow it in your phone settings.'); return; }
        if (code === 'language-not-supported' && onDevicePref.current) { onDevicePref.current = false; }
        if (code === 'no-speech' || code === 'aborted' || code === 'speech-timeout') return;   // normal; 'end' follows
        backoff.current = Math.min(backoff.current * 2, 10000);
        if (Date.now() - errAt.current > 30000) { errAt.current = Date.now(); showError('Hey coach is having trouble hearing right now. The mic button still works.'); }
      }),
    ];
    return () => {
      alive.current = false; unTalk(); unReg(); un(); unMic(); app.remove(); subs.forEach((s) => s.remove());
      clearTimers(); if (bubbleT.current) clearTimeout(bubbleT.current); endTalk(true);
      if (running.current) { running.current = false; try { MOD.abort?.(); } catch { /* ignore */ } }
      if (micOwner() === 'wake') releaseMic('wake'); stopSpeaking();
    };
  }, []);

  // ---------------------------------------------------------------- what is shown (only while something is happening)
  const active = talk.phase !== 'idle';
  const label = talk.phase === 'listening' ? 'Listening…' : talk.phase === 'followup' ? 'Your answer?' : talk.phase === 'thinking' ? 'On it…' : talk.phase === 'speaking' ? (talk.note || 'Coach') : talk.note;
  const top = { position: 'absolute' as const, top: insets.top + 6, left: 0, right: 0, alignItems: 'center' as const, gap: 8, zIndex: 50, elevation: 50 };
  const card = (err: boolean) => ({ maxWidth: '88%' as const, paddingHorizontal: 14, paddingVertical: 10, borderRadius: 16, backgroundColor: err ? '#3A1D22' : '#222838', borderWidth: 1, borderColor: err ? 'rgba(255,107,107,0.6)' : 'rgba(198,255,61,0.35)', shadowColor: '#000', shadowOpacity: 0.45, shadowRadius: 12, shadowOffset: { width: 0, height: 6 }, elevation: 8 });
  if (!active && !bubble && !talk.heard && !talk.note) {
    // Hey coach is on and listening: one tiny dot, no text, so it never gets in the way.
    return wakeOn ? <View pointerEvents="none" accessibilityLabel="Hey coach is listening" style={{ position: 'absolute', top: insets.top + 8, right: 14, width: 8, height: 8, borderRadius: 4, backgroundColor: colors.lime, opacity: 0.7, zIndex: 50 }} /> : null;
  }
  return (
    <View pointerEvents="none" style={top}>
      {active || !!label ? (
        <View accessibilityLabel={label} accessibilityLiveRegion="polite" style={{ flexDirection: 'row', alignItems: 'center', gap: 6, paddingHorizontal: 14, paddingVertical: 7, borderRadius: 18, maxWidth: '90%', backgroundColor: 'rgba(24,32,8,0.92)', borderWidth: 1, borderColor: colors.lime }}>
          <Ionicons name="mic" size={15} color={colors.lime} />
          <Txt numberOfLines={1} style={{ fontFamily: font.semibold, fontSize: 13, color: colors.lime, flexShrink: 1 }}>{label}</Txt>
        </View>
      ) : null}
      {!!talk.heard && talk.phase !== 'speaking' && (
        <Txt numberOfLines={3} style={{ maxWidth: '88%', fontSize: 15, lineHeight: 21, color: colors.text, backgroundColor: 'rgba(20,24,36,0.92)', paddingHorizontal: 12, paddingVertical: 8, borderRadius: 12, overflow: 'hidden' }}>{`“${talk.heard}”`}</Txt>
      )}
      {!!bubble && (
        <View accessibilityLiveRegion="polite" style={card(talk.error)}>
          <Txt style={{ fontSize: 14, lineHeight: 19 }} numberOfLines={5}>{bubble}</Txt>
        </View>
      )}
    </View>
  );
}
