import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ActivityIndicator, KeyboardAvoidingView, Modal, Platform, Pressable, RefreshControl, ScrollView, TextInput, View } from 'react-native';
import { LinearGradient } from 'expo-linear-gradient';
import { Ionicons } from '@expo/vector-icons';
import * as Haptics from '../haptics';
import { deleteMeal, getNutritionDay, getPortionInfo, getRecentMeals, getWeek, logMeal, logPortion, MealPreviewItem, PortionInfo, previewMeal, repeatMeal, restoreMeal, setMealPortion } from '../api';
import PortionSheet, { Macros } from '../PortionSheet';
import { DAYS, parseIso, shortDate } from '../dates';
import { useVoice } from '../useVoice';
import { onVoiceAction, takePendingVoiceActions, VoiceAction } from '../voiceBus';
import { setVoiceDay } from '../voiceScreen';
import { colors, font, gradients } from '../theme';
import { Card, CountUp, ErrorCard, FadeIn, IconButton, Ring, SectionTitle, Skeleton, Txt, useOnline, useToast } from '../ui';
import AnimBar from '../ux/f_AnimBar';
import Diet from './Diet';
import { SafeAreaView } from 'react-native-safe-area-context';

const GROUPS = [
  { key: 'breakfast', label: 'Breakfast', icon: 'sunny-outline', color: colors.amber },
  { key: 'lunch', label: 'Lunch', icon: 'restaurant-outline', color: colors.lime },
  { key: 'dinner', label: 'Dinner', icon: 'moon-outline', color: colors.violet },
  { key: 'snacks', label: 'Snacks', icon: 'nutrition-outline', color: colors.cyan },
] as const;

// Macro colours are used ONLY here (the three bars), so they mean something.
const MACRO = { protein: colors.pink, carbs: colors.cyan, fat: colors.amber } as const;

function groupOf(ts: string): (typeof GROUPS)[number]['key'] {
  const h = new Date(ts).getHours();
  if (isNaN(h)) return 'snacks';
  return h < 11 ? 'breakfast' : h < 15 ? 'lunch' : h >= 17 && h < 22 ? 'dinner' : 'snacks';
}

/** A quiet week: day, date and a hairline of progress. No rings. */
function WeekStrip({ week, selected, onSelect }: { week: any[]; selected: string; onSelect: (d: string) => void }) {
  return (
    <View style={{ flexDirection: 'row', gap: 6 }}>
      {week.map((d, i) => {
        const on = d.day === selected;
        const isToday = i === week.length - 1;
        const pct = d.target_kcal ? Math.min(1, d.kcal / d.target_kcal) : 0;
        const over = d.target_kcal && d.kcal > d.target_kcal * 1.05;
        return (
          <Pressable key={d.day} onPress={() => { Haptics.selectionAsync(); onSelect(d.day); }} accessibilityRole="button" accessibilityLabel={`${d.day}${i === week.length - 1 ? ', today' : ''}, ${Math.round(d.kcal)} calories`} accessibilityState={{ selected: on }} style={{ flex: 1, minHeight: 44 }}>
            <View style={{ alignItems: 'center', gap: 6, paddingVertical: 10, borderRadius: 16, backgroundColor: on ? 'rgba(198,255,61,0.13)' : 'transparent', borderWidth: 1, borderColor: on ? 'rgba(198,255,61,0.35)' : 'transparent' }}>
              <Txt style={{ color: on ? colors.lime : colors.dim, fontSize: 10.5, fontFamily: font.bold, letterSpacing: 0.6 }}>{DAYS[(parseIso(d.day).getDay() + 6) % 7].toUpperCase().slice(0, 3)}</Txt>
              <Txt style={{ fontFamily: font.black, fontSize: 17, color: on ? colors.text : colors.dim }}>{+d.day.slice(8, 10)}</Txt>
              <View style={{ width: 22, height: 3, borderRadius: 2, backgroundColor: 'rgba(255,255,255,0.10)', overflow: 'hidden' }}>
                <View style={{ width: `${pct * 100}%`, height: '100%', backgroundColor: over ? colors.amber : colors.lime }} />
              </View>
              {isToday && <View style={{ position: 'absolute', top: 4, right: 6, width: 4, height: 4, borderRadius: 2, backgroundColor: colors.lime }} />}
            </View>
          </Pressable>
        );
      })}
    </View>
  );
}

