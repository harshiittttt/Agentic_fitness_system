import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, KeyboardAvoidingView, Platform, Pressable, ScrollView, TextInput, View } from 'react-native';
import { LinearGradient } from 'expo-linear-gradient';
import { Ionicons } from '@expo/vector-icons';
import * as Haptics from '../haptics';
import { deleteSet, getCoach, getLiveState, getPrefill, getSessionSets, liveCue, liveLog, liveSay, liveStart, Prefill } from '../api';
import { personaById, Tts } from '../personas';
import { speak, stopSpeaking } from '../tts';
import { setRecognitionLang, shouldUsePremium } from '../voiceSettings';
import { getTalkState, muteVoice, onTalkState, onVoiceReply, setVoiceHints, stopTalk, talkAvailable, TalkState, toggleTalk } from '../voiceTalk';
import { Dial } from '../SetLogger';
import SwapSheet from '../SwapSheet';
import { dlog } from '../diag';
import { colors, font, gradients } from '../theme';
import { Card, Confetti, Orb, Ring, Txt } from '../ui';
import WorkoutSummary from '../ux/WorkoutSummary';

type Phase = 'idle' | 'thinking' | 'speaking';

// Lifts where the number on the bar is EXTRA weight (0 = just your bodyweight), and holds counted in seconds (same rule as the set logger).
const BODYWEIGHT = new Set(['pull up', 'push up', 'dips', 'plank', 'hanging leg raise', 'back extension']);
const HOLD = new Set(['plank']);
const RPE: { v: number; hint: string }[] = [{ v: 6, hint: 'Easy' }, { v: 7, hint: 'Solid' }, { v: 8, hint: 'Hard' }, { v: 9, hint: 'Grind' }, { v: 10, hint: 'Max' }];
const cap = (s: string) => s.replace(/\b\w/g, (c) => c.toUpperCase());
const fmt = (n: number) => String(+n.toFixed(2));
const GYM_WORDS = ["warm up", "warm-up", "bench press", "set", "reps", "kilos", "kg", "rest", "skip", "next exercise", "what is next", "done", "I did", "ready", "lets go", "finish workout"];
/** The coach's opening can run to five sentences (a long monologue every time the screen opens): speak and show the first two. */
const brief = (t: string, n = 2) => { const parts = (t || '').match(/[^.!?]+[.!?]+(\s|$)|[^.!?]+$/g); return parts && parts.length > n ? parts.slice(0, n).join('').trim() : (t || ''); };

/** Small round action under the set card. */
function Mini({ icon, label, onPress, color = colors.text, disabled }: { icon: string; label: string; onPress: () => void; color?: string; disabled?: boolean }) {
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={label} disabled={disabled} onPress={() => { Haptics.selectionAsync(); onPress(); }}
      style={({ pressed }) => ({ flex: 1, minHeight: 52, borderRadius: 16, alignItems: 'center', justifyContent: 'center', gap: 2, backgroundColor: pressed ? colors.cardHi : 'rgba(255,255,255,0.05)', opacity: disabled ? 0.4 : 1 })}>
      <Ionicons name={icon as any} size={19} color={color} />
      <Txt numberOfLines={1} style={{ fontFamily: font.semibold, fontSize: 12, color }}>{label}</Txt>
    </Pressable>
  );
}

/** Guided workout with your coach. You tap the numbers (always works, even in a loud gym), the coach reacts, times your rest and calls you back.
 *  Voice is a bonus: tap the mic and say it ("bench 80 for 8", "skip this", "what's next"), or switch on hands-free listening. */
