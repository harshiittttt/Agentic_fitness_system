import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, Alert, Pressable, RefreshControl, ScrollView, Switch, View } from 'react-native';
import AsyncStorage from '@react-native-async-storage/async-storage';
import { Ionicons } from '@expo/vector-icons';
import { LinearGradient } from 'expo-linear-gradient';
import { AgentStatus, Decision, decide, getAgentStatus, getDecisions, runAgents, setDial, setKill } from '../api';
import { TEAM, colors, font, gradients } from '../theme';
import { Avatar, Card, ErrorCard, FadeIn, IconButton, SectionTitle, Segmented, Skeleton, Txt, useOnline } from '../ui';
import * as Haptics from '../haptics';
import Review from './Review';
import Roundtable from './Roundtable';
import SpeakButton from '../ux/SpeakButton';

const STATUS: Record<string, { label: string; color: string }> = {
  applied: { label: 'Applied', color: colors.mint },
  pending_approval: { label: 'Needs your OK', color: colors.amber },
  pending_veto: { label: 'Applies unless you stop it', color: colors.cyan },
  blocked: { label: 'Blocked by a safety rule', color: colors.red },
  escalated: { label: 'Escalated', color: colors.red },
  reverted: { label: 'Undone', color: colors.dim },
  rejected: { label: 'Declined', color: colors.dim },
  vetoed: { label: 'Stopped', color: colors.dim },
};
const TIER: Record<string, string> = { auto: 'Automatic', notify_first: 'Heads-up first', approve: 'Needs approval', escalate: 'Safety' };
const RULE: Record<string, string> = {
  max_weekly_load_increase: 'Weekly load cap', max_volume_vs_baseline: 'Volume ceiling', min_volume: 'Volume floor',
  min_calorie_floor: 'Calorie floor', max_calorie_change: 'Calorie change limit', no_increase_under_pain: 'No increase while in pain',
  agent_scope: 'Agent not allowed to do this', domain_frozen: 'Frozen for safety', kill_switch: 'Agents paused', escalation: 'Safety escalation',
  new_block_needs_approval: 'New block needs approval', no_plan: 'No plan yet', invalid_swap: 'Not a safe swap', apply_failed: 'Could not be applied',
};
const DIAL: { key: 'hands_on' | 'balanced' | 'autopilot'; label: string; blurb: string }[] = [
  { key: 'hands_on', label: 'Hands-on', blurb: 'The team asks before almost every change.' },
  { key: 'balanced', label: 'Balanced', blurb: 'Small, safe changes apply on their own and tell you. Bigger ones ask first.' },
  { key: 'autopilot', label: 'Autopilot', blurb: 'More changes apply after a short window to stop them. Safety and big changes still ask.' },
];
const STEPS = [
  { k: 'event', label: 'Trigger', icon: 'flash' },
  { k: 'agents', label: 'Coaches', icon: 'people' },
  { k: 'critic', label: 'Review', icon: 'shield-checkmark' },
  { k: 'gate', label: 'Rules', icon: 'lock-closed' },
];

