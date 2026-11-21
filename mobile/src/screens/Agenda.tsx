import React, { useCallback, useEffect, useState } from 'react';
import { Pressable, RefreshControl, ScrollView, TextInput, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { AgendaEvent, AgendaKind, addAgendaEvent, getAgenda, removeAgendaEvent } from '../api';
import { Sheet } from '../forms';
import * as Haptics from '../haptics';
import { isoOf, parseIso, shortDate } from '../dates';
import { colors, font } from '../theme';
import { Card, EmptyState, ErrorCard, FadeIn, GradientButton, IconButton, Skeleton, Txt, useOnline, useToast } from '../ui';
import { Pill } from '../ux/controls';
import SpeakButton from '../ux/SpeakButton';
import CoachShell, { inputStyle } from './CoachShell';

const KINDS: { key: AgendaKind; label: string; icon: string; color: string; blurb: string }[] = [
  { key: 'competition', label: 'Competition', icon: 'trophy', color: colors.amber, blurb: 'Your Periodization coach will plan a taper and a peak so you arrive fresh.' },
  { key: 'trip', label: 'Trip', icon: 'airplane', color: colors.cyan, blurb: 'Your Logistics coach will move or shorten sessions around travel.' },
  { key: 'no_gym', label: 'No gym', icon: 'close-circle', color: colors.violet, blurb: 'For a long stretch away from a gym, your coach may trim volume a little. It never skips you silently.' },
  { key: 'busy', label: 'Busy day', icon: 'briefcase', color: colors.pink, blurb: 'Your coach will suggest a shorter session or a different day.' },
  { key: 'equipment', label: 'Limited equipment', icon: 'construct', color: colors.mint, blurb: 'Your coach will avoid exercises you cannot do with what you have.' },
];
const KIND = Object.fromEntries(KINDS.map((k) => [k.key, k])) as Record<AgendaKind, (typeof KINDS)[number]>;
const EQUIP = ['barbell', 'dumbbell', 'machine', 'cable', 'bodyweight'];
const MINUTES = [20, 30, 45, 60];
const MONTHS_FULL = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

const addDays = (iso: string, n: number) => { const d = parseIso(iso); d.setDate(d.getDate() + n); return isoOf(d); };
const addMonths = (iso: string, n: number) => {
  const d = parseIso(iso);
  const last = new Date(d.getFullYear(), d.getMonth() + n + 1, 0).getDate();
  return isoOf(new Date(d.getFullYear(), d.getMonth() + n, Math.min(d.getDate(), last)));
};
const today = () => isoOf(new Date());

/** Date picker with no native module: nudge by day, week or month with big buttons. Always a valid date, never before `min`. */
function DateField({ label, value, onChange, min }: { label: string; value: string; onChange: (iso: string) => void; min: string }) {
  const d = parseIso(value);
  const go = (iso: string) => { Haptics.selectionAsync(); onChange(iso < min ? min : iso); };
  const Btn = ({ icon, onPress, a11y }: { icon: string; onPress: () => void; a11y: string }) => (
    <Pressable accessibilityRole="button" accessibilityLabel={a11y} onPress={onPress}
      style={({ pressed }) => ({ width: 48, height: 48, borderRadius: 16, alignItems: 'center', justifyContent: 'center', backgroundColor: 'rgba(255,255,255,0.07)', borderWidth: 1, borderColor: colors.border, opacity: pressed ? 0.6 : 1 })}>
      <Ionicons name={icon as any} size={20} color={colors.text} />
    </Pressable>
  );
  return (
    <View style={{ gap: 8 }}>
      <Txt style={{ color: colors.dim, fontSize: 12, letterSpacing: 0.8, fontFamily: font.semibold }}>{label.toUpperCase()}</Txt>
      <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
        <Btn icon="remove" a11y={`${label}: one day earlier`} onPress={() => go(addDays(value, -1))} />
        <View style={{ flex: 1, alignItems: 'center' }}>
          <Txt style={{ fontFamily: font.black, fontSize: 20 }}>{d.getDate()} {MONTHS_FULL[d.getMonth()]}</Txt>
          <Txt style={{ color: colors.dim, fontSize: 13 }}>{d.getFullYear()}</Txt>
        </View>
        <Btn icon="add" a11y={`${label}: one day later`} onPress={() => go(addDays(value, 1))} />
      </View>
      <View style={{ flexDirection: 'row', gap: 8 }}>
        <Pill label="-1 week" onPress={() => go(addDays(value, -7))} flex />
        <Pill label="+1 week" onPress={() => go(addDays(value, 7))} flex />
        <Pill label="+1 month" onPress={() => go(addMonths(value, 1))} flex />
      </View>
    </View>
  );
}

function when(e: AgendaEvent): string {
  const range = e.start === e.end ? shortDate(e.start) : `${shortDate(e.start)} to ${shortDate(e.end)}`;
  if (e.active) return `${range} · on now`;
  if (e.days_until === 0) return `${range} · today`;
  if (e.days_until === 1) return `${range} · tomorrow`;
  return `${range} · in ${e.days_until} days`;
}

function Body() {
  const { epoch } = useOnline();
  const toast = useToast();
  const [events, setEvents] = useState<AgendaEvent[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [kind, setKind] = useState<AgendaKind>('competition');
  const [start, setStart] = useState(addDays(today(), 7));
  const [end, setEnd] = useState(addDays(today(), 7));
  const [label, setLabel] = useState('');
  const [minutes, setMinutes] = useState<number | null>(null);
  const [equip, setEquip] = useState<string[]>([]);
  const [formErr, setFormErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setErr(null);
    try { setEvents((await getAgenda()).events); } catch (e: any) { setErr(e?.message ?? 'Could not load your calendar'); }
  }, []);
  useEffect(() => { load(); }, [load, epoch]);

  const openAdd = () => {
    const s = addDays(today(), 7);
    setKind('competition'); setStart(s); setEnd(s); setLabel(''); setMinutes(null); setEquip([]); setFormErr(null); setAdding(true);
  };
  const setStartSafe = (iso: string) => { setStart(iso); if (end < iso) setEnd(iso); };
  const problem = (): string | null => {
    if (start < today()) return 'The start date is in the past. Pick today or later.';
    if (kind !== 'competition' && end < start) return 'The end date has to be on or after the start date.';
    if (kind !== 'competition' && (parseIso(end).getTime() - parseIso(start).getTime()) / 86400000 > 120) return 'An event can last at most 120 days.';
    if (kind === 'equipment' && !equip.length) return 'Choose the equipment you will have.';
    return null;
  };
  const save = async () => {
    const p = problem();
    if (p) { setFormErr(p); return; }
    setBusy(true); setFormErr(null);
    try {
      await addAgendaEvent({ kind, start, end: kind === 'competition' ? start : end, label: label.trim() || undefined, minutes: kind === 'busy' && minutes ? minutes : undefined, equipment: kind === 'equipment' ? equip : undefined });
      Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
      setAdding(false);
      toast('Added. Your coaches are taking a look.');
      load();
    } catch (e: any) { setFormErr(e?.message ?? 'Could not save that'); }
    finally { setBusy(false); }
  };
  const remove = async (e: AgendaEvent) => {
    try { await removeAgendaEvent(e.id); Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light); setEvents((l) => (l ?? []).filter((x) => x.id !== e.id)); }
    catch (x: any) { toast(x?.message ?? 'Could not remove that'); load(); }
  };

  const meta = KIND[kind];
  return (
    <>
      <ScrollView contentContainerStyle={{ padding: 16, gap: 14, paddingBottom: 40 }} refreshControl={<RefreshControl refreshing={refreshing} tintColor={colors.lime} onRefresh={async () => { setRefreshing(true); await load(); setRefreshing(false); }} />}>
        <FadeIn>
          <Card style={{ gap: 6 }}>
            <Txt style={{ fontFamily: font.bold, fontSize: 15 }}>What this does</Txt>
            <Txt style={{ color: colors.dim, fontSize: 14, lineHeight: 21 }}>Tell your coaches what is coming up. A competition gets a taper, a trip or a busy week gets shorter or moved sessions, and a stretch without a gym gets a small volume trim. Your plan never changes silently: every change is listed under Decisions and follows your approval settings.</Txt>
          </Card>
        </FadeIn>
        {err ? <ErrorCard message={err} onRetry={load} />
          : !events ? <><Skeleton height={76} /><Skeleton height={76} /></>
          : events.length === 0 ? <EmptyState icon="calendar" tint={colors.cyan} title="Nothing coming up" body="Add a competition, trip or busy stretch so your coaches can plan around it." action="Add an event" onAction={openAdd} />
          : events.map((e, i) => {
            const k = KIND[e.kind] ?? KIND.busy;
            const extra = [e.minutes ? `${e.minutes} min sessions` : '', (e.equipment ?? []).join(', ')].filter(Boolean).join(' · ');
            return (
              <FadeIn key={e.id} delay={i * 40}>
                <Card glow={e.active ? k.color : undefined} style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
                  <View style={{ width: 40, height: 40, borderRadius: 13, backgroundColor: k.color + '22', alignItems: 'center', justifyContent: 'center' }}>
                    <Ionicons name={k.icon as any} size={20} color={k.color} />
                  </View>
                  <View style={{ flex: 1 }}>
                    <Txt style={{ fontFamily: font.bold, fontSize: 15.5 }} numberOfLines={2}>{e.label}</Txt>
                    <Txt style={{ color: colors.dim, fontSize: 13 }}>{k.label} · {when(e)}</Txt>
                    {!!extra && <Txt style={{ color: colors.faint, fontSize: 12.5 }}>{extra}</Txt>}
                  </View>
                  <IconButton icon="trash-outline" label={`Remove ${e.label}`} onPress={() => remove(e)} color={colors.red} />
                </Card>
              </FadeIn>
            );
          })}
        {!!events?.length && <GradientButton label="Add an event" icon="add" onPress={openAdd} />}
      </ScrollView>

      <Sheet visible={adding} onClose={() => setAdding(false)} title="Add an event"
        footer={<>
          {!!formErr && <Txt accessibilityRole="alert" style={{ color: colors.red, fontSize: 14, lineHeight: 20 }}>{formErr}</Txt>}
          <GradientButton label={busy ? 'Saving...' : 'Save event'} onPress={busy ? () => {} : save} />
        </>}>
        <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8 }}>
          {KINDS.map((k) => <Pill key={k.key} label={k.label} active={kind === k.key} color={k.color} onPress={() => { setKind(k.key); setFormErr(null); }} />)}
        </View>
        <Txt style={{ color: colors.dim, fontSize: 14, lineHeight: 20 }}>{meta.blurb}</Txt>
        <SpeakButton key={kind} text={meta.blurb} agent={kind === 'competition' ? 'periodization' : 'logistics'} color={meta.color} name="what this does" label />
        <DateField label={kind === 'competition' ? 'Date' : 'Starts'} value={start} onChange={setStartSafe} min={today()} />
        {kind !== 'competition' && <DateField label="Ends" value={end} onChange={setEnd} min={start} />}
        {kind === 'busy' && (
          <View style={{ gap: 8 }}>
            <Txt style={{ color: colors.dim, fontSize: 12, letterSpacing: 0.8, fontFamily: font.semibold }}>MINUTES YOU CAN TRAIN (OPTIONAL)</Txt>
            <View style={{ flexDirection: 'row', gap: 8 }}>{MINUTES.map((m) => <Pill key={m} label={`${m}`} active={minutes === m} onPress={() => setMinutes(minutes === m ? null : m)} flex />)}</View>
          </View>
        )}
        {kind === 'equipment' && (
          <View style={{ gap: 8 }}>
            <Txt style={{ color: colors.dim, fontSize: 12, letterSpacing: 0.8, fontFamily: font.semibold }}>WHAT YOU WILL HAVE</Txt>
            <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8 }}>{EQUIP.map((q) => <Pill key={q} label={q} active={equip.includes(q)} onPress={() => setEquip((l) => (l.includes(q) ? l.filter((x) => x !== q) : [...l, q]))} />)}</View>
          </View>
        )}
        <View style={{ gap: 8 }}>
          <Txt style={{ color: colors.dim, fontSize: 12, letterSpacing: 0.8, fontFamily: font.semibold }}>NAME (OPTIONAL)</Txt>
          <TextInput value={label} onChangeText={setLabel} maxLength={80} placeholder={kind === 'competition' ? 'e.g. City 10K' : 'e.g. Work trip'} placeholderTextColor={colors.faint} accessibilityLabel="Event name" style={inputStyle} />
        </View>
      </Sheet>
    </>
  );
}

export default function Agenda({ visible, onClose }: { visible: boolean; onClose: () => void }) {
  return <CoachShell visible={visible} onClose={onClose} title="Your calendar" subtitle="Competitions, trips and busy stretches your coaches plan around."><Body /></CoachShell>;
}
