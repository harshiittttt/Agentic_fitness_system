import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, KeyboardAvoidingView, Platform, Pressable, ScrollView, TextInput, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import * as Haptics from '../haptics';
import { getCoach, liveCue, liveSay, liveStart } from '../api';
import { personaById, Tts } from '../personas';
import { speak, stopSpeaking } from '../tts';
import { setRecognitionLang, shouldUsePremium } from '../voiceSettings';
import { useVoice } from '../useVoice';
import { colors, font } from '../theme';
import { Card, Confetti, GradientButton, Orb, Ring, Txt } from '../ui';
import WorkoutSummary from '../ux/WorkoutSummary';

type Phase = 'idle' | 'thinking' | 'speaking';

/** A big, thumb-sized quick action. */
function Quick({ label, icon, color = colors.text, onPress }: { label: string; icon: string; color?: string; onPress: () => void }) {
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={label} onPress={() => { Haptics.selectionAsync(); onPress(); }}
      style={({ pressed }) => ({ width: '48.5%', minHeight: 56, borderRadius: 18, flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 8, paddingHorizontal: 10,
        backgroundColor: pressed ? colors.cardHi : colors.card, borderWidth: 1, borderColor: color === colors.text ? colors.border : color + '55' })}>
      <Ionicons name={icon as any} size={18} color={color} />
      <Txt style={{ fontFamily: font.bold, fontSize: 15, color, flexShrink: 1 }} numberOfLines={2}>{label}</Txt>
    </Pressable>
  );
}

