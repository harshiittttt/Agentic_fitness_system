import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Platform, Pressable, RefreshControl, ScrollView, Share, Switch, TextInput, View } from 'react-native';
import { LinearGradient } from 'expo-linear-gradient';
import { Ionicons } from '@expo/vector-icons';
import * as Haptics from '../haptics';
import { API, CheckinSummary, getCheckinSummary, getMemoryOverview, getProfile, getReadiness, getSeries, getTtsStatus, getTtsTest, postWeighIn, deleteAllData, exportMyData, getAccessLog, resetDemo, seedDemo, sendCheckin, updateProfile } from '../api';
import { isoOf, weekStreak, weeklyBuckets } from '../dates';
import { Form, INJURIES, TRAINING_KEYS, customInjuries, diff, hasInj, toForm, toggleInj } from '../profileForm';
import { Choice, Group, Label, Row, Sheet, Stepper } from '../forms';
import PersonaPicker from '../PersonaPicker';
import { PersonaId, personaById } from '../personas';
import { hasVoiceFor, speak } from '../tts';
import { VoiceMode, getVoiceMode, loadVoiceSettings, setRecognitionLang, setVoiceMode } from '../voiceSettings';
import { colors, font } from '../theme';
import { Bar, Card, FadeIn, GradientButton, SectionTitle, Segmented, Skeleton, Txt, useOnline } from '../ui';
import { Nudge, Pill } from '../ux/controls';
import Sparkline from '../ux/Sparkline';
import ConfirmSheet, { Confirm } from '../ux/ConfirmSheet';
import KnowsYou from './KnowsYou';
import Agenda from './Agenda';
import Trace from './Trace';
import Intake from './Intake';
import Research from './Research';
import PushSettings from './PushSettings';
import ServerForm from './ServerForm';
import CoachShell from './CoachShell';

const GOALS = [
  { k: 'bulk', label: 'Build muscle', sub: 'Eat a little above maintenance, lift progressively', icon: 'trending-up', color: colors.lime },
  { k: 'cut', label: 'Lose fat', sub: 'A moderate deficit with high protein to keep muscle', icon: 'flame', color: colors.pink },
  { k: 'maintain', label: 'Stay fit', sub: 'Maintain weight, get stronger and healthier', icon: 'heart', color: colors.cyan },
] as const;
const EXPERIENCE = ['beginner', 'intermediate', 'advanced'] as const;
const ACTIVITY = [{ k: 'sedentary', label: 'Mostly seated' }, { k: 'light', label: 'Light' }, { k: 'moderate', label: 'Moderate' }, { k: 'high', label: 'Very active' }];
const VOICE_LABEL: Record<VoiceMode, string> = { all: 'ElevenLabs', moments: 'Key moments', phone: 'Phone' };
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
function readinessWord(score?: number) {
  if (score == null) return { text: 'No check-in yet', color: colors.faint };
  if (score >= 75) return { text: 'Good to push', color: colors.mint };
  if (score >= 50) return { text: 'Take it steady', color: colors.amber };
  return { text: 'Go easy today', color: colors.red };
}
const r1 = (n: number) => +n.toFixed(1);
function daysAgo(day: string) {
  const d = Math.round((Date.parse(isoOf(new Date())) - Date.parse(day)) / 864e5);
  return d <= 0 ? 'today' : d === 1 ? 'yesterday' : `${d} days ago`;
}

/** 1 to 5 tap scale with 48px buttons and a plain-words legend, so the numbers mean something. */
function Scale5({ label, low, high, value, onChange, icon, color }: { label: string; low: string; high: string; value: number; onChange: (n: number) => void; icon: string; color: string }) {
  return (
    <View style={{ gap: 8 }}>
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
        <Ionicons name={icon as any} size={17} color={color} />
        <Txt style={{ fontFamily: font.semibold, fontSize: 15 }}>{label}</Txt>
      </View>
      <View style={{ flexDirection: 'row', gap: 8 }}>
        {[1, 2, 3, 4, 5].map((n) => (
          <Pressable key={n} accessibilityRole="button" accessibilityLabel={`${label} ${n} of 5`} accessibilityState={{ selected: n === value }} onPress={() => { Haptics.selectionAsync(); onChange(n); }}
            style={{ flex: 1, height: 48, borderRadius: 14, alignItems: 'center', justifyContent: 'center', backgroundColor: n <= value ? color : colors.bg2, borderWidth: 1, borderColor: n <= value ? color : colors.border }}>
            <Txt style={{ fontFamily: font.bold, fontSize: 16, color: n <= value ? '#07080B' : colors.dim }}>{n}</Txt>
          </Pressable>
        ))}
      </View>
      <View style={{ flexDirection: 'row', justifyContent: 'space-between' }}>
        <Txt style={{ color: colors.faint, fontSize: 12 }}>1 · {low}</Txt><Txt style={{ color: colors.faint, fontSize: 12 }}>5 · {high}</Txt>
      </View>
    </View>
  );
}