/** One macro = one slim bar, the amount, and a calm "left" or "over" note. Over is amber, never red: one heavy meal is not a failure. */
function MacroBar({ label, eaten, target, color }: { label: string; eaten: number; target: number; color: string }) {
  const over = eaten > target * 1.05;
  const pct = Math.min(1, eaten / Math.max(1, target));
  const left = Math.round(target - eaten);
  const note = over ? `${Math.abs(left)} g over` : left <= 0 ? 'Goal reached' : `${left} g left`;
  return (
    <View style={{ gap: 6 }} accessible accessibilityLabel={`${label}: ${Math.round(eaten)} of ${target} grams, ${note}`}>
      <View style={{ flexDirection: 'row', justifyContent: 'space-between', alignItems: 'baseline', gap: 8 }}>
        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 7, flexShrink: 1 }}>
          <View style={{ width: 8, height: 8, borderRadius: 4, backgroundColor: color }} />
          <Txt style={{ fontFamily: font.semibold, fontSize: 13, color: colors.dim }} numberOfLines={1}>{label}</Txt>
        </View>
        <Txt style={{ fontFamily: font.bold, fontSize: 13, color: colors.text }} numberOfLines={1}>
          {Math.round(eaten)}<Txt style={{ color: colors.dim, fontFamily: font.medium }}> / {target} g</Txt>
          <Txt style={{ color: over ? colors.amber : colors.faint, fontFamily: font.medium, fontSize: 12 }}>{'  '}{note}</Txt>
        </Txt>
      </View>
      <AnimBar value={pct} color={over ? colors.amber : color} />
    </View>
  );
}

/** One short, kind sentence about where the day stands. Never scolds. */
function dayLine(eatenKcal: number, targetKcal: number, isToday: boolean): string {
  if (eatenKcal <= 0) return isToday ? 'Nothing logged yet. Add your first meal below.' : 'Nothing was logged this day.';
  const r = eatenKcal / Math.max(1, targetKcal);
  if (r < 0.5) return isToday ? 'Plenty of room left for today.' : 'A light day.';
  if (r < 0.9) return isToday ? 'On track. Keep going.' : 'A solid day.';
  if (r <= 1.05) return 'Right on your goal.';
  return 'A bit over today. That is fine, one day does not decide anything.';
}

