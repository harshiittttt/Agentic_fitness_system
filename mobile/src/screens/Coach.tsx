import React, { useEffect, useRef, useState } from 'react';
import { Animated, Easing, KeyboardAvoidingView, Platform, Pressable, ScrollView, Share, TextInput, View } from 'react-native';
import { LinearGradient } from 'expo-linear-gradient';
import { Ionicons } from '@expo/vector-icons';
import AsyncStorage from '@react-native-async-storage/async-storage';
import * as Haptics from '../haptics';
import { personaById } from '../personas';
import { speak, stopSpeaking } from '../tts';
import { shouldUsePremium } from '../voiceSettings';
import { getCoach, sendChat, undoCoachChange, CoachAction } from '../api';
import { bumpPlan } from '../planSignal';
import Roundtable from './Roundtable';
import { useVoice } from '../useVoice';
import { colors, font, gradients } from '../theme';
import { Avatar, FadeIn, IconButton, Orb, Txt, useOnline, useToast } from '../ui';

type Msg = { id: number; me: boolean; t: string; at: number; failed?: boolean; actions?: CoachAction[] };

// The chat survives switching tabs (the screen unmounts) and app restarts: it is kept in memory and on the phone.
const KEY = 'coach_chat_v1';
let memory: Msg[] | null = null;
let nextId = 1;
const persist = (m: Msg[]) => { AsyncStorage.setItem(KEY, JSON.stringify(m.filter((x) => !x.failed).slice(-40))).catch(() => {}); };

// The coach opens in their own voice, so the screen feels like a person is waiting, not an empty box.
const GREETING: Record<string, string> = {
  hype: "Let's GO! I'm warmed up and ready. What are we crushing today?",
  mentor: 'Good to see you. Take a breath. What would you like to talk through?',
  straight: "I'm here. Ask your question, I'll give you the answer.",
  haryanvi: 'राम राम भाई! बता, के सवाल सै? सीधा बोल दे।',
  roast: 'अबे, आ गया? बोल, क्या पूछना है। जल्दी बोल, मेरे पास तेरे लिए ज्यादा टाइम नहीं है।',
};
const SUGGESTIONS: { label: string; icon: string; plan?: boolean }[] = [
  { label: "What's my next set?", icon: 'flash' },
  { label: 'My shoulder feels tweaky', icon: 'medkit' },
  { label: 'Short on time today', icon: 'timer' },
  { label: 'Why did my plan change?', icon: 'git-compare', plan: true },
];

const clock = (t: number) => {
  const d = new Date(t);
  const h = d.getHours();
  return `${((h + 11) % 12) + 1}:${String(d.getMinutes()).padStart(2, '0')} ${h < 12 ? 'AM' : 'PM'}`;
};

/** Three dots that bob in turn, inside a coach bubble: "your coach is thinking". */
function TypingBubble({ accent, icon }: { accent: string; icon: string }) {
  const dots = useRef([0, 1, 2].map(() => new Animated.Value(0))).current;
  useEffect(() => {
    const loops = dots.map((d, i) => Animated.loop(Animated.sequence([
      Animated.delay(i * 160),
      Animated.timing(d, { toValue: 1, duration: 360, easing: Easing.inOut(Easing.quad), useNativeDriver: true }),
      Animated.timing(d, { toValue: 0, duration: 360, easing: Easing.inOut(Easing.quad), useNativeDriver: true }),
      Animated.delay(320 - i * 160),
    ])));
    loops.forEach((l) => l.start());
    return () => loops.forEach((l) => l.stop());
  }, []);
  return (
    <View accessibilityLabel="Your coach is typing" accessibilityLiveRegion="polite" style={{ flexDirection: 'row', alignItems: 'flex-end', gap: 8, alignSelf: 'flex-start' }}>
      <Avatar size={28} color={accent} icon={icon} />
      <View style={{ flexDirection: 'row', gap: 6, paddingHorizontal: 16, height: 44, alignItems: 'center', borderRadius: 22, borderBottomLeftRadius: 6, backgroundColor: 'rgba(255,255,255,0.06)', borderWidth: 1, borderColor: 'rgba(255,255,255,0.10)' }}>
        {dots.map((d, i) => (
          <Animated.View key={i} style={{ width: 8, height: 8, borderRadius: 4, backgroundColor: accent, opacity: d.interpolate({ inputRange: [0, 1], outputRange: [0.35, 1] }), transform: [{ translateY: d.interpolate({ inputRange: [0, 1], outputRange: [0, -4] }) }] }} />
        ))}
      </View>
    </View>
  );
}

