import React, { useCallback, useEffect, useState } from 'react';
import { ScrollView, TextInput, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { IntakeQuestion, IntakeReply, IntakeStatus, answerIntake, getIntake } from '../api';
import * as Haptics from '../haptics';
import { colors, font } from '../theme';
import { Bar, Card, EmptyState, ErrorCard, FadeIn, IconButton, Skeleton, Txt, useOnline } from '../ui';
import { Pill } from '../ux/controls';
import SpeakButton from '../ux/SpeakButton';
import CoachShell, { inputStyle } from './CoachShell';

const CORE = 10;   // 8 required answers + injuries + equipment

const label = (f: string) => f.replace(/_/g, ' ');

function Body() {
  const { epoch } = useOnline();
  const [st, setSt] = useState<IntakeStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState<IntakeQuestion | null>(null);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [reply, setReply] = useState<IntakeReply | null>(null);
  const [sendErr, setSendErr] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const load = useCallback(async () => {
    setErr(null);
    try { const s = await getIntake(); setSt(s); setQ(s.next_question); setDone(!s.next_question); } catch (e: any) { setErr(e?.message ?? 'Could not load your questions'); }
  }, []);
  useEffect(() => { load(); }, [load, epoch]);

  const remaining = st ? st.missing_required.length + st.ask_once.length : 0;
  const answered = Math.max(0, CORE - remaining);
  const shown = answered;

  const send = async (raw: string) => {
    const t = raw.trim();
    if (!t || busy) return;
    setBusy(true); setSendErr(null);
    try {
      const r = await answerIntake(t, q?.slot);
      setReply(r); setText('');
      if (r.care) Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning);
      else if (Object.keys(r.accepted).length) Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
      else if (r.refused.length) Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning);
      if (!r.care) {
        setQ(r.next_question);
        setDone(!r.next_question);
        try { const s = await getIntake(); setSt(s); } catch { /* keep the old progress */ }
      }
    } catch (e: any) { setSendErr(e?.message ?? 'Could not send that. Try again.'); }
    finally { setBusy(false); }
  };

  if (err) return <View style={{ padding: 16 }}><ErrorCard message={err} onRetry={load} /></View>;
  if (!st) return <View style={{ padding: 16, gap: 12 }}><Skeleton height={60} /><Skeleton height={160} /></View>;

  const refused = reply?.refused ?? [];
  const accepted = Object.entries(reply?.accepted ?? {});
  return (
    <ScrollView contentContainerStyle={{ padding: 16, gap: 14, paddingBottom: 40 }} keyboardShouldPersistTaps="handled">
      <FadeIn>
        <Card style={{ gap: 8 }}>
          <View style={{ flexDirection: 'row', justifyContent: 'space-between' }}>
            <Txt style={{ fontFamily: font.bold, fontSize: 14 }}>{remaining === 0 ? 'Basics complete' : `${shown} of ${CORE} basics`}</Txt>
            <Txt style={{ color: colors.dim, fontSize: 13 }}>{remaining === 0 ? 'Done' : `${remaining} to go`}</Txt>
          </View>
          <Bar value={shown / CORE} color={colors.lime} />
          <Txt style={{ color: colors.dim, fontSize: 13, lineHeight: 19 }}>One question at a time. I only keep what you tell me, and I never guess. You can change any answer later.</Txt>
        </Card>
      </FadeIn>

      {!!reply?.care && (
        <Card glow={colors.red} style={{ gap: 8 }} >
          <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10 }}>
            <Ionicons name="heart" size={20} color={colors.red} />
            <Txt style={{ fontFamily: font.bold, fontSize: 16, flex: 1 }}>This matters more than the questions</Txt>
          </View>
          <Txt style={{ fontSize: 14.5, lineHeight: 22 }}>{reply.care}</Txt>
          <SpeakButton text={reply.care} agent="intake" color={colors.red} name="this message" label />
          <Txt style={{ color: colors.dim, fontSize: 13 }}>I have not saved that answer. Come back to the questions whenever you are ready.</Txt>
        </Card>
      )}

      {!reply?.care && !!reply && (accepted.length > 0 || refused.length > 0 || reply.warnings.length > 0) && (
        <FadeIn>
          <Card style={{ gap: 8 }}>
            {accepted.length > 0 && (
              <View style={{ flexDirection: 'row', gap: 8, alignItems: 'flex-start' }}>
                <Ionicons name="checkmark-circle" size={18} color={colors.mint} style={{ marginTop: 2 }} />
                <Txt style={{ flex: 1, fontSize: 14, lineHeight: 20 }}>Got it: {accepted.map(([k, v]) => `${label(k)} ${Array.isArray(v) ? (v.length ? v.join(', ') : 'none') : String(v)}`).join(', ')}.</Txt>
              </View>
            )}
            {refused.map((r, i) => (
              <View key={i} style={{ flexDirection: 'row', gap: 8, alignItems: 'flex-start' }}>
                <Ionicons name="alert-circle" size={18} color={colors.amber} style={{ marginTop: 2 }} />
                <Txt style={{ flex: 1, fontSize: 14, lineHeight: 20 }}>{r.reason}</Txt>
              </View>
            ))}
            {reply.warnings.map((w, i) => (
              <View key={`w${i}`} style={{ flexDirection: 'row', gap: 8, alignItems: 'flex-start' }}>
                <Ionicons name="information-circle" size={18} color={colors.cyan} style={{ marginTop: 2 }} />
                <Txt style={{ flex: 1, fontSize: 14, lineHeight: 20 }}>{w}</Txt>
              </View>
            ))}
            {!!reply.needs_rebuild && <Txt style={{ color: colors.dim, fontSize: 13 }}>Your plan was not changed. Ask your coach to rebuild it when you are ready.</Txt>}
          </Card>
        </FadeIn>
      )}

      {!reply?.care && !reply?.refused?.length && !accepted.length && !!reply?.reply && (
        <Card style={{ gap: 8 }}><Txt style={{ fontSize: 14, lineHeight: 21 }}>{reply.reply}</Txt><SpeakButton text={reply.reply} agent="intake" name="the reply" label /></Card>
      )}

      {q && !done ? (
        <FadeIn key={q.slot}>
          <Card glow={colors.lime} style={{ gap: 12 }}>
            <Txt style={{ color: colors.dim, fontSize: 12, letterSpacing: 0.8, fontFamily: font.semibold }}>{q.required ? 'NEEDED FOR YOUR PLAN' : 'OPTIONAL'}</Txt>
            <Txt accessibilityRole="header" style={{ fontFamily: font.black, fontSize: 20, lineHeight: 27 }}>{q.question}</Txt>
            <SpeakButton text={q.question} agent="intake" name="the question" label />
            <Txt style={{ color: colors.dim, fontSize: 13.5, lineHeight: 20 }}>Why I ask: {q.why}</Txt>
            {!!q.chips?.length && (
              <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8 }}>
                {q.chips.map((c) => <Pill key={c} label={c} onPress={() => send(c)} />)}
              </View>
            )}
            <View style={{ flexDirection: 'row', gap: 8, alignItems: 'center' }}>
              <TextInput value={text} onChangeText={setText} placeholder="Or type your answer" placeholderTextColor={colors.faint} maxLength={300} accessibilityLabel="Your answer" returnKeyType="send" onSubmitEditing={() => send(text)} style={[inputStyle, { flex: 1 }]} />
              <IconButton icon="send" label="Send answer" onPress={() => send(text)} color="#07080B" bg={colors.lime} disabled={!text.trim() || busy} />
            </View>
            {!!sendErr && <Txt accessibilityRole="alert" style={{ color: colors.red, fontSize: 14 }}>{sendErr}</Txt>}
            {!q.required && <Pill label="Skip for now" onPress={() => send('skip')} />}
          </Card>
        </FadeIn>
      ) : (
        <EmptyState icon="checkmark-circle" tint={colors.mint} title="Nothing left to ask" body="Your profile is complete and up to date. I will check in again if it starts to look stale." />
      )}
    </ScrollView>
  );
}

export default function Intake({ visible, onClose }: { visible: boolean; onClose: () => void }) {
  return <CoachShell visible={visible} onClose={onClose} title="Get to know you" subtitle="A short interview that keeps your plan accurate."><Body /></CoachShell>;
}