export default function Nutrition() {
  const { online, epoch } = useOnline();
  const toast = useToast();
  const [failed, setFailed] = useState(false);
  const [why, setWhy] = useState(false);
  const [editing, setEditing] = useState<number | null>(null);
  const [week, setWeek] = useState<any[]>([]);
  const [selected, setSelected] = useState<string>('');
  const [data, setData] = useState<any>(null);
  const [recent, setRecent] = useState<any[]>([]);
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [openGroup, setOpenGroup] = useState<Record<string, boolean>>({});
  // Portion sheet for quick-add chips (kind 'add') and for logged rows (editing + editInfo).
  const [sheet, setSheet] = useState<{ name: string; grams: number | null; macros: Macros | null; recentId: number | null } | null>(null);
  const [sheetInfo, setSheetInfo] = useState<PortionInfo | null>(null);
  const [editInfo, setEditInfo] = useState<PortionInfo | null>(null);
  const [sheetBusy, setSheetBusy] = useState(false);
  const [focus, setFocus] = useState(false);

  const today = week.length ? week[week.length - 1].day : '';
  const isToday = !!selected && selected === today;

  const loadDay = useCallback(async (day: string) => {
    try { setData(await getNutritionDay(day)); } catch { /* keep what we have */ }
  }, []);

  const loadAll = useCallback(async (keep?: string) => {
    setRefreshing(true);
    try {
      const [w, r] = await Promise.all([getWeek(), getRecentMeals()]);
      setWeek(w); setRecent(r); setFailed(false);
      const day = keep && w.some((x: any) => x.day === keep) ? keep : w[w.length - 1].day;
      setSelected(day);
      await loadDay(day);
    } catch { setFailed(true); }
    setRefreshing(false);
  }, [loadDay]);
  useEffect(() => { loadAll(selected || undefined); }, [loadAll, epoch]);
  // "Hey coach, I had two eggs" should land on the day shown here (and "yesterday" is relative to the real today on the server).
  useEffect(() => { setVoiceDay(selected || null); return () => setVoiceDay(null); }, [selected]);

  const pickDay = (d: string) => { setSelected(d); setNote(''); loadDay(d); };

  // Voice control: "Hey coach, show yesterday's food". Also drains actions queued before this tab mounted.
  const [dietOpen, setDietOpen] = useState(false);
  const voiceApply = useRef<(a: VoiceAction) => boolean>(() => false);
  voiceApply.current = (a) => {
    if (a.type === 'open' && a.screen === 'diet') { setDietOpen(true); return true; }
    if (a.type === 'close' && dietOpen) { setDietOpen(false); return true; }
    if (a.type === 'nutrition_day') {
      if (week.length && !week.some((x: any) => x.day === a.day)) return false;   // not loaded yet or out of range: stay queued
      pickDay(a.day); return true;
    }
    if (a.type === 'refresh' && (!a.what || a.what.some((w) => w === 'nutrition' || w === 'all'))) { loadAll(selected || undefined); return true; }
    return false;
  };
  useEffect(() => {
    const mine = (a: VoiceAction) => a.type === 'nutrition_day' || (a.type === 'open' && a.screen === 'diet');
    const off = onVoiceAction((a) => voiceApply.current(a));
    const t = setInterval(() => { takePendingVoiceActions(mine).forEach((a) => voiceApply.current(a)); }, 400);
    const stop = setTimeout(() => clearInterval(t), 12000);
    return () => { off(); clearInterval(t); clearTimeout(stop); };
  }, [week.length]);

  // "How much?" flow: items the user gave no amount for are asked one by one; the rest are kept as parsed.
  const [ask, setAsk] = useState<{ text: string; items: MealPreviewItem[]; unknown: string[]; missing: number[]; step: number; chosen: Record<number, number> } | null>(null);

  const finishLog = useCallback(async (text: string, unknown: string[], log: () => Promise<void>) => {
    setBusy(true); setNote('');
    try {
      const before = new Set<number>((data?.meals ?? []).map((m: any) => m.id));
      await log();
      Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium);
      if (unknown.length) setNote(`Couldn't recognise "${unknown.join(', ')}". Try a common name, or add grams.`);
      else setNote('');
      setTyped(''); setAsk(null);
      await loadAll(selected);
      await offerUndoAdd(before, unknown.length ? 'Added the rest' : 'Meal added');
    } catch { setNote(online ? 'Something went wrong saving that. Try again.' : "You're offline, so this meal wasn't saved. Your text is still here. Try again when you're back."); }
    setBusy(false);
  }, [selected, loadAll, data, online]);

  const submit = useCallback(async (text: string) => {
    if (!text.trim() || !selected) return;
    setBusy(true); setNote('');
    let pv: Awaited<ReturnType<typeof previewMeal>> | null = null;
    try { pv = await previewMeal(text, selected); } catch { pv = null; } // older backend or offline: log directly as before
    setBusy(false);
    if (pv && pv.needs_amount && pv.items.length) {
      const missing = pv.items.map((it, i) => (it.qty_given ? -1 : i)).filter((i) => i >= 0);
      setAsk({ text, items: pv.items, unknown: pv.unknown ?? [], missing, step: 0, chosen: {} });
      return;
    }
    await finishLog(text, pv?.unknown ?? [], async () => {
      const r = await logMeal(text, selected);
      if (!pv && r.unknown?.length) setNote(`Couldn't recognise "${r.unknown.join(', ')}". Try a common name, or add grams.`);
    });
  }, [selected, finishLog]);

  /** Log every previewed item at its final grams. Known foods go in exact; estimated ones via the text path with the grams embedded. */
  const confirmAsk = async (chosen: Record<number, number>) => {
    if (!ask) return;
    const a = ask;
    await finishLog(a.text, a.unknown, async () => {
      for (let i = 0; i < a.items.length; i++) {
        const it = a.items[i];
        const g = Math.round(chosen[i] ?? it.grams);
        if (it.known) {
          try { await logPortion(it.name, g, selected); continue; } catch { /* fall through to text */ }
        }
        await logMeal(`${g}${it.measure} ${it.name}`, selected);
      }
    });
  };

  /** After anything that adds meals, find what is new and offer to take it back. */
  const offerUndoAdd = async (before: Set<number>, label: string) => {
    try {
      const d = await getNutritionDay(selected);
      const added = (d.meals ?? []).filter((m: any) => !before.has(m.id));
      if (!added.length) return;
      toast(`${label}: ${added.map((m: any) => m.name).slice(0, 2).join(', ')}${added.length > 2 ? '…' : ''}`, {
        lift: 64, action: { label: 'Undo', onPress: async () => { for (const m of added) await deleteMeal(m.id).catch(() => {}); loadAll(selected); } },
      });
    } catch { /* the meal is saved either way */ }
  };
  const voice = useVoice((t) => { setTyped(`Heard: ${t}`); setNote(''); submit(t).finally(() => { /* finishLog clears the composer on success; on failure keep what was heard */ setTyped((cur) => (cur === `Heard: ${t}` ? t : cur)); }); }, (m) => setNote(m));

  const quickAdd = async (id: number) => {
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light);
    try {
      const before = new Set<number>((data?.meals ?? []).map((m: any) => m.id));
      await repeatMeal(id, selected); await loadAll(selected); await offerUndoAdd(before, 'Added');
    } catch { setNote(online ? 'Something went wrong adding that. Try again.' : "You're offline, so that wasn't added. Try again when you're back."); }
  };
  const openSheet = (s: { name: string; grams: number | null; macros: Macros | null; recentId: number | null }) => {
    Haptics.selectionAsync();
    setSheet(s); setSheetInfo(null); setNote('');
    getPortionInfo(s.name).then((i) => setSheetInfo((cur) => cur ?? i ?? { name: s.name, known: false, unit: null, unit_g: null, per100: null }));
  };
  const confirmAdd = async (grams: number) => {
    if (!sheet || !selected) return;
    setSheetBusy(true); setNote('');
    const before = new Set<number>((data?.meals ?? []).map((m: any) => m.id));
    try {
      try { await logPortion(sheet.name, grams, selected); }
      catch {
        // Food not in the local DB (or older backend): reuse the chip's meal when the amount is unchanged, else the text path.
        if (sheet.recentId != null && sheet.grams && Math.round(sheet.grams) === grams) await repeatMeal(sheet.recentId, selected);
        else await logMeal(`${grams}g ${sheet.name}`, selected);
      }
      Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium);
      setSheet(null);
      await loadAll(selected); await offerUndoAdd(before, 'Added');
    } catch { setNote(online ? 'Something went wrong adding that. Try again.' : "You're offline, so that wasn't added. Try again when you're back."); }
    setSheetBusy(false);
  };
  const startEdit = (m: any) => {
    Haptics.selectionAsync();
    if (editing === m.id) { setEditing(null); return; }
    setEditing(m.id); setEditInfo(null);
    getPortionInfo(m.name).then((i) => setEditInfo((cur) => cur ?? i ?? { name: m.name, known: false, unit: null, unit_g: null, per100: null }));
  };
  /** Save a new portion in place; Undo puts the previous grams back. */
  const savePortion = async (m: any, grams: number) => {
    const old = Math.round(m.grams);
    if (grams === old) { setEditing(null); return; }
    setSheetBusy(true);
    try {
      try { await setMealPortion(m.id, { grams }); }
      catch { await scale(m, grams / m.grams); setSheetBusy(false); return; }
      Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium);
      setEditing(null); await loadAll(selected);
      toast(`${m.name}: ${old} g to ${grams} g`, { lift: 64, action: { label: 'Undo', onPress: async () => {
        try { await setMealPortion(m.id, { grams: old }); } catch { try { await deleteMeal(m.id); await restoreMeal(asRestore(m)); } catch { /* ignore */ } }
        loadAll(selected);
      } } });
    } catch { toast(online ? "Couldn't change that. Try again." : "You're offline, so it wasn't changed.", { tone: 'warn' }); }
    setSheetBusy(false);
  };
  const asRestore = (m: any) => ({ day: selected, name: m.name, grams: m.grams ?? null, kcal: m.kcal, protein: m.protein, carbs: m.carbs, fat: m.fat, estimated: !!m.estimated });
  const remove = async (m: any) => {
    Haptics.selectionAsync();
    try {
      await deleteMeal(m.id); setEditing(null); await loadAll(selected);
      toast(`Removed ${m.name}`, { lift: 64, action: { label: 'Undo', onPress: async () => { try { await restoreMeal(asRestore(m)); } catch { toast("Couldn't bring it back", { tone: 'warn' }); } loadAll(selected); } } });
    } catch { toast(online ? "Couldn't remove that. Try again." : "You're offline, so it wasn't removed.", { tone: 'warn' }); }
  };
  /** Change a portion: put the scaled entry in, take the old one out. One tap to undo. */
  const scale = async (m: any, f: number) => {
    Haptics.selectionAsync();
    const r1 = (x: number) => Math.round(x * 10) / 10;
    const next = { ...asRestore(m), grams: m.grams ? Math.round(m.grams * f) : null, kcal: Math.round(m.kcal * f), protein: r1(m.protein * f), carbs: r1(m.carbs * f), fat: r1(m.fat * f) };
    try {
      const { id } = await restoreMeal(next); await deleteMeal(m.id); setEditing(null); await loadAll(selected);
      toast(`${m.name}: portion ${f < 1 ? 'reduced' : 'increased'}`, { lift: 64, action: { label: 'Undo', onPress: async () => { try { await deleteMeal(id); await restoreMeal(asRestore(m)); } catch { /* ignore */ } loadAll(selected); } } });
    } catch { toast(online ? "Couldn't change that. Try again." : "You're offline, so it wasn't changed.", { tone: 'warn' }); }
  };

  const t = data?.target, e = data?.eaten, rem = data?.remaining;
  const over = !!t && e.kcal > t.kcal;
  const grouped = useMemo(() => GROUPS.map((g) => {
    const meals = (data?.meals ?? []).filter((m: any) => groupOf(m.ts) === g.key);
    const sum = (k: string) => Math.round(meals.reduce((a: number, m: any) => a + (m[k] || 0), 0));
    return { ...g, meals, kcal: sum('kcal'), p: sum('protein'), c: sum('carbs'), f: sum('fat') };
  }).filter((g) => g.meals.length), [data]);
  const dayWord = isToday ? 'Today' : selected ? `${DAYS[(parseIso(selected).getDay() + 6) % 7]}, ${shortDate(selected)}` : '';

  // ONE quick-add row instead of two sections: what would close your protein gap first, then your usuals.
  const quick: { key: string; title: string; sub: string; hot: boolean; run: () => void }[] = [
    ...(isToday ? (data?.suggestions ?? []) : []).slice(0, 3).map((s: any) => ({
      key: `s-${s.food}`, title: s.food, sub: `+${Math.round(s.protein)}g protein`, hot: true, run: () => openSheet({ name: s.food, grams: s.grams ?? null, macros: null, recentId: null }),
    })),
    ...recent.slice(0, 6).map((r: any) => ({
      key: `r-${r.id}`, title: `${r.name}${r.grams ? ` ${Math.round(r.grams)}g` : ''}`, sub: `${Math.round(r.kcal)} kcal`, hot: false, run: () => openSheet({ name: r.name, grams: r.grams ?? null, macros: { kcal: r.kcal, protein: r.protein ?? 0, carbs: r.carbs ?? 0, fat: r.fat ?? 0 }, recentId: r.id }),
    })),
  ];

  return (
    <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
      <ScrollView contentContainerStyle={{ padding: 20, gap: 18, paddingBottom: 24 }} keyboardShouldPersistTaps="handled"
        refreshControl={<RefreshControl refreshing={refreshing && !!data} onRefresh={() => loadAll(selected)} tintColor={colors.lime} />}>
        <View>
          <Txt style={{ color: colors.dim, fontFamily: font.semibold, fontSize: 12, letterSpacing: 1.1 }}>{(dayWord || 'Fuel').toUpperCase()}</Txt>
          <Txt style={{ fontFamily: font.black, fontSize: 30, letterSpacing: -0.7 }}>Fuel</Txt>
        </View>

        <Pressable accessibilityRole="button" accessibilityLabel="Open your diet coach" onPress={() => { Haptics.selectionAsync(); setDietOpen(true); }}
          style={({ pressed }) => ({ flexDirection: 'row', alignItems: 'center', gap: 12, padding: 14, borderRadius: 22, backgroundColor: pressed ? 'rgba(198,255,61,0.16)' : 'rgba(198,255,61,0.09)', borderWidth: 1, borderColor: 'rgba(198,255,61,0.32)' })}>
          <View style={{ width: 44, height: 44, borderRadius: 22, backgroundColor: colors.lime, alignItems: 'center', justifyContent: 'center' }}><Ionicons name="nutrition" size={24} color="#07080B" /></View>
          <View style={{ flex: 1 }}>
            <Txt style={{ fontFamily: font.bold, fontSize: 16 }}>Diet coach</Txt>
            <Txt style={{ color: colors.dim, fontSize: 13.5, lineHeight: 19 }}>What to eat, how much, meal plans and groceries.</Txt>
          </View>
          <Ionicons name="chevron-forward" size={20} color={colors.lime} />
        </Pressable>

        {week.length > 0 && <WeekStrip week={week} selected={selected} onSelect={pickDay} />}

        {!data && !failed && (<><Skeleton height={240} radius={30} /><Skeleton height={56} /><Skeleton height={140} /></>)}
        {!data && failed && <ErrorCard message="Couldn't load your meals" onRetry={() => loadAll(selected || undefined)} />}

        {data && !t && (
          <Card style={{ gap: 6, flexDirection: 'row', alignItems: 'center' }}>
            <View style={{ flex: 1, gap: 4 }}>
              <Txt style={{ color: colors.dim, fontSize: 11, letterSpacing: 1.2, fontFamily: font.bold }}>LOGGED</Txt>
              <Txt style={{ fontFamily: font.black, fontSize: 30, letterSpacing: -0.6 }} numberOfLines={1} adjustsFontSizeToFit>{Math.round(e.kcal)} <Txt style={{ color: colors.dim, fontSize: 14, fontFamily: font.medium }}>kcal</Txt></Txt>
              <Txt style={{ color: colors.dim, lineHeight: 20, fontSize: 14 }}>Build your plan in Profile to get calorie and macro targets.</Txt>
            </View>
          </Card>
        )}

        {/* The hero: one ring, one number that matters, three quiet bars. */}
        {data && t && (
          <FadeIn>
            <Card style={{ gap: 20, padding: 20 }}>
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 18 }}>
                <Ring size={124} stroke={12} progress={Math.min(e.kcal / t.kcal, 1)} from={over ? colors.amber : colors.lime} to={over ? colors.red : colors.mint}>
                  <CountUp value={Math.round(e.kcal)} style={{ fontFamily: font.black, fontSize: 28, letterSpacing: -0.6 }} />
                  <Txt style={{ color: colors.dim, fontSize: 11, fontFamily: font.semibold }}>EATEN</Txt>
                </Ring>
                <View style={{ flex: 1, gap: 2 }}>
                  <Txt style={{ color: colors.dim, fontSize: 11, letterSpacing: 1.2, fontFamily: font.bold }}>{over ? 'OVER BY' : isToday ? 'LEFT TODAY' : 'LEFT THAT DAY'}</Txt>
                  <View style={{ flexDirection: 'row', alignItems: 'baseline' }} accessible accessibilityLabel={`${Math.abs(Math.round(rem.kcal))} calories ${over ? 'over' : 'left'}`}>
                    <CountUp value={Math.abs(Math.round(rem.kcal))} style={{ fontFamily: font.black, fontSize: 40, letterSpacing: -1, color: over ? colors.amber : colors.text }} />
                    <Txt style={{ color: colors.dim, fontSize: 14, fontFamily: font.medium }}> kcal</Txt>
                  </View>
                  <Txt style={{ color: colors.dim, fontSize: 13, lineHeight: 18 }}>{dayLine(e.kcal, t.kcal, isToday)}</Txt>
                  <Pressable onPress={() => { Haptics.selectionAsync(); setWhy((w) => !w); }} accessibilityRole="button" accessibilityState={{ expanded: why }} accessibilityLabel={`Goal ${t.kcal} calories. Where does it come from?`} style={{ minHeight: 44, flexDirection: 'row', alignItems: 'center', gap: 5 }}>
                    <Txt style={{ color: colors.dim, fontSize: 12.5 }}>of {t.kcal} kcal goal</Txt>
                    <Ionicons name="information-circle-outline" size={16} color={colors.dim} />
                  </Pressable>
                </View>
              </View>
              {why && (
                <Txt style={{ color: colors.dim, fontSize: 13, lineHeight: 19, backgroundColor: 'rgba(255,255,255,0.05)', borderRadius: 14, padding: 12 }}>
                  Your goal is worked out from your body stats, how active you are and what you're aiming for. Your nutrition coach nudges it if your progress calls for it, and tells you when it does. Protein is set to protect muscle. Change your goal or stats in Profile.
                </Txt>
              )}
              <View style={{ gap: 14 }}>
                <MacroBar label="Protein" eaten={e.protein} target={t.protein} color={MACRO.protein} />
                <MacroBar label="Carbs" eaten={e.carbs} target={t.carbs} color={MACRO.carbs} />
                <MacroBar label="Fat" eaten={e.fat} target={t.fat} color={MACRO.fat} />
              </View>
            </Card>
          </FadeIn>
        )}

        {data && grouped.length === 0 && (
          <Card style={{ alignItems: 'center', gap: 8, padding: 28 }}>
            <Ionicons name="restaurant-outline" size={34} color={colors.faint} />
            <Txt style={{ fontFamily: font.bold, fontSize: 16 }}>Nothing logged {isToday ? 'yet' : 'for this day'}</Txt>
            <Txt style={{ color: colors.dim, textAlign: 'center', lineHeight: 20 }}>Say or type what you ate in the box below. Plain words work, and you can add amounts. Tap an example to try it.</Txt>
            <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, justifyContent: 'center', marginTop: 4 }}>
              {['3 roti with dal', '2 eggs and a banana', 'chicken rice bowl'].map((x) => (
                <Pressable key={x} onPress={() => { Haptics.selectionAsync(); setTyped(x); }} accessibilityRole="button" accessibilityLabel={`Use example: ${x}`}
                  style={{ minHeight: 44, justifyContent: 'center', paddingHorizontal: 14, borderRadius: 999, borderWidth: 1, borderColor: 'rgba(255,255,255,0.14)', backgroundColor: 'rgba(255,255,255,0.05)' }}>
                  <Txt style={{ fontFamily: font.semibold, fontSize: 13 }}>{x}</Txt>
                </Pressable>
              ))}
            </View>
          </Card>
        )}

        {quick.length > 0 && (
          <View style={{ gap: 10 }}>
            <SectionTitle>Quick add</SectionTitle>
            <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ gap: 8 }} style={{ marginHorizontal: -20 }}>
              <View style={{ width: 12 }} />
              {quick.map((q) => (
                <Pressable key={q.key} onPress={q.run} accessibilityRole="button" accessibilityLabel={`Add ${q.title}, choose amount`} style={({ pressed }) => ({ opacity: pressed ? 0.8 : 1 })}>
                  <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10, minHeight: 48, paddingVertical: 8, paddingLeft: 14, paddingRight: 12, borderRadius: 999, borderWidth: 1,
                    backgroundColor: q.hot ? 'rgba(255,92,158,0.10)' : 'rgba(255,255,255,0.05)', borderColor: q.hot ? 'rgba(255,92,158,0.40)' : 'rgba(255,255,255,0.10)' }}>
                    <View>
                      <Txt style={{ fontFamily: font.semibold, fontSize: 13, textTransform: 'capitalize' }}>{q.title}</Txt>
                      <Txt style={{ color: q.hot ? colors.pink : colors.dim, fontSize: 11.5, fontFamily: q.hot ? font.bold : font.regular }}>{q.sub}</Txt>
                    </View>
                    <Ionicons name="add" size={18} color={q.hot ? colors.pink : colors.lime} />
                  </View>
                </Pressable>
              ))}
              <View style={{ width: 12 }} />
            </ScrollView>
          </View>
        )}

        {ask && (() => {
          const i = ask.missing[ask.step], it = ask.items[i];
          const last = ask.step === ask.missing.length - 1;
          const info: PortionInfo = { name: it.name, known: it.known, unit: it.unit, unit_g: it.unit_g, mode: it.mode, measure: it.measure, per100: null };
          return (
            <View style={{ gap: 8 }}>
              <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
                <Txt style={{ fontFamily: font.bold, fontSize: 15 }} accessibilityLiveRegion="polite">
                  How much? {ask.missing.length > 1 ? `${ask.step + 1} of ${ask.missing.length}` : ''}
                </Txt>
                {ask.missing.length > 1 && (
                  <View style={{ flexDirection: 'row', gap: 5 }} accessibilityElementsHidden importantForAccessibility="no-hide-descendants">
                    {ask.missing.map((_, k) => <View key={k} style={{ width: k === ask.step ? 18 : 6, height: 6, borderRadius: 3, backgroundColor: k <= ask.step ? colors.lime : 'rgba(255,255,255,0.14)' }} />)}
                  </View>
                )}
              </View>
              <PortionSheet key={`${ask.step}-${it.name}`} name={it.name} info={info} baseGrams={it.grams} baseMacros={{ kcal: it.kcal, protein: it.protein, carbs: it.carbs, fat: it.fat }}
                confirmLabel={last ? 'Add meal' : 'Next'} busy={busy}
                onConfirm={(g) => { const chosen = { ...ask.chosen, [i]: g }; if (last) confirmAsk(chosen); else setAsk({ ...ask, chosen, step: ask.step + 1 }); }}
                onCancel={() => setAsk(null)} />
              <Pressable onPress={() => setAsk(null)} accessibilityRole="button" accessibilityLabel="Cancel, log nothing"
                style={{ minHeight: 44, alignItems: 'center', justifyContent: 'center' }}>
                <Txt style={{ color: colors.dim, fontFamily: font.semibold, fontSize: 14 }}>Skip, don't log this meal</Txt>
              </Pressable>
            </View>
          );
        })()}

        {sheet && (
          <PortionSheet name={sheet.name} info={sheetInfo} baseGrams={sheet.grams} baseMacros={sheet.macros} confirmLabel="Add" busy={sheetBusy}
            onConfirm={confirmAdd} onCancel={() => setSheet(null)} />
        )}

        {/* One card per meal: a headline and a calm list. Macros are summed per meal, not repeated on every item. */}
        {grouped.map((g, gi) => {
          const open = openGroup[g.key] ?? true;
          return (
            <FadeIn key={g.key} delay={gi * 60}>
              <Card style={{ padding: 0, gap: 0, overflow: 'hidden' }}>
                <Pressable onPress={() => { Haptics.selectionAsync(); setOpenGroup((o) => ({ ...o, [g.key]: !open })); }}
                  accessibilityRole="button" accessibilityState={{ expanded: open }}
                  accessibilityLabel={`${g.label}, ${g.kcal} calories, ${g.meals.length} ${g.meals.length === 1 ? 'item' : 'items'}. ${open ? 'Collapse' : 'Expand'}`}
                  style={({ pressed }) => ({ flexDirection: 'row', alignItems: 'center', gap: 12, padding: 16, minHeight: 64, backgroundColor: pressed ? 'rgba(255,255,255,0.04)' : 'transparent' })}>
                  <View style={{ width: 36, height: 36, borderRadius: 12, backgroundColor: g.color + '22', alignItems: 'center', justifyContent: 'center' }}>
                    <Ionicons name={g.icon as any} size={19} color={g.color} />
                  </View>
                  <View style={{ flex: 1 }}>
                    <Txt style={{ fontFamily: font.bold, fontSize: 16 }}>{g.label}</Txt>
                    <Txt style={{ color: colors.dim, fontSize: 12 }} numberOfLines={1}>{g.meals.length} {g.meals.length === 1 ? 'item' : 'items'} · P {g.p} · C {g.c} · F {g.f} g</Txt>
                  </View>
                  <Txt style={{ fontFamily: font.black, fontSize: 18, letterSpacing: -0.3 }} numberOfLines={1}>{g.kcal}<Txt style={{ color: colors.dim, fontSize: 11, fontFamily: font.semibold }}> kcal</Txt></Txt>
                  <Ionicons name={open ? 'chevron-up' : 'chevron-down'} size={16} color={colors.faint} />
                </Pressable>
                {open && g.meals.map((m: any) => (
                  <View key={m.id} style={{ borderTopWidth: 1, borderTopColor: 'rgba(255,255,255,0.06)' }}>
                    <View style={{ flexDirection: 'row', alignItems: 'center', gap: 4, paddingLeft: 16, paddingRight: 6 }}>
                      <Pressable onPress={() => startEdit(m)} accessibilityRole="button" accessibilityLabel={`${m.name}, ${Math.round(m.kcal)} calories. Tap to change the portion`}
                        accessibilityState={{ expanded: editing === m.id }}
                        style={({ pressed }) => ({ flex: 1, minHeight: 48, flexDirection: 'row', alignItems: 'center', gap: 10, opacity: pressed ? 0.7 : 1 })}>
                        <Txt style={{ flex: 1, fontSize: 14.5, fontFamily: font.medium }} numberOfLines={1}>
                          <Txt style={{ textTransform: 'capitalize' }}>{m.name}</Txt>
                          {!!m.grams && <Txt style={{ color: colors.dim, fontSize: 12.5 }}>  {Math.round(m.grams)}g</Txt>}
                          {!!m.estimated && <Txt style={{ color: colors.amber, fontSize: 10.5, fontFamily: font.bold }}>  EST</Txt>}
                        </Txt>
                        <Txt style={{ color: editing === m.id ? colors.lime : colors.dim, fontFamily: font.semibold, fontSize: 14 }}>{Math.round(m.kcal)}</Txt>
                      </Pressable>
                      <IconButton icon="close" label={`Remove ${m.name}`} onPress={() => remove(m)} size={18} />
                    </View>
                    {editing === m.id && !!m.grams && (
                      <View style={{ paddingHorizontal: 10, paddingBottom: 10 }}>
                        <PortionSheet name={m.name} info={editInfo} baseGrams={m.grams} baseMacros={{ kcal: m.kcal, protein: m.protein, carbs: m.carbs, fat: m.fat }}
                          confirmLabel="Save" busy={sheetBusy} onConfirm={(g) => savePortion(m, g)} onCancel={() => setEditing(null)} />
                      </View>
                    )}
                    {editing === m.id && !m.grams && (
                      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8, paddingHorizontal: 16, paddingBottom: 12 }}>
                        <Txt style={{ color: colors.dim, fontSize: 12.5, marginRight: 2 }}>Portion</Txt>
                        {[{ l: 'Half', f: 0.5 }, { l: '×1.5', f: 1.5 }, { l: 'Double', f: 2 }].map((b) => (
                          <Pressable key={b.l} onPress={() => scale(m, b.f)} accessibilityRole="button" accessibilityLabel={`Make the portion ${b.l}`}
                            style={{ minHeight: 44, minWidth: 64, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 12, borderRadius: 14, backgroundColor: 'rgba(255,255,255,0.08)' }}>
                            <Txt style={{ fontFamily: font.bold, fontSize: 13.5 }}>{b.l}</Txt>
                          </Pressable>
                        ))}
                      </View>
                    )}
                  </View>
                ))}
              </Card>
            </FadeIn>
          );
        })}
      </ScrollView>

      {/* Docked composer: log food from anywhere on the screen */}
      <View style={{ paddingHorizontal: 14, paddingTop: 6, paddingBottom: 6, gap: 6 }}>
        {!!note && <Txt style={{ color: colors.amber, fontSize: 12.5, lineHeight: 18, paddingHorizontal: 6 }}>{note}</Txt>}
        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8, backgroundColor: 'rgba(22,26,36,0.96)', borderRadius: 30, borderWidth: 1, borderColor: focus ? 'rgba(198,255,61,0.45)' : 'rgba(255,255,255,0.10)', padding: 6 }}>
          {(
            <Pressable onPress={() => (voice.listening ? voice.stop() : voice.start())} disabled={busy} accessibilityRole="button" accessibilityLabel={voice.listening ? 'Stop listening' : 'Say what you ate'}>
              <LinearGradient colors={voice.listening ? gradients.warm : ['#232838', '#1A1E28']} style={{ width: 44, height: 44, borderRadius: 22, alignItems: 'center', justifyContent: 'center' }}>
                <Ionicons name={voice.listening ? 'stop' : 'mic'} size={20} color={voice.listening ? '#fff' : colors.lime} />
              </LinearGradient>
            </Pressable>
          )}
          <TextInput value={typed} onChangeText={setTyped} placeholder={voice.listening ? 'Listening…' : isToday ? 'What did you eat?' : `Add to ${shortDate(selected)}…`}
            placeholderTextColor={colors.faint} onSubmitEditing={() => submit(typed)} returnKeyType="send" accessibilityLabel="What did you eat?" onFocus={() => setFocus(true)} onBlur={() => setFocus(false)}
            style={{ flex: 1, minHeight: 44, color: colors.text, fontFamily: font.medium, fontSize: 15, paddingHorizontal: 8, paddingVertical: 10 }} />
          {!!typed && !busy && (
            <IconButton icon="close-circle" label="Clear what you typed" onPress={() => setTyped('')} size={20} color={colors.faint} />
          )}
          <Pressable onPress={() => submit(typed)} disabled={busy || !typed.trim()} accessibilityRole="button" accessibilityLabel="Add this meal" accessibilityState={{ disabled: busy || !typed.trim(), busy }}>
            <LinearGradient colors={typed.trim() ? gradients.lime : ['#232838', '#1A1E28']} style={{ width: 44, height: 44, borderRadius: 22, alignItems: 'center', justifyContent: 'center' }}>
              {busy ? <ActivityIndicator color="#07080B" /> : <Ionicons name="arrow-up" size={22} color={typed.trim() ? '#07080B' : colors.faint} />}
            </LinearGradient>
          </Pressable>
        </View>
      </View>
      <Modal visible={dietOpen} animationType="slide" onRequestClose={() => setDietOpen(false)}>
        <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={['top', 'bottom']}>
          {dietOpen && <Diet onClose={() => setDietOpen(false)} onLoggedMeal={() => loadAll(selected)} />}
        </SafeAreaView>
      </Modal>
    </KeyboardAvoidingView>
  );
}