async function copyOut(text: string): Promise<'copied' | 'shared'> {
  const nav: any = typeof navigator !== 'undefined' ? navigator : null;
  if (Platform.OS === 'web' && nav?.clipboard?.writeText) { await nav.clipboard.writeText(text); return 'copied'; }
  await Share.share({ message: text });
  return 'shared';
}

export default function Coach({ onOpenTeam }: { onOpenTeam?: () => void }) {
  const { online } = useOnline();
  const toast = useToast();
  const [msgs, setMsgs] = useState<Msg[]>(memory ?? []);
  const [text, setText] = useState('');
  const [typing, setTyping] = useState(false);
  const [personaId, setPersonaId] = useState('hype');
  const [speakingId, setSpeakingId] = useState<number | null>(null);
  const [table, setTable] = useState(false);
  useEffect(() => { getCoach().then((c) => setPersonaId(c.persona)).catch(() => {}); }, []);
  useEffect(() => {
    if (memory) return;
    AsyncStorage.getItem(KEY).then((raw) => {
      if (!raw) { memory = []; return; }
      try { const m: Msg[] = JSON.parse(raw); memory = m; nextId = Math.max(0, ...m.map((x) => x.id)) + 1; setMsgs((cur) => (cur.length ? cur : m)); } catch { memory = []; }
    }).catch(() => { memory = []; });
  }, []);
  useEffect(() => () => { stopSpeaking(); }, []);
  const scroll = useRef<ScrollView>(null);
  const persona = personaById(personaId);
  const accent = persona.color;

  const commit = (fn: (m: Msg[]) => Msg[]) => setMsgs((cur) => { const n = fn(cur); memory = n; persist(n); return n; });

  /** Ask the coach. `retryId` re-asks a message that failed, without adding it twice. */
  const send = async (t: string, retryId?: number, byVoice = false) => {
    const q = t.trim();
    if (!q || typing) return;
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light);
    const mineId = retryId ?? nextId++;
    if (retryId == null) commit((m) => [...m, { id: mineId, me: true, t: q, at: Date.now() }]);
    else commit((m) => m.map((x) => (x.id === retryId ? { ...x, failed: false } : x)));
    setText(''); setTyping(true);
    try {
      const r = await sendChat(q);
      const reply: string = String(r.reply ?? '').trim() || "I didn't catch that. Could you say it another way?";
      const id = nextId++;
      const actions: CoachAction[] = Array.isArray(r.actions) ? r.actions : [];
      commit((m) => [...m, { id, me: false, t: reply, at: Date.now(), actions: actions.length ? actions : undefined }]);
      if (actions.some((a) => a.kind === 'changed' || a.kind === 'undone')) bumpPlan();
      if (byVoice) {   // typed messages get a silent text reply; only a spoken question is answered out loud (the speaker button replays any reply)
        setSpeakingId(id);
        speak(reply, persona.tts, () => setSpeakingId((s) => (s === id ? null : s)), { persona: personaId, premium: shouldUsePremium(true) });
      }
    } catch {
      commit((m) => m.map((x) => (x.id === mineId ? { ...x, failed: true } : x)));
    }
    setTyping(false);
  };
  const voice = useVoice((t) => send(t, undefined, true));

  const regenerate = () => {
    const lastMine = [...msgs].reverse().find((m) => m.me);
    if (!lastMine || typing) return;
    commit((m) => (m.length && !m[m.length - 1].me ? m.slice(0, -1) : m));
    send(lastMine.t, lastMine.id);
  };
  const copy = async (m: Msg) => {
    try { const r = await copyOut(m.t); if (r === 'copied') toast('Copied'); } catch { toast("Couldn't copy that", { tone: 'warn' }); }
  };
  const replay = (m: Msg) => {
    if (speakingId === m.id) { stopSpeaking(); setSpeakingId(null); return; }
    setSpeakingId(m.id);
    speak(m.t, persona.tts, () => setSpeakingId((s) => (s === m.id ? null : s)), { persona: personaId, premium: shouldUsePremium(true) });
  };
  const clear = () => { stopSpeaking(); commit(() => []); };
  const undo = async (msgId: number, a: CoachAction) => {
    try {
      await undoCoachChange(a.decision_id);
      commit((m) => m.map((x) => (x.id === msgId ? { ...x, actions: x.actions?.map((y) => (y === a || y.decision_id === a.decision_id ? { ...y, kind: 'undone', can_undo: false, summary: `Undone: ${y.summary ?? 'change'}` } : y)) } : x)));
      bumpPlan(); toast('Undone');
    } catch { toast("Couldn't undo that", { tone: 'warn' }); }
  };

  const lastCoachId = [...msgs].reverse().find((m) => !m.me)?.id;
  const ask = (x: { label: string; plan?: boolean }) => { if (x.plan && onOpenTeam) { Haptics.selectionAsync(); onOpenTeam(); } else send(x.label); };

  return (
    <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
      <View style={{ paddingHorizontal: 20, paddingTop: 12, paddingBottom: 6, flexDirection: 'row', alignItems: 'center', gap: 13 }}>
        <Avatar size={46} color={accent} icon={persona.icon} ring />
        <View style={{ flex: 1 }}>
          <Txt style={{ fontFamily: font.black, fontSize: 22, letterSpacing: -0.4 }}>{persona.name}</Txt>
          <Txt style={{ color: accent, fontSize: 12.5, fontFamily: font.semibold }}>{persona.short} · knows your plan and your team</Txt>
        </View>
        <IconButton icon="people-circle-outline" label="Ask the whole team" onPress={() => { Haptics.selectionAsync(); setTable(true); }} size={28} color={colors.mint} />
        {msgs.length > 0 && <IconButton icon="refresh-circle-outline" label="Start a new chat" onPress={clear} size={26} />}
      </View>

      <ScrollView ref={scroll} onContentSizeChange={() => scroll.current?.scrollToEnd({ animated: true })} keyboardShouldPersistTaps="handled" keyboardDismissMode="interactive"
        contentContainerStyle={{ padding: 20, gap: 6, flexGrow: 1, justifyContent: msgs.length ? 'flex-start' : 'center' }}>
        {msgs.length === 0 && (
          <FadeIn>
            <View style={{ alignItems: 'center', gap: 22 }}>
              <Orb state={typing ? 'thinking' : 'idle'} size={150} color={accent} />
              <View style={{ maxWidth: '88%', padding: 16, borderRadius: 24, borderBottomLeftRadius: 8, backgroundColor: accent + '18', borderWidth: 1, borderColor: accent + '44' }}>
                <Txt style={{ fontFamily: font.semibold, fontSize: 16.5, lineHeight: 24 }}>{GREETING[personaId] ?? GREETING.hype}</Txt>
              </View>
              <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 10, justifyContent: 'center' }}>
                {SUGGESTIONS.map((x) => (
                  <Pressable key={x.label} onPress={() => ask(x)} accessibilityRole="button" accessibilityLabel={x.plan && onOpenTeam ? `${x.label} Opens the team's decisions` : x.label}
                    style={({ pressed }) => ({ transform: [{ scale: pressed ? 0.96 : 1 }], width: '48%' })}>
                    <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10, padding: 14, minHeight: 56, borderRadius: 18, backgroundColor: 'rgba(255,255,255,0.05)', borderWidth: 1, borderColor: 'rgba(255,255,255,0.10)', borderTopColor: 'rgba(255,255,255,0.18)' }}>
                      <Ionicons name={x.icon as any} size={18} color={accent} />
                      <Txt style={{ flex: 1, fontFamily: font.semibold, fontSize: 13.5, lineHeight: 18 }}>{x.label}</Txt>
                      {x.plan && onOpenTeam && <Ionicons name="arrow-forward" size={14} color={colors.dim} />}
                    </View>
                  </Pressable>
                ))}
              </View>
              {!online && <Txt style={{ color: colors.amber, fontSize: 13, textAlign: 'center', lineHeight: 19 }}>You're offline, so {persona.name} can't answer yet. Your plan and set logging still work.</Txt>}
            </View>
          </FadeIn>
        )}

        {msgs.map((m, i) => {
          const prev = msgs[i - 1], next = msgs[i + 1];
          const startsGroup = !prev || prev.me !== m.me || m.at - prev.at > 5 * 60000;
          const endsGroup = !next || next.me !== m.me || next.at - m.at > 5 * 60000;
          const mt = startsGroup && i > 0 ? 10 : 0;
          return m.me ? (
            <View key={m.id} style={{ alignSelf: 'flex-end', maxWidth: '84%', marginTop: mt, alignItems: 'flex-end', gap: 3 }}>
              <Pressable disabled={!m.failed} onPress={() => send(m.t, m.id)} accessibilityRole={m.failed ? 'button' : undefined} accessibilityLabel={m.failed ? `Message not sent. Tap to retry: ${m.t}` : undefined}
                style={{ padding: 14, borderRadius: 22, borderBottomRightRadius: endsGroup ? 6 : 22, backgroundColor: m.failed ? 'rgba(255,90,95,0.14)' : 'rgba(198,255,61,0.16)', borderWidth: 1, borderColor: m.failed ? colors.red + '88' : 'rgba(198,255,61,0.4)' }}>
                <Txt selectable style={{ fontFamily: font.medium, fontSize: 15.5, lineHeight: 22 }}>{m.t}</Txt>
              </Pressable>
              {m.failed
                ? <Pressable onPress={() => send(m.t, m.id)} accessibilityRole="button" accessibilityLabel="Retry sending" style={{ minHeight: 44, justifyContent: 'center', flexDirection: 'row', alignItems: 'center', gap: 6 }}>
                    <Ionicons name="alert-circle" size={15} color={colors.red} /><Txt style={{ color: colors.red, fontFamily: font.bold, fontSize: 12.5 }}>Couldn't reach {persona.name}. Tap to try again</Txt>
                  </Pressable>
                : endsGroup && <Txt style={{ color: colors.dim, fontSize: 11 }}>{clock(m.at)}</Txt>}
            </View>
          ) : (
            <View key={m.id} style={{ marginTop: mt, gap: 2, maxWidth: '94%', alignSelf: 'flex-start' }}>
              <View style={{ flexDirection: 'row', alignItems: 'flex-end', gap: 8 }}>
                {endsGroup ? <Avatar size={28} color={accent} icon={persona.icon} /> : <View style={{ width: 28 }} />}
                <View style={{ flexShrink: 1, padding: 14, borderRadius: 22, borderBottomLeftRadius: endsGroup ? 6 : 22, backgroundColor: 'rgba(255,255,255,0.06)', borderWidth: 1, borderColor: 'rgba(255,255,255,0.10)', borderTopColor: 'rgba(255,255,255,0.16)' }}>
                  <Txt selectable style={{ fontSize: 15.5, lineHeight: 24 }}>{m.t}</Txt>
                </View>
              </View>
              {m.actions?.map((a, k) => {
                const tone = a.kind === 'changed' ? colors.lime : a.kind === 'undone' ? colors.dim : a.kind === 'pushback' ? colors.amber : colors.red;
                const icon = a.kind === 'changed' ? 'checkmark-circle' : a.kind === 'undone' ? 'arrow-undo' : a.kind === 'pushback' ? 'hand-left' : 'shield-checkmark';
                return (
                  <View key={k} style={{ marginLeft: 36, marginTop: 4, flexDirection: 'row', alignItems: 'center', gap: 8, padding: 10, borderRadius: 14, backgroundColor: tone + '14', borderWidth: 1, borderColor: tone + '55' }}>
                    <Ionicons name={icon as any} size={18} color={tone} />
                    <Txt style={{ flex: 1, fontSize: 13, lineHeight: 18 }}>{a.summary || (a.kind === 'changed' ? 'Plan updated' : a.kind)}</Txt>
                    {a.kind === 'changed' && a.can_undo && (
                      <Pressable onPress={() => undo(m.id, a)} accessibilityRole="button" accessibilityLabel={`Undo: ${a.summary ?? 'this change'}`} style={{ minHeight: 44, justifyContent: 'center', paddingHorizontal: 8 }}>
                        <Txt style={{ color: tone, fontFamily: font.bold, fontSize: 13 }}>Undo</Txt>
                      </Pressable>
                    )}
                  </View>
                );
              })}
              {endsGroup && (
                <View style={{ flexDirection: 'row', alignItems: 'center', marginLeft: 30 }}>
                  <Txt style={{ color: colors.dim, fontSize: 11, marginRight: 6 }}>{clock(m.at)}</Txt>
                  <IconButton icon="copy-outline" label="Copy or share this reply" onPress={() => copy(m)} size={17} />
                  <IconButton icon={speakingId === m.id ? 'stop-circle-outline' : 'volume-high-outline'} label={speakingId === m.id ? 'Stop reading aloud' : 'Read this reply aloud'} onPress={() => replay(m)} size={18} color={speakingId === m.id ? accent : colors.dim} />
                  {m.id === lastCoachId && <IconButton icon="reload" label="Ask again for a different answer" onPress={regenerate} size={17} disabled={typing} />}
                </View>
              )}
            </View>
          );
        })}
        {typing && <View style={{ marginTop: 8 }}><TypingBubble accent={accent} icon={persona.icon} /></View>}
      </ScrollView>

      {msgs.length > 0 && !typing && (
        <ScrollView horizontal showsHorizontalScrollIndicator={false} keyboardShouldPersistTaps="handled" style={{ flexGrow: 0 }} contentContainerStyle={{ gap: 8, paddingHorizontal: 16, paddingTop: 4 }}>
          {SUGGESTIONS.map((x) => (
            <Pressable key={x.label} onPress={() => ask(x)} accessibilityRole="button" accessibilityLabel={x.label}
              style={{ minHeight: 44, flexDirection: 'row', alignItems: 'center', gap: 7, paddingHorizontal: 14, borderRadius: 999, backgroundColor: 'rgba(255,255,255,0.06)', borderWidth: 1, borderColor: 'rgba(255,255,255,0.12)' }}>
              <Ionicons name={x.icon as any} size={15} color={accent} />
              <Txt style={{ fontFamily: font.semibold, fontSize: 13 }}>{x.label}</Txt>
            </Pressable>
          ))}
        </ScrollView>
      )}

      <View style={{ flexDirection: 'row', gap: 10, padding: 16, paddingTop: 8, alignItems: 'center' }}>
        <TextInput value={text} onChangeText={setText} placeholder={online ? `Message ${persona.name}` : `Offline. ${persona.name} can't answer yet.`} placeholderTextColor={colors.faint}
          onSubmitEditing={() => send(text)} returnKeyType="send" accessibilityLabel={`Message ${persona.name}`} onFocus={() => setTimeout(() => scroll.current?.scrollToEnd({ animated: true }), 250)}
          style={{ flex: 1, minHeight: 52, backgroundColor: 'rgba(255,255,255,0.06)', color: colors.text, borderRadius: 26, paddingHorizontal: 20, paddingVertical: 14, fontFamily: font.medium, fontSize: 15, borderWidth: 1, borderColor: 'rgba(255,255,255,0.10)' }} />
        <Pressable onPress={() => (text.trim() ? send(text) : voice.available && (voice.listening ? voice.stop() : voice.start()))} disabled={typing}
          accessibilityRole="button" accessibilityLabel={text.trim() ? 'Send message' : voice.listening ? 'Stop listening' : voice.available ? 'Speak to your coach' : 'Type a message to send'}>
          <LinearGradient colors={voice.listening ? gradients.warm : gradients.lime} style={{ width: 52, height: 52, borderRadius: 26, alignItems: 'center', justifyContent: 'center', opacity: typing ? 0.5 : 1 }}>
            <Ionicons name={text.trim() ? 'arrow-up' : voice.listening ? 'stop' : 'mic'} size={24} color="#07080B" />
          </LinearGradient>
        </Pressable>
      </View>
      <Roundtable visible={table} onClose={() => setTable(false)} onOpenTeam={onOpenTeam} />
    </KeyboardAvoidingView>
  );
}
