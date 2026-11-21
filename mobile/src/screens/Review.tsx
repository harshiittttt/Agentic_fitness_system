import React, { useEffect, useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, View } from 'react-native';
import { LinearGradient } from 'expo-linear-gradient';
import { Ionicons } from '@expo/vector-icons';
import { getChanges, runReview } from '../api';
import { agentStyle, colors, font, gradients } from '../theme';
import { Bar, Card, GradientButton, SectionTitle, Txt, useOnline, useToast } from '../ui';

function AgentCard({ d }: { d: any }) {
  const st = agentStyle[d.agent] ?? { color: colors.lime, icon: 'sparkles' };
  const conf = d.data?.confidence ?? d.data?.readiness?.score / 100;
  return (
    <Card style={{ gap: 10 }}>
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
        <View style={{ width: 40, height: 40, borderRadius: 13, backgroundColor: st.color + '22', alignItems: 'center', justifyContent: 'center' }}>
          <Ionicons name={st.icon as any} size={20} color={st.color} />
        </View>
        <Txt style={{ fontFamily: font.bold, fontSize: 16, color: st.color }}>{d.agent}</Txt>
      </View>
      <Txt style={{ fontSize: 15, lineHeight: 22 }}>{d.says}</Txt>
      {typeof conf === 'number' && !isNaN(conf) && (
        <View style={{ gap: 4 }}>
          <Txt style={{ color: colors.dim, fontSize: 11, letterSpacing: 0.8 }}>HOW SURE</Txt>
          <Bar value={conf} color={st.color} />
        </View>
      )}
    </Card>
  );
}

/**
 * The quick weekly check-in, now living at the bottom of the Team screen so there is one place for "what are my coaches doing".
 * It differs from "Ask the team now" above: that one asks the AI coaches. This is the fast, rule-based second opinion with no AI wait,
 * plus the plan's change history.
 */
export default function Review({ embedded = false }: { embedded?: boolean }) {
  const { epoch } = useOnline();
  const toast = useToast();
  const [data, setData] = useState<any>(null);
  const [changes, setChanges] = useState<any[]>([]);
  const [busy, setBusy] = useState(false);
  const [showLog, setShowLog] = useState(false);
  const load = () => getChanges().then((c) => setChanges(Array.isArray(c) ? c : [])).catch(() => {});
  useEffect(() => { load(); }, [epoch]);
  const run = async () => {
    setBusy(true);
    try { setData(await runReview()); load(); } catch { toast("Couldn't run the check. Try again in a moment.", { tone: 'warn' }); }
    setBusy(false);
  };

  const body = (
    <View style={{ gap: 14 }}>
      {!embedded && (
        <View>
          <Txt style={{ color: colors.dim, fontFamily: font.medium, fontSize: 14 }}>Your coaches</Txt>
          <Txt style={{ fontFamily: font.black, fontSize: 32, letterSpacing: -0.5 }}>Weekly check</Txt>
        </View>
      )}
      <SectionTitle>Quick weekly check</SectionTitle>
      <Txt style={{ color: colors.dim, fontSize: 14, lineHeight: 21 }}>A fast second opinion from your coaches' standard rules, with no waiting on AI. Use it when you wonder "should anything change this week?"</Txt>
      <GradientButton label={busy ? 'Checking…' : 'Should anything change?'} icon="git-compare" onPress={run} />
      {busy && <ActivityIndicator color={colors.lime} />}

      {data?.debate?.map((d: any) => <AgentCard key={d.agent} d={d} />)}
      {data && (
        <LinearGradient colors={data.changed ? gradients.lime : ['#1F2430', '#171B24']} style={{ borderRadius: 26, padding: 20, gap: 6 }}>
          <Txt style={{ fontFamily: font.bold, fontSize: 12, letterSpacing: 1.2, color: data.changed ? '#07080B' : colors.dim }}>THE VERDICT</Txt>
          <Txt style={{ fontFamily: font.bold, fontSize: 18, lineHeight: 25, color: data.changed ? '#07080B' : colors.text }}>{data.verdict}</Txt>
        </LinearGradient>
      )}

      <Pressable onPress={() => setShowLog((v) => !v)} accessibilityRole="button" accessibilityState={{ expanded: showLog }} accessibilityLabel="Plan history"
        style={{ minHeight: 48, flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
        <SectionTitle>Plan history</SectionTitle>
        <Ionicons name={showLog ? 'chevron-up' : 'chevron-down'} size={20} color={colors.dim} />
      </Pressable>
      {showLog && changes.length === 0 && <Txt style={{ color: colors.dim, lineHeight: 21 }}>No changes yet. The plan only moves when the evidence is strong.</Txt>}
      {showLog && changes.map((c, i) => {
        const st = agentStyle[c.agent.charAt(0).toUpperCase() + c.agent.slice(1)] ?? { color: colors.lime };
        return (
          <View key={c.id} style={{ flexDirection: 'row', gap: 14 }}>
            <View style={{ alignItems: 'center' }}>
              <View style={{ width: 12, height: 12, borderRadius: 6, backgroundColor: st.color, marginTop: 6 }} />
              {i < changes.length - 1 && <View style={{ flex: 1, width: 2, backgroundColor: colors.border }} />}
            </View>
            <View style={{ flex: 1, paddingBottom: 18, gap: 2 }}>
              <Txt style={{ fontFamily: font.semibold, fontSize: 16 }}>{c.summary}</Txt>
              <Txt style={{ color: colors.dim, fontSize: 13, lineHeight: 19 }}>{c.reason}</Txt>
              {!!c.evidence && <Txt style={{ color: colors.dim, fontSize: 12 }}>{c.evidence}</Txt>}
            </View>
          </View>
        );
      })}
    </View>
  );
  return embedded ? body : <ScrollView contentContainerStyle={{ padding: 20, paddingBottom: 40 }}>{body}</ScrollView>;
}
