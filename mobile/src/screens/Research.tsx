import React, { useState } from 'react';
import { ScrollView, TextInput, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { ResearchAnswer, askResearch } from '../api';
import * as Haptics from '../haptics';
import { colors, font } from '../theme';
import { Card, FadeIn, GradientButton, Txt } from '../ui';
import { Pill } from '../ux/controls';
import SpeakButton from '../ux/SpeakButton';
import CoachShell, { inputStyle } from './CoachShell';

const EXAMPLES = ['How much protein helps build muscle?', 'Is training each muscle twice a week better?', 'Does creatine work?'];

function Body() {
  const [q, setQ] = useState('');
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<ResearchAnswer | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const ask = async (question: string) => {
    const t = question.trim();
    if (!t || busy) return;
    setBusy(true); setErr(null);
    try { setRes(await askResearch(t)); Haptics.selectionAsync(); } catch (e: any) { setErr(e?.message ?? 'Could not reach your coach. Try again.'); }
    finally { setBusy(false); }
  };

  return (
    <ScrollView contentContainerStyle={{ padding: 16, gap: 14, paddingBottom: 40 }} keyboardShouldPersistTaps="handled">
      <Card style={{ gap: 6 }}>
        <Txt style={{ fontFamily: font.bold, fontSize: 15 }}>Ask about the evidence</Txt>
        <Txt style={{ color: colors.dim, fontSize: 14, lineHeight: 21 }}>I answer from a small library of studies and guidelines on training and nutrition, and I cite what I used. If my library does not cover your question I will say so instead of guessing. I cannot give medical advice.</Txt>
      </Card>

      <View style={{ gap: 10 }}>
        <TextInput value={q} onChangeText={setQ} placeholder="e.g. How much protein do I need?" placeholderTextColor={colors.faint} maxLength={400} multiline accessibilityLabel="Your question" style={[inputStyle, { minHeight: 80, paddingTop: 12, textAlignVertical: 'top' }]} />
        <GradientButton label={busy ? 'Looking it up...' : 'Ask'} icon="search" onPress={() => ask(q)} />
        {!res && <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8 }}>{EXAMPLES.map((e) => <Pill key={e} label={e} onPress={() => { setQ(e); ask(e); }} />)}</View>}
      </View>

      {!!err && <Card glow={colors.amber}><Txt accessibilityRole="alert" style={{ fontSize: 14, lineHeight: 20 }}>{err}</Txt></Card>}

      {!!res && (
        <FadeIn key={res.question + res.answer.length}>
          {res.referred ? (
            <Card glow={colors.red} style={{ gap: 8 }}>
              <View style={{ flexDirection: 'row', gap: 10, alignItems: 'center' }}>
                <Ionicons name="medkit" size={20} color={colors.red} />
                <Txt style={{ fontFamily: font.bold, fontSize: 16, flex: 1 }}>Best asked of a professional</Txt>
              </View>
              <Txt style={{ fontSize: 14.5, lineHeight: 22 }}>{res.answer}</Txt>
              <SpeakButton text={res.answer} agent="research" color={colors.red} name="the answer" label />
            </Card>
          ) : res.no_sources ? (
            <Card style={{ gap: 8 }}>
              <View style={{ flexDirection: 'row', gap: 10, alignItems: 'center' }}>
                <Ionicons name="library-outline" size={20} color={colors.amber} />
                <Txt style={{ fontFamily: font.bold, fontSize: 16, flex: 1 }}>No sources available</Txt>
              </View>
              <Txt style={{ color: colors.dim, fontSize: 14.5, lineHeight: 22 }}>{res.answer}</Txt>
              <SpeakButton text={res.answer} agent="research" color={colors.amber} name="the answer" label />
            </Card>
          ) : (
            <Card style={{ gap: 12 }}>
              <Txt style={{ color: colors.dim, fontSize: 12, letterSpacing: 0.8, fontFamily: font.semibold }}>WHAT THE EVIDENCE SUGGESTS</Txt>
              <Txt style={{ fontSize: 15.5, lineHeight: 24 }}>{res.answer}</Txt>
              <SpeakButton text={res.answer} agent="research" color={colors.cyan} name="the answer" label />
              <View style={{ gap: 6 }}>
                <Txt style={{ color: colors.dim, fontSize: 12, letterSpacing: 0.8, fontFamily: font.semibold }}>SOURCES</Txt>
                {res.sources.map((s) => (
                  <View key={s.id} style={{ flexDirection: 'row', gap: 8, alignItems: 'flex-start' }}>
                    <Txt style={{ color: colors.cyan, fontFamily: font.bold, fontSize: 13 }}>[{s.id}]</Txt>
                    <Txt style={{ flex: 1, color: colors.dim, fontSize: 13.5, lineHeight: 19 }}>{s.cite}</Txt>
                  </View>
                ))}
              </View>
              <Txt style={{ color: colors.faint, fontSize: 12.5, lineHeight: 18 }}>{res.caveat}</Txt>
            </Card>
          )}
        </FadeIn>
      )}
    </ScrollView>
  );
}

export default function Research({ visible, onClose }: { visible: boolean; onClose: () => void }) {
  return <CoachShell visible={visible} onClose={onClose} title="Ask the evidence" subtitle="Cited answers, or an honest 'no sources'."><Body /></CoachShell>;
}
