// The voice engine, mounted once in App.tsx. Two ways in, one brain (POST /voice/command, falling back to /live/say on an older server):
//
//  1. A VOICE SESSION (the main way): the mic button (tab bar, or Train with coach) starts it with one tap; a second tap stops it.
//       * The microphone STAYS ON for the whole conversation (continuous recognition: no restarts, no start/stop beeps).
//       * WE decide when you have finished speaking (voiceEndpoint.ts), not the phone's recognizer, which cuts you off after ~1 s of silence. A sentence
//         that ends on "like", "uhh", "I did" waits up to ~4 s for the rest; "skip" or "bench 60 for 8 reps" is sent after ~0.7 s.
//       * While the coach thinks or speaks the microphone is muted (ignored), so it never hears itself, and listening resumes by itself.
//       * If the answer is slow, a short "one sec" is spoken at once. If the server says your sentence is unfinished, it is held and joined to the next one.
//  2. "HEY COACH" (opt-in, OFF by default; Profile > Talking to your coach): always-on speech-to-text filtered for the phrase. Not a hardware wake word, so
//     it can miss and costs battery. Profile shows live diagnostics.
//
// REAL PLATFORM LIMITS: needs the installed app (Expo Go has no native speech module). Continuous recognition needs Android 13+ (older phones get one
// utterance per start, so the engine restarts it: slower, with a beep). Foreground only.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AppState, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { Ionicons } from '@expo/vector-icons';
import * as Haptics from './haptics';
import { ApiError, getCoach, liveSay, voiceCommand, voiceWsUrl } from './api';
import { VoiceStream } from './streamVoice';
import { makeDevice } from './streamAudioDevice';
import { bargeInEnabled, liveVoiceEnabled } from './voiceSettings';
import { emitVoiceActions, parseAction, VoiceAction } from './voiceBus';
import { getVoiceDay, getVoiceScreen } from './voiceScreen';
import { speak, stopSpeaking } from './tts';
import { shouldUsePremium, getRecognitionLang } from './voiceSettings';
import { acquireMic, micOwner, onMicChange, releaseMic } from './micLock';
import { createWakeTracker, matchWake, wakeNames, wakePhrases } from './wakeWord';
import { getWakeState, loadWakeSetting, onWakeState, patchDiag, patchWakeState } from './wakeSettings';
import { emitVoiceReply, getTalkState, getVoiceHints, hasReplyListener, onTalkState, registerTalk, setTalkState, TalkState } from './voiceTalk';
import { cleanSpeech, Endpointer, joinSegments, pickFiller, tuning } from './voiceEndpoint';
import { voiceErrorMessage } from './useVoice';
import { dlog } from './diag';
import { colors, font } from './theme';
import { Txt } from './ui';

let SR: any = null;
try { SR = require('expo-speech-recognition'); } catch { SR = null; }
const MOD = SR?.ExpoSpeechRecognitionModule;