/** Full-screen training session with the coach: it talks, listens, tracks your sets and keeps you company. */
export default function Live({ onClose }: { onClose: () => void }) {
  const [phase, setPhase] = useState<Phase>('idle');
  const [coachName, setCoachName] = useState('Coach');
  const [persona, setPersona] = useState('hype');
  const [state, setState] = useState<any>(null);
  const [line, setLine] = useState('');
  const [heard, setHeard] = useState('');
  const [rest, setRest] = useState(0);
  const [handsFree, setHandsFree] = useState(true);
  const [typed, setTyped] = useState('');
  const [problem, setProblem] = useState('');
  const [opening, setOpening] = useState(true);
  const [ended, setEnded] = useState(false);
  const [burst, setBurst] = useState(0);

  const alive = useRef(true);
  const token = useRef(0);               // invalidates stale "finished speaking" callbacks
  const tts = useRef<Tts>({ rate: 1, pitch: 1 });
  const personaRef = useRef('hype');
  const stopListenRef = useRef<() => void>(() => {});
  const manual = useRef(false);          // true when the lifter tapped the orb (push-to-talk) instead of ambient listening
  const restTotal = useRef(90);
  const restRunning = useRef(false);
  const tick = useRef<ReturnType<typeof setInterval> | null>(null);

  const talk = useCallback((text: string, premium = false) => {
    if (!text) { setPhase('idle'); return; }
    const mine = ++token.current;
    setLine(text); setPhase('speaking');
    stopListenRef.current();             // never listen to ourselves
    speak(text, tts.current, () => { if (alive.current && token.current === mine) setPhase('idle'); }, { persona: personaRef.current, premium });
  }, []);

  const startRest = useCallback((sec: number) => {
    if (tick.current) clearInterval(tick.current);
    restTotal.current = sec; restRunning.current = true; setRest(sec);
    tick.current = setInterval(() => setRest((l) => {
      if (l <= 1) { if (tick.current) clearInterval(tick.current); return 0; }
      return l - 1;
    }), 1000);
  }, []);

  const apply = useCallback((r: any) => {
    if (r.state) setState(r.state);
    if (r.intent === 'ignored') { setPhase('idle'); setHeard(''); return; }
    if (r.pr) { Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success); setBurst((b) => b + 1); }
    else if (r.safety_flag) Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning);
    else if (r.intent === 'set') Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium);
    for (const a of r.actions ?? []) {
      if (a.type === 'rest') startRest(a.seconds);
      if (a.type === 'end') { setEnded(true); if (tick.current) clearInterval(tick.current); setRest(0); restRunning.current = false; }
    }
    talk(r.reply, shouldUsePremium(!!r.moment));
  }, [startRest, talk]);

  /** Returns true when the coach heard it, so a typed message can be put back in the box if the network failed. */
  const send = useCallback(async (text: string, ambient: boolean): Promise<boolean> => {
    if (!text.trim()) return true;
    setHeard(text); setPhase('thinking'); setProblem('');
    try { apply(await liveSay(text, ambient)); return true; }
    catch { setPhase('idle'); setProblem("Couldn't reach your coach. Your sets are safe. Check your connection and send it again."); return false; }
  }, [apply]);

  const voice = useVoice((t) => { const ambient = !manual.current; manual.current = false; send(t, ambient); });
  stopListenRef.current = voice.stop;

  // Open the session: load persona, then the coach greets you.
  const open = useCallback(async () => {
    setProblem(''); setOpening(true);
    try {
      const c = await getCoach();
      setCoachName(c.coach_name); setPersona(c.persona); personaRef.current = c.persona; tts.current = c.tts ?? tts.current; setRecognitionLang(c.tts?.lang);
      const r = await liveStart();
      apply(r);
    } catch { setProblem("Can't reach your coach. Check your connection, then try again."); }
    setOpening(false);
  }, [apply]);
  useEffect(() => {
    alive.current = true;
    open();
    return () => {
      alive.current = false; token.current++;
      stopSpeaking(); stopListenRef.current();
      if (tick.current) clearInterval(tick.current);
    };
  }, []);

  // Rest finished: the coach calls you back in.
  useEffect(() => {
    if (rest === 0 && restRunning.current) {
      restRunning.current = false;
      Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
      liveCue('rest_over').then((r) => { setState(r.state); talk(r.reply, shouldUsePremium(false)); }).catch(() => {});
    }
  }, [rest, talk]);

  // Hands-free: whenever the coach is quiet and we're not already listening, listen again.
  const failures = useRef(0);
  useEffect(() => {
    if (!handsFree || !voice.available || phase !== 'idle' || voice.listening || ended) return;
    const t = setTimeout(async () => {
      const ok = await voice.start();
      if (!ok && ++failures.current >= 3) {
        setHandsFree(false);
        setProblem('I need microphone access to hear you. Allow the microphone in your phone settings, or tap the big circle to talk.');
      }
    }, 450);
    return () => clearTimeout(t);
  }, [handsFree, voice.available, phase, voice.listening, ended]);

  const onOrb = async () => {
    if (phase === 'speaking') { token.current++; stopSpeaking(); setPhase('idle'); }
    if (voice.listening) { voice.stop(); return; }
    if (voice.available) { manual.current = true; await voice.start(); }
  };
  const sendTyped = () => {
    const t = typed;
    if (!t.trim()) return;
    setTyped('');
    send(t, false).then((ok) => { if (!ok) setTyped(t); });
  };

  const display: 'idle' | 'listening' | 'thinking' | 'speaking' = voice.listening ? 'listening' : phase;
  const accent = personaById(persona).color;
  const stateColor = { idle: colors.dim, listening: colors.cyan, thinking: colors.amber, speaking: accent }[display];
  const status = display === 'listening' ? 'Listening… tap to stop'
    : display === 'thinking' ? 'Thinking…'
    : display === 'speaking' ? `${coachName} is talking`
    : voice.available ? (handsFree ? 'Hands-free is on. Just talk.' : 'Tap the circle to talk') : 'Type below to talk to me';
  const cur = state?.current;
  const allDone = !!state?.finished && !ended;

  return (
    <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined} style={{ flex: 1, backgroundColor: colors.bg }}>
      <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingHorizontal: 12, paddingTop: 6 }}>
        <Pressable accessibilityRole="button" accessibilityLabel={ended ? 'Close' : 'Leave session'} onPress={onClose} style={{ width: 52, height: 52, alignItems: 'center', justifyContent: 'center' }}>
          <Ionicons name="close" size={30} color={colors.text} />
        </Pressable>
        <View style={{ alignItems: 'center', flex: 1 }}>
          <Txt style={{ fontFamily: font.bold, fontSize: 16 }}>{coachName}</Txt>
          <Txt style={{ color: colors.dim, fontSize: 13 }} numberOfLines={1}>{state?.day ?? 'Live session'}</Txt>
        </View>
        <Pressable accessibilityRole="switch" accessibilityState={{ checked: handsFree && voice.available }} accessibilityLabel="Hands-free listening"
          onPress={() => { Haptics.selectionAsync(); setHandsFree((h) => !h); }} disabled={!voice.available || ended}
          style={{ width: 52, height: 52, alignItems: 'center', justifyContent: 'center', opacity: ended ? 0 : 1 }}>
          <Ionicons name={handsFree && voice.available ? 'ear' : 'ear-outline'} size={28} color={handsFree && voice.available ? accent : colors.faint} />
        </Pressable>
      </View>

      <ScrollView contentContainerStyle={{ padding: 20, gap: 14, alignItems: 'center', paddingBottom: 24 }} keyboardShouldPersistTaps="handled">
        {ended ? (
          <View style={{ alignSelf: 'stretch', paddingTop: 6 }}>
            <WorkoutSummary accent={accent} coachLine={line} onDone={onClose} />
          </View>
        ) : (
          <>
            <Pressable onPress={onOrb} accessibilityRole="button" accessibilityLabel={voice.listening ? 'Stop listening' : 'Talk to your coach'}>
              <Orb state={display} color={accent} size={170} />
            </Pressable>
            <View accessibilityLiveRegion="polite" accessibilityLabel={opening ? 'Getting your session ready' : status} style={{ flexDirection: 'row', alignItems: 'center', gap: 8, marginTop: -26, paddingHorizontal: 14, paddingVertical: 7, borderRadius: 999, backgroundColor: stateColor + '1F', borderWidth: 1, borderColor: stateColor + '55' }}>
              {opening ? <ActivityIndicator size="small" color={colors.amber} /> : <View style={{ width: 8, height: 8, borderRadius: 4, backgroundColor: stateColor }} />}
              <Txt style={{ color: opening ? colors.amber : display === 'idle' ? colors.text : stateColor, fontFamily: font.semibold, fontSize: 15, flexShrink: 1, textAlign: 'center' }}>{opening ? 'Getting your session ready…' : status}</Txt>
            </View>
            {!!line && <Txt style={{ fontFamily: font.bold, fontSize: line.length > 110 ? 20 : 24, lineHeight: line.length > 110 ? 28 : 33, textAlign: 'center', letterSpacing: -0.2 }}>{line}</Txt>}
            {!!heard && <Txt style={{ color: colors.dim, fontSize: 15, textAlign: 'center', lineHeight: 21 }}>You said: <Txt style={{ color: colors.text, fontFamily: font.medium }}>{heard}</Txt></Txt>}
            {!!problem && (
              <Card style={{ alignSelf: 'stretch', borderColor: colors.amber + '66', gap: 10 }}>
                <View style={{ flexDirection: 'row', gap: 10 }}>
                  <Ionicons name="alert-circle" size={22} color={colors.amber} />
                  <Txt style={{ flex: 1, color: colors.text, fontSize: 15, lineHeight: 22 }}>{problem}</Txt>
                </View>
                {!state && !opening && <GradientButton label="Try again" icon="refresh" onPress={open} />}
              </Card>
            )}
            {!voice.available && !problem && <Txt style={{ color: colors.dim, fontSize: 13.5, textAlign: 'center', lineHeight: 20 }}>Voice input is not available on this device yet. Your coach still talks. Type your sets below.</Txt>}

            {cur && (
              <Card style={{ alignSelf: 'stretch', gap: 12 }}>
                <View style={{ flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
                  <View style={{ flex: 1 }}>
                    <Txt style={{ color: colors.dim, fontSize: 12, letterSpacing: 1.2, fontFamily: font.semibold }}>NOW</Txt>
                    <Txt style={{ fontFamily: font.black, fontSize: 26, letterSpacing: -0.4, textTransform: 'capitalize' }} numberOfLines={2}>{cur.name}</Txt>
                    <Txt style={{ color: colors.text, opacity: 0.85, fontSize: 16, lineHeight: 22, marginTop: 2 }}>Set {cur.set_no} of {cur.sets} · {cur.reps} reps · RPE {cur.target_rpe}</Txt>
                  </View>
                  <View style={{ alignItems: 'center', minWidth: 56 }}>
                    <Txt style={{ fontFamily: font.black, fontSize: 32, color: accent }}>{state.remaining_sets}</Txt>
                    <Txt style={{ color: colors.dim, fontSize: 12 }}>sets left</Txt>
                  </View>
                </View>
                <View style={{ flexDirection: 'row', gap: 5 }} accessible accessibilityRole="progressbar" accessibilityLabel={`Set ${cur.set_no} of ${cur.sets}`}>
                  {Array.from({ length: cur.sets }).map((_, k) => (
                    <View key={k} style={{ flex: 1, height: 8, borderRadius: 4, backgroundColor: k < cur.set_no - 1 ? accent : 'rgba(255,255,255,0.14)' }} />
                  ))}
                </View>
              </Card>
            )}

            {rest > 0 && (
              <Card style={{ alignSelf: 'stretch', flexDirection: 'row', alignItems: 'center', gap: 16 }}>
                <Ring size={76} stroke={8} progress={rest / restTotal.current} from={colors.cyan} to={colors.violet}>
                  <Txt style={{ fontFamily: font.black, fontSize: rest >= 60 ? 18 : 22 }}>{rest >= 60 ? `${Math.floor(rest / 60)}:${String(rest % 60).padStart(2, '0')}` : rest}</Txt>
                </Ring>
                <View style={{ flex: 1 }} accessible accessibilityLabel={`Resting, ${rest} seconds left`}>
                  <Txt style={{ fontFamily: font.bold, fontSize: 18 }}>Resting</Txt>
                  <Txt style={{ color: colors.dim, fontSize: 14, lineHeight: 20 }}>I'll call you back in. You can still talk to me.</Txt>
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

            {allDone ? (
              <View style={{ alignSelf: 'stretch', gap: 8 }}>
                <Txt style={{ color: colors.dim, textAlign: 'center', fontSize: 14 }}>Every planned set is done.</Txt>
                <GradientButton label="Finish workout" icon="trophy" onPress={() => send('wrap it up', false)} />
              </View>
            ) : (
              <View style={{ alignSelf: 'stretch', flexDirection: 'row', flexWrap: 'wrap', gap: 10, justifyContent: 'space-between' }}>
                <Quick label="Skip this" icon="play-skip-forward" onPress={() => send('skip this exercise', false)} />
                <Quick label="What's next?" icon="help-circle" onPress={() => send("what's next", false)} />
                <Quick label="30 min left" icon="time" color={colors.cyan} onPress={() => send('I only have 30 minutes', false)} />
                <Quick label="Finish up" icon="flag" color={colors.amber} onPress={() => send('wrap it up', false)} />
              </View>
            )}
          </>
        )}
      </ScrollView>

      {!ended && (
        <View style={{ flexDirection: 'row', gap: 10, padding: 16, paddingTop: 6, alignItems: 'center' }}>
          <TextInput value={typed} onChangeText={setTyped} placeholder="Say it here: bench 80 for 8, felt good" placeholderTextColor={colors.faint}
            accessibilityLabel="Tell your coach what you did" onSubmitEditing={sendTyped} returnKeyType="send"
            style={{ flex: 1, minHeight: 52, backgroundColor: colors.card, color: colors.text, borderRadius: 26, paddingHorizontal: 18, paddingVertical: 13, fontFamily: font.medium, fontSize: 16, borderWidth: 1, borderColor: colors.border }} />
          {phase === 'thinking' ? <ActivityIndicator color={accent} style={{ width: 52 }} /> : (
            <Pressable accessibilityRole="button" accessibilityLabel="Send" onPress={sendTyped} disabled={!typed.trim()}
              style={({ pressed }) => ({ width: 52, height: 52, borderRadius: 26, backgroundColor: accent, alignItems: 'center', justifyContent: 'center', opacity: typed.trim() ? 1 : 0.45, transform: [{ scale: pressed ? 0.94 : 1 }] })}>
              <Ionicons name="arrow-up" size={26} color="#07080B" />
            </Pressable>
          )}
        </View>
      )}
      <Confetti burst={burst} />
    </KeyboardAvoidingView>
  );
}