/** A big tappable shortcut with a status line, so the two things people hunt for (what the coach knows, the server) are one tap from the top. */
function Shortcut({ icon, color, title, status, statusColor, onPress }: { icon: string; color: string; title: string; status: string; statusColor: string; onPress: () => void }) {
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={`${title}. ${status}`} onPress={() => { Haptics.selectionAsync(); onPress(); }}
      style={({ pressed }) => ({ flex: 1, minHeight: 88, borderRadius: 22, padding: 14, gap: 8, backgroundColor: colors.card, borderWidth: 1, borderColor: color + '44', opacity: pressed ? 0.75 : 1, transform: [{ scale: pressed ? 0.98 : 1 }] })}>
      <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
        <View style={{ width: 34, height: 34, borderRadius: 11, backgroundColor: color + '22', alignItems: 'center', justifyContent: 'center' }}><Ionicons name={icon as any} size={18} color={color} /></View>
        <Ionicons name="chevron-forward" size={16} color={colors.faint} />
      </View>
      <View style={{ gap: 2 }}>
        <Txt maxFontSizeMultiplier={1.3} style={{ fontFamily: font.bold, fontSize: 14.5, lineHeight: 19 }}>{title}</Txt>
        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
          <View style={{ width: 7, height: 7, borderRadius: 4, backgroundColor: statusColor }} />
          <Txt maxFontSizeMultiplier={1.3} numberOfLines={1} style={{ color: colors.dim, fontSize: 12.5, flexShrink: 1 }}>{status}</Txt>
        </View>
      </View>
    </Pressable>
  );
}

