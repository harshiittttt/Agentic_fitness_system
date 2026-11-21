import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, KeyboardAvoidingView, Platform, Pressable, ScrollView, TextInput, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import * as Haptics from '../haptics';
import { DietDay, DietMeal, DietPrefs, DietReply, GroceryItem, dietChat, dietSwap, getDietPlan, getDietState, getDietWeek, logMeal, saveDietPrefs } from '../api';
import { useVoice } from '../useVoice';
import { colors, font } from '../theme';
import { Card, Chip, FadeIn, Txt, useToast } from '../ui';

type Msg = { id: number; role: 'user' | 'assistant'; text: string; plan?: DietDay | null; week?: DietDay[] | null; grocery?: GroceryItem[] | null; undo?: string };

const DIET_LABEL: Record<string, string> = { veg: 'Vegetarian', egg: 'Veg + eggs', nonveg: 'Eggs + chicken + fish' };
const SUGGEST = ['Build me a full day plan', 'Make a week plan with a grocery list', 'Is my protein enough today?', 'High-protein snack ideas', 'What should I eat after my workout?'];
const cap = (s: string) => s.replace(/\b\w/g, (c) => c.toUpperCase());

function Delta({ day }: { day: DietDay }) {
  const k = day.delta.kcal, p = day.delta.protein;
  const ok = Math.abs(k) <= 0.08 * (day.target.kcal || 1) && Math.abs(p) <= 0.1 * (day.target.protein || 1);
  return (
    <Txt style={{ color: ok ? colors.mint : colors.amber, fontSize: 12.5, fontFamily: font.semibold }}>
      {ok ? 'On target' : `${k >= 0 ? '+' : ''}${k} kcal · ${p >= 0 ? '+' : ''}${p} g protein vs target`}
    </Txt>
  );
}

