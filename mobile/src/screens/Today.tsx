import React, { useCallback, useEffect, useRef, useState } from 'react';
import { usePlanEpoch } from '../planSignal';
import { getNudges, readNudge, Nudge } from '../api';
import { ActivityIndicator, KeyboardAvoidingView, Modal, Platform, Pressable, RefreshControl, ScrollView, TextInput, View, useWindowDimensions } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { LinearGradient } from 'expo-linear-gradient';
import { Ionicons } from '@expo/vector-icons';
import * as Haptics from '../haptics';
import * as Speech from 'expo-speech';
import SpeakButton from '../ux/SpeakButton';
import { Decision, TodayState, fitSession, flushQueue, getCoach, getDecisions, getLiveState, getPlan, getProfile, getReadiness, getTodayState, logVoice, restoreSession } from '../api';
import Live from './Live';
import SwapSheet from '../SwapSheet';
import SetLogger, { LogTarget } from '../SetLogger';
import { useVoice } from '../useVoice';
import { onVoiceAction, takePendingVoiceActions, VoiceAction } from '../voiceBus';
import { setVoiceOverlay } from '../voiceScreen';
import { isoOf } from '../dates';
import { TEAM, colors, font, gradients } from '../theme';
import { Avatar, Card, Chip, Confetti, CountUp, EmptyState, ErrorCard, FadeIn, IconButton, MicButton, Ring, SectionTitle, Skeleton, Txt, useOnline } from '../ui';

function greeting() {
  const h = new Date().getHours();
  return h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
}

const mmss = (n: number) => (n >= 60 ? `${Math.floor(n / 60)}:${String(n % 60).padStart(2, '0')}` : String(n));
const dateLabel = () => new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' });

function readinessWord(score?: number) {
  if (score == null) return { text: 'No check-in yet', color: colors.dim };
  if (score >= 75) return { text: 'Good to push', color: colors.mint };
  if (score >= 50) return { text: 'Take it steady', color: colors.amber };
  return { text: 'Go easy today', color: colors.red };
}

const STATUS_VERB: Record<string, string> = { applied: 'applied', pending_approval: 'needs your OK', pending_veto: 'will apply soon', blocked: 'blocked by a safety rule', escalated: 'escalated for safety', reverted: 'undone', rejected: 'declined', vetoed: 'stopped' };
const since = (iso: string) => {
  const t = new Date(iso.includes('T') ? iso : iso.replace(' ', 'T')).getTime();
  if (isNaN(t)) return '';
  const m = Math.max(0, Math.round((Date.now() - t) / 60000));
  return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
};

/** The team, visible on the home screen: who is on duty, and the last thing they did for you. */
function TeamStrip({ items, onOpen, unreachable }: { items: Decision[]; onOpen?: () => void; unreachable?: boolean }) {
  const dayAgo = Date.now() - 24 * 3600 * 1000;
  const recent = (a: string) => items.some((d) => d.agent === a && new Date(d.ts).getTime() > dayAgo);
  const pending = items.filter((d) => d.pending).length;
  const last = items.find((d) => ['applied', 'pending_approval', 'pending_veto', 'escalated'].includes(d.status)) ?? items[0];
  const lt = last ? TEAM[last.agent] ?? TEAM.orchestrator : null;
  return (
    <Card onPress={onOpen} style={{ gap: 14 }}>
      <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10 }}>
          {(['training', 'recovery', 'nutrition', 'progress'] as const).map((a) => <Avatar key={a} size={38} color={TEAM[a].color} icon={TEAM[a].icon} ring={!unreachable && recent(a)} dim={unreachable || (!recent(a) && items.length > 0)} />)}
        </View>
        {unreachable
          ? <View style={{ backgroundColor: 'rgba(255,255,255,0.08)', borderRadius: 999, paddingHorizontal: 11, paddingVertical: 5 }}><Txt style={{ color: colors.dim, fontFamily: font.bold, fontSize: 12 }}>Offline</Txt></View>
          : pending > 0
          ? <View style={{ backgroundColor: colors.amber + '24', borderRadius: 999, paddingHorizontal: 11, paddingVertical: 5 }}><Txt style={{ color: colors.amber, fontFamily: font.bold, fontSize: 12 }}>{pending} waiting for you</Txt></View>
          : <Ionicons name="chevron-forward" size={18} color={colors.faint} />}
      </View>
      {unreachable && items.length === 0 ? (
        <Txt style={{ color: colors.dim, lineHeight: 21 }}>Can't reach your team right now. Your plan and logging still work, and they'll catch up when you're back online.</Txt>
      ) : last && lt ? (
        <View style={{ gap: 4 }}>
          <Txt style={{ fontFamily: font.semibold, fontSize: 15.5, lineHeight: 22 }}>
            <Txt style={{ color: lt.color, fontFamily: font.bold }}>{lt.label}</Txt> · {last.summary.replace(/->/g, '\u2192')} <Txt style={{ color: colors.dim, fontFamily: font.medium }}>({STATUS_VERB[last.status] ?? last.status})</Txt>
          </Txt>
          <Txt style={{ color: colors.dim, fontSize: 13.5, lineHeight: 20 }} numberOfLines={2}>{last.why}</Txt>
          <Txt style={{ color: colors.dim, fontSize: 12 }}>{since(last.ts)}{unreachable ? ' · last known, may be out of date' : ''}</Txt>
          <SpeakButton text={`${lt.label}. ${last.summary.replace(/->/g, ' to ')}. ${last.why ?? ''}`} agent={last.agent} color={lt.color} name={`${lt.label}'s note`} />
        </View>
      ) : (
        <Txt style={{ color: colors.dim, lineHeight: 21 }}>Your four coaches are on watch. When you train, check in or mention pain, they step in. Anything they do shows up here with the reason. Tap to see more.</Txt>
      )}
    </Card>
  );
}

