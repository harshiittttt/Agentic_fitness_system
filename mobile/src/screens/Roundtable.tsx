import React, { useEffect, useRef, useState } from 'react';
import { KeyboardAvoidingView, Modal, Platform, Pressable, ScrollView, TextInput, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { askTeam, RoundtableResult } from '../api';
import { TEAM, colors, font } from '../theme';
import { Avatar, Card, ErrorCard, FadeIn, GradientButton, IconButton, Skeleton, Txt } from '../ui';
import * as Haptics from '../haptics';
import { speak, stopSpeaking } from '../tts';
import { shouldUsePremium } from '../voiceSettings';

const SUGGESTIONS = [
  "My knee hurts on squats and I'm sleeping badly. What now?",
  'Should I eat more protein or cut calories?',
  'Am I overtraining? I feel flat this week.',
  "I keep missing sessions. How do I stay consistent?",
];
const WHO = ['training', 'nutrition', 'recovery', 'sleep', 'injury', 'progress', 'adherence'] as const;

const OUTCOME: Record<string, { label: string; color: string; icon: string }> = {
  applied: { label: 'Done', color: colors.lime, icon: 'checkmark-circle' },
  pending_approval: { label: 'Needs your OK', color: colors.amber, icon: 'hand-left' },
  pending_veto: { label: 'Applies unless you stop it', color: colors.cyan, icon: 'time' },
  blocked: { label: 'Not allowed', color: colors.red, icon: 'shield-checkmark' },
  escalated: { label: 'Paused for safety', color: colors.red, icon: 'warning' },
};

function who(a: string) { return TEAM[a] ?? { label: a, role: '', color: colors.lime, icon: 'sparkles' }; }

/** Talk to the whole team: a moderator invites 2-3 specialists, they answer, disagree at most twice, and one verdict comes back. */
export default function Roundtable({ visible, onClose, onOpenTeam }: { visible: boolean; onClose: () => void; onOpenTeam?: () => void }) {
  const [q, setQ] = useState('');
  const [picked, setPicked] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [res, setRes] = useState<RoundtableResult | null>(null);
  const scroll = useRef<ScrollView>(null);
  const [speakingKey, setSpeakingKey] = useState<string | null>(null);
  useEffect(() => () => { stopSpeaking(); }, []);
  useEffect(() => { if (!visible) { stopSpeaking(); setSpeakingKey(null); } }, [visible]);
  // Each specialist speaks in its own voice (backend maps agent -> voice settings; phone voice is the fallback).
  const say = (key: string, text: string, agent: string) => {
    if (speakingKey === key) { stopSpeaking(); setSpeakingKey(null); return; }
    setSpeakingKey(key);
    speak(text, { rate: 1, pitch: 1, lang: 'en-US' }, () => setSpeakingKey((k) => (k === key ? null : k)), { persona: 'mentor', agent, premium: shouldUsePremium(true) });
  };

  const ask = async (text = q) => {
    const question = text.trim();
    if (!question || busy) return;
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light);
    setQ(question); setBusy(true); setErr(''); setRes(null);
    try { setRes(await askTeam(question, picked)); }
    catch (e: any) { setErr(String(e?.message ?? e)); }
    setBusy(false);
    setTimeout(() => scroll.current?.scrollTo({ y: 0, animated: true }), 50);
  };
  const reset = () => { setRes(null); setErr(''); setQ(''); };
  const toggle = (k: string) => { Haptics.selectionAsync(); setPicked((p) => (p.includes(k) ? p.filter((x) => x !== k) : [...p, k].slice(0, 3))); };

  const rounds = res ? Array.from(new Set(res.turns.map((t) => t.round))).sort() : [];

  return (
    <Modal visible={visible} animationType="slide" onRequestClose={onClose}>
      <View style={{ flex: 1, backgroundColor: colors.bg }}>
        <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
          <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12, paddingHorizontal: 20, paddingTop: Platform.OS === 'ios' ? 56 : 28, paddingBottom: 8 }}>
            <Avatar size={44} color={TEAM.moderator.color} icon={TEAM.moderator.icon} ring />
            <View style={{ flex: 1 }}>
              <Txt style={{ fontFamily: font.black, fontSize: 22, letterSpacing: -0.3 }}>Ask the whole team</Txt>
              <Txt style={{ color: colors.dim, fontSize: 13 }}>A moderator picks who should weigh in</Txt>
            </View>
            <IconButton icon="close" label="Close" onPress={onClose} size={26} />
          </View>

          <ScrollView ref={scroll} keyboardShouldPersistTaps="handled" contentContainerStyle={{ padding: 20, gap: 16, paddingBottom: 40 }}>
            {!res && !busy && (
              <FadeIn>
                <View style={{ gap: 14 }}>
                  <TextInput value={q} onChangeText={setQ} multiline placeholder="What do you want the team to weigh in on?" placeholderTextColor={colors.faint}
                    accessibilityLabel="Your question for the team" maxLength={500}
                    style={{ minHeight: 96, textAlignVertical: 'top', backgroundColor: 'rgba(255,255,255,0.06)', color: colors.text, borderRadius: 20, padding: 16, fontFamily: font.medium, fontSize: 16, lineHeight: 23, borderWidth: 1, borderColor: 'rgba(255,255,255,0.10)' }} />
                  <View style={{ gap: 8 }}>
                    <Txt style={{ color: colors.dim, fontSize: 12.5, fontFamily: font.semibold, letterSpacing: 0.6 }}>WHO SHOULD ANSWER? (OPTIONAL, UP TO 3)</Txt>
                    <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8 }}>
                      {WHO.map((k) => {
                        const on = picked.includes(k); const t = who(k);
                        return (
                          <Pressable key={k} onPress={() => toggle(k)} accessibilityRole="button" accessibilityState={{ selected: on }} accessibilityLabel={`${t.label}${on ? ', selected' : ''}`}
                            style={{ minHeight: 44, flexDirection: 'row', alignItems: 'center', gap: 7, paddingHorizontal: 12, borderRadius: 999, backgroundColor: on ? t.color + '26' : 'rgba(255,255,255,0.05)', borderWidth: 1, borderColor: on ? t.color : 'rgba(255,255,255,0.12)' }}>
                            <Ionicons name={t.icon as any} size={15} color={on ? t.color : colors.dim} />
                            <Txt style={{ fontFamily: font.semibold, fontSize: 13, color: on ? t.color : colors.text }}>{t.label}</Txt>
                          </Pressable>
                        );
                      })}
                    </View>
                    {picked.length === 0 && <Txt style={{ color: colors.faint, fontSize: 12.5 }}>Leave this empty and the moderator chooses.</Txt>}
                  </View>
                  <GradientButton label="Ask the team" icon="people" onPress={() => ask()} />
                  <View style={{ gap: 8, marginTop: 4 }}>
                    <Txt style={{ color: colors.dim, fontSize: 12.5, fontFamily: font.semibold, letterSpacing: 0.6 }}>OR TRY</Txt>
                    {SUGGESTIONS.map((s) => (
                      <Pressable key={s} onPress={() => ask(s)} accessibilityRole="button" style={({ pressed }) => ({ opacity: pressed ? 0.7 : 1, padding: 14, borderRadius: 16, backgroundColor: 'rgba(255,255,255,0.05)', borderWidth: 1, borderColor: 'rgba(255,255,255,0.10)', minHeight: 48, justifyContent: 'center' })}>
                        <Txt style={{ fontFamily: font.medium, fontSize: 14.5, lineHeight: 20 }}>{s}</Txt>
                      </Pressable>
                    ))}
                  </View>
                </View>
              </FadeIn>
            )}

            {busy && (
              <View style={{ gap: 14 }} accessibilityLiveRegion="polite" accessibilityLabel="The team is discussing your question">
                <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10 }}>
                  <Avatar size={36} color={TEAM.moderator.color} icon={TEAM.moderator.icon} ring />
                  <Txt style={{ color: colors.dim, fontSize: 14.5, flex: 1 }}>The moderator is picking who should weigh in, then they'll discuss it. This can take up to a minute.</Txt>
                </View>
                <Card style={{ padding: 14 }}><Txt style={{ fontFamily: font.semibold, color: colors.dim, fontSize: 13 }}>YOU ASKED</Txt><Txt style={{ fontSize: 15.5, lineHeight: 22 }}>{q}</Txt></Card>
                <Skeleton height={92} /><Skeleton height={92} /><Skeleton height={70} />
              </View>
            )}

            {!!err && !busy && <ErrorCard message="The team couldn't be reached" onRetry={() => ask()} />}

            {res && !busy && (
              <FadeIn>
                <View style={{ gap: 14 }}>
                  <Card style={{ padding: 14, gap: 4 }}>
                    <Txt style={{ fontFamily: font.semibold, color: colors.dim, fontSize: 12.5, letterSpacing: 0.6 }}>YOU ASKED</Txt>
                    <Txt style={{ fontSize: 15.5, lineHeight: 22 }}>{res.question}</Txt>
                  </Card>

                  {res.care ? (
                    <Card glow={colors.red} style={{ gap: 8 }}>
                      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}><Ionicons name="heart" size={20} color={colors.red} /><Txt style={{ fontFamily: font.bold, color: colors.red, fontSize: 16 }}>Please read this first</Txt></View>
                      <Txt selectable style={{ fontSize: 15.5, lineHeight: 24 }}>{res.verdict}</Txt>
                    </Card>
                  ) : (
                    <>
                      {res.speakers.length > 0 && (
                        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                          <Txt style={{ color: colors.dim, fontSize: 13 }}>Invited:</Txt>
                          {res.speakers.map((s) => <View key={s} style={{ alignItems: 'center', gap: 3 }}><Avatar size={36} color={who(s).color} icon={who(s).icon} /><Txt style={{ fontSize: 10.5, color: colors.dim }}>{who(s).label.split(' ')[0]}</Txt></View>)}
                        </View>
                      )}

                      {rounds.map((r) => (
                        <View key={r} style={{ gap: 10 }}>
                          {rounds.length > 1 && <Txt style={{ color: colors.amber, fontFamily: font.bold, fontSize: 12.5, letterSpacing: 0.6 }}>{r === 1 ? 'FIRST ANSWERS' : `THEY DISAGREED · ROUND ${r}`}</Txt>}
                          {res.turns.filter((t) => t.round === r).map((t, i) => {
                            const w = who(t.agent);
                            return (
                              <View key={`${r}-${i}`} style={{ flexDirection: 'row', gap: 10, alignItems: 'flex-start' }}>
                                <Avatar size={34} color={w.color} icon={w.icon} />
                                <View style={{ flex: 1, padding: 13, borderRadius: 20, borderTopLeftRadius: 6, backgroundColor: w.color + '14', borderWidth: 1, borderColor: w.color + '44', gap: 6 }}>
                                  <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
                                    <Txt style={{ color: w.color, fontFamily: font.bold, fontSize: 12.5 }}>{w.label}</Txt>
                                    {!!t.text && (
                                      <Pressable onPress={() => say(`${r}-${i}`, t.text, t.agent)} accessibilityRole="button" accessibilityLabel={speakingKey === `${r}-${i}` ? `Stop ${w.label}` : `Hear ${w.label}`} hitSlop={8}
                                        style={{ width: 44, height: 44, marginVertical: -12, marginRight: -10, alignItems: 'center', justifyContent: 'center' }}>
                                        <Ionicons name={speakingKey === `${r}-${i}` ? 'stop-circle' : 'volume-high'} size={20} color={w.color} />
                                      </Pressable>
                                    )}
                                  </View>
                                  <Txt selectable style={{ fontSize: 15, lineHeight: 22 }}>{t.text || 'No change from me.'}</Txt>
                                  {t.proposals.map((p) => <View key={p} style={{ alignSelf: 'flex-start', paddingHorizontal: 10, paddingVertical: 5, borderRadius: 999, backgroundColor: 'rgba(255,255,255,0.08)' }}><Txt style={{ fontSize: 12, fontFamily: font.semibold }}>Suggests: {p}</Txt></View>)}
                                </View>
                              </View>
                            );
                          })}
                        </View>
                      ))}

                      {res.conflict && <Txt style={{ color: colors.amber, fontSize: 13, lineHeight: 19 }}>They still didn't fully agree after {res.rebuttal_rounds} round{res.rebuttal_rounds === 1 ? '' : 's'}, so the safer option wins.</Txt>}

                      <Card glow={colors.lime} style={{ gap: 8 }}>
                        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 9 }}>
                          <Avatar size={30} color={TEAM.moderator.color} icon={TEAM.moderator.icon} />
                          <Txt style={{ fontFamily: font.black, fontSize: 17 }}>The call</Txt>
                        </View>
                        <Txt selectable style={{ fontSize: 16, lineHeight: 24 }}>{res.verdict}</Txt>
                        {!!res.verdict && (
                          <Pressable onPress={() => say('verdict', res.verdict, 'moderator')} accessibilityRole="button" accessibilityLabel={speakingKey === 'verdict' ? 'Stop reading the verdict' : 'Hear the verdict'}
                            style={{ minHeight: 44, flexDirection: 'row', alignItems: 'center', gap: 6 }}>
                            <Ionicons name={speakingKey === 'verdict' ? 'stop-circle' : 'volume-high'} size={20} color={colors.lime} />
                            <Txt style={{ color: colors.lime, fontFamily: font.semibold, fontSize: 13.5 }}>{speakingKey === 'verdict' ? 'Stop' : 'Hear it'}</Txt>
                          </Pressable>
                        )}
                        {!res.llm && <Txt style={{ color: colors.faint, fontSize: 12.5 }}>Connect the AI service (GEMINI_API_KEY) so the team can discuss this.</Txt>}
                      </Card>

                      {res.results.length > 0 && (
                        <View style={{ gap: 8 }}>
                          <Txt style={{ color: colors.dim, fontSize: 12.5, fontFamily: font.semibold, letterSpacing: 0.6 }}>WHAT HAPPENED TO THEIR SUGGESTIONS</Txt>
                          {res.results.map((r, i) => {
                            const o = OUTCOME[r.status] ?? { label: r.status, color: colors.dim, icon: 'ellipse' };
                            return (
                              <View key={i} style={{ flexDirection: 'row', alignItems: 'center', gap: 10, padding: 12, borderRadius: 16, backgroundColor: o.color + '12', borderWidth: 1, borderColor: o.color + '44' }}>
                                <Ionicons name={o.icon as any} size={20} color={o.color} />
                                <View style={{ flex: 1 }}>
                                  <Txt style={{ fontFamily: font.semibold, fontSize: 14.5 }}>{r.proposal}</Txt>
                                  <Txt style={{ color: o.color, fontSize: 12.5, fontFamily: font.bold }}>{o.label}{r.explain && r.status === 'blocked' ? ` · ${r.explain}` : ''}</Txt>
                                </View>
                              </View>
                            );
                          })}
                          {onOpenTeam && res.results.some((r) => r.status === 'pending_approval' || r.status === 'pending_veto') && (
                            <Pressable onPress={() => { onClose(); onOpenTeam(); }} accessibilityRole="button" style={{ minHeight: 44, justifyContent: 'center' }}>
                              <Txt style={{ color: colors.cyan, fontFamily: font.bold, fontSize: 14.5 }}>Review it in Your team →</Txt>
                            </Pressable>
                          )}
                        </View>
                      )}
                    </>
                  )}

                  <Pressable onPress={reset} accessibilityRole="button" style={{ minHeight: 48, alignItems: 'center', justifyContent: 'center', borderRadius: 24, borderWidth: 1, borderColor: 'rgba(255,255,255,0.18)' }}>
                    <Txt style={{ fontFamily: font.bold, fontSize: 15 }}>Ask something else</Txt>
                  </Pressable>
                </View>
              </FadeIn>
            )}
          </ScrollView>
        </KeyboardAvoidingView>
      </View>
    </Modal>
  );
}