/** One day as cards: every food with a quantity you can measure, per-meal totals, tap a food to swap it, log a meal with one tap. */
function DayPlan({ day, onSwap, onAnother, onLogged, busy }: { day: DietDay; onSwap?: (slot: string, food: string) => void; onAnother?: () => void; onLogged: (m: DietMeal) => void; busy?: boolean }) {
  return (
    <View style={{ gap: 10 }}>
      <Card style={{ gap: 6 }}>
        <View style={{ flexDirection: 'row', justifyContent: 'space-between', alignItems: 'flex-end' }}>
          <View>
            <Txt style={{ color: colors.dim, fontSize: 12, letterSpacing: 1.2, fontFamily: font.semibold }}>THE DAY</Txt>
            <Txt style={{ fontFamily: font.black, fontSize: 26, letterSpacing: -0.5 }}>{day.totals.kcal} <Txt style={{ fontSize: 15, color: colors.dim, fontFamily: font.semibold }}>kcal</Txt></Txt>
          </View>
          <View style={{ alignItems: 'flex-end' }}>
            <Txt style={{ fontFamily: font.black, fontSize: 22, color: colors.pink }}>{Math.round(day.totals.protein)} g <Txt style={{ fontSize: 13, color: colors.dim, fontFamily: font.semibold }}>protein</Txt></Txt>
            <Txt style={{ color: colors.dim, fontSize: 12.5 }}>carbs {Math.round(day.totals.carbs)} g · fat {Math.round(day.totals.fat)} g</Txt>
          </View>
        </View>
        <Delta day={day} />
        {day.notes.map((n, i) => <Txt key={i} style={{ color: colors.dim, fontSize: 12.5, lineHeight: 18 }}>{n}</Txt>)}
      </Card>
      {day.meals.map((m) => (
        <Card key={m.slot} style={{ gap: 8 }}>
          <View style={{ flexDirection: 'row', justifyContent: 'space-between', gap: 8 }}>
            <View style={{ flex: 1 }}>
              <Txt style={{ color: colors.lime, fontSize: 12, letterSpacing: 1, fontFamily: font.bold }}>{m.slot.toUpperCase()}</Txt>
              <Txt style={{ fontFamily: font.bold, fontSize: 16.5, lineHeight: 22 }}>{m.title}</Txt>
            </View>
            <View style={{ alignItems: 'flex-end' }}>
              <Txt style={{ fontFamily: font.bold, fontSize: 15 }}>{m.totals.kcal} kcal</Txt>
              <Txt style={{ color: colors.pink, fontSize: 13, fontFamily: font.semibold }}>{Math.round(m.totals.protein)} g protein</Txt>
            </View>
          </View>
          {m.items.map((i) => (
            <Pressable key={i.food} accessibilityRole="button" accessibilityLabel={`${i.label}. Tap to swap`} disabled={!onSwap || busy} onPress={() => { Haptics.selectionAsync(); onSwap?.(m.slot, i.food); }}
              style={({ pressed }) => ({ flexDirection: 'row', alignItems: 'center', gap: 8, minHeight: 40, paddingHorizontal: 10, borderRadius: 12, backgroundColor: pressed ? colors.cardHi : 'rgba(255,255,255,0.04)' })}>
              <Txt style={{ flex: 1, fontSize: 15 }}>{cap(i.label)}</Txt>
              <Txt style={{ color: colors.dim, fontSize: 13 }}>{i.kcal} · {Math.round(i.protein)}p</Txt>
              {onSwap && <Ionicons name="swap-horizontal" size={16} color={colors.faint} />}
            </Pressable>
          ))}
          <Pressable accessibilityRole="button" accessibilityLabel={`Log ${m.slot}`} onPress={() => onLogged(m)} style={{ minHeight: 44, flexDirection: 'row', alignItems: 'center', gap: 6, alignSelf: 'flex-start' }}>
            <Ionicons name="add-circle-outline" size={20} color={colors.cyan} />
            <Txt style={{ color: colors.cyan, fontFamily: font.semibold, fontSize: 14 }}>I ate this, log it</Txt>
          </Pressable>
        </Card>
      ))}
      {onAnother && (
        <Pressable accessibilityRole="button" onPress={onAnother} disabled={busy} style={{ minHeight: 48, borderRadius: 16, alignItems: 'center', justifyContent: 'center', flexDirection: 'row', gap: 8, backgroundColor: 'rgba(255,255,255,0.06)' }}>
          {busy ? <ActivityIndicator color={colors.lime} /> : <Ionicons name="shuffle" size={18} color={colors.text} />}
          <Txt style={{ fontFamily: font.semibold }}>Show me a different day</Txt>
        </Pressable>
      )}
    </View>
  );
}