export default function Live({ onClose }: { onClose: () => void }) {
  const [phase, setPhase] = useState<Phase>('idle');
  const [coachName, setCoachName] = useState('Coach');
  const [persona, setPersona] = useState('hype');
  const [state, setState] = useState<any>(null);
  const [selected, setSelected] = useState<string | null>(null);       // lifter picked another exercise from the list
  const [line, setLine] = useState('');
  const [heard, setHeard] = useState('');
  const [rest, setRest] = useState(0);
  const [talkS, setTalkS] = useState<TalkState>(getTalkState());   // the voice session (owned by the voice engine): one tap starts it, the next stops it
  const [sound, setSound] = useState(true);
  const [typing, setTyping] = useState(false);
  const [typed, setTyped] = useState('');
  const [problem, setProblem] = useState('');
  const [opening, setOpening] = useState(true);
  const [ended, setEnded] = useState(false);
  const [burst, setBurst] = useState(0);
  const [swap, setSwap] = useState<string | null>(null);
  // the dials
  const [weight, setWeight] = useState(20);
  const [reps, setReps] = useState(8);
  const [rpe, setRpe] = useState<number | null>(null);
  const [pre, setPre] = useState<Prefill | null>(null);
  const [logging, setLogging] = useState(false);
  const [note, setNote] = useState('');

  const alive = useRef(true);
  const token = useRef(0);                 // invalidates stale "finished speaking" callbacks
  const tts = useRef<Tts>({ rate: 1, pitch: 1 });
  const personaRef = useRef('hype');
  const soundRef = useRef(true);
  const restTotal = useRef(90);
  const restRunning = useRef(false);
  const tick = useRef<ReturnType<typeof setInterval> | null>(null);
  const dialFor = useRef('');              // which exercise the dials were last filled for

  const talk = useCallback((text: string, premium = false) => {
    if (!text) { setPhase('idle'); return; }
    const mine = ++token.current;
    setLine(text);
    if (!soundRef.current) { setPhase('idle'); return; }
    setPhase('speaking');
    muteVoice(60000);                      // never listen to ourselves: the voice engine ignores the microphone until this line is done
    speak(text, tts.current, () => { muteVoice(700, true); if (alive.current && token.current === mine) setPhase('idle'); }, { persona: personaRef.current, premium });
  }, []);

  const startRest = useCallback((sec: number) => {
    if (tick.current) clearInterval(tick.current);
    restTotal.current = sec; restRunning.current = true; setRest(sec);
    tick.current = setInterval(() => setRest((l) => {
      if (l <= 1) { if (tick.current) clearInterval(tick.current); return 0; }
      return l - 1;
    }), 1000);
  }, []);

  /** Everything the server answers (a set, a spoken command, a cue) lands here. */
  const apply = useCallback((r: any, opts: { speakIt?: boolean } = {}) => {
    if (r.state) setState(r.state);
    if (r.intent === 'ignored') { setPhase('idle'); setHeard(''); return; }
    if (r.pr) { Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success); setBurst((b) => b + 1); }
    else if (r.safety_flag) Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning);
    else if (r.intent === 'set') Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium);
    for (const a of r.actions ?? []) {
      if (a.type === 'rest') startRest(a.seconds);
      if (a.type === 'rest_add') { restTotal.current += a.seconds; setRest((x) => x + a.seconds); }
      if (a.type === 'rest_skip') { if (tick.current) clearInterval(tick.current); restRunning.current = false; setRest(0); }
      if (a.type === 'end') { setEnded(true); if (tick.current) clearInterval(tick.current); setRest(0); restRunning.current = false; }
    }
    if (opts.speakIt === false) { setLine(r.reply || ''); setPhase('idle'); } else talk(r.reply, shouldUsePremium(!!r.moment));
  }, [startRest, talk]);

  // ---- what the dials should start from, for whichever exercise is active
  const active: string | null = selected ?? state?.current?.name ?? null;
  const row = state?.exercises?.find((e: any) => e.name === active) ?? null;
  const bw = !!active && BODYWEIGHT.has(active.toLowerCase());
  const hold = !!active && HOLD.has(active.toLowerCase());
  useEffect(() => {
    if (!active || !row || dialFor.current === active) return;
    dialFor.current = active;
    setPre(null); setRpe(null); setNote('');
    setReps(row.reps); setWeight(bw ? 0 : 20);
    let live = true;
    getPrefill(active, row.reps, row.target_rpe ?? 8).then((p) => {
      if (!live || dialFor.current !== active) return;
      setPre(p);
      const w = p.suggest?.weight ?? p.last?.weight ?? row.target?.weight;
      if (w != null) setWeight(+w);
      setReps(p.suggest?.reps ?? p.last?.reps ?? row.reps);
    }).catch(() => { if (live && row.target?.weight != null) setWeight(+row.target.weight); });
    return () => { live = false; };
  }, [active, row?.name]);

  // ---- the main action: log the set from the dials. Answers at once.
  const logIt = async () => {
    if (!active || logging) return;
    setLogging(true); setProblem(''); setNote('');
    dlog('live_log', { active, weight, reps, rpe });
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium);
    try {
      const r = await liveLog(weight, reps, { exercise: active, rpe });
      const done = r.state?.exercises?.find((e: any) => e.name === active);
      if (done && done.done >= done.sets) setSelected(null);                // that lift is finished: the session moves on
      apply(r);
      if (r.next_set && !r.state?.finished && (!done || done.done < done.sets)) { setWeight(+r.next_set.weight); setReps(r.next_set.reps); }
      setRpe(null);
    } catch (e: any) {
      dlog('live_log_fail', String(e?.message ?? e).slice(0, 100));
      setProblem("Couldn't reach your coach, so that set was NOT saved. Check your connection and tap Log again.");
    }
    setLogging(false);
  };

  /** A spoken / typed message or a quick button. `fast` = a button: the instant line, no waiting for the composed one. */
  const send = useCallback(async (text: string, ambient: boolean, fast = false): Promise<boolean> => {
    if (!text.trim()) return true;
    setHeard(text); setPhase('thinking'); setProblem('');
    dlog('live_say', { fast, ambient, text: text.slice(0, 60) });
    try { apply(await liveSay(text, ambient, fast)); if (fast) setSelected(null); return true; }
    catch { setPhase('idle'); setProblem("Couldn't reach your coach. Nothing was lost. Check your connection and try again."); return false; }
  }, [apply]);

  const hintWords = () => [...GYM_WORDS, ...((state?.exercises ?? []).map((e: any) => e.name))];

  const open = useCallback(async () => {
    setProblem(''); setOpening(true);
    dlog('live_open');
    try {
      const c = await getCoach();
      setCoachName(c.coach_name); setPersona(c.persona); personaRef.current = c.persona; tts.current = c.tts ?? tts.current; setRecognitionLang(c.tts?.lang);
      const first = await liveStart();
      apply({ ...first, reply: brief(first.reply) });
    } catch (e: any) { dlog('live_open_fail', String(e?.message ?? e).slice(0, 100)); setProblem("Can't reach your coach. Check your connection, then try again."); }
    setOpening(false);
  }, [apply]);
  useEffect(() => {
    alive.current = true;
    dlog('live_mount');
    open();
    return () => { dlog('live_unmount'); alive.current = false; token.current++; stopSpeaking(); stopTalk(); if (tick.current) clearInterval(tick.current); };
  }, []);

  // Rest finished: the coach calls you back in.
  useEffect(() => {
    if (rest === 0 && restRunning.current) {
      restRunning.current = false;
      Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
      liveCue('rest_over').then((r) => { setState(r.state); talk(r.reply, shouldUsePremium(false)); }).catch(() => {});
    }
  }, [rest, talk]);

  // The voice engine (WakeCoach) answers voice turns itself and speaks; every reply also comes here so sets, the rest timer and the lineup update.
  useEffect(() => onVoiceReply((r) => apply(r, { speakIt: false })), [apply]);
  useEffect(() => onTalkState(setTalkS), []);
  useEffect(() => { setVoiceHints(hintWords()); }, [state?.exercises?.length]);

  /** One tap starts the voice session, the next tap stops it. */
  const onMic = () => {
    if (!talkAvailable()) { setProblem('Voice needs the installed app, not Expo Go. You can still type below.'); return; }
    Haptics.selectionAsync();
    if (!talkS.session && phase === 'speaking') { token.current++; stopSpeaking(); setPhase('idle'); }
    toggleTalk();
  };
  const sendTyped = () => { const t = typed; if (!t.trim()) return; setTyped(''); send(t, false).then((ok) => { if (!ok) setTyped(t); }); };

  /** Remove the most recent set of this session (a wrong tap), put its numbers back in the dials. */
  const undoLast = async () => {
    if (!state?.date || logging) return;
    setLogging(true);
    try {
      const sets = await getSessionSets(state.date);
      const last = sets.length ? sets.reduce((a, b) => (b.id > a.id ? b : a)) : null;
      if (!last) { setNote('Nothing to undo yet.'); }
      else {
        await deleteSet(last.id);
        Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning);
        if (last.weight != null) setWeight(+last.weight);
        if (last.reps) setReps(last.reps);
        setSelected(last.exercise); dialFor.current = last.exercise;
        setNote(`Removed ${last.exercise} ${fmt(+(last.weight ?? 0))} × ${last.reps}. Fix the numbers and log it again.`);
        const r = await getLiveState().catch(() => null);
        if (r) setState(r);
      }
    } catch { setNote("Couldn't undo that. Check your connection and try again."); }
    setLogging(false);
  };

  const voiceOn = talkS.session;
  const display: 'idle' | 'listening' | 'thinking' | 'speaking' = voiceOn && talkS.phase !== 'idle' ? (talkS.phase === 'followup' ? 'listening' : talkS.phase) : phase;
  const accent = personaById(persona).color;
  const total = row?.sets ?? 0;
  const setNo = (row?.done ?? 0) + 1;
  const allDone = !!state?.finished && !ended;
  const hint = pre?.last ? `Last time: ${bw && !+pre.last.weight ? 'bodyweight' : `${fmt(+pre.last.weight)} kg`} × ${pre.last.reps}${pre.last.rpe ? ` @ RPE ${pre.last.rpe}` : ''}`
    : pre ? (bw ? 'Add weight only if you want to. 0 is just your bodyweight.' : 'First time on this lift: pick a weight you could do for a few more reps.') : '';
  const status = voiceOn && display !== 'thinking' && display !== 'speaking' ? 'Voice on · tap the mic to stop' : display === 'listening' ? 'Listening… tap to stop' : display === 'thinking' ? 'Thinking…' : display === 'speaking' ? `${coachName} is talking` : '';

  return (
    <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined} style={{ flex: 1, backgroundColor: colors.bg }}>
      <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingHorizontal: 8, paddingTop: 6 }}>
        <Pressable accessibilityRole="button" accessibilityLabel={ended ? 'Close' : 'Leave session'} onPress={onClose} style={{ width: 52, height: 52, alignItems: 'center', justifyContent: 'center' }}>
          <Ionicons name="close" size={30} color={colors.text} />
        </Pressable>
        <View style={{ alignItems: 'center', flex: 1 }}>
          <Txt style={{ fontFamily: font.bold, fontSize: 16 }}>{coachName}</Txt>
          <Txt style={{ color: colors.dim, fontSize: 13 }} numberOfLines={1}>{state?.day ?? 'Workout'}{state ? ` · ${state.remaining_sets} sets left` : ''}</Txt>
        </View>
        <Pressable accessibilityRole="switch" accessibilityState={{ checked: sound }} accessibilityLabel="Coach voice on or off" onPress={() => { Haptics.selectionAsync(); const n = !sound; setSound(n); soundRef.current = n; if (!n) { token.current++; stopSpeaking(); setPhase('idle'); } }}
          style={{ width: 48, height: 52, alignItems: 'center', justifyContent: 'center', opacity: ended ? 0 : 1 }} disabled={ended}>
          <Ionicons name={sound ? 'volume-high' : 'volume-mute'} size={25} color={sound ? accent : colors.faint} />
        </Pressable>
      </View>

      <ScrollView contentContainerStyle={{ padding: 16, gap: 12, paddingBottom: 100 }} keyboardShouldPersistTaps="handled">
        {ended ? (
          <WorkoutSummary accent={accent} coachLine={line} onDone={onClose} />
        ) : (
          <>
            {/* the coach */}
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
              <Pressable onPress={onMic} accessibilityRole="button" accessibilityLabel={voiceOn ? 'Stop the voice conversation' : 'Talk to your coach'}>
                <Orb state={display} color={accent} size={76} />
              </Pressable>
              <View style={{ flex: 1, gap: 2 }}>
                {opening ? <View style={{ flexDirection: 'row', gap: 8, alignItems: 'center' }}><ActivityIndicator size="small" color={colors.amber} /><Txt style={{ color: colors.amber, fontFamily: font.semibold }}>Getting your session ready…</Txt></View>
                  : <Txt accessibilityLiveRegion="polite" style={{ fontFamily: font.bold, fontSize: line.length > 120 ? 16 : 19, lineHeight: line.length > 120 ? 22 : 26, letterSpacing: -0.2 }}>{line || `Ready when you are.`}</Txt>}
                {!!status && <Txt style={{ color: accent, fontFamily: font.semibold, fontSize: 13 }}>{status}</Txt>}
                {voiceOn && !!talkS.heard && talkS.phase === 'listening' ? <Txt numberOfLines={3} style={{ color: colors.text, fontSize: 14, fontFamily: font.medium }}>“{talkS.heard}”</Txt> : !!heard && !status && <Txt numberOfLines={2} style={{ color: colors.dim, fontSize: 13 }}>You said: {heard}</Txt>}
              </View>
            </View>

            {!!problem && (
              <Card style={{ borderColor: colors.amber + '66', gap: 10 }}>
                <View style={{ flexDirection: 'row', gap: 10 }}>
                  <Ionicons name="alert-circle" size={22} color={colors.amber} />
                  <Txt style={{ flex: 1, fontSize: 15, lineHeight: 22 }}>{problem}</Txt>
                </View>
                {!state && !opening && (
                  <Pressable accessibilityRole="button" onPress={open} style={{ minHeight: 48, alignItems: 'center', justifyContent: 'center', borderRadius: 14, backgroundColor: 'rgba(255,255,255,0.07)' }}>
                    <Txt style={{ fontFamily: font.bold }}>Try again</Txt>
                  </Pressable>
                )}
              </Card>
            )}

            {/* the set */}
            {row && !allDone && (
              <Card style={{ gap: 14 }}>
                <View style={{ flexDirection: 'row', alignItems: 'flex-start', justifyContent: 'space-between', gap: 10 }}>
                  <View style={{ flex: 1 }}>
                    <Txt style={{ color: colors.dim, fontSize: 12, letterSpacing: 1.2, fontFamily: font.semibold }}>{selected && selected !== state?.current?.name ? 'DOING NOW' : 'NOW'}</Txt>
                    <Txt numberOfLines={2} style={{ fontFamily: font.black, fontSize: 25, letterSpacing: -0.4 }}>{cap(active ?? '')}</Txt>
                    <Txt style={{ color: colors.dim, fontSize: 14, marginTop: 2 }}>Set {Math.min(setNo, total)} of {total} · aim {row.reps}{hold ? ' s' : ' reps'} · RPE {row.target_rpe ?? 8}</Txt>
                  </View>
                  <View accessible accessibilityRole="progressbar" accessibilityLabel={`${row.done} of ${total} sets done`} style={{ flexDirection: 'row', gap: 5, alignItems: 'center', paddingTop: 10 }}>
                    {Array.from({ length: Math.min(total, 10) }).map((_, k) => (
                      <View key={k} style={{ width: 11, height: 11, borderRadius: 6, backgroundColor: k < row.done ? accent : 'transparent', borderWidth: 1.5, borderColor: k < row.done ? accent : 'rgba(255,255,255,0.25)' }} />
                    ))}
                  </View>
                </View>
                {!!hint && <Txt style={{ color: colors.dim, fontSize: 13.5 }}>{hint}</Txt>}
                <Dial label="Weight" unit={hold ? 'kg added' : bw ? 'kg added (0 = bodyweight)' : 'kg'} value={weight} onChange={setWeight} step={pre?.increment || 2.5} min={0} max={600} decimals />
                <Dial label={hold ? 'Seconds' : 'Reps'} unit={hold ? 'seconds' : 'reps'} value={reps} onChange={setReps} step={hold ? 5 : 1} min={1} max={200} decimals={false} />
                <View style={{ flexDirection: 'row', gap: 6 }} accessibilityLabel="How hard was it, optional">
                  {RPE.map((x) => (
                    <Pressable key={x.v} accessibilityRole="button" accessibilityState={{ selected: rpe === x.v }} accessibilityLabel={`RPE ${x.v}, ${x.hint}`} onPress={() => { Haptics.selectionAsync(); setRpe(rpe === x.v ? null : x.v); }}
                      style={{ flex: 1, minHeight: 46, borderRadius: 14, alignItems: 'center', justifyContent: 'center', backgroundColor: rpe === x.v ? accent : 'rgba(255,255,255,0.06)' }}>
                      <Txt style={{ fontFamily: font.black, fontSize: 15, color: rpe === x.v ? '#07080B' : colors.text }}>{x.v}</Txt>
                      <Txt style={{ fontSize: 10.5, color: rpe === x.v ? '#07080B' : colors.dim }}>{x.hint}</Txt>
                    </Pressable>
                  ))}
                </View>
                <Pressable accessibilityRole="button" accessibilityLabel={`Log set ${Math.min(setNo, total)} of ${total}`} onPress={logIt} disabled={logging}
                  style={({ pressed }) => ({ opacity: pressed || logging ? 0.85 : 1, transform: [{ scale: pressed ? 0.98 : 1 }] })}>
                  <LinearGradient colors={gradients.lime} start={{ x: 0, y: 0 }} end={{ x: 1, y: 1 }} style={{ borderRadius: 22, minHeight: 68, flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 10 }}>
                    {logging ? <ActivityIndicator color="#07080B" /> : <Ionicons name="checkmark-circle" size={26} color="#07080B" />}
                    <Txt style={{ color: '#07080B', fontFamily: font.black, fontSize: 19 }}>Log set {Math.min(setNo, total)} of {total}</Txt>
                  </LinearGradient>
                </Pressable>
                {!!note && <Txt accessibilityLiveRegion="polite" style={{ color: colors.mint, fontSize: 13.5, lineHeight: 19 }}>{note}</Txt>}
                <View style={{ flexDirection: 'row', gap: 8 }}>
                  <Mini icon="arrow-undo" label="Undo last" onPress={undoLast} disabled={logging} color={colors.amber} />
                  <Mini icon="swap-horizontal" label="Swap" onPress={() => active && setSwap(active)} />
                  <Mini icon="play-skip-forward" label="Skip" onPress={() => send(active && active !== state?.current?.name ? `skip ${active}` : 'skip this exercise', false, true)} />
                  <Mini icon={typing ? 'keypad' : 'chatbubble-ellipses'} label="Type" onPress={() => setTyping((t) => !t)} color={colors.cyan} />
                </View>
              </Card>
            )}

            {rest > 0 && (
              <Card style={{ flexDirection: 'row', alignItems: 'center', gap: 16 }}>
                <Ring size={76} stroke={8} progress={rest / restTotal.current} from={colors.cyan} to={colors.violet}>
                  <Txt style={{ fontFamily: font.black, fontSize: rest >= 60 ? 18 : 22 }}>{rest >= 60 ? `${Math.floor(rest / 60)}:${String(rest % 60).padStart(2, '0')}` : rest}</Txt>
                </Ring>
                <View style={{ flex: 1 }} accessible accessibilityLabel={`Resting, ${rest} seconds left`}>
                  <Txt style={{ fontFamily: font.bold, fontSize: 18 }}>Resting</Txt>
                  <Txt style={{ color: colors.dim, fontSize: 14, lineHeight: 20 }}>I'll call you back in.</Txt>
                </View>
                <Pressable accessibilityRole="button" accessibilityLabel="Add 30 seconds of rest" onPress={() => { Haptics.selectionAsync(); restTotal.current += 30; setRest((r) => r + 30); }}
                  style={({ pressed }) => ({ minWidth: 52, height: 52, paddingHorizontal: 8, borderRadius: 26, alignItems: 'center', justifyContent: 'center', backgroundColor: pressed ? 'rgba(255,255,255,0.14)' : 'rgba(255,255,255,0.07)' })}>
                  <Txt style={{ fontFamily: font.bold, fontSize: 13 }}>+30s</Txt>
                </Pressable>
                <Pressable accessibilityRole="button" accessibilityLabel="Skip rest" onPress={() => { if (tick.current) clearInterval(tick.current); restRunning.current = false; setRest(0); }}
                  style={{ width: 52, height: 52, borderRadius: 26, alignItems: 'center', justifyContent: 'center', backgroundColor: 'rgba(255,255,255,0.07)' }}>
                  <Ionicons name="play-skip-forward" size={22} color={colors.text} />
                </Pressable>
              </Card>
            )}

            {typing && (
              <View style={{ flexDirection: 'row', gap: 10, alignItems: 'center' }}>
                <TextInput value={typed} onChangeText={setTyped} placeholder="Tell your coach: bench 80 for 8, my knee hurts…" placeholderTextColor={colors.faint}
                  accessibilityLabel="Tell your coach what you did" onSubmitEditing={sendTyped} returnKeyType="send"
                  style={{ flex: 1, minHeight: 52, backgroundColor: colors.card, color: colors.text, borderRadius: 26, paddingHorizontal: 18, paddingVertical: 13, fontFamily: font.medium, fontSize: 16, borderWidth: 1, borderColor: colors.border }} />
                {phase === 'thinking' ? <ActivityIndicator color={accent} style={{ width: 52 }} /> : (
                  <Pressable accessibilityRole="button" accessibilityLabel="Send" onPress={sendTyped} disabled={!typed.trim()}
                    style={{ width: 52, height: 52, borderRadius: 26, backgroundColor: accent, alignItems: 'center', justifyContent: 'center', opacity: typed.trim() ? 1 : 0.45 }}>
                    <Ionicons name="arrow-up" size={26} color="#07080B" />
                  </Pressable>
                )}
              </View>
            )}

            {/* the lineup */}
            {state?.exercises?.length > 0 && (
              <Card style={{ gap: 2, paddingVertical: 8 }}>
                <Txt style={{ color: colors.dim, fontSize: 12, letterSpacing: 1.2, fontFamily: font.semibold, paddingHorizontal: 4, paddingVertical: 6 }}>TODAY'S LINEUP · TAP TO DO ONE NOW</Txt>
                {state.exercises.map((e: any) => {
                  const done = e.done >= e.sets, on = e.name === active;
                  return (
                    <Pressable key={e.name} accessibilityRole="button" accessibilityLabel={`${cap(e.name)}, ${e.skipped ? 'skipped' : `${e.done} of ${e.sets} sets`}`} disabled={done || e.skipped}
                      onPress={() => { Haptics.selectionAsync(); if (e.name !== active) { setSelected(e.name); } }}
                      style={{ minHeight: 50, flexDirection: 'row', alignItems: 'center', gap: 10, paddingHorizontal: 8, borderRadius: 14, backgroundColor: on ? accent + '1F' : 'transparent' }}>
                      <Ionicons name={e.skipped ? 'remove-circle-outline' : done ? 'checkmark-circle' : on ? 'radio-button-on' : 'ellipse-outline'} size={22} color={done ? colors.mint : on ? accent : colors.faint} />
                      <Txt numberOfLines={1} style={{ flex: 1, fontFamily: on ? font.bold : font.medium, fontSize: 15.5, color: done || e.skipped ? colors.faint : colors.text, textDecorationLine: e.skipped ? 'line-through' : 'none' }}>{cap(e.name)}</Txt>
                      <Txt style={{ color: colors.dim, fontSize: 13.5 }}>{e.skipped ? 'skipped' : `${e.done}/${e.sets}`}</Txt>
                    </Pressable>
                  );
                })}
              </Card>
            )}

            {allDone ? (
              <View style={{ gap: 8 }}>
                <Txt style={{ color: colors.dim, textAlign: 'center', fontSize: 14 }}>Every planned set is done.</Txt>
                <Pressable accessibilityRole="button" accessibilityLabel="Finish workout" onPress={() => send('wrap it up', false, true)}>
                  <LinearGradient colors={gradients.lime} start={{ x: 0, y: 0 }} end={{ x: 1, y: 1 }} style={{ borderRadius: 22, minHeight: 64, flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 10 }}>
                    <Ionicons name="trophy" size={24} color="#07080B" /><Txt style={{ color: '#07080B', fontFamily: font.black, fontSize: 18 }}>Finish workout</Txt>
                  </LinearGradient>
                </Pressable>
              </View>
            ) : (
              <View style={{ flexDirection: 'row', gap: 8 }}>
                <Mini icon="time" label="30 min left" color={colors.cyan} onPress={() => send('I only have 30 minutes', false, true)} />
                <Mini icon="help-circle" label="What's next?" onPress={() => send("what's next", false, true)} />
                <Mini icon="flag" label="Finish up" color={colors.amber} onPress={() => send('wrap it up', false, true)} />
              </View>
            )}
          </>
        )}
      </ScrollView>

      {!ended && talkAvailable() && (
        <View pointerEvents="box-none" style={{ position: 'absolute', right: 16, bottom: 18 }}>
          <Pressable accessibilityRole="button" accessibilityLabel={voiceOn ? 'Stop the voice conversation' : 'Talk to your coach'} onPress={onMic}
            style={({ pressed }) => ({ width: 64, height: 64, borderRadius: 32, alignItems: 'center', justifyContent: 'center', backgroundColor: voiceOn ? colors.cyan : accent, transform: [{ scale: pressed ? 0.94 : 1 }], shadowColor: '#000', shadowOpacity: 0.5, shadowRadius: 12, shadowOffset: { width: 0, height: 5 }, elevation: 10 })}>
            <Ionicons name={voiceOn ? 'stop' : 'mic'} size={voiceOn ? 24 : 28} color="#07080B" />
          </Pressable>
        </View>
      )}
      <SwapSheet exercise={swap} dayIndex={state?.day_index ?? 0} onClose={() => setSwap(null)} onSwapped={() => { setSwap(null); dialFor.current = ''; setSelected(null); setNote('Swapped for today.'); getLiveState().then((r) => { if (r) setState(r); }).catch(() => {}); }} />
      <Confetti burst={burst} />
    </KeyboardAvoidingView>
  );
}