export default function Profile({ onReset }: { onReset?: () => void }) {
  const { online } = useOnline();
  const [data, setData] = useState<any>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [series, setSeries] = useState<any>(null);
  const [ready, setReady] = useState<any>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [sheet, setSheet] = useState<'edit' | 'coach' | 'voice' | null>(null);
  const [toast, setToast] = useState<{ t: string; ok: boolean } | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [knows, setKnows] = useState(false);                       // the "What I know about you" screen
  const [knowsPct, setKnowsPct] = useState<number | null>(null);
  const [serverOpen, setServerOpen] = useState(false);   // Setup > Server: address + access key
  const [coachTool, setCoachTool] = useState<'agenda' | 'intake' | 'research' | 'trace' | null>(null);   // calendar / interview / evidence screens

  // check-in + weigh-in
  const [sleep, setSleep] = useState(7.5); const [sore, setSore] = useState(2); const [energy, setEnergy] = useState(4);
  const [checked, setChecked] = useState(false); const [savingCheck, setSavingCheck] = useState(false);
  const [sum, setSum] = useState<CheckinSummary | null>(null);
  const [kg, setKg] = useState<number | null>(null);
  const [kgStep, setKgStep] = useState(0.1);
  const [savingKg, setSavingKg] = useState(false);

  // edit sheet
  const [form, setForm] = useState<Form | null>(null);
  const [base, setBase] = useState<Form | null>(null);
  const [saving, setSaving] = useState(false);

  // coach + voice + feel
  const [persona, setPersona] = useState<PersonaId>('hype');
  const [myName, setMyName] = useState('');
  const [vibrate, setVibrate] = useState(Haptics.hapticsEnabled());
  const [vMode, setVMode] = useState<VoiceMode>(getVoiceMode());
  const [tts, setTts] = useState<any>(null);
  const [voiceMsg, setVoiceMsg] = useState('');
  const [hindiVoice, setHindiVoice] = useState(true);

  const show = (t: string, ok = true) => {
    setToast({ t, ok });
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 3400);
  };
  const offline = () => show("Couldn't reach the server. Check your connection and try again.", false);

  const applySummary = useCallback((s: CheckinSummary, first: boolean) => {
    setSum(s);
    if (first || kg == null) setKg(s.last_weight?.kg ?? null);
    if (first && s.today) {
      if (s.today.sleep_h) setSleep(s.today.sleep_h);
      if (s.today.soreness) setSore(s.today.soreness);
      if (s.today.energy) setEnergy(s.today.energy);
      setChecked(true);
    }
  }, [kg]);

  const loaded = useRef(false);
  const load = useCallback(async () => {
    setRefreshing(true); setLoadFailed(false);
    const first = !loaded.current;
    const [prof] = await Promise.all([
      getProfile().then((p) => { setData(p); if (p) { setPersona(p.profile.coach_persona ?? 'hype'); setMyName(p.profile.name ?? ''); setRecognitionLang(personaById(p.profile.coach_persona).tts.lang); } return p; }).catch(() => null),
      getSeries().then(setSeries).catch(() => {}),
      getReadiness().then(setReady).catch(() => {}),
      getTtsStatus().then(setTts).catch(() => setTts(null)),
      getCheckinSummary().then((s) => applySummary(s, first)).catch(() => {}),
    ]);
    if (!prof) setLoadFailed(true); else loaded.current = true;
    setRefreshing(false);
  }, [applySummary]);
  useEffect(() => { load(); loadVoiceSettings().then(setVMode); Haptics.loadHaptics().then(setVibrate); }, []);
  useEffect(() => () => { if (toastTimer.current) clearTimeout(toastTimer.current); }, []);

  useEffect(() => { if ((persona === 'haryanvi' || persona === 'roast')) hasVoiceFor('hi-IN').then(setHindiVoice); }, [persona]);

  const p = data?.profile;
  const pers = personaById(persona);
  const streak = series ? weekStreak(weeklyBuckets(series.activity ?? [], 12)) : 0;
  const rw = readinessWord(ready?.readiness?.score);
  const weights = sum?.weights ?? [];
  const lastW = sum?.last_weight ?? null;
  const prevW = weights.length > 1 ? weights[weights.length - 2] : null;
  const shownKg = kg ?? p?.weight_kg ?? 75;
  const kgDirty = lastW == null || r1(shownKg) !== r1(lastW.kg) || lastW.day !== isoOf(new Date());

  // ---------------- actions
  const saveCheckin = async () => {
    setSavingCheck(true);
    try {
      await sendCheckin({ sleep_h: sleep, soreness: sore, energy });
      Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
      setReady(await getReadiness()); setChecked(true); show('Check-in saved. Your coach will use it.');
    } catch { offline(); }
    setSavingCheck(false);
  };
  const saveWeight = async () => {
    setSavingKg(true);
    try {
      await postWeighIn(r1(shownKg));
      Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
      applySummary(await getCheckinSummary(), true); show(`Weight saved: ${r1(shownKg)} kg`);
    } catch { offline(); }
    setSavingKg(false);
  };

  const openEdit = () => { if (!p) return; const f = toForm(p); setForm(f); setBase(f); setSheet('edit'); };
  const patch = form && base ? diff(form, base) : {};
  const changedKeys = Object.keys(patch);
  const trainingChanged = changedKeys.some((k) => TRAINING_KEYS.includes(k));
  const save = async (rebuild: boolean) => {
    setSaving(true);
    try {
      const r = await updateProfile({ ...patch, rebuild });
      const bits = [r.nutrition_updated && 'nutrition targets updated', r.rebuilt && 'training plan rebuilt'].filter(Boolean) as string[];
      show(bits.length ? cap(bits.join(' · ')) : 'Saved');
      Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
      setSheet(null); await load();
    } catch { show('Could not save. Check your connection.', false); }
    setSaving(false);
  };

  const ask = (c: Confirm) => setConfirm(c);
  const run = (fn: () => Promise<any>, done: () => void) => async () => {
    setConfirmBusy(true);
    try { await fn(); setConfirm(null); done(); } catch { setConfirm(null); offline(); }
    setConfirmBusy(false);
  };
  const loadDemo = () => ask({ title: 'Load sample data?', icon: 'flask', action: 'Load sample data',
    message: 'Adds 8 weeks of made-up training, check-ins and weigh-ins so you can explore the charts and the coach review. You can clear it afterwards from this screen.',
    onYes: run(() => seedDemo(), () => { show('Sample data loaded'); load(); }) });
  const clearHistory = () => ask({ title: 'Clear your history?', danger: true, icon: 'trash', action: 'Clear history',
    message: 'This deletes every logged workout, check-in, weigh-in and meal, plus the change log. Your profile and training plan stay. It cannot be undone.',
    onYes: run(() => resetDemo(), () => { show('History cleared'); loaded.current = false; load(); }) });
  const startOver = () => ask({ title: 'Start over from scratch?', danger: true, icon: 'refresh-circle', action: 'Delete everything',
    message: 'This deletes your profile, plan, history and everything your coach remembers, then takes you back to setup. It cannot be undone.',
    onYes: run(() => deleteAllData(), () => onReset?.()) });
  const [exporting, setExporting] = useState(false);
  const [lastExport, setLastExport] = useState('');
  useEffect(() => { if (!knows) getMemoryOverview().then((o) => setKnowsPct(o.coverage_pct)).catch(() => setKnowsPct(null)); }, [knows]);
  useEffect(() => { getAccessLog().then((l) => { const e = l.find((x) => x.action === 'export'); if (e) setLastExport(e.ts.slice(0, 10)); }).catch(() => {}); }, []);
  /** Everything stored about you, as one JSON file. On a phone it opens the share sheet; in a browser it downloads. */
  const exportData = async () => {
    setExporting(true);
    try {
      const text = JSON.stringify(await exportMyData(), null, 2);
      const doc = (globalThis as any).document;
      if (Platform.OS === 'web' && doc) {
        const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
        const a = doc.createElement('a'); a.href = url; a.download = 'my-fitness-data.json'; a.click(); URL.revokeObjectURL(url);
        show('Your data was downloaded');
      } else {
        await Share.share({ message: text, title: 'My fitness data' });
      }
      setLastExport(new Date().toISOString().slice(0, 10));
    } catch { show(`Couldn't export from here. You can open ${API}/agents/export in a browser instead.`, false); }
    setExporting(false);
  };

  const testVoice = async () => {
    setVoiceMsg('Testing with ElevenLabs…');
    try {
      const r = await getTtsTest(persona);
      getTtsStatus().then(setTts).catch(() => {});
      if (!r.ok) {
        const why: Record<string, string> = {
          not_configured: 'Not set up yet. Add ELEVENLABS_API_KEY to the backend .env file, then fully restart the backend.',
          auth: 'ElevenLabs rejected the key. Check ELEVENLABS_API_KEY in the backend .env.',
          permission: `The key is missing a permission. ${r.detail ?? ''} Edit the key on elevenlabs.io and allow Text to Speech.`,
          quota: 'ElevenLabs says your credits or plan limit has been reached.',
          budget: 'The monthly credit cap you set has been reached, so your phone voice is used.',
          no_voice: `No voice on your plan could be used. ${r.detail ?? ''}`,
          cooldown: `Paused after a recent error. ${r.detail ?? ''} Restart the backend to try again now.`,
        };
        setVoiceMsg(why[r.reason] ?? 'ElevenLabs could not be reached, so your phone voice is used.');
        return;
      }
      setVoiceMsg(`Working. ${pers.name} sounds like "${r.voice}"${r.cached ? ' (replayed free from the saved copy)' : ` (${r.credits} credits)`}.`);
      speak(r.sample, pers.tts, undefined, { persona, premium: true });
    } catch { setVoiceMsg('Could not reach your backend.'); }
  };

  const goal = GOALS.find((g) => g.k === p?.goal) ?? GOALS[2];
  const eq: string[] = p?.equipment ?? [];
  const eqText = !eq.length || eq.length >= 5 ? 'Full gym' : eq.map(cap).join(', ');
  const delta = lastW && prevW ? r1(lastW.kg - prevW.kg) : null;

  return (
    <View style={{ flex: 1 }}>
      <ScrollView contentContainerStyle={{ padding: 20, gap: 18, paddingBottom: 30 }} keyboardShouldPersistTaps="handled"
        refreshControl={<RefreshControl refreshing={refreshing && !!data} onRefresh={load} tintColor={colors.lime} />}>

        {loadFailed && !data ? (
          <Card style={{ alignItems: 'center', gap: 12, paddingVertical: 30 }}>
            <Ionicons name="cloud-offline" size={42} color={colors.amber} />
            <Txt style={{ fontFamily: font.bold, fontSize: 19 }}>Can't load your profile</Txt>
            <Txt style={{ color: colors.dim, textAlign: 'center', lineHeight: 21 }}>Check your connection and try again. Nothing is lost.</Txt>
            <View style={{ alignSelf: 'stretch' }}><GradientButton label="Try again" icon="refresh" onPress={load} /></View>
          </Card>
        ) : !data ? (<><Skeleton height={190} radius={30} /><Skeleton height={330} /><Skeleton height={200} /></>) : (
          <>
            {/* identity */}
            <FadeIn>
              <LinearGradient colors={['#161A24', '#10131A']} style={{ borderRadius: 30, padding: 22, gap: 18, borderWidth: 1, borderColor: colors.border }}>
                <View style={{ flexDirection: 'row', alignItems: 'center', gap: 16 }}>
                  <LinearGradient colors={[pers.color, '#34E3A4']} start={{ x: 0, y: 0 }} end={{ x: 1, y: 1 }} style={{ width: 74, height: 74, borderRadius: 37, alignItems: 'center', justifyContent: 'center' }}>
                    {p.name ? <Txt style={{ fontFamily: font.black, fontSize: 32, color: '#07080B' }}>{p.name.charAt(0).toUpperCase()}</Txt> : <Ionicons name="person" size={34} color="#07080B" />}
                  </LinearGradient>
                  <View style={{ flex: 1, gap: 6 }}>
                    <Txt style={{ fontFamily: font.black, fontSize: 26, letterSpacing: -0.4 }} numberOfLines={1}>{p.name || 'Athlete'}</Txt>
                    <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 6 }}>
                      {[goal.label, `${data.plan.days} days/wk`, cap(p.experience ?? 'intermediate')].map((t) => (
                        <View key={t} style={{ backgroundColor: 'rgba(255,255,255,0.07)', borderRadius: 999, paddingHorizontal: 10, paddingVertical: 4 }}>
                          <Txt style={{ fontSize: 12, fontFamily: font.semibold, color: colors.dim }}>{t}</Txt>
                        </View>
                      ))}
                    </View>
                  </View>
                </View>
                <View style={{ flexDirection: 'row' }}>
                  {[{ l: 'Sessions', v: String(series?.sessions ?? 0) }, { l: 'Week streak', v: String(streak) }, { l: 'Coach', v: pers.name }].map((s, i) => (
                    <View key={s.l} style={{ flex: 1, alignItems: 'center', borderLeftWidth: i ? 1 : 0, borderLeftColor: colors.border }}>
                      <Txt style={{ fontFamily: font.black, fontSize: 22 }} numberOfLines={1}>{s.v}</Txt>
                      <Txt style={{ color: colors.dim, fontSize: 12 }}>{s.l}</Txt>
                    </View>
                  ))}
                </View>
              </LinearGradient>
            </FadeIn>

            {/* shortcuts: the two things people look for first */}
            <FadeIn delay={40}>
              <View style={{ flexDirection: 'row', gap: 12 }}>
                <Shortcut icon="sparkles" color={colors.violet} title="What I know about you" status={knowsPct == null ? 'Tap to review' : `${knowsPct}% of the basics`} statusColor={colors.violet} onPress={() => setKnows(true)} />
                <Shortcut icon="server" color={colors.cyan} title="Your coach server" status={online ? 'Connected' : 'Not reachable'} statusColor={online ? colors.mint : colors.amber} onPress={() => setServerOpen(true)} />
              </View>
            </FadeIn>

            {/* today: check-in + weigh-in */}
            <FadeIn delay={70} style={{ gap: 12 }}>
              <SectionTitle>Today</SectionTitle>
              <Card style={{ gap: 18 }}>
                <View style={{ flexDirection: 'row', alignItems: 'flex-start', gap: 12 }}>
                  <View style={{ flex: 1, gap: 2 }}>
                    <Txt style={{ fontFamily: font.bold, fontSize: 19 }}>How are you today?</Txt>
                    <Txt style={{ color: colors.dim, fontSize: 13.5, lineHeight: 19 }}>Your coach uses this to tune your session.</Txt>
                  </View>
                  {ready?.readiness && (
                    <View style={{ alignItems: 'flex-end' }}>
                      <Txt style={{ fontFamily: font.black, fontSize: 28, color: rw.color, lineHeight: 32 }}>{ready.readiness.score}</Txt>
                      <Txt style={{ color: rw.color, fontSize: 12, fontFamily: font.semibold }}>{rw.text}</Txt>
                    </View>
                  )}
                </View>
                <View style={{ gap: 8 }}>
                  <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
                    <Ionicons name="moon" size={17} color={colors.violet} />
                    <Txt style={{ fontFamily: font.semibold, fontSize: 15 }}>Sleep last night</Txt>
                  </View>
                  <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
                    <Nudge icon="remove" label="Less sleep" onStep={() => setSleep((v) => Math.max(3, r1(v - 0.5)))} />
                    <View style={{ alignItems: 'center' }}>
                      <Txt accessibilityLabel={`${sleep} hours`} style={{ fontFamily: font.black, fontSize: 38, letterSpacing: -1 }}>{sleep}<Txt style={{ color: colors.dim, fontSize: 16, fontFamily: font.semibold }}> h</Txt></Txt>
                    </View>
                    <Nudge icon="add" label="More sleep" onStep={() => setSleep((v) => Math.min(12, r1(v + 0.5)))} />
                  </View>
                </View>
                <Scale5 label="Soreness" low="fresh" high="very sore" value={sore} onChange={setSore} icon="body" color={colors.amber} />
                <Scale5 label="Energy" low="drained" high="great" value={energy} onChange={setEnergy} icon="flash" color={colors.lime} />
                <GradientButton label={savingCheck ? 'Saving…' : checked ? 'Update check-in' : 'Save check-in'} icon={checked ? 'checkmark-circle' : 'pulse'} onPress={saveCheckin} />
              </Card>

              <Card style={{ gap: 14 }}>
                <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
                  <View style={{ flex: 1, gap: 2 }}>
                    <Txt style={{ fontFamily: font.bold, fontSize: 19 }}>Weigh-in</Txt>
                    <Txt style={{ color: colors.dim, fontSize: 13.5, lineHeight: 19 }}>
                      {lastW ? `Last: ${lastW.kg} kg, ${daysAgo(lastW.day)}` : 'No weigh-in yet. Same time each morning works best.'}
                    </Txt>
                    {delta != null && (
                      <Txt style={{ fontFamily: font.semibold, fontSize: 13.5, color: delta === 0 ? colors.dim : colors.cyan }}>
                        {delta === 0 ? 'No change since last time' : `${delta > 0 ? '+' : '−'}${Math.abs(delta)} kg since last time`}
                      </Txt>
                    )}
                  </View>
                  <Sparkline values={weights.slice(-12).map((w) => w.kg)} />
                </View>
                <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
                  <Nudge icon="remove" label="Less weight" onStep={() => setKg((v) => Math.max(30, r1((v ?? shownKg) - kgStep)))} />
                  <Txt accessibilityLabel={`${r1(shownKg)} kilos`} style={{ fontFamily: font.black, fontSize: 44, letterSpacing: -1.5 }}>{r1(shownKg)}<Txt style={{ color: colors.dim, fontSize: 16, fontFamily: font.semibold }}> kg</Txt></Txt>
                  <Nudge icon="add" label="More weight" onStep={() => setKg((v) => Math.min(300, r1((v ?? shownKg) + kgStep)))} />
                </View>
                <View style={{ flexDirection: 'row', gap: 8, justifyContent: 'center' }}>
                  <Pill label="0.1 kg steps" active={kgStep === 0.1} onPress={() => setKgStep(0.1)} color={colors.cyan} />
                  <Pill label="0.5 kg steps" active={kgStep === 0.5} onPress={() => setKgStep(0.5)} color={colors.cyan} />
                </View>
                <GradientButton label={savingKg ? 'Saving…' : kgDirty ? 'Save weight' : 'Weight saved today'} icon={kgDirty ? 'save' : 'checkmark-circle'} onPress={saveWeight} />
              </Card>
            </FadeIn>

            {/* plan summary */}
            <FadeIn delay={140}>
              <Group title="Your plan" footer="Changing your weight or goal only recalculates your nutrition targets. Your training plan and history are never touched unless you choose to rebuild.">
                <Row icon="body" color={colors.cyan} title="Body" value={`${p.age} y · ${p.height_cm} cm · ${p.weight_kg} kg`} onPress={openEdit} />
                <Row icon={goal.icon} color={goal.color} title="Goal" value={goal.label} onPress={openEdit} />
                <Row icon="barbell" color={colors.lime} title="Training" value={`${data.plan.days} days · ${cap(p.experience ?? 'intermediate')}`} onPress={openEdit} />
                <Row icon="construct" color={colors.violet} title="Equipment" value={eqText} />
                <Row icon="shield-checkmark" color={colors.amber} title="Injuries" value={(p.injuries ?? []).length ? p.injuries.join(', ') : 'None'} onPress={openEdit} />
                {data.plan.nutrition && <Row icon="nutrition" color={colors.pink} title="Nutrition" value={`${data.plan.nutrition.calories} kcal · ${data.plan.nutrition.protein_g} g protein`} onPress={openEdit} />}
              </Group>
            </FadeIn>

            <FadeIn delay={180}>
              <Group title="Your coach knows you" footer="Everything I've learned about you, in one place. Correct anything that's wrong, or have me forget it.">
                <Row icon="sparkles" color={colors.violet} title="What I know about you" value={knowsPct == null ? undefined : `${knowsPct}% of the basics`} onPress={() => setKnows(true)} />
                <Row icon="chatbubbles" color={colors.lime} title="Update my profile" value="Short interview" divider onPress={() => setCoachTool('intake')} />
                <Row icon="calendar" color={colors.cyan} title="Your calendar" value="Trips, competitions" divider onPress={() => setCoachTool('agenda')} />
                <Row icon="library" color={colors.amber} title="Ask the evidence" value="Cited answers" divider onPress={() => setCoachTool('research')} />
                <Row icon="pulse" color={colors.pink} title="Agent trace" value="What the coaches did" divider onPress={() => setCoachTool('trace')} />
              </Group>
            </FadeIn>

            <FadeIn delay={200}>
              <Group title="Coach">
                <Row icon={pers.icon} color={pers.color} title="Your coach" value={`${pers.name} · ${pers.short}`} onPress={() => setSheet('coach')} />
                <Row icon="volume-high" color={colors.cyan} title="Coach voice" value={VOICE_LABEL[vMode]} onPress={() => setSheet('voice')} />
              </Group>
            </FadeIn>

            <FadeIn delay={260}>
              <PushSettings />
              <Group title="App">
                <Row icon="server" color={colors.cyan} title="Server" value={API.replace(/^https?:\/\//, '')} onPress={() => setServerOpen(true)} />
                <Row icon="phone-portrait" color={colors.violet} title="Vibration"
                  right={<Switch accessibilityLabel="Vibration" value={vibrate} trackColor={{ false: colors.cardHi, true: colors.lime }} thumbColor="#fff"
                    onValueChange={async (on) => { setVibrate(on); await Haptics.setHapticsEnabled(on); if (on) Haptics.selectionAsync(); }} />} />
              </Group>
            </FadeIn>

            <FadeIn delay={280}>
              <Group title="Your data" footer={`Your health data stays on your own computer. Export gives you every record as one file.${lastExport ? ` Last exported ${lastExport}.` : ''}`}>
                <Row icon="download" color={colors.cyan} title={exporting ? 'Preparing your file…' : 'Export my data'} value="JSON file" onPress={exporting ? undefined : exportData} />
              </Group>
            </FadeIn>

            <FadeIn delay={300}>
              <Group title="Try it out" footer="Sample data is made-up training history so you can explore the charts before you have your own.">
                <Row icon="flask" color={colors.amber} title="Load sample data" onPress={loadDemo} />
              </Group>
            </FadeIn>

            <FadeIn delay={340} style={{ gap: 8 }}>
              <Txt style={{ color: colors.red, fontSize: 12, letterSpacing: 1, fontFamily: font.semibold, marginLeft: 6 }}>DANGER ZONE</Txt>
              <Card style={{ padding: 0, gap: 0, overflow: 'hidden', borderColor: colors.red + '55' }}>
                <Pressable accessibilityRole="button" onPress={clearHistory} style={({ pressed }) => ({ padding: 16, gap: 4, opacity: pressed ? 0.6 : 1 })}>
                  <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10 }}>
                    <Ionicons name="trash-bin" size={20} color={colors.red} />
                    <Txt style={{ flex: 1, fontFamily: font.bold, fontSize: 16, color: colors.red }}>Clear logged history</Txt>
                    <Ionicons name="chevron-forward" size={18} color={colors.faint} />
                  </View>
                  <Txt style={{ color: colors.dim, fontSize: 13, lineHeight: 18, marginLeft: 30 }}>Removes workouts, check-ins, weigh-ins and meals. Keeps your plan.</Txt>
                </Pressable>
                <View style={{ height: 1, backgroundColor: colors.red + '33' }} />
                <Pressable accessibilityRole="button" onPress={startOver} style={({ pressed }) => ({ padding: 16, gap: 4, opacity: pressed ? 0.6 : 1 })}>
                  <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10 }}>
                    <Ionicons name="refresh-circle" size={22} color={colors.red} />
                    <Txt style={{ flex: 1, fontFamily: font.bold, fontSize: 16, color: colors.red }}>Start over</Txt>
                    <Ionicons name="chevron-forward" size={18} color={colors.faint} />
                  </View>
                  <Txt style={{ color: colors.dim, fontSize: 13, lineHeight: 18, marginLeft: 32 }}>Deletes everything, including your plan, and restarts setup.</Txt>
                </Pressable>
              </Card>
            </FadeIn>

            <Txt style={{ color: colors.faint, fontSize: 12, textAlign: 'center' }}>AI Coach · backend {API}</Txt>
          </>
        )}
      </ScrollView>

      {toast && (
        <View pointerEvents="none" style={{ position: 'absolute', top: 10, left: 16, right: 16 }}>
          <FadeIn key={toast.t} distance={-8}>
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10, padding: 14, borderRadius: 18, backgroundColor: toast.ok ? '#16200A' : '#2A1214', borderWidth: 1, borderColor: (toast.ok ? colors.lime : colors.red) + '88' }}>
              <Ionicons name={toast.ok ? 'checkmark-circle' : 'alert-circle'} size={22} color={toast.ok ? colors.lime : colors.red} />
              <Txt style={{ flex: 1, fontFamily: font.semibold, fontSize: 14.5, lineHeight: 20 }}>{toast.t}</Txt>
            </View>
          </FadeIn>
        </View>
      )}

      <KnowsYou visible={knows} onClose={() => setKnows(false)} />
      <CoachShell visible={serverOpen} onClose={() => setServerOpen(false)} title="Server" subtitle="Where your coach lives">
        <ScrollView contentContainerStyle={{ padding: 20, paddingTop: 8 }} keyboardShouldPersistTaps="handled">
          <ServerForm onSaved={() => { setServerOpen(false); onReset?.(); }} />
        </ScrollView>
      </CoachShell>
      <Agenda visible={coachTool === 'agenda'} onClose={() => setCoachTool(null)} />
      <Intake visible={coachTool === 'intake'} onClose={() => { setCoachTool(null); load(); }} />
      <Research visible={coachTool === 'research'} onClose={() => setCoachTool(null)} />
      <Trace visible={coachTool === 'trace'} onClose={() => setCoachTool(null)} />
      <ConfirmSheet confirm={confirm} busy={confirmBusy} onClose={() => { if (!confirmBusy) setConfirm(null); }} />

      {/* ---------------- edit profile */}
      <Sheet visible={sheet === 'edit'} onClose={() => setSheet(null)} title="Edit profile"
        footer={!form ? null : changedKeys.length === 0 ? (
          <Txt style={{ color: colors.faint, textAlign: 'center', fontSize: 13 }}>Change anything above to save</Txt>
        ) : trainingChanged ? (
          <>
            <Txt style={{ color: colors.dim, fontSize: 13, lineHeight: 19 }}>You changed training details. Rebuild your training plan to match, or keep your current plan (your nutrition targets update either way).</Txt>
            <GradientButton label={saving ? 'Saving…' : 'Save & rebuild training plan'} icon="construct" onPress={() => save(true)} />
            <Pressable onPress={() => save(false)} disabled={saving} style={{ alignItems: 'center', justifyContent: 'center', minHeight: 48 }}>
              <Txt style={{ fontFamily: font.bold, color: colors.dim }}>Save, keep my current plan</Txt>
            </Pressable>
          </>
        ) : (
          <GradientButton label={saving ? 'Saving…' : 'Save changes'} icon="checkmark" onPress={() => save(false)} />
        )}>
        {form && (
          <>
            <Label>Sex</Label>
            <View style={{ flexDirection: 'row', gap: 10 }}>
              <Pill label="Male" active={form.sex === 'm'} onPress={() => setForm({ ...form, sex: 'm' })} />
              <Pill label="Female" active={form.sex === 'f'} onPress={() => setForm({ ...form, sex: 'f' })} />
            </View>
            <Stepper label="Age" value={form.age} unit="yrs" min={14} max={100} onChange={(n) => setForm({ ...form, age: n })} />
            <Stepper label="Height" value={form.height_cm} unit="cm" min={100} max={250} onChange={(n) => setForm({ ...form, height_cm: n })} />
            <Stepper label="Weight" value={form.weight_kg} unit="kg" step={0.5} min={30} max={300} onChange={(n) => setForm({ ...form, weight_kg: n })} />

            <Label>Activity outside the gym</Label>
            <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8 }}>
              {ACTIVITY.map((a) => <Pill key={a.k} label={a.label} active={form.activity === a.k} onPress={() => setForm({ ...form, activity: a.k })} color={colors.cyan} />)}
            </View>

            <Label>Goal</Label>
            {GOALS.map((g) => <Choice key={g.k} icon={g.icon} title={g.label} sub={g.sub} color={g.color} selected={form.goal === g.k} onPress={() => setForm({ ...form, goal: g.k })} />)}

            <Label>Training days per week</Label>
            <View style={{ flexDirection: 'row', gap: 8 }}>
              {[3, 4, 5].map((d) => <Pill flex key={d} label={`${d} days`} active={form.days_per_week === d} onPress={() => setForm({ ...form, days_per_week: d })} color={colors.cyan} />)}
            </View>

            <Label>Experience</Label>
            <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8 }}>
              {EXPERIENCE.map((x) => <Pill key={x} label={cap(x)} active={form.experience === x} onPress={() => setForm({ ...form, experience: x })} color={colors.amber} />)}
            </View>

            <Label>Injuries to protect</Label>
            <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8 }}>
              {INJURIES.map((k) => (
                <Pill key={k} label={cap(k)} active={hasInj(form.injuries, k)} color={colors.amber}
                  onPress={() => setForm({ ...form, injuries: toggleInj(form.injuries, k) })} />
              ))}
              {customInjuries(form.injuries).map((i) => (
                <Pill key={i} label={`${i} ×`} active color={colors.amber} onPress={() => setForm({ ...form, injuries: form.injuries.filter((x) => x !== i) })} />
              ))}
            </View>
          </>
        )}
      </Sheet>

      {/* ---------------- coach */}
      <Sheet visible={sheet === 'coach'} onClose={() => setSheet(null)} title="Your coach">
        <PersonaPicker value={persona} onChange={(id) => { setPersona(id); updateProfile({ coach_persona: id }).then(load).catch(offline); }} />
        {(persona === 'haryanvi' || persona === 'roast') && (
          <View style={{ gap: 8 }}>
            <Txt style={{ color: colors.dim, fontSize: 13, lineHeight: 19 }}>
              Pehalwan speaks Haryanvi and listens for Hindi. Try: "बेंच प्रेस अस्सी किलो आठ बार", "खतम", or "सिर्फ तीस मिनट हैं". Simple English works too.
            </Txt>
            {!hindiVoice && (
              <Card style={{ borderColor: 'rgba(255,184,77,0.45)', gap: 4 }}>
                <Txt style={{ color: colors.amber, fontFamily: font.bold, fontSize: 14 }}>No Hindi voice found on this phone</Txt>
                <Txt style={{ color: colors.dim, fontSize: 13, lineHeight: 19 }}>
                  Without one, the phone reads Hindi in an English voice and it sounds wrong. Install Hindi in Settings → Language → Text-to-speech output (Google Speech Services), then reopen the app. Or switch Coach voice to ElevenLabs.
                </Txt>
              </Card>
            )}
          </View>
        )}
        <View>
          <Label>What should your coach call you?</Label>
          <TextInput value={myName} onChangeText={setMyName} placeholder="Your name" placeholderTextColor={colors.faint} maxLength={30} accessibilityLabel="Your name"
            onEndEditing={() => updateProfile({ name: myName.trim() }).then(() => { show('Saved'); load(); }).catch(offline)}
            style={{ backgroundColor: colors.card, color: colors.text, borderRadius: 16, paddingHorizontal: 16, paddingVertical: 14, fontFamily: font.semibold, fontSize: 16, borderWidth: 1, borderColor: colors.border }} />
        </View>
      </Sheet>

      {/* ---------------- voice */}
      <Sheet visible={sheet === 'voice'} onClose={() => setSheet(null)} title="Coach voice">
        <Segmented<VoiceMode> options={[{ key: 'all', label: 'ElevenLabs' }, { key: 'moments', label: 'Key moments' }, { key: 'phone', label: 'Phone' }]}
          value={vMode} onChange={(m) => { setVMode(m); setVoiceMode(m); }} />
        <Txt style={{ color: colors.dim, fontSize: 13.5, lineHeight: 20 }}>
          {vMode === 'all' ? 'Every line uses ElevenLabs. Best sound, but uses your credits fastest.'
            : vMode === 'moments' ? 'ElevenLabs for openers, PRs, pain warnings and wrap-ups. Your phone voice for routine cues. Best fit for the free plan.'
            : 'Always your phone voice. Uses no credits.'}
        </Txt>
        {tts?.configured ? (
          <View style={{ gap: 6 }}>
            <Bar value={tts.used / Math.max(1, tts.budget)} color={tts.remaining < tts.budget * 0.15 ? colors.amber : colors.lime} />
            <Txt style={{ color: colors.dim, fontSize: 12.5 }}>{tts.used} of {tts.budget} credits used this month · {tts.cached_lines} lines saved for free replays</Txt>
          </View>
        ) : (
          <Txt style={{ color: colors.amber, fontSize: 13.5, lineHeight: 20 }}>ElevenLabs isn't set up. Add ELEVENLABS_API_KEY to the backend .env file and restart the backend.</Txt>
        )}
        <GradientButton label="Test the voice (ElevenLabs)" icon="play" onPress={testVoice} />
        <Pressable onPress={() => speak(pers.sample, pers.tts, undefined, { persona, premium: false })} style={{ alignItems: 'center', justifyContent: 'center', minHeight: 48 }}>
          <Txt style={{ fontFamily: font.bold, color: colors.cyan }}>Test my phone's voice</Txt>
        </Pressable>
        {!!voiceMsg && <Txt style={{ color: colors.dim, fontSize: 13.5, lineHeight: 20 }}>{voiceMsg}</Txt>}
      </Sheet>
    </View>
  );
}