function WeekPlan({ days, grocery, onLogged }: { days: DietDay[]; grocery: GroceryItem[] | null | undefined; onLogged: (m: DietMeal) => void }) {
  const [open, setOpen] = useState(0);
  const [shop, setShop] = useState(false);
  const names = ['Day 1', 'Day 2', 'Day 3', 'Day 4', 'Day 5', 'Day 6', 'Day 7'];
  return (
    <View style={{ gap: 8 }}>
      {days.map((d, i) => (
        <View key={i} style={{ gap: 8 }}>
          <Pressable accessibilityRole="button" accessibilityState={{ expanded: open === i }} onPress={() => { Haptics.selectionAsync(); setOpen(open === i ? -1 : i); }}
            style={{ minHeight: 52, flexDirection: 'row', alignItems: 'center', gap: 10, paddingHorizontal: 14, borderRadius: 16, backgroundColor: colors.card, borderWidth: 1, borderColor: colors.border }}>
            <Txt style={{ fontFamily: font.bold, fontSize: 15.5, flex: 1 }}>{names[i]}</Txt>
            <Txt style={{ color: colors.dim, fontSize: 13 }}>{d.totals.kcal} kcal · {Math.round(d.totals.protein)} g</Txt>
            <Ionicons name={open === i ? 'chevron-up' : 'chevron-down'} size={18} color={colors.faint} />
          </Pressable>
          {open === i && <DayPlan day={d} onLogged={onLogged} />}
        </View>
      ))}
      {!!grocery?.length && (
        <>
          <Pressable accessibilityRole="button" onPress={() => { Haptics.selectionAsync(); setShop(!shop); }} style={{ minHeight: 52, flexDirection: 'row', alignItems: 'center', gap: 10, paddingHorizontal: 14, borderRadius: 16, backgroundColor: 'rgba(198,255,61,0.10)', borderWidth: 1, borderColor: 'rgba(198,255,61,0.3)' }}>
            <Ionicons name="cart" size={20} color={colors.lime} />
            <Txt style={{ flex: 1, fontFamily: font.bold, color: colors.lime }}>Grocery list for the week</Txt>
            <Ionicons name={shop ? 'chevron-up' : 'chevron-down'} size={18} color={colors.lime} />
          </Pressable>
          {shop && (
            <Card style={{ gap: 2 }}>
              {grocery.map((g) => (
                <View key={g.food} style={{ flexDirection: 'row', justifyContent: 'space-between', minHeight: 38, alignItems: 'center' }}>
                  <Txt style={{ fontSize: 15 }}>{cap(g.food)}</Txt>
                  <Txt style={{ color: colors.dim, fontSize: 14 }}>{g.buy}</Txt>
                </View>
              ))}
              <Txt style={{ color: colors.faint, fontSize: 12, marginTop: 6 }}>Cooked foods (dal, rice, rajma) are weights as eaten, not dry weights.</Txt>
            </Card>
          )}
        </>
      )}
    </View>
  );
}

/** First-time questions, asked once: how you eat. Saved on the server and used for every plan. */
function Basics({ prefs, onSaved }: { prefs: DietPrefs; onSaved: (p: DietPrefs) => void }) {
  const [type, setType] = useState<DietPrefs['diet_type']>(prefs.diet_type_known ? prefs.diet_type : 'veg');
  const [meals, setMeals] = useState(prefs.meals_known ? prefs.meals_per_day : 3);
  const [dislikes, setDislikes] = useState(prefs.dislikes.join(', '));
  const [saving, setSaving] = useState(false);
  const save = async () => {
    setSaving(true);
    try { onSaved(await saveDietPrefs({ diet_type: type, meals_per_day: meals, dislikes: dislikes.split(',').map((x) => x.trim()).filter(Boolean) })); } catch { /* the caller shows nothing; the card stays so they can retry */ }
    setSaving(false);
  };
  return (
    <Card style={{ gap: 12 }}>
      <Txt style={{ fontFamily: font.bold, fontSize: 17 }}>Tell me how you eat</Txt>
      <Txt style={{ color: colors.dim, fontSize: 14, lineHeight: 20 }}>Asked once. Every plan after this uses it.</Txt>
      <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8 }}>
        {(['veg', 'egg', 'nonveg'] as const).map((k) => <Chip key={k} label={DIET_LABEL[k]} active={type === k} onPress={() => setType(k)} />)}
      </View>
      <Txt style={{ color: colors.dim, fontSize: 13 }}>Meals per day</Txt>
      <View style={{ flexDirection: 'row', gap: 8 }}>
        {[3, 4, 5].map((n) => <Chip key={n} label={`${n} meals`} active={meals === n} onPress={() => setMeals(n)} />)}
      </View>
      <TextInput value={dislikes} onChangeText={setDislikes} placeholder="Foods you won't eat (optional): karela, mushroom" placeholderTextColor={colors.faint} accessibilityLabel="Foods you will not eat"
        style={{ minHeight: 48, backgroundColor: colors.bg2, color: colors.text, borderRadius: 14, paddingHorizontal: 14, fontFamily: font.medium, fontSize: 15, borderWidth: 1, borderColor: colors.border }} />
      <Pressable accessibilityRole="button" onPress={save} disabled={saving} style={{ minHeight: 52, borderRadius: 16, backgroundColor: colors.lime, alignItems: 'center', justifyContent: 'center' }}>
        {saving ? <ActivityIndicator color="#07080B" /> : <Txt style={{ color: '#07080B', fontFamily: font.black, fontSize: 16 }}>Save and build my plan</Txt>}
      </Pressable>
    </Card>
  );
}