const arrow = (s: string) => s.replace(/->/g, '\u2192');
const parse = (iso: string) => new Date(iso.includes('T') ? iso : iso.replace(' ', 'T'));
const ago = (iso: string) => {
  const t = parse(iso).getTime();
  if (isNaN(t)) return '';
  const m = Math.max(0, Math.round((Date.now() - t) / 60000));
  return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
};
const clock = (iso: string | null) => {
  if (!iso) return '';
  const d = parse(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
};

function Pill({ label, color }: { label: string; color: string }) {
  return (
    <View style={{ paddingHorizontal: 9, paddingVertical: 3, borderRadius: 999, backgroundColor: color + '22' }}>
      <Txt style={{ fontFamily: font.bold, fontSize: 11, color }}>{label}</Txt>
    </View>
  );
}

function Btn({ label, onPress, color = 'rgba(255,255,255,0.07)', text = colors.text, busy }: { label: string; onPress: () => void; color?: string; text?: string; busy?: boolean }) {
  return (
    <Pressable onPress={onPress} disabled={busy} style={({ pressed }) => ({ flex: 1, paddingVertical: 13, borderRadius: 15, alignItems: 'center', backgroundColor: color, opacity: pressed || busy ? 0.7 : 1 })}>
      {busy ? <ActivityIndicator color={text} /> : <Txt style={{ fontFamily: font.bold, fontSize: 14, color: text }}>{label}</Txt>}
    </Pressable>
  );
}

/** The loop, drawn: event -> agents -> critic -> gate. Lights up in turn while the team works, solid once it has answered. */
function Pipeline({ running, done }: { running: boolean; done: boolean }) {
  const [i, setI] = useState(0);
  useEffect(() => {
    if (!running) return;
    setI(0);
    const t = setInterval(() => setI((n) => (n + 1) % STEPS.length), 850);
    return () => clearInterval(t);
  }, [running]);
  return (
    <View style={{ flexDirection: 'row', alignItems: 'center' }}>
      {STEPS.map((st, k) => {
        const lit = done || (running && k <= i);
        const now = running && k === i;
        return (
          <React.Fragment key={st.k}>
            <View style={{ alignItems: 'center', gap: 7, width: 62 }}>
              <Avatar size={46} color={lit ? colors.lime : colors.faint} icon={st.icon} ring={now} dim={!lit} />
              <Txt style={{ fontFamily: font.bold, fontSize: 11, letterSpacing: 0.6, color: lit ? colors.text : colors.faint }}>{st.label.toUpperCase()}</Txt>
            </View>
            {k < STEPS.length - 1 && <View style={{ flex: 1, height: 2, borderRadius: 1, marginBottom: 20, backgroundColor: done || (running && k < i) ? colors.lime + 'aa' : 'rgba(255,255,255,0.10)' }} />}
          </React.Fragment>
        );
      })}
    </View>
  );
}

/** What the team actually said on the last run: each agent's own reasoning, the critic's verdicts, what the gate did. */
function RunReport({ r }: { r: any }) {
  const agents = Object.entries(r.agents ?? {}) as [string, any][];
  const verdicts = r.critic?.verdicts ?? [];
  return (
    <View style={{ gap: 12 }}>
      {agents.map(([a, v], i) => {
        const t = TEAM[a] ?? TEAM.orchestrator;
        return (
          <FadeIn key={a} delay={i * 90} distance={10}>
            <Card style={{ gap: 10 }}>
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
                <Avatar size={40} color={t.color} icon={t.icon} />
                <View style={{ flex: 1 }}>
                  <Txt style={{ fontFamily: font.bold, fontSize: 16, color: t.color }}>{t.label}</Txt>
                  <Txt style={{ color: colors.dim, fontSize: 12 }}>{t.role}</Txt>
                </View>
                <Pill label={v.proposals?.length ? `${v.proposals.length} proposal${v.proposals.length > 1 ? 's' : ''}` : 'No change'} color={v.proposals?.length ? t.color : colors.faint} />
              </View>
              {!!v.reasoning && <Txt style={{ color: colors.dim, fontSize: 14, lineHeight: 21 }} numberOfLines={8}>{v.reasoning}</Txt>}
              {!!v.reasoning && <SpeakButton text={String(v.reasoning)} agent={a} color={t.color} name={`${t.label}'s reasoning`} label />}
              {v.proposals?.map((p: string, k: number) => (
                <View key={k} style={{ flexDirection: 'row', gap: 8, alignItems: 'center' }}>
                  <Ionicons name="arrow-forward-circle" size={16} color={t.color} />
                  <Txt style={{ flex: 1, fontFamily: font.semibold, fontSize: 14 }}>{arrow(p)}</Txt>
                </View>
              ))}
            </Card>
          </FadeIn>
        );
      })}
      {r.critic && (
        <FadeIn delay={agents.length * 90} distance={10}>
          <Card style={{ gap: 10 }} glow={r.critic.critic_ok ? undefined : colors.amber}>
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
              <Avatar size={40} color={TEAM.safety_critic.color} icon="shield-checkmark" />
              <View style={{ flex: 1 }}>
                <Txt style={{ fontFamily: font.bold, fontSize: 16, color: TEAM.safety_critic.color }}>Safety critic</Txt>
                <Txt style={{ color: colors.faint, fontSize: 12 }}>{r.critic.critic_ok ? 'Checked every suggestion' : 'Could not run, so risky changes were held back'}</Txt>
              </View>
            </View>
            {!!r.critic.assessment && <Txt style={{ color: colors.dim, fontSize: 14, lineHeight: 21 }}>{r.critic.assessment}</Txt>}
            <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 6 }}>
              {verdicts.map((v: any, k: number) => (
                <Pill key={k} label={`${v.verdict === 'approve' ? 'Approved' : v.verdict === 'block' ? 'Blocked' : 'Revised'}: ${(v.proposal ?? '').slice(0, 28)}`} color={v.verdict === 'approve' ? colors.mint : v.verdict === 'block' ? colors.red : colors.amber} />
              ))}
            </View>
          </Card>
        </FadeIn>
      )}
      {(r.results?.length ?? 0) > 0 && (
        <FadeIn delay={(agents.length + 1) * 90} distance={10}>
          <Card style={{ gap: 10 }}>
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
              <Avatar size={40} color={colors.lime} icon="lock-closed" />
              <View style={{ flex: 1 }}>
                <Txt style={{ fontFamily: font.bold, fontSize: 16 }}>Safety rules</Txt>
                <Txt style={{ color: colors.dim, fontSize: 12 }}>Fixed limits the coaches cannot override</Txt>
              </View>
            </View>
            {r.results.map((x: any, k: number) => {
              const st = STATUS[x.status] ?? { label: x.status, color: colors.dim };
              return (
                <View key={k} style={{ gap: 4 }}>
                  <Txt style={{ fontFamily: font.semibold, fontSize: 14 }}>{arrow(x.proposal)}</Txt>
                  <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 6 }}>
                    <Pill label={st.label} color={st.color} />
                    {(x.fired ?? []).map((f: string) => <Pill key={f} label={RULE[f] ?? f.replace(/_/g, ' ')} color={colors.amber} />)}
                  </View>
                </View>
              );
            })}
          </Card>
        </FadeIn>
      )}
    </View>
  );
}