export default function Today({ onOpenTeam, onOpenProfile }: { onOpenTeam?: () => void; onOpenProfile?: () => void }) {
  const narrow = useWindowDimensions().width < 380;
  const { online, epoch } = useOnline();
  const planEpoch = usePlanEpoch();   // the coach changed the plan from chat: reload
  const [nudges, setNudges] = useState<Nudge[]>([]);
  useEffect(() => { getNudges().then(setNudges).catch(() => {}); }, [epoch, planEpoch]);
  const dismissNudge = (id: number) => { setNudges((n) => n.filter((x) => x.id !== id)); readNudge(id).catch(() => {}); };
  const [plan, setPlan] = useState<any>(null);
  const [loaded, setLoaded] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [ready, setReady] = useState<any>(null);
  const [coachName, setCoachName] = useState('your coach');
  const [name, setName] = useState('');
  const [team, setTeam] = useState<Decision[]>([]);
  const [typed, setTyped] = useState('');
  const [result, setResult] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [logOpen, setLogOpen] = useState(false);
  const [done, setDone] = useState<Record<string, number>>({});
  const [rest, setRest] = useState(0);
  const [swapping, setSwapping] = useState<string | null>(null);
  const [logging, setLogging] = useState<LogTarget | null>(null);
  const [live, setLive] = useState(false);
  const [fitting, setFitting] = useState(false);
  const [tstate, setTstate] = useState<TodayState | null>(null);
  const [loadErr, setLoadErr] = useState(false);
  const [teamErr, setTeamErr] = useState(false);
  const [restOver, setRestOver] = useState(false);
  const [burst, setBurst] = useState(0);
  const wasDone = useRef<boolean | null>(null);
  const restTotal = useRef(90);
  const tick = useRef<ReturnType<typeof setInterval> | null>(null);
  // Local calendar date (the backend's "today" is local too). The UTC date would be yesterday for an early-morning or late-evening session and split it in two.
  const session = useRef(isoOf(new Date())).current;

  const startRest = (sec = 90) => {
    if (tick.current) clearInterval(tick.current);
    restTotal.current = sec; setRestOver(false); setRest(sec);
    tick.current = setInterval(() => setRest((l) => {
      if (l <= 1) { if (tick.current) clearInterval(tick.current); Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success); setRestOver(true); setTimeout(() => setRestOver(false), 7000); return 0; }
      return l - 1;
    }), 1000);
  };

  const reloadState = useCallback(() => { getTodayState().then(setTstate).catch(() => {}); }, []);

  const submit = useCallback(async (text: string) => {
    if (!text.trim()) return;
    setBusy(true);
    let r: any;
    try { r = await logVoice(session, text); }
    catch (e: any) { setBusy(false); setResult({ error: String(e?.message || 'The server did not accept that. Try rephrasing it.'), saved: [] }); return; }
    setResult(r); setTyped(''); setBusy(false);
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium);
    setDone((d) => { const n = { ...d }; r.saved?.forEach((x: any) => { n[x.exercise.toLowerCase()] = (n[x.exercise.toLowerCase()] ?? 0) + 1; }); return n; });
    reloadState();
    if (r.next_set) { startRest(90); Speech.speak(`Next set, ${r.next_set.weight} kilos for ${r.next_set.reps}.`); }
  }, [session]);
  const onSetLogged = useCallback((r: any) => {
    setResult(r);
    setDone((d) => { const n = { ...d }; r.saved?.forEach((x: any) => { n[x.exercise.toLowerCase()] = (n[x.exercise.toLowerCase()] ?? 0) + 1; }); return n; });
    reloadState();
    if (r.next_set) startRest(90);
  }, []);
  const voice = useVoice(submit);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    await Promise.all([
      getPlan().then((p) => { setPlan(p); setLoadErr(false); }).catch(() => setLoadErr(true)),
      getTodayState().then(setTstate).catch(() => {}),
      getReadiness().then(setReady).catch(() => {}),
      getCoach().then((c) => setCoachName(c.coach_name)).catch(() => {}),
      getProfile().then((p) => setName(p?.profile?.name ?? '')).catch(() => {}),
      getDecisions(8).then((d) => { setTeam(d); setTeamErr(false); }).catch(() => setTeamErr(true)),
      getLiveState().then((st) => { if (st) setDone(Object.fromEntries(st.exercises.map((e: any) => [e.name, e.done]))); }).catch(() => {}),
    ]);
    flushQueue();
    setLoaded(true); setRefreshing(false);
  }, []);
  useEffect(() => { refresh(); }, [refresh, epoch, planEpoch]);
  // Tell the voice layer when the full-screen live session is open, so "log this set" etc. are understood in that context.
  useEffect(() => { setVoiceOverlay(live ? 'live' : null); return () => setVoiceOverlay(null); }, [live]);

  // Voice control ("Hey coach, start my workout / rest 90 seconds / skip rest"): handled here, also when queued before we mounted.
  const refreshRef = useRef(refresh); refreshRef.current = refresh;
  const startRestRef = useRef(startRest); startRestRef.current = startRest;
  useEffect(() => {
    const mineOf = (a: VoiceAction) => (a.type === 'open' && a.screen === 'live') || a.type === 'end' || a.type === 'rest' || a.type === 'rest_add' || a.type === 'rest_skip'
      || (a.type === 'refresh' && (!a.what || a.what.some((w) => w === 'today' || w === 'plan' || w === 'all')));
    const apply = (a: VoiceAction): boolean => {
      switch (a.type) {
        case 'open': if (a.screen !== 'live') return false; setLive(true); return true;
        case 'end': setLive(false); refreshRef.current(); return true;
        case 'rest': startRestRef.current(a.seconds); return true;
        case 'rest_add': restTotal.current += a.seconds; setRest((r) => r + a.seconds); return true;
        case 'rest_skip': if (tick.current) clearInterval(tick.current); setRest(0); return true;
        case 'refresh': if (!mineOf(a)) return false; refreshRef.current(); return true;
        default: return false;
      }
    };
    takePendingVoiceActions(mineOf).forEach(apply);
    return onVoiceAction(apply);
  }, []);

  const ov = plan?.today_override;
  const day = ov?.day ?? plan?.days?.[plan?.today_index ?? 0];
  const fit = async (m: number) => {
    setFitting(true); Haptics.selectionAsync();
    try { setPlan(await fitSession(m)); } catch {}
    setFitting(false);
  };
  const restore = async () => { Haptics.selectionAsync(); try { setPlan(await restoreSession()); } catch {} };
  const score = ready?.readiness?.score;
  const rw = readinessWord(score);
  const totalSets = day?.exercises.reduce((a: number, e: any) => a + e.sets, 0) ?? 0;
  const doneSets = day ? day.exercises.reduce((a: number, e: any) => a + Math.min(e.sets, done[e.name] ?? 0), 0) : 0;
  const ringColor = score == null ? [colors.faint, colors.faint] : score < 50 ? [colors.amber, colors.red] : [colors.lime, colors.mint];
  const allDone = totalSets > 0 && doneSets >= totalSets;
  // Celebrate the moment a session finishes (not every time you open the app after it).
  useEffect(() => {
    if (!loaded) return;
    if (wasDone.current === false && allDone) { setBurst((b) => b + 1); Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success); }
    wasDone.current = allDone;
  }, [allDone, loaded]);
  const weekDone = !!tstate?.week_done && !allDone && doneSets === 0;
  const nextUp = tstate?.next;
  const nextEx = day?.exercises.find((e: any) => Math.min(e.sets, done[e.name] ?? 0) < e.sets);

  return (
    <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
      <ScrollView contentContainerStyle={{ padding: 20, gap: 16, paddingBottom: 30 }} keyboardShouldPersistTaps="handled"
        refreshControl={<RefreshControl refreshing={refreshing && loaded} onRefresh={refresh} tintColor={colors.lime} />}>

        <View style={{ paddingTop: 4 }}>
          <Txt style={{ color: colors.dim, fontFamily: font.semibold, fontSize: 12, letterSpacing: 1.1 }}>{dateLabel().toUpperCase()}</Txt>
          <Txt style={{ fontFamily: font.black, fontSize: 30, letterSpacing: -0.7 }}>{greeting()}{name ? `, ${name}` : ''}</Txt>
        </View>

        {nudges.slice(0, 1).map((n) => (
          <Pressable key={n.id} onPress={() => dismissNudge(n.id)} accessibilityRole="button" accessibilityLabel={`Message from your team: ${n.text}. Tap to dismiss`}
            style={{ flexDirection: 'row', alignItems: 'center', gap: 12, padding: 14, borderRadius: 20, backgroundColor: colors.cyan + '14', borderWidth: 1, borderColor: colors.cyan + '55' }}>
            <Ionicons name="chatbubble-ellipses" size={20} color={colors.cyan} />
            <Txt style={{ flex: 1, fontSize: 14.5, lineHeight: 21 }}>{n.text}</Txt>
            <Ionicons name="close" size={18} color={colors.dim} />
          </Pressable>
        ))}

        {!loaded && (<><Skeleton height={250} radius={32} /><Skeleton height={120} /><Skeleton height={76} /><Skeleton height={76} /></>)}

        {loaded && !plan && (loadErr
          ? <ErrorCard message="Couldn't load today's session" onRetry={refresh} />
          : <EmptyState icon="barbell" tint={colors.lime} title="No training plan yet" body="Tell your coach about your goal, your schedule and any injuries, and they'll build your program in about a minute." action={onOpenProfile ? 'Build my plan' : undefined} onAction={onOpenProfile} />)}

        {loaded && plan && allDone && (
          <FadeIn>
            <View>
              <LinearGradient colors={['#1F2E1A', '#111A16', '#0C0F16']} start={{ x: 0, y: 0 }} end={{ x: 1, y: 1 }}
                style={{ borderRadius: 32, padding: 22, gap: 18, borderWidth: 1, borderColor: 'rgba(198,255,61,0.55)', shadowColor: colors.lime, shadowOpacity: 0.35, shadowRadius: 34, shadowOffset: { width: 0, height: 10 }, elevation: 8 }}>
                <View style={{ flexDirection: 'row', alignItems: 'center', gap: 16 }}>
                  <LinearGradient colors={gradients.lime} style={{ width: 64, height: 64, borderRadius: 32, alignItems: 'center', justifyContent: 'center' }}>
                    <Ionicons name="checkmark" size={38} color="#07080B" />
                  </LinearGradient>
                  <View style={{ flex: 1, gap: 2 }}>
                    <Txt style={{ color: colors.lime, fontFamily: font.bold, fontSize: 11, letterSpacing: 1.2 }}>SESSION COMPLETE</Txt>
                    <Txt style={{ fontFamily: font.black, fontSize: 28, letterSpacing: -0.7, lineHeight: 32 }}>Nice work{name ? `, ${name}` : ''}</Txt>
                    <Txt style={{ color: colors.dim, fontSize: 14 }}>{day?.name} is done. {coachName} is proud of you.</Txt>
                  </View>
                </View>
                <View style={{ flexDirection: 'row', gap: 10 }}>
                  {[
                    { v: String(totalSets), l: 'Sets' },
                    { v: tstate?.tonnage_today ? `${(tstate.tonnage_today / 1000).toFixed(1)}t` : String(day?.exercises.length ?? 0), l: tstate?.tonnage_today ? 'Lifted' : 'Lifts' },
                    { v: tstate?.week ? `${tstate.week.sessions}/${tstate.week.target}` : '-', l: 'This week' },
                  ].map((x) => (
                    <View key={x.l} style={{ flex: 1, backgroundColor: 'rgba(255,255,255,0.06)', borderRadius: 16, paddingVertical: 12, alignItems: 'center', gap: 2 }}>
                      <Txt style={{ fontFamily: font.black, fontSize: 22, letterSpacing: -0.4 }}>{x.v}</Txt>
                      <Txt style={{ color: colors.dim, fontFamily: font.semibold, fontSize: 11, letterSpacing: 0.8 }}>{x.l.toUpperCase()}</Txt>
                    </View>
                  ))}
                </View>
                <Pressable onPress={() => { Haptics.selectionAsync(); setLive(true); }} accessibilityRole="button" accessibilityLabel="Reopen today's session" style={{ minHeight: 44, justifyContent: 'center' }}>
                  <Txt style={{ color: colors.dim, fontFamily: font.semibold, fontSize: 13.5, textAlign: 'center' }}>Add an extra set or look back at the session</Txt>
                </Pressable>
              </LinearGradient>
              <Confetti burst={burst} />
            </View>
          </FadeIn>
        )}

        {loaded && plan && allDone && nextUp && (
          <FadeIn delay={80}>
            <Card style={{ gap: 10 }}>
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
                <Ionicons name="moon" size={16} color={colors.violet} />
                <Txt style={{ color: colors.violet, fontFamily: font.bold, fontSize: 11, letterSpacing: 1.2 }}>REST UP, THEN</Txt>
              </View>
              <Txt style={{ fontFamily: font.black, fontSize: 24, letterSpacing: -0.5 }}>Next: {nextUp.name}</Txt>
              <Txt style={{ color: colors.dim, fontFamily: font.medium, fontSize: 14 }}>{nextUp.lifts} lifts · {nextUp.sets} sets{nextUp.minutes ? ` · ~${nextUp.minutes} min` : ''}</Txt>
              <Txt style={{ color: colors.dim, fontSize: 13.5, lineHeight: 20, textTransform: 'capitalize' }} numberOfLines={2}>{nextUp.exercises.join(' · ')}</Txt>
              <Txt style={{ color: colors.dim, fontSize: 13, lineHeight: 19 }}>It will be here when you're ready. Eat well and sleep well tonight, that is where the progress happens.</Txt>
            </Card>
          </FadeIn>
        )}

        {loaded && plan && !allDone && (
          <FadeIn>
            <LinearGradient colors={['#1D2638', '#10141E', '#0C0F16']} start={{ x: 0, y: 0 }} end={{ x: 1, y: 1 }}
              style={{ borderRadius: 32, padding: 22, gap: 20, borderWidth: 1, borderColor: 'rgba(198,255,61,0.30)', borderTopColor: 'rgba(198,255,61,0.5)',
                shadowColor: colors.lime, shadowOpacity: 0.22, shadowRadius: 30, shadowOffset: { width: 0, height: 12 }, elevation: 8 }}>
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 16 }}>
                <View style={{ flex: 1, gap: 8 }}>
                  <View style={{ alignSelf: 'flex-start', backgroundColor: 'rgba(198,255,61,0.14)', borderRadius: 999, paddingHorizontal: 11, paddingVertical: 5 }}>
                    <Txt style={{ color: colors.lime, fontFamily: font.bold, fontSize: 11, letterSpacing: 1.2 }}>
                      {weekDone ? 'WEEK TARGET HIT' : ov?.minutes ? `FITTED TO ${ov.minutes} MIN` : ov ? 'ADJUSTED TODAY' : doneSets > 0 ? 'IN PROGRESS' : 'TODAY'}
                    </Txt>
                  </View>
                  <Txt style={{ fontFamily: font.black, fontSize: narrow ? 28 : 36, letterSpacing: -1, lineHeight: narrow ? 32 : 40, flexShrink: 1 }} numberOfLines={2}>{day?.name ?? 'Create your plan'}</Txt>
                  <Txt style={{ color: colors.dim, fontFamily: font.medium, fontSize: 14 }}>
                    {day ? `${day.exercises.length} lifts · ${totalSets} sets · ~${day.est_minutes ?? '?'} min` : 'Head to Profile to build your program'}
                  </Txt>
                  {!!nextEx && !weekDone && (
                    <Txt style={{ color: colors.text, fontFamily: font.semibold, fontSize: 14, lineHeight: 20, textTransform: 'capitalize' }} numberOfLines={2}>
                      {doneSets > 0 ? 'Up next' : 'First up'}: {nextEx.name}
                    </Txt>
                  )}
                  {weekDone && <Txt style={{ color: colors.dim, fontSize: 13, lineHeight: 19 }}>You've trained {tstate?.week?.sessions} of {tstate?.week?.target} days this week. Resting is part of the plan. Train anyway only if you feel good.</Txt>}
                </View>
                <View style={{ alignItems: 'center', gap: 6 }}>
                  <Ring size={92} stroke={9} progress={(score ?? 0) / 100} from={ringColor[0]} to={ringColor[1]}>
                    <CountUp value={score ?? 0} style={{ fontFamily: font.black, fontSize: 26, letterSpacing: -0.5 }} />
                  </Ring>
                  <Txt style={{ color: rw.color, fontFamily: font.bold, fontSize: 11.5 }}>{rw.text}</Txt>
                </View>
              </View>

              {day && (
                <View style={{ gap: 8 }} accessible accessibilityRole="progressbar" accessibilityLabel={`${doneSets} of ${totalSets} sets done`}>
                  <View style={{ flexDirection: 'row', gap: 5 }}>
                    {Array.from({ length: Math.min(totalSets, 24) }).map((_, k) => (
                      <View key={k} style={{ flex: 1, height: 6, borderRadius: 3, backgroundColor: k < doneSets ? colors.lime : 'rgba(255,255,255,0.10)' }} />
                    ))}
                  </View>
                  <Txt style={{ color: colors.dim, fontSize: 12.5, fontFamily: font.medium }}>{doneSets} of {totalSets} sets done{doneSets === 0 ? '. Tap below to start.' : ` · ${Math.round((doneSets / Math.max(1, totalSets)) * 100)}%`}</Txt>
                </View>
              )}

              {day && (
                <Pressable onPress={() => { Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium); setLive(true); }} accessibilityRole="button" accessibilityLabel={`${doneSets > 0 ? 'Continue' : 'Start'} your session with ${coachName}`}
                  style={({ pressed }) => ({ transform: [{ scale: pressed ? 0.97 : 1 }] })}>
                  <LinearGradient colors={gradients.lime} start={{ x: 0, y: 0 }} end={{ x: 1, y: 1 }}
                    style={{ borderRadius: 22, paddingVertical: 16, paddingHorizontal: 18, flexDirection: 'row', alignItems: 'center', gap: 13 }}>
                    <View style={{ width: 38, height: 38, borderRadius: 19, backgroundColor: '#07080B', alignItems: 'center', justifyContent: 'center' }}>
                      <Ionicons name="mic" size={19} color={colors.lime} />
                    </View>
                    <View style={{ flex: 1 }}>
                      <Txt numberOfLines={2} style={{ color: '#07080B', fontFamily: font.black, fontSize: 17, letterSpacing: -0.2 }}>{doneSets > 0 ? 'Continue with' : weekDone ? 'Train anyway with' : 'Train with'} {coachName}</Txt>
                      <Txt style={{ color: 'rgba(7,8,11,0.75)', fontFamily: font.semibold, fontSize: 12 }}>Voice-guided, hands-free</Txt>
                    </View>
                    <Ionicons name="arrow-forward" size={22} color="#07080B" />
                  </LinearGradient>
                </Pressable>
              )}
            </LinearGradient>
          </FadeIn>
        )}

        {/* Rest timer (manual logging) */}
        {restOver && rest === 0 && (
          <Card glow={colors.lime} style={{ flexDirection: 'row', alignItems: 'center', gap: 14 }}>
            <Ionicons name="flash" size={26} color={colors.lime} />
            <Txt style={{ flex: 1, fontFamily: font.bold, fontSize: 17 }}>Rest is over. Next set when you're ready.</Txt>
            <IconButton icon="close" label="Dismiss" onPress={() => setRestOver(false)} />
          </Card>
        )}
        {rest > 0 && (
          <Card glow={colors.cyan} style={{ gap: 12 }}>
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 18 }}>
              <Ring size={76} stroke={8} progress={rest / restTotal.current} from={colors.cyan} to={colors.violet}>
                <Txt style={{ fontFamily: font.black, fontSize: rest >= 60 ? 17 : 20 }}>{mmss(rest)}</Txt>
              </Ring>
              <View style={{ flex: 1 }} accessible accessibilityLabel={`Rest, ${rest} seconds left`}>
                <Txt style={{ fontFamily: font.bold, fontSize: 17 }}>Rest</Txt>
                <Txt style={{ color: colors.dim, fontSize: 13, lineHeight: 18 }}>{rest} seconds left. Breathe. Next set when the ring fills.</Txt>
              </View>
            </View>
            <View style={{ flexDirection: 'row', gap: 10 }}>
              {[{ l: '+30 s', f: () => { restTotal.current += 30; setRest((r) => r + 30); } }, { l: 'Skip rest', f: () => { if (tick.current) clearInterval(tick.current); setRest(0); } }].map((b) => (
                <Pressable key={b.l} onPress={() => { Haptics.selectionAsync(); b.f(); }} accessibilityRole="button" accessibilityLabel={b.l === '+30 s' ? 'Add 30 seconds of rest' : 'Skip rest'}
                  style={({ pressed }) => ({ flex: 1, minHeight: 52, borderRadius: 16, alignItems: 'center', justifyContent: 'center', backgroundColor: 'rgba(255,255,255,0.08)', borderWidth: 1, borderColor: 'rgba(255,255,255,0.10)', opacity: pressed ? 0.7 : 1 })}>
                  <Txt style={{ fontFamily: font.bold, fontSize: 14 }}>{b.l}</Txt>
                </Pressable>
              ))}
            </View>
          </Card>
        )}

        {loaded && plan && <FadeIn delay={60}><TeamStrip items={team} onOpen={onOpenTeam} unreachable={teamErr || !online} /></FadeIn>}

        {/* Short on time */}
        {loaded && day && !allDone && (
          <FadeIn delay={80}>
            <View style={{ gap: 8 }}>
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
                <Ionicons name="timer-outline" size={16} color={colors.dim} />
                <Txt style={{ color: colors.dim, fontFamily: font.semibold, fontSize: 13 }}>{fitting ? 'Re-fitting…' : 'Short on time?'}</Txt>
              </View>
              <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ gap: 8 }}>
                {[20, 30, 45, 60].map((m) => <Chip key={m} label={`${m} min`} active={ov?.minutes === m} onPress={() => fit(m)} color={colors.cyan} />)}
                {ov && <Chip label="Restore full" onPress={restore} color={colors.amber} />}
              </ScrollView>
              {ov?.minutes && <Txt style={{ color: colors.dim, fontSize: 12 }}>Main lifts kept, accessories trimmed. Your plan is unchanged.</Txt>}
            </View>
          </FadeIn>
        )}

        {/* Exercises */}
        {loaded && day && <SectionTitle>{allDone ? "Today's lifts" : 'Exercises'}</SectionTitle>}
        {day?.exercises.map((e: any, i: number) => {
          const n = Math.min(e.sets, done[e.name] ?? 0);
          const complete = n >= e.sets;
          const isNext = !complete && nextEx?.name === e.name && !allDone;
          return (
            <FadeIn key={e.name} delay={140 + i * 70}>
              <Pressable onPress={() => { Haptics.selectionAsync(); setLogging({ name: e.name, sets: e.sets, reps: e.reps, target_rpe: e.target_rpe }); }}
                accessibilityRole="button" accessibilityLabel={`${e.name}, ${n} of ${e.sets} sets done, ${e.reps} reps. Tap to log a set`}
                style={({ pressed }) => ({ transform: [{ scale: pressed ? 0.98 : 1 }], opacity: pressed ? 0.92 : 1 })}>
              <Card glow={isNext ? colors.lime : undefined} style={{ flexDirection: 'row', alignItems: 'center', gap: 12, padding: 16, opacity: complete ? 0.72 : 1 }}>
                <LinearGradient colors={complete ? gradients.lime : ['#232838', '#1A1E28']} style={{ width: 44, height: 44, borderRadius: 14, alignItems: 'center', justifyContent: 'center' }}>
                  {complete ? <Ionicons name="checkmark" size={24} color="#07080B" /> : <Txt style={{ fontFamily: font.bold, color: colors.dim }}>{i + 1}</Txt>}
                </LinearGradient>
                <View style={{ flex: 1 }}>
                  {isNext && <Txt style={{ color: colors.lime, fontFamily: font.bold, fontSize: 10.5, letterSpacing: 1.1 }}>UP NEXT</Txt>}
                  <Txt style={{ fontFamily: font.semibold, fontSize: 16, textTransform: 'capitalize' }} numberOfLines={2}>{e.name}</Txt>
                  <Txt style={{ color: colors.dim, fontSize: 13 }}>{e.sets} × {e.reps} · RPE {e.target_rpe}</Txt>
                </View>
                <View style={{ flexDirection: 'row', gap: 5, maxWidth: 90, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                  {Array.from({ length: e.sets }).map((_, k) => (
                    <View key={k} style={{ width: 9, height: 9, borderRadius: 5, backgroundColor: k < n ? colors.lime : 'rgba(255,255,255,0.12)' }} />
                  ))}
                </View>
                <Pressable onPress={() => setSwapping(e.name)} accessibilityRole="button" accessibilityLabel={`Swap ${e.name}`} hitSlop={4} style={({ pressed }) => ({ width: 44, height: 44, borderRadius: 22, alignItems: 'center', justifyContent: 'center', backgroundColor: pressed ? 'rgba(255,255,255,0.08)' : 'transparent' })}>
                  <Ionicons name="swap-horizontal" size={20} color={colors.dim} />
                </Pressable>
                <View style={{ width: 34, height: 34, borderRadius: 17, alignItems: 'center', justifyContent: 'center', backgroundColor: complete ? 'transparent' : 'rgba(198,255,61,0.14)', borderWidth: complete ? 0 : 1, borderColor: 'rgba(198,255,61,0.45)' }}>
                  <Ionicons name={complete ? 'checkmark-circle' : 'add'} size={complete ? 24 : 20} color={colors.lime} />
                </View>
              </Card>
              </Pressable>
            </FadeIn>
          );
        })}

        {/* Last log result */}
        {result && (
          <Card style={{ borderColor: result.safety_flag ? colors.red : 'rgba(198,255,61,0.35)' }}>
            {!!result.error && <Txt style={{ color: colors.amber, fontSize: 13, lineHeight: 18 }}>{result.error}</Txt>}
            {result.queued && <Txt style={{ color: colors.amber, fontSize: 13, lineHeight: 18 }}>You're offline. Saved on your phone, and it will be sent when you reconnect.</Txt>}
            {!!result.saved?.length && <Txt style={{ color: colors.lime, fontFamily: font.bold, fontSize: 11, letterSpacing: 1.2 }}>LOGGED</Txt>}
            {result.saved?.map((x: any, i: number) => (
              <Txt key={i} style={{ fontFamily: font.semibold, fontSize: 16, textTransform: 'capitalize' }}>
                {x.exercise}  {x.weight}kg × {x.reps}{x.rpe ? `  @${x.rpe}` : ''}
              </Txt>
            ))}
            {result.next_set && (
              <View style={{ flexDirection: 'row', gap: 10, alignItems: 'flex-start', marginTop: 4 }}>
                <Ionicons name="flash" size={18} color={colors.lime} style={{ marginTop: 2 }} />
                <Txt style={{ flex: 1, color: colors.lime, fontFamily: font.semibold, lineHeight: 21 }}>
                  Next: {result.next_set.weight}kg × {result.next_set.reps}. <Txt style={{ color: colors.dim, fontFamily: font.regular }}>{result.next_set.reason}</Txt>
                </Txt>
              </View>
            )}
            {result.safety_flag && <Txt style={{ color: colors.red, fontFamily: font.semibold }}>Pain noted — skip that movement. Your coach has been told.</Txt>}
          </Card>
        )}

        {/* Manual logging, tucked away: the live coach is the main way to log */}
        {loaded && day && (
          <View style={{ gap: 12 }}>
            <Pressable onPress={() => { Haptics.selectionAsync(); setLogOpen((o) => !o); }} accessibilityRole="button" accessibilityLabel="Type or say a set instead" accessibilityState={{ expanded: logOpen }}
              style={{ minHeight: 44, flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingVertical: 6 }}>
              <Txt style={{ color: colors.dim, fontFamily: font.semibold, fontSize: 14 }}>Type or say a set instead</Txt>
              <Ionicons name={logOpen ? 'chevron-up' : 'chevron-down'} size={18} color={colors.dim} />
            </Pressable>
            {logOpen && (
              <FadeIn distance={6}>
                <View style={{ gap: 12 }}>
                  <View style={{ alignItems: 'center', gap: 8 }}>
                    {busy ? <ActivityIndicator color={colors.lime} style={{ height: 96 }} /> : (
                      <MicButton listening={voice.listening} onPress={() => (voice.listening ? voice.stop() : voice.start())} />
                    )}
                    <Txt style={{ color: colors.dim, fontSize: 13 }}>
                      {voice.listening ? 'Listening…' : 'Tap and say "bench 80 for 8, felt easy"'}
                    </Txt>
                    {!!voice.error && <Txt accessibilityRole="alert" style={{ color: colors.amber, fontSize: 12.5, textAlign: 'center' }}>{voice.error}</Txt>}
                  </View>
                  <View style={{ flexDirection: 'row', gap: 10 }}>
                    <TextInput value={typed} onChangeText={setTyped} placeholder="bench press 80 for 8 rpe 8" placeholderTextColor={colors.faint}
                      onSubmitEditing={() => submit(typed)} returnKeyType="send" accessibilityLabel="Type a set, for example bench press 80 for 8"
                      style={{ flex: 1, minHeight: 54, backgroundColor: colors.card, color: colors.text, borderRadius: 18, paddingHorizontal: 18, paddingVertical: 15, fontFamily: font.medium, fontSize: 15, borderWidth: 1, borderColor: colors.border }} />
                    <Pressable onPress={() => submit(typed)} accessibilityRole="button" accessibilityLabel="Log this set" disabled={busy || !typed.trim()} style={({ pressed }) => ({ opacity: !typed.trim() ? 0.5 : 1, transform: [{ scale: pressed ? 0.95 : 1 }] })}>
                      <LinearGradient colors={gradients.lime} style={{ width: 54, height: 54, borderRadius: 18, alignItems: 'center', justifyContent: 'center' }}>
                        <Ionicons name="arrow-up" size={24} color="#07080B" />
                      </LinearGradient>
                    </Pressable>
                  </View>
                  <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8 }}>
                    {['felt easy', 'rpe 9', 'sharp pain in shoulder'].map((c) => <Chip key={c} label={c} onPress={() => setTyped((t) => `${t} ${c}`.trim())} />)}
                  </View>
                </View>
              </FadeIn>
            )}
          </View>
        )}
      </ScrollView>

      <Modal visible={live} animationType="slide" onRequestClose={() => { setLive(false); refresh(); }}>
        <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={['top', 'bottom']}>
          {live && <Live onClose={() => { setLive(false); refresh(); }} />}
        </SafeAreaView>
      </Modal>
      <SetLogger target={logging} done={logging ? Math.min(logging.sets, done[logging.name] ?? 0) : 0} session={session} onClose={() => setLogging(null)} onLogged={onSetLogged}
        onUndone={(ex, remaining) => setDone((d) => ({ ...d, [ex.toLowerCase()]: remaining }))} />
      <SwapSheet exercise={swapping} dayIndex={plan?.today_index ?? 0} onClose={() => setSwapping(null)} onSwapped={setPlan} />
    </KeyboardAvoidingView>
  );
}