/** Your own dietitian: asks what it needs once, builds plans from your real targets, answers food questions, swaps foods, logs meals. */
export default function Diet({ onClose, onLoggedMeal }: { onClose: () => void; onLoggedMeal?: () => void }) {
  const toast = useToast();
  const [prefs, setPrefs] = useState<DietPrefs | null>(null);
  const [targets, setTargets] = useState<{ kcal: number; protein: number } | null>(null);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [plan, setPlan] = useState<DietDay | null>(null);        // the day on screen (the one a swap refers to)
  const [variation, setVariation] = useState(0);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [planBusy, setPlanBusy] = useState(false);
  const [problem, setProblem] = useState('');
  const [loading, setLoading] = useState(true);
  const id = useRef(1);
  const scroll = useRef<ScrollView>(null);

  const load = useCallback(async () => {
    setProblem(''); setLoading(true);
    try {
      const s = await getDietState();
      setPrefs(s.prefs); setTargets(s.targets); setPlan(s.plan); setVariation(s.plan?.variation ?? 0);
      setMsgs(s.history.map((h) => ({ id: id.current++, role: h.role, text: h.text })));
    } catch { setProblem("Can't reach your dietitian. Check your connection and try again."); }
    setLoading(false);
  }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { const t = setTimeout(() => scroll.current?.scrollToEnd({ animated: true }), 150); return () => clearTimeout(t); }, [msgs.length, plan?.totals.kcal, busy]);

  const needsBasics = !!prefs && !(prefs.diet_type_known && prefs.meals_known);

  const ask = useCallback(async (q: string) => {
    const m = q.trim();
    if (!m || busy) return;
    setText(''); setProblem(''); setBusy(true);
    setMsgs((x) => [...x, { id: id.current++, role: 'user', text: m }]);
    try {
      const r: DietReply = await dietChat(m);
      if (r.prefs) setPrefs(r.prefs);
      if (r.plan && !r.week) { setPlan(r.plan); setVariation(r.plan.variation ?? 0); }
      const undo = r.actions.find((a) => a.kind === 'changed' && a.can_undo)?.summary;
      setMsgs((x) => [...x, { id: id.current++, role: 'assistant', text: r.reply || 'Done.', plan: r.week ? null : r.plan, week: r.week, grocery: r.grocery, undo }]);
      Haptics.selectionAsync();
    } catch (e: any) {
      setProblem(e?.message && !/Network request failed/i.test(e.message) ? e.message : "Couldn't reach your dietitian. Check your connection and try again.");
      setText(m);
    }
    setBusy(false);
  }, [busy]);
  const voice = useVoice((t) => { ask(t); }, (m) => setProblem(m));

  const buildPlan = async (v = 0) => {
    setPlanBusy(true); setProblem('');
    try {
      const d = await getDietPlan(v);
      setPlan(d); setVariation(v);
      setMsgs((x) => [...x, { id: id.current++, role: 'assistant', text: `Here is a day sized to your targets (${d.target.kcal} kcal, ${d.target.protein} g protein). Tap any food to swap it.`, plan: d }]);
    } catch (e: any) { setProblem(e?.message || "Couldn't build the plan."); }
    setPlanBusy(false);
  };
  const weekPlan = async () => {
    setPlanBusy(true); setProblem('');
    try {
      const w = await getDietWeek();
      setMsgs((x) => [...x, { id: id.current++, role: 'assistant', text: 'Your 7 days, with one grocery list for the week.', week: w.days, grocery: w.grocery }]);
    } catch (e: any) { setProblem(e?.message || "Couldn't build the week."); }
    setPlanBusy(false);
  };
  const swap = async (slot: string, food: string) => {
    if (planBusy) return;
    setPlanBusy(true);
    try {
      const d = await dietSwap(slot, food);
      setPlan(d);
      setMsgs((x) => x.map((m) => (m.plan ? { ...m, plan: d } : m)));
      toast(`Swapped ${food}.`);
    } catch (e: any) { toast(e?.message || 'No like-for-like swap found for that.', { tone: 'warn' }); }
    setPlanBusy(false);
  };
  const logIt = async (m: DietMeal) => {
    try {
      await logMeal(m.items.map((i) => `${i.grams} g ${i.food}`).join(', '));
      Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
      toast(`Logged ${m.slot.toLowerCase()}: ${m.totals.kcal} kcal.`);
      onLoggedMeal?.();
    } catch { toast("Couldn't log that. Check your connection.", { tone: 'warn' }); }
  };

  const latestPlanId = [...msgs].reverse().find((m) => m.plan)?.id;
  return (
    <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined} style={{ flex: 1, backgroundColor: colors.bg }}>
      <View style={{ flexDirection: 'row', alignItems: 'center', paddingHorizontal: 8, paddingTop: 6 }}>
        <Pressable accessibilityRole="button" accessibilityLabel="Close" onPress={onClose} style={{ width: 52, height: 52, alignItems: 'center', justifyContent: 'center' }}>
          <Ionicons name="close" size={30} color={colors.text} />
        </Pressable>
        <View style={{ flex: 1 }}>
          <Txt style={{ fontFamily: font.black, fontSize: 20, letterSpacing: -0.3 }}>Diet coach</Txt>
          <Txt style={{ color: colors.dim, fontSize: 13 }} numberOfLines={1}>{targets ? `${targets.kcal} kcal · ${targets.protein} g protein a day${prefs?.diet_type_known ? ` · ${DIET_LABEL[prefs.diet_type]}` : ''}` : 'Food, plans and quantities'}</Txt>
        </View>
      </View>

      <ScrollView ref={scroll} contentContainerStyle={{ padding: 16, gap: 12, paddingBottom: 20 }} keyboardShouldPersistTaps="handled">
        {loading && <ActivityIndicator color={colors.lime} style={{ marginTop: 40 }} />}
        {!!problem && (
          <Card style={{ borderColor: colors.amber + '66', flexDirection: 'row', gap: 10, alignItems: 'center' }}>
            <Ionicons name="alert-circle" size={22} color={colors.amber} />
            <Txt style={{ flex: 1, fontSize: 14.5, lineHeight: 21 }}>{problem}</Txt>
            {!prefs && <Pressable accessibilityRole="button" onPress={load} style={{ minHeight: 44, justifyContent: 'center' }}><Txt style={{ color: colors.cyan, fontFamily: font.bold }}>Retry</Txt></Pressable>}
          </Card>
        )}

        {needsBasics && prefs && <FadeIn><Basics prefs={prefs} onSaved={(p) => { setPrefs(p); buildPlan(0); }} /></FadeIn>}

        {!loading && !needsBasics && msgs.length === 0 && (
          <FadeIn style={{ gap: 12 }}>
            <Card style={{ gap: 6 }}>
              <Txt style={{ fontFamily: font.bold, fontSize: 17 }}>Ask me anything about food</Txt>
              <Txt style={{ color: colors.dim, fontSize: 14, lineHeight: 21 }}>I build plans from your real targets with quantities you can measure, in Indian home food. I only change your targets when you ask me to.</Txt>
            </Card>
          </FadeIn>
        )}

        {msgs.map((m) => (
          <FadeIn key={m.id} distance={6} style={{ gap: 10, alignItems: m.role === 'user' ? 'flex-end' : 'stretch' }}>
            <View style={{ maxWidth: m.role === 'user' ? '86%' : '100%', alignSelf: m.role === 'user' ? 'flex-end' : 'flex-start', paddingHorizontal: 14, paddingVertical: 11, borderRadius: 18, backgroundColor: m.role === 'user' ? 'rgba(198,255,61,0.16)' : colors.card, borderWidth: 1, borderColor: m.role === 'user' ? 'rgba(198,255,61,0.3)' : colors.border }}>
              <Txt selectable style={{ fontSize: 15.5, lineHeight: 23 }}>{m.text}</Txt>
              {!!m.undo && <Txt style={{ color: colors.mint, fontSize: 12.5, marginTop: 6 }}>Changed: {m.undo} (you can undo this in the Coach tab)</Txt>}
            </View>
            {m.plan && <DayPlan day={m.id === latestPlanId && plan ? plan : m.plan} busy={planBusy} onSwap={m.id === latestPlanId ? swap : undefined} onAnother={m.id === latestPlanId ? () => buildPlan(variation + 1) : undefined} onLogged={logIt} />}
            {m.week && <WeekPlan days={m.week} grocery={m.grocery} onLogged={logIt} />}
          </FadeIn>
        ))}
        {busy && <View style={{ flexDirection: 'row', gap: 8, alignItems: 'center' }}><ActivityIndicator color={colors.lime} /><Txt style={{ color: colors.dim }}>Working it out…</Txt></View>}
        {planBusy && !busy && <ActivityIndicator color={colors.lime} />}

        {!needsBasics && !loading && !busy && (
          <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginTop: 4 }}>
            {SUGGEST.map((s) => <Chip key={s} label={s} onPress={() => ask(s)} />)}
          </View>
        )}
        {!needsBasics && !loading && prefs && (
          <View style={{ flexDirection: 'row', gap: 8 }}>
            <Pressable accessibilityRole="button" onPress={() => buildPlan(variation)} disabled={planBusy} style={{ flex: 1, minHeight: 48, borderRadius: 16, alignItems: 'center', justifyContent: 'center', backgroundColor: 'rgba(255,255,255,0.06)' }}><Txt style={{ fontFamily: font.semibold }}>Today's plan</Txt></Pressable>
            <Pressable accessibilityRole="button" onPress={weekPlan} disabled={planBusy} style={{ flex: 1, minHeight: 48, borderRadius: 16, alignItems: 'center', justifyContent: 'center', backgroundColor: 'rgba(255,255,255,0.06)' }}><Txt style={{ fontFamily: font.semibold }}>Week + groceries</Txt></Pressable>
          </View>
        )}
      </ScrollView>

      {!needsBasics && (
        <View style={{ flexDirection: 'row', gap: 10, padding: 14, paddingTop: 6, alignItems: 'center' }}>
          <TextInput value={text} onChangeText={setText} placeholder="Ask: how much paneer for 40 g protein?" placeholderTextColor={colors.faint} accessibilityLabel="Ask your dietitian"
            onSubmitEditing={() => ask(text)} returnKeyType="send" multiline={false}
            style={{ flex: 1, minHeight: 52, backgroundColor: colors.card, color: colors.text, borderRadius: 26, paddingHorizontal: 18, fontFamily: font.medium, fontSize: 16, borderWidth: 1, borderColor: colors.border }} />
          {voice.available && (
            <Pressable accessibilityRole="button" accessibilityLabel={voice.listening ? 'Stop listening' : 'Speak your question'} onPress={() => (voice.listening ? voice.stop() : voice.start())}
              style={{ width: 52, height: 52, borderRadius: 26, alignItems: 'center', justifyContent: 'center', backgroundColor: voice.listening ? colors.cyan : 'rgba(255,255,255,0.08)' }}>
              <Ionicons name={voice.listening ? 'stop' : 'mic'} size={24} color={voice.listening ? '#07080B' : colors.text} />
            </Pressable>
          )}
          <Pressable accessibilityRole="button" accessibilityLabel="Send" onPress={() => ask(text)} disabled={!text.trim() || busy}
            style={{ width: 52, height: 52, borderRadius: 26, backgroundColor: colors.lime, alignItems: 'center', justifyContent: 'center', opacity: text.trim() && !busy ? 1 : 0.45 }}>
            <Ionicons name="arrow-up" size={26} color="#07080B" />
          </Pressable>
        </View>
      )}
    </KeyboardAvoidingView>
  );
}