function Row({ d, busy, onAct, open, onToggle }: { d: Decision; busy: boolean; onAct: (a: 'approve' | 'reject' | 'revert') => void; open: boolean; onToggle: () => void }) {
  const a = TEAM[d.agent] ?? { label: d.agent, color: colors.lime, icon: 'sparkles', role: '' };
  const st = STATUS[d.status] ?? { label: d.status, color: colors.dim };
  const hot = d.status === 'pending_approval' || d.status === 'pending_veto';
  return (
    <Card onPress={onToggle} glow={hot ? st.color : undefined} style={{ gap: 11 }}>
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 13 }}>
        <Avatar size={42} color={a.color} icon={a.icon} />
        <View style={{ flex: 1, gap: 2 }}>
          <Txt style={{ fontFamily: font.bold, fontSize: 16, lineHeight: 21, letterSpacing: -0.2 }}>{arrow(d.summary)}</Txt>
          <Txt style={{ color: colors.dim, fontSize: 12 }}>{a.label} · {ago(d.ts)}</Txt>
        </View>
        <Ionicons name={open ? 'chevron-up' : 'chevron-down'} size={18} color={colors.faint} />
      </View>
      <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 6 }}>
        <Pill label={st.label} color={st.color} />
        <Pill label={TIER[d.tier] ?? d.tier} color={colors.dim} />
      </View>
      <Txt style={{ color: colors.dim, fontSize: 14, lineHeight: 21 }}>{d.why}</Txt>

      {d.status === 'pending_approval' && (
        <View style={{ flexDirection: 'row', gap: 10 }}>
          <Btn label="Not now" onPress={() => onAct('reject')} busy={busy} />
          <Btn label="Approve" onPress={() => onAct('approve')} color={colors.lime} text="#07080B" busy={busy} />
        </View>
      )}
      {d.status === 'pending_veto' && (
        <View style={{ gap: 9 }}>
          <Txt style={{ color: colors.cyan, fontSize: 13, fontFamily: font.semibold }}>Will apply at {clock(d.veto_until)} unless you stop it.</Txt>
          <View style={{ flexDirection: 'row', gap: 10 }}>
            <Btn label="Stop it" onPress={() => onAct('reject')} busy={busy} />
            <Btn label="Apply now" onPress={() => onAct('approve')} busy={busy} />
          </View>
        </View>
      )}

      {open && (
        <View style={{ gap: 8, borderTopWidth: 1, borderTopColor: colors.border, paddingTop: 11 }}>
          {!!d.evidence && <Txt style={{ color: colors.dim, fontSize: 12.5, lineHeight: 18 }}>What they looked at: {d.evidence}</Txt>}
          {d.fired.length > 0 && (
            <Txt style={{ color: colors.amber, fontSize: 12.5, lineHeight: 18 }}>Safety rules that applied: {d.fired.map((f) => RULE[f] ?? f.replace(/_/g, ' ')).join(', ')}</Txt>
          )}
          <Txt style={{ color: colors.dim, fontSize: 12 }}>Started by {d.trigger.replace('watchdog:', 'a safety watch on ').replace(/[._]/g, ' ')} · ref #{d.id}</Txt>
          {d.can_revert && <Btn label="Undo this change" onPress={() => onAct('revert')} busy={busy} />}
        </View>
      )}
    </Card>
  );
}