const CMD_WINDOW_MS = 6000;       // wake mode: a bare "hey coach" waits this long for the command
const STABLE_MS = 1600;           // wake mode: phrase heard + no new words for this long = utterance finished
const BUBBLE_MS = 7000;
const TICK_MS = 120;              // how often the end-of-speech decision is checked
const MUTE_GRACE_MS = 650;        // after the coach stops talking, ignore the microphone a little longer (echo, late partial results)
const FILLER_AFTER_MS = 3200;     // an answer slower than this gets a short spoken "one sec" (normal answers take 1.5-3 s: no filler for those)
const SESSION_QUIET_MS = 5 * 60 * 1000;   // a session ends by itself after this long with nothing said (battery), and says so
const CARRY_MS = 25000;           // an unfinished sentence is held this long and joined to the next one

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
  const skipped = useRef(false);                  // the user cut the coach's sentence short: listen again at once
  const stream = useRef<VoiceStream | null>(null);        // live voice (streamed audio to the PC's voice agent) when it is reachable
  const streamDownAt = useRef(0);                         // it was unreachable at this time: use the built-in recogniser for a while
  const streamQuietT = useRef<ReturnType<typeof setTimeout> | null>(null);
  const connectUntil = useRef(0);                          // taps that arrive while live voice is still connecting are ignored (a second tap used to cancel it)

  const enabled = useRef(getWakeState().enabled);
  const appActive = useRef(AppState.currentState === 'active');
  const alive = useRef(true);
  const perm = useRef<'unknown' | 'granted' | 'denied'>('unknown');

  // ---- a command in flight
  const speaking = useRef(false);
  const busy = useRef(false);                       // a command is being sent / answered / spoken
  const busyAt = useRef(0);
  const cmdSeq = useRef(0);                         // a cancelled command's late answer is ignored
  const fillerT = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fillerN = useRef(0);
  const muteUntil = useRef(0);

  // ---- the voice session
  const session = useRef(false);
  const quietSince = useRef(0);
  const ep = useRef(new Endpointer());
  const committed = useRef('');                     // finished segments of the current utterance
  const partial = useRef('');                       // the segment being spoken now
  const carry = useRef<{ text: string; at: number } | null>(null);   // an unfinished sentence waiting for its other half
  const followNext = useRef(false);                 // the coach just asked a question: the next utterance is its answer
  const talkLive = useRef(false);                   // our recognizer session has really been started (events before that are stale ones from the wake session)
  const talkStartedAt = useRef(0);
  const lastResultAt = useRef(0);
  const tickT = useRef<ReturnType<typeof setInterval> | null>(null);
  const recT = useRef<ReturnType<typeof setTimeout> | null>(null);
  const endTimes = useRef<number[]>([]);
  const contOK = useRef(true);                      // continuous recognition works on this phone (Android 13+)
  const hardErrors = useRef(0);

  // ---- "Hey coach" (wake listener)
  const running = useRef(false);
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
  const diagAt = useRef(0);

  const shouldRun = () => !!MOD && enabled.current && appActive.current && !speaking.current && !busy.current && !session.current && micOwner() !== 'ui' && perm.current !== 'denied';
  const muted = () => busy.current || speaking.current || Date.now() < muteUntil.current;

  const showBubble = (t: string) => {
    setBubble(t);
    if (bubbleT.current) clearTimeout(bubbleT.current);
    bubbleT.current = t ? setTimeout(() => { setBubble(''); if (!session.current && !busy.current) setTalkState({ heard: '', note: '', error: false }); }, BUBBLE_MS) : null;
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

  // ================================================================ "Hey coach" listener
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
      if (getWakeState().diag.sessions <= 3) dlog('wake_session', { onDevice, lang });
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

  // ================================================================ the voice session
  const stopTicker = () => { if (tickT.current) { clearInterval(tickT.current); tickT.current = null; } };

  /** End the session: stop listening, drop anything in flight, free the microphone. `msg` is shown (a reason, or an error). */
  const stopSession = useCallback((msg?: string, isError = false) => {
    const was = session.current;
    session.current = false;
    { const s = stream.current; stream.current = null; if (streamQuietT.current) { clearTimeout(streamQuietT.current); streamQuietT.current = null; } if (s) { try { s.close(); } catch { /* already closed */ } } }
    stopTicker();
    if (recT.current) { clearTimeout(recT.current); recT.current = null; }
    if (fillerT.current) { clearTimeout(fillerT.current); fillerT.current = null; }
    talkLive.current = false;
    if (was) { try { MOD?.abort?.(); } catch { /* not running */ } releaseMic('ui'); dlog('session_stop', msg ? msg.slice(0, 60) : ''); }
    cmdSeq.current++; busy.current = false; speaking.current = false; muteUntil.current = 0;
    committed.current = ''; partial.current = ''; carry.current = null; followNext.current = false; ep.current.reset();
    stopSpeaking();
    setTalkState({ phase: 'idle', heard: '', note: '', session: false, error: isError });
    if (msg) showBubble(msg);
    reconcile();
  }, [reconcile]);

  const showError = (t: string) => { stopSession(t, true); };

  // ---- live voice: one result from the PC's voice agent, applied exactly like a /voice/command answer (the agent speaks it itself)
  const applyStreamResult = useCallback((r: any) => {
    const reply: string = r?.reply ?? '';
    dlog('s_bot', { intent: r?.intent, acts: (r?.actions ?? []).length, expects: r?.expects, reply: reply.slice(0, 80) });
    const acts = (Array.isArray(r?.actions) ? r.actions : []).map(parseAction).filter(Boolean) as VoiceAction[];
    const forLive = getVoiceScreen() === 'live' && hasReplyListener();
    if (forLive) { try { emitVoiceReply(r); } catch { /* the workout screen applies sets / rest itself */ } }
    const forBus = forLive ? acts.filter((a) => !['rest', 'rest_add', 'rest_skip', 'end'].includes(a.type)) : acts;
    if (forBus.length) { try { emitVoiceActions(forBus as any); } catch { /* a UI listener bug must not stop the conversation */ } }
    const did = describeActions(acts);
    if (r?.pr) Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
    else if (acts.length) Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light);
    setTalkState({ note: did });
    if (reply && r?.intent !== 'ignored') showBubble(reply); else if (did) showBubble(did);
  }, []);

  const armStreamQuiet = () => {
    if (streamQuietT.current) clearTimeout(streamQuietT.current);
    streamQuietT.current = setTimeout(() => { if (stream.current) stopSession('Voice paused after a few quiet minutes. Tap the mic to carry on.'); }, SESSION_QUIET_MS);
  };

  /** Try the live voice. true = it is running; false = not available (no server, Expo Go, agent off): the caller uses the built-in recogniser. */
  const startStream = useCallback(async (): Promise<boolean> => {
    if (!liveVoiceEnabled() || Date.now() - streamDownAt.current < 60000) return false;
    const url = voiceWsUrl(); const dev = makeDevice();
    if (!url || !dev) return false;
    session.current = true; quietSince.current = Date.now(); connectUntil.current = Date.now() + 1800 * tuning.scale;
    acquireMic('ui'); if (running.current) stopRun();
    setBubble(''); setTalkState({ phase: 'listening', heard: '', note: '', error: false, session: true });
    const t0 = Date.now();
    const vs = new VoiceStream(url, dev, {
      onFirstMic: (ms) => dlog('s_mic', { ms }),
      onNoMic: (status) => {                                  // connected but the phone delivered no microphone audio: do not leave the user talking to nothing
        if (stream.current !== vs) return;
        dlog('s_nomic', status.slice(0, 160));
        streamDownAt.current = Date.now();
        stream.current = null;
        try { vs.close(); } catch { /* ignore */ }
        if (streamQuietT.current) { clearTimeout(streamQuietT.current); streamQuietT.current = null; }
        session.current = false; releaseMic('ui');
        setTalkState({ phase: 'idle', heard: '', note: '', session: false, error: false });
        setTimeout(() => { if (alive.current) startSessionRef.current(); }, 60);      // the phone's own recogniser takes over (live voice is skipped for a minute)
      },
      onUser: (t) => { dlog('s_user', { text: t.slice(0, 80) }); armStreamQuiet(); setTalkState({ phase: 'thinking', heard: cleanSpeech(t), error: false }); },
      onBot: (r) => { armStreamQuiet(); applyStreamResult(r); },
      onPhase: (p) => { dlog('s_phase', p); setTalkState(p === 'listening' ? { phase: p, heard: '' } : { phase: p }); },
      onClose: (why) => { if (stream.current !== vs) return; dlog('s_close', why); stream.current = null; if (why === 'denied') showError('The voice connection was refused. Check your access key in Profile.'); else if (why === 'closed') showError('Voice connection dropped. Tap the mic to reconnect.'); },
    });
    stream.current = vs;
    try {
      await vs.open(getVoiceScreen(), bargeInEnabled());
    } catch (e) {
      if (stream.current === vs) stream.current = null;
      dlog('s_unavailable', String((e as any)?.message ?? e).slice(0, 80));
      streamDownAt.current = Date.now();
      if (session.current) { session.current = false; releaseMic('ui'); setTalkState({ phase: 'idle', heard: '', note: '', session: false, error: false }); }
      return false;
    }
    if (!session.current) { try { vs.close(); } catch { /* stopped while connecting */ } return true; }
    dlog('s_open', { ms: Date.now() - t0 });
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium);
    armStreamQuiet();
    return true;
  }, [applyStreamResult, stopRun]);

  /** Start (or restart after a break) the recognizer of the session. The session itself stays on. */
  const startRecognizer = useCallback(() => {
    if (!session.current || !alive.current || !MOD) return;
    try {
      MOD.start({
        lang: getRecognitionLang(), interimResults: true, continuous: contOK.current, maxAlternatives: 1, addsPunctuation: false,
        contextualStrings: getVoiceHints(),
        // the phone's own endpointing is only a backstop: we decide when you are done (voiceEndpoint.ts)
        androidIntentOptions: { EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS: 4000, EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS: 4000 },
      });
      talkLive.current = true; talkStartedAt.current = Date.now();
      dlog('rec_start', { cont: contOK.current });
    } catch (e: any) {
      dlog('rec_start_throw', { code: e?.code, msg: String(e?.message ?? e).slice(0, 100) });
      talkLive.current = false;
      if (++hardErrors.current >= 3) showError(voiceErrorMessage(e?.code || e?.error, e?.message) || "Couldn't start the microphone.");
      else scheduleRecRestart(600);
    }
  }, []);

  const scheduleRecRestart = useCallback((ms: number) => {
    if (recT.current || !session.current) return;
    recT.current = setTimeout(() => { recT.current = null; if (session.current && !talkLive.current) startRecognizer(); }, ms);
  }, [startRecognizer]);

  // ---- one utterance to the coach
  const handleCommand = useCallback(async (command: string, followup = false) => {
    if (busy.current && Date.now() - busyAt.current < 45000) return;
    const seq = ++cmdSeq.current;
    const inSession = session.current;
    busy.current = true; busyAt.current = Date.now();
    setTalkState({ phase: 'thinking', heard: command, note: '', error: false }); showBubble('');
    dlog('cmd_send', { followup, screen: getVoiceScreen(), text: command.slice(0, 80) });
    stopRun();
    // a slow answer gets a short spoken "one sec" (phone voice, no network) so the silence never feels like a hang
    if (fillerT.current) clearTimeout(fillerT.current);
    fillerT.current = setTimeout(() => {
      fillerT.current = null;
      if (cmdSeq.current === seq && busy.current && alive.current) { dlog('filler'); speak(pickFiller(fillerN.current++), ttsCfg.current, undefined, { persona: persona.current, premium: false }).catch(() => {}); }
    }, FILLER_AFTER_MS * tuning.scale);
    let expects: string | null = null;
    const t0 = Date.now();
    try {
      let r: any;
      try {
        r = await voiceCommand(command, getVoiceScreen(), getVoiceDay(), followup);
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) r = await liveSay(command, false);   // older server without /voice/command
        else throw e;
      }
      if (fillerT.current) { clearTimeout(fillerT.current); fillerT.current = null; }
      if (seq !== cmdSeq.current) return;                    // cancelled while waiting: say nothing, touch nothing
      const reply: string = r?.reply ?? '';
      dlog('cmd_reply', { ms: Date.now() - t0, intent: r?.intent, acts: (r?.actions ?? []).length, expects: r?.expects, reply: reply.slice(0, 80) });
      if (r?.intent === 'fragment') {                        // "I did like": the server says it is unfinished: hold it, say nothing, join it to what comes next
        carry.current = { text: command, at: Date.now() };
        busy.current = false; stopSpeaking();
        setTalkState({ phase: 'listening', heard: command });
        return;
      }
      const acts = (Array.isArray(r?.actions) ? r.actions : []).map(parseAction).filter(Boolean) as VoiceAction[];
      const forLive = getVoiceScreen() === 'live' && hasReplyListener();
      if (forLive) { try { emitVoiceReply(r); } catch { /* the workout screen applies sets / rest itself */ } }
      const forBus = forLive ? acts.filter((a) => !['rest', 'rest_add', 'rest_skip', 'end'].includes(a.type)) : acts;
      if (forBus.length) { try { emitVoiceActions(forBus as any); } catch { /* a UI listener bug must not stop the reply */ } }
      const did = describeActions(acts);
      if (r?.pr) Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
      else if (acts.length) Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light);
      if (reply && r?.intent !== 'ignored') {
        expects = r?.expects ?? null;
        setTalkState({ note: did }); showBubble(reply); setTalkState({ phase: 'speaking' }); speaking.current = true;
        const premium = shouldUsePremium(!!r.moment) && (!inSession || !!r.pr || r.intent === 'start' || r.intent === 'end');   // premium audio costs seconds of download: only for the big moments
        const spoke0 = Date.now();
        await new Promise<void>((done) => {
          const cap = setTimeout(done, Math.min(40000, 6000 + reply.length * 90));   // never wait forever on a speech engine that forgot to say "done"
          const cfg = inSession ? { ...ttsCfg.current, rate: Math.min(1.3, (ttsCfg.current.rate ?? 1) * 1.15) } : ttsCfg.current;   // a conversation: a little quicker than the narration voice
          speak(reply, cfg, () => { clearTimeout(cap); done(); }, { persona: persona.current, premium }).catch(() => { clearTimeout(cap); done(); });
        });
        dlog('spoke', { ms: Date.now() - spoke0, chars: reply.length, premium });
      } else if (did) { setTalkState({ note: did }); showBubble(did); }
      else if (!inSession) showBubble("I didn't catch a command in that. Tap the mic and try again.");
    } catch (e) {
      if (fillerT.current) { clearTimeout(fillerT.current); fillerT.current = null; }
      if (seq !== cmdSeq.current) return;
      expects = null;
      dlog('cmd_error', String((e as any)?.message ?? e).slice(0, 120));
      const m = e instanceof ApiError && e.status !== 404 && e.message ? e.message : "Couldn't reach your coach. Check the connection and try again.";
      speaking.current = false; busy.current = false;
      if (inSession) { stopSession(m, true); return; }
      showBubble(m); setTalkState({ error: true });
    }
    if (seq !== cmdSeq.current) return;
    speaking.current = false; busy.current = false; muteUntil.current = Date.now() + (skipped.current ? 150 : MUTE_GRACE_MS) * tuning.scale; skipped.current = false;
    if (!alive.current) return;
    followNext.current = !!expects;
    if (session.current) { setTalkState({ phase: 'listening', heard: '' }); return; }      // the microphone never stopped: just listening again
    setTalkState({ phase: 'idle' });
    backoff.current = 500;
    if (expects) startSessionRef.current();                // a Hey-coach command that asked a question: open a session to hear the answer
    else reconcile();
  }, [reconcile, stopRun, stopSession]);

  const startSessionRef = useRef<() => void>(() => {});

  const sendUtterance = useCallback((text: string) => {
    committed.current = ''; partial.current = '';
    let full = text;
    const c = carry.current;
    if (c && Date.now() - c.at < CARRY_MS) full = `${c.text} ${text}`;
    carry.current = null;
    quietSince.current = Date.now();
    const f = followNext.current; followNext.current = false;
    dlog('utt_send', { chars: full.length, follow: f });
    handleCommand(cleanSpeech(full), f);
  }, [handleCommand]);

  const startTicker = useCallback(() => {
    stopTicker();
    tickT.current = setInterval(() => {
      if (!session.current || !alive.current) return;
      const now = Date.now();
      if (carry.current && now - carry.current.at > CARRY_MS) carry.current = null;
      if (muted()) return;
      const out = ep.current.ready(now);
      if (out) { sendUtterance(out); return; }
      if (!ep.current.peek() && !carry.current && now - quietSince.current > SESSION_QUIET_MS) stopSession('Voice paused after a few quiet minutes. Tap the mic to carry on.');
    }, TICK_MS);
  }, [sendUtterance, stopSession]);

  const startSession = useCallback(async () => {
    if (session.current) return;
    if (await startStream()) return;
    if (!MOD) { setTalkState({ phase: 'idle', error: true }); showBubble('Voice needs the installed app build, not Expo Go. Type to your coach instead.'); return; }
    if (session.current) return;
    try {
      const avail = MOD.isRecognitionAvailable?.();
      dlog('recognition_available', avail);
      if (avail === false) { showError('This phone has no speech-recognition service. Install or enable "Speech Recognition & Synthesis" (Google) from the Play Store, then try again.'); return; }
    } catch { /* unknown: try anyway */ }
    try {
      if (perm.current !== 'granted') {
        const p = await MOD.requestPermissionsAsync();
        perm.current = p.granted ? 'granted' : 'denied';
        if (!p.granted) { status(); showError('Microphone permission is off. Allow it in your phone settings.'); return; }
      }
    } catch { showError("Couldn't ask for microphone permission."); return; }
    if (session.current) return;
    const wakeWasRunning = running.current;
    session.current = true; quietSince.current = Date.now();
    committed.current = ''; partial.current = ''; carry.current = null; followNext.current = false; ep.current.reset();
    contOK.current = true; hardErrors.current = 0; endTimes.current = []; lastResultAt.current = 0;
    dlog('session_start', { lang: getRecognitionLang(), wake: wakeWasRunning, owner: micOwner() });
    acquireMic('ui');                                              // takes the mic from the wake listener
    if (running.current) stopRun();
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium);
    setBubble(''); setTalkState({ phase: 'listening', heard: '', note: '', error: false, session: true });
    await new Promise((r) => setTimeout(r, wakeWasRunning ? 600 : 100));   // let a running wake session wind down; its late 'end' event is ignored
    if (!session.current) return;
    startRecognizer();
    startTicker();
  }, [startRecognizer, startTicker, status, stopRun]);
  startSessionRef.current = () => { startSession(); };

  /** A tap while the coach is talking: cut the sentence short, keep the session, listen at once. */
  const skipSpeech = useCallback(() => {
    if (stream.current) { dlog('skip_speech'); stream.current.interrupt(); setTalkState({ phase: 'listening' }); return; }
    if (!speaking.current) return;
    skipped.current = true; dlog('skip_speech'); stopSpeaking();
  }, []);

  /** A screen that speaks by itself (a button press) asks us to ignore the microphone for a while. */
  const muteFor = useCallback((ms: number, exact = false) => { stream.current?.mute(ms); muteUntil.current = exact ? Date.now() + ms : Math.max(muteUntil.current, Date.now() + ms); }, []);

  // ================================================================ "Hey coach": an utterance, a hit, a command
  const dispatch = useCallback((events: ReturnType<typeof tracker.current.feed>) => {
    for (const e of events) {
      if (e.kind === 'wake') { patchDiag({ lastMatch: Date.now() }); Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium); setTalkState({ phase: 'listening', note: 'Heard "Hey coach"' }); }
      else if (e.kind === 'command') handleCommand(e.command, !!e.followup);
    }
    if (events.length === 0) return;
    if (tracker.current.awaiting(Date.now())) setTimeout(() => { if (!busy.current && !session.current) setTalkState({ phase: 'idle', note: '' }); }, CMD_WINDOW_MS + 200);
  }, [handleCommand]);

  // ================================================================ bootstrap + native events
  useEffect(() => {
    alive.current = true;
    const unTalk = onTalkState(setTalk);
    const unReg = registerTalk(() => { startSession(); }, () => { if (stream.current && Date.now() < connectUntil.current) { dlog('tap_ignored_connecting'); return; } stopSession(); }, muteFor, skipSpeech);
    loadWakeSetting().then((on) => { enabled.current = on; reconcile(); });
    const un = onWakeState((s) => { setWakeOn(s.status === 'listening'); if (s.enabled !== enabled.current) { enabled.current = s.enabled; reconcile(); } });
    const unMic = onMicChange(reconcile);
    const app = AppState.addEventListener('change', (s) => { appActive.current = s === 'active'; if (s !== 'active' && session.current) stopSession(); reconcile(); });
    if (!MOD) { status(); return () => { alive.current = false; unTalk(); unReg(); un(); unMic(); app.remove(); { const s = stream.current; stream.current = null; try { s?.close(); } catch { /* ignore */ } } }; }

    const subs = [
      // MOD.addListener, not SR.addSpeechRecognitionListener: the library exports that one as an UNBOUND method, so called through the package it
      // attaches to nothing and no event ever arrives (this is why "Hey coach" and tap-to-talk never heard a word before 0.3.4).
      MOD.addListener('result', (e: any) => {
        const t: string = e.results?.[0]?.transcript ?? '';
        if (!t) return;
        if (session.current) {                                   // the voice session: collect the words, the ticker decides when you are done
          if (!talkLive.current) return;
          const now = Date.now();
          if (muted()) return;                                   // the coach is thinking / talking (or just stopped): ignore, so it never hears itself
          if (!lastResultAt.current || lastResultAt.current < talkStartedAt.current) dlog('first_words', t.slice(0, 60));
          lastResultAt.current = now; hardErrors.current = 0;
          if (e.isFinal) { committed.current = joinSegments(committed.current, t); partial.current = ''; } else partial.current = t;
          const whole = joinSegments(committed.current, partial.current);
          ep.current.feed(whole, now, !!e.isFinal);
          setTalkState({ phase: 'listening', heard: cleanSpeech(joinSegments(carry.current?.text ?? '', whole)) });
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
      MOD.addListener('end', () => {
        if (session.current) {
          if (!talkLive.current) { dlog('rec_stale_end'); return; }
          if (Date.now() - talkStartedAt.current < 500 && lastResultAt.current < talkStartedAt.current) { dlog('rec_stale_end'); return; }   // the wake session winding down, not ours
          talkLive.current = false;
          const now = Date.now();
          if (partial.current) {                                  // the recognizer stopped mid-sentence: keep what it had
            committed.current = joinSegments(committed.current, partial.current); partial.current = '';
            ep.current.feed(committed.current, now, true);
          }
          endTimes.current = [...endTimes.current.filter((x) => now - x < 8000), now];
          if (endTimes.current.length >= 4 && lastResultAt.current < endTimes.current[0] && contOK.current) { contOK.current = false; dlog('cont_off'); }   // restarting without ever hearing anything: no continuous mode here
          dlog('rec_end', { cont: contOK.current });
          scheduleRecRestart(contOK.current ? 100 : 250);
          return;
        }
        if (!running.current) return;                            // ours was stopped on purpose (or a stale end from another session)
        running.current = false; if (micOwner() === 'wake') releaseMic('wake');
        dispatch(tracker.current.flush(Date.now()));
        backoff.current = Date.now() - startedAt.current > 4000 ? 400 : Math.min(backoff.current * 2, 10000);
        status(); reconcile();
      }),
      MOD.addListener('error', (e: any) => {
        const code = e?.error;
        if (session.current) {
          if (!talkLive.current) { dlog('rec_stale_error', code); return; }
          dlog('rec_error', { code, msg: String(e?.message ?? '').slice(0, 100) });
          if (code === 'not-allowed' || code === 'service-not-allowed') { perm.current = 'denied'; status(); showError('Microphone or speech permission was refused. Allow it in your phone settings.'); return; }
          talkLive.current = false;
          if (code === 'no-speech' || code === 'speech-timeout' || code === 'aborted') { scheduleRecRestart(contOK.current ? 100 : 250); return; }   // silence is normal
          if (++hardErrors.current >= 4) { showError(voiceErrorMessage(code, e?.message) || "I can't hear you right now. Tap the mic to try again."); return; }
          scheduleRecRestart(500 * hardErrors.current);
          return;
        }
        patchDiag({ lastError: String(code || e?.message || 'error').slice(0, 60) });
        dlog('wake_error', code);
        if (code === 'not-allowed') { perm.current = 'denied'; running.current = false; if (micOwner() === 'wake') releaseMic('wake'); status(); showError('Microphone or speech permission was refused. Allow it in your phone settings.'); return; }
        if (code === 'language-not-supported' && onDevicePref.current) { onDevicePref.current = false; }
        if (code === 'no-speech' || code === 'aborted' || code === 'speech-timeout') return;   // normal; 'end' follows
        backoff.current = Math.min(backoff.current * 2, 10000);
        if (Date.now() - errAt.current > 30000) { errAt.current = Date.now(); showError('Hey coach is having trouble hearing right now. The mic button still works.'); }
      }),
    ];
    return () => {
      alive.current = false; unTalk(); unReg(); un(); unMic(); app.remove(); subs.forEach((s) => s.remove());
      { const s = stream.current; stream.current = null; if (streamQuietT.current) clearTimeout(streamQuietT.current); try { s?.close(); } catch { /* ignore */ } }
      clearTimers(); if (bubbleT.current) clearTimeout(bubbleT.current);
      stopTicker(); if (recT.current) clearTimeout(recT.current); if (fillerT.current) clearTimeout(fillerT.current);
      if (session.current) { session.current = false; try { MOD.abort?.(); } catch { /* ignore */ } releaseMic('ui'); }
      if (running.current) { running.current = false; try { MOD.abort?.(); } catch { /* ignore */ } }
      if (micOwner() === 'wake') releaseMic('wake'); stopSpeaking(); setTalkState({ phase: 'idle', heard: '', note: '', error: false, session: false });
    };
  }, []);

  // ================================================================ what is shown (only while something is happening)
  const active = talk.phase !== 'idle';
  const label = talk.phase === 'listening' ? (talk.session ? 'Voice on · tap the mic to stop' : 'Listening…') : talk.phase === 'followup' ? 'Your answer?' : talk.phase === 'thinking' ? 'On it…' : talk.phase === 'speaking' ? (talk.note || 'Coach') : talk.note;
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