export default function Decisions({ onPending }: { onPending?: (n: number) => void }) {
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const [items, setItems] = useState<Decision[]>([]);
  const [err, setErr] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [running, setRunning] = useState(false);
  const [table, setTable] = useState(false);
  const [report, setReport] = useState<any>(null);
  const [runErr, setRunErr] = useState('');
  const alive = useRef(true);
  const { epoch } = useOnline();
  const [intro, setIntro] = useState(false);
  useEffect(() => { AsyncStorage.getItem('team_intro_seen_v1').then((v) => { if (!v) setIntro(true); }).catch(() => {}); }, []);
  const dismissIntro = () => { Haptics.selectionAsync(); setIntro(false); AsyncStorage.setItem('team_intro_seen_v1', '1').catch(() => {}); };

  const load = useCallback(async () => {
    try {
      const [s, d] = await Promise.all([getAgentStatus(), getDecisions()]);
      if (!alive.current) return;
      setStatus(s); setItems(d); setErr('');
      onPending?.(s.pending.length);
    } catch (e: any) {
      if (alive.current) setErr(String(e?.message ?? e));
    } finally {
      if (alive.current) setRefreshing(false);
    }
  }, [onPending]);

  useEffect(() => {
    alive.current = true;
    load();
    const t = setInterval(load, 15000);
    return () => { alive.current = false; clearInterval(t); };
  }, [load, epoch]);

  const act = async (d: Decision, action: 'approve' | 'reject' | 'revert') => {
    const go = async () => {
      setBusyId(d.id);
      try { await decide(d.id, action); } catch (e: any) { Alert.alert('Could not do that', String(e?.message ?? e)); }
      setBusyId(null); load();
    };
    if (action === 'revert') Alert.alert('Undo this change?', d.summary, [{ text: 'Keep it', style: 'cancel' }, { text: 'Undo', style: 'destructive', onPress: go }]);
    else go();
  };

  const runTeam = async () => {
    setRunning(true); setRunErr(''); setReport(null);
    try { const [r] = await Promise.all([runAgents('cron.daily'), new Promise((res) => setTimeout(res, 2400))]); setReport(r); } catch (e: any) { setRunErr(`Could not reach the team: ${String(e?.message ?? e)}`); }
    setRunning(false); load();
  };

  const toggleKill = async (on: boolean) => {
    const go = async () => { try { await setKill(on); } catch (e: any) { Alert.alert('Could not change that', String(e?.message ?? e)); } load(); };
    if (on) Alert.alert('Pause all agent changes?', 'Nothing will change on its own until you turn this off. Safety escalations still work.', [{ text: 'Cancel', style: 'cancel' }, { text: 'Pause', onPress: go }]);
    else go();
  };
  const pickDial = async (k: 'hands_on' | 'balanced' | 'autopilot') => {
    if (status) setStatus({ ...status, dial: k });
    try { await setDial(k); } catch (e: any) { Alert.alert('Could not change that', String(e?.message ?? e)); }
    load();
  };

  if (!status && !err) {
    return <ScrollView contentContainerStyle={{ padding: 20, gap: 14 }}><Skeleton height={150} /><Skeleton height={110} /><Skeleton height={110} /></ScrollView>;
  }
  if (!status) {
    return (
      <View style={{ padding: 20 }}>
        <ErrorCard message="Couldn't load your team" onRetry={load} />
      </View>
    );
  }

  const pending = items.filter((d) => d.pending);
  const history = items.filter((d) => !d.pending);
  const dial = DIAL.find((x) => x.key === status.dial) ?? DIAL[1];
  const dayAgo = Date.now() - 24 * 3600 * 1000;
  const active = (a: string) => items.some((d) => d.agent === a && parse(d.ts).getTime() > dayAgo);

  return (
    <ScrollView contentContainerStyle={{ padding: 20, gap: 14, paddingBottom: 40 }}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={() => { setRefreshing(true); load(); }} tintColor={colors.lime} />}>
      {!status.llm && (
        <Card glow={colors.amber}>
          <Txt style={{ fontFamily: font.bold, color: colors.amber }}>Your coaches are resting</Txt>
          <Txt style={{ color: colors.dim, lineHeight: 20 }}>The AI service isn't connected, so only the safety watch is running. Plans and logging still work. (To fix: add GEMINI_API_KEY to .env and restart the backend.)</Txt>
        </Card>
      )}
      {status.frozen.length > 0 && (
        <Card glow={colors.red}>
          <Txt style={{ fontFamily: font.bold, color: colors.red }}>Frozen for your safety: {status.frozen.join(', ')}</Txt>
          <Txt style={{ color: colors.dim, lineHeight: 20 }}>Something you reported needs a qualified professional to look at it. The agents will not change this part of your plan until you clear the freeze.</Txt>
        </Card>
      )}
      {status.watchdog_flags.length > 0 && (
        <Card glow={colors.amber}>
          <Txt style={{ fontFamily: font.bold, color: colors.amber }}>Safety watch</Txt>
          {status.watchdog_flags.map((w, i) => <Txt key={i} style={{ color: colors.dim, fontSize: 13, lineHeight: 19 }}>{w.watchdog.replace(/_/g, ' ')}: {w.evidence}</Txt>)}
        </Card>
      )}

      {pending.length > 0 && (
        <>
          <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10, padding: 14, borderRadius: 18, backgroundColor: 'rgba(255,184,77,0.14)', borderWidth: 1, borderColor: 'rgba(255,184,77,0.5)' }}>
            <Ionicons name="notifications" size={22} color={colors.amber} />
            <Txt style={{ flex: 1, fontFamily: font.bold, fontSize: 15, lineHeight: 20 }}>{pending.length === 1 ? 'A change is waiting for your OK' : `${pending.length} changes are waiting for your OK`}</Txt>
          </View>
          {pending.map((d) => <Row key={d.id} d={d} busy={busyId === d.id} open={open === d.id} onToggle={() => setOpen(open === d.id ? null : d.id)} onAct={(a) => act(d, a)} />)}
        </>
      )}

      {intro && (
        <Card glow={colors.lime} style={{ gap: 10 }}>
          <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10 }}>
            <Txt style={{ fontFamily: font.black, fontSize: 18, flex: 1 }}>Meet your coaching team</Txt>
            <IconButton icon="close" label="Dismiss this introduction" onPress={dismissIntro} />
          </View>
          <Txt style={{ color: colors.dim, fontSize: 14, lineHeight: 21 }}>Four specialist coaches look after you in the background: <Txt style={{ color: colors.lime, fontFamily: font.bold }}>Training</Txt> (your sessions), <Txt style={{ color: colors.violet, fontFamily: font.bold }}>Recovery</Txt> (rest and readiness), <Txt style={{ color: colors.amber, fontFamily: font.bold }}>Nutrition</Txt> (calories) and <Txt style={{ color: colors.cyan, fontFamily: font.bold }}>Progress</Txt> (trends).</Txt>
          <Txt style={{ color: colors.dim, fontSize: 14, lineHeight: 21 }}>They suggest changes, a safety checker reviews each one, and fixed safety rules have the final word. Small changes happen on their own, bigger ones wait for your OK, and everything shows up here with the reason. You can undo most of it.</Txt>
          <Btn label="Got it" onPress={dismissIntro} color={colors.lime} text="#07080B" />
        </Card>
      )}

      {/* The team and the loop they run on */}
      <Card style={{ gap: 18 }} glow={running ? colors.lime : undefined}>
        <View style={{ flexDirection: 'row', justifyContent: 'space-between' }}>
          {(['training', 'recovery', 'nutrition', 'progress', 'safety_critic'] as const).map((a) => (
            <View key={a} style={{ alignItems: 'center', gap: 6, width: 58 }}>
              <Avatar size={44} color={TEAM[a].color} icon={TEAM[a].icon} ring={active(a) || running} dim={!active(a) && !running} />
              <Txt style={{ fontFamily: font.semibold, fontSize: 10.5, color: colors.dim }} numberOfLines={1}>{TEAM[a].label.replace('Safety critic', 'Critic')}</Txt>
            </View>
          ))}
        </View>
        <View style={{ height: 1, backgroundColor: colors.border }} />
        <Pipeline running={running} done={!!report && !running} />
        <Pressable onPress={runTeam} disabled={running} style={({ pressed }) => ({ transform: [{ scale: pressed ? 0.97 : 1 }], opacity: running ? 0.85 : 1 })}>
          <LinearGradient colors={gradients.lime} start={{ x: 0, y: 0 }} end={{ x: 1, y: 1 }} style={{ borderRadius: 20, paddingVertical: 16, flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 10 }}>
            {running ? <ActivityIndicator color="#07080B" /> : <Ionicons name="git-compare" size={20} color="#07080B" />}
            <Txt style={{ color: '#07080B', fontFamily: font.black, fontSize: 16 }}>{running ? 'The team is working…' : 'Ask the team now'}</Txt>
          </LinearGradient>
        </Pressable>
        <Pressable onPress={() => { Haptics.selectionAsync(); setTable(true); }} accessibilityRole="button" accessibilityLabel="Ask the whole team a question"
          style={({ pressed }) => ({ opacity: pressed ? 0.7 : 1, minHeight: 48, flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 8, borderRadius: 20, borderWidth: 1, borderColor: colors.mint + '66', backgroundColor: colors.mint + '12' })}>
          <Ionicons name="people" size={18} color={colors.mint} />
          <Txt style={{ color: colors.mint, fontFamily: font.bold, fontSize: 15 }}>Ask the whole team a question</Txt>
        </Pressable>
      </Card>
      <Roundtable visible={table} onClose={() => { setTable(false); load(); }} />

      {!!runErr && <Txt style={{ color: colors.red, lineHeight: 20 }}>{runErr}</Txt>}
      {report && !report.llm && <Txt style={{ color: colors.amber, lineHeight: 20 }}>The agents are off: no Gemini key is set on the backend. Safety checks still ran.</Txt>}
      {report?.llm && (
        <>
          <SectionTitle>What the team said</SectionTitle>
          {(report.results?.length ?? 0) === 0 && (report.critic?.held?.length ?? 0) === 0 && <Txt style={{ color: colors.dim, lineHeight: 21 }}>Nothing worth changing right now. That is a good outcome: the plan only moves when the evidence is strong.</Txt>}
          {(report.errors?.length ?? 0) > 0 && <Txt style={{ color: colors.amber, fontSize: 13, lineHeight: 19 }}>{report.errors.length} agent{report.errors.length > 1 ? 's' : ''} could not answer (you may be rate-limited). The others still counted.</Txt>}
          <RunReport r={report} />
        </>
      )}

      <SectionTitle>How much the team decides alone</SectionTitle>
      <Card style={{ gap: 14 }}>
        <Segmented options={DIAL.map((x) => ({ key: x.key, label: x.label }))} value={status.dial as any} onChange={pickDial} />
        <Txt style={{ color: colors.dim, fontSize: 13.5, lineHeight: 20 }}>{dial.blurb}</Txt>
        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12, borderTopWidth: 1, borderTopColor: colors.border, paddingTop: 14 }}>
          <View style={{ flex: 1 }}>
            <Txt style={{ fontFamily: font.semibold, fontSize: 15 }}>Pause all agent changes</Txt>
            <Txt style={{ color: colors.dim, fontSize: 12.5, lineHeight: 18 }}>The kill switch. Nothing changes on its own while this is on.</Txt>
          </View>
          <Switch value={status.kill_switch} onValueChange={toggleKill} trackColor={{ false: colors.cardHi, true: colors.red }} thumbColor="#fff" />
        </View>
        {!!status.designed_by && (
          <Txt style={{ color: colors.dim, fontSize: 12 }}>
            Your plan was {status.designed_by === 'training-agent' ? 'designed for you by the Training agent' : 'built from a starter template (the agents were offline)'}.
          </Txt>
        )}
      </Card>

      <SectionTitle>What the team has done for you</SectionTitle>
      {history.length === 0 && (
        <Txt style={{ color: colors.dim, lineHeight: 21 }}>Nothing yet, which is normal at the start. When you check in, finish a workout or mention pain, the team looks at it. Anything they change shows up here with the reason, and most changes can be undone.</Txt>
      )}
      {history.map((d) => <Row key={d.id} d={d} busy={busyId === d.id} open={open === d.id} onToggle={() => setOpen(open === d.id ? null : d.id)} onAct={(a) => act(d, a)} />)}

      <Review embedded />
    </ScrollView>
  );
}
