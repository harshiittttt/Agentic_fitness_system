import React, { useEffect, useRef, useState } from 'react';
import { Animated, KeyboardAvoidingView, Platform, Pressable, ScrollView, TextInput, View } from 'react-native';
import { LinearGradient } from 'expo-linear-gradient';
import { Ionicons } from '@expo/vector-icons';
import * as Haptics from '../haptics';
import { onboard } from '../api';
import { colors, font, gradients } from '../theme';
import { Bar, Card, FadeIn, GradientButton, Orb, Ring, Txt } from '../ui';
import PersonaPicker from '../PersonaPicker';
import { Choice, Label, Stepper } from '../forms';
import { PersonaId, personaById } from '../personas';
import { stopSpeaking } from '../tts';
import { Pill } from '../ux/controls';
import SwapSheet from '../SwapSheet';

const STEPS = 8; // 0 welcome, 1 about you, 2 goal, 3 training, 4 equipment, 5 injuries, 6 coach, 7 confirm, then build -> reveal

const SPLITS: Record<number, string> = { 3: 'Full body each session', 4: 'Upper / Lower split', 5: 'Push / Pull / Legs + Upper / Lower' };
const INJURIES = ['shoulder', 'lower back', 'knee', 'wrist', 'elbow', 'neck', 'hip'];
const EQUIPMENT = [
  { k: 'barbell', title: 'Barbell and rack', sub: 'Squat, bench press, deadlift', icon: 'barbell', color: colors.lime },
  { k: 'dumbbell', title: 'Dumbbells', sub: 'Presses, rows, lunges, curls', icon: 'barbell-outline', color: colors.cyan },
  { k: 'machine', title: 'Machines', sub: 'Leg press, leg curl, chest supported rows', icon: 'cog', color: colors.violet },
  { k: 'cable', title: 'Cables', sub: 'Pulldowns, pushdowns, face pulls', icon: 'git-network', color: colors.amber },
  { k: 'bodyweight', title: 'Bodyweight', sub: 'Pull ups, push ups, planks', icon: 'body', color: colors.mint },
] as const;
const STEP_NAMES = ['Welcome', 'About you', 'Your goal', 'Your training', 'Equipment', 'Protect', 'Your coach', 'Review'];
const ALL_EQ: string[] = EQUIPMENT.map((e) => e.k);
const BUILD_LINES = ['Reading your goal and schedule', 'Choosing exercises that fit your equipment', 'Keeping your problem areas safe', 'Setting your calories and protein'];
const goalWord = (g: string) => (g === 'bulk' ? 'Build muscle' : g === 'cut' ? 'Lose fat' : 'Stay fit');
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

export default function Onboarding({ onDone }: { onDone: () => void }) {
  const [step, setStep] = useState(0);
  const [sex, setSex] = useState<'m' | 'f'>('m');
  const [age, setAge] = useState(28); const [height, setHeight] = useState(175); const [weight, setWeight] = useState(75);
  const [goal, setGoal] = useState<'bulk' | 'cut' | 'maintain'>('maintain');
  const [exp, setExp] = useState<'beginner' | 'intermediate' | 'advanced'>('intermediate');
  const [days, setDays] = useState(4);
  const [equipment, setEquipment] = useState<string[]>(ALL_EQ);
  const [injuries, setInjuries] = useState<string[]>([]);
  const [name, setName] = useState('');
  const [persona, setPersona] = useState<PersonaId>('hype');
  const [building, setBuilding] = useState(false);
  const [buildLine, setBuildLine] = useState(0);
  const [plan, setPlan] = useState<any>(null);
  const [error, setError] = useState('');
  const [swapping, setSwapping] = useState<{ day: number; name: string } | null>(null);

  const fade = useRef(new Animated.Value(0)).current;
  const scroller = useRef<ScrollView>(null);
  useEffect(() => {
    fade.setValue(0);
    Animated.timing(fade, { toValue: 1, duration: 320, useNativeDriver: true }).start();
    scroller.current?.scrollTo({ y: 0, animated: false });
  }, [step, !!plan, building]);
  useEffect(() => {
    if (!building) return;
    setBuildLine(0);
    const t = setInterval(() => setBuildLine((i) => Math.min(i + 1, BUILD_LINES.length - 1)), 1600);
    return () => clearInterval(t);
  }, [building]);

  const toggleInjury = (i: string) => setInjuries((l) => (l.includes(i) ? l.filter((x) => x !== i) : [...l, i]));
  const toggleEq = (k: string) => setEquipment((l) => (l.includes(k) ? l.filter((x) => x !== k) : [...l, k]));
  const fullGym = ALL_EQ.every((k) => equipment.includes(k));
  const eqSummary = fullGym ? 'Full gym' : equipment.length ? equipment.map(cap).join(', ') : 'Nothing picked';

  // Back from the reveal to a step with every answer kept; Build my plan then replaces this plan on the server.
  const changePlan = (to: number) => { Haptics.selectionAsync(); setPlan(null); setStep(to); };

  const build = async () => {
    setBuilding(true); setError('');
    try {
      // A full gym is sent as an empty list, which the plan builder reads as "no limits".
      const p = await onboard({ age, sex, height_cm: height, weight_kg: weight, goal, days_per_week: days, experience: exp, injuries, name: name.trim(), coach_persona: persona, equipment: fullGym ? [] : equipment });
      Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
      setPlan(p);
    } catch { setError("We couldn't reach your coach's server, so nothing was saved. Check your connection and try again."); }
    setBuilding(false);
  };

  const head = (title: string, sub: string) => (
    <View style={{ gap: 6, marginBottom: 8 }}>
      <Txt accessibilityRole="header" style={{ fontFamily: font.black, fontSize: 30, letterSpacing: -0.6, lineHeight: 36 }}>{title}</Txt>
      <Txt style={{ color: colors.dim, fontSize: 15.5, lineHeight: 23 }}>{sub}</Txt>
    </View>
  );

  // ---------- building ----------
  if (building) {
    return (
      <Animated.View style={{ flex: 1, opacity: fade, alignItems: 'center', justifyContent: 'center', padding: 28, gap: 18 }}>
        <Orb state="thinking" size={170} />
        <Txt style={{ fontFamily: font.black, fontSize: 26, textAlign: 'center', marginTop: -24 }}>Building your plan</Txt>
        <Txt accessibilityLiveRegion="polite" style={{ color: colors.dim, fontSize: 15.5, textAlign: 'center', lineHeight: 23, minHeight: 46 }}>{BUILD_LINES[buildLine]}…</Txt>
        <View accessibilityLabel={`Step ${buildLine + 1} of ${BUILD_LINES.length}`} style={{ flexDirection: 'row', gap: 6 }}>
          {BUILD_LINES.map((_, i) => <View key={i} style={{ width: i === buildLine ? 22 : 8, height: 8, borderRadius: 4, backgroundColor: i <= buildLine ? colors.lime : 'rgba(255,255,255,0.14)' }} />)}
        </View>
        <Txt style={{ color: colors.faint, fontSize: 13, textAlign: 'center' }}>This can take up to half a minute.</Txt>
      </Animated.View>
    );
  }

  // ---------- reveal ----------
  if (plan) {
    const n = plan.nutrition;
    return (
      <Animated.View style={{ flex: 1, opacity: fade }}>
        <ScrollView contentContainerStyle={{ padding: 22, gap: 16, paddingBottom: 30 }}>
          <FadeIn style={{ gap: 12 }}>
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
              <Ionicons name="checkmark-circle" size={18} color={colors.lime} />
              <Txt style={{ color: colors.lime, fontFamily: font.bold, fontSize: 12, letterSpacing: 1.2 }}>{name.trim() ? `READY, ${name.trim().toUpperCase()}` : 'ALL SET'}</Txt>
            </View>
            {head('Your plan is ready', 'Built from your goal, schedule and equipment. It adapts as you train.')}
          </FadeIn>
          <FadeIn delay={90}>
            <LinearGradient colors={gradients.hero} start={{ x: 0, y: 0 }} end={{ x: 1, y: 1 }} style={{ borderRadius: 28, padding: 22, gap: 4 }}>
              <Txt style={{ color: 'rgba(255,255,255,0.75)', fontFamily: font.semibold, fontSize: 12, letterSpacing: 1.2 }}>TRAINING</Txt>
              <Txt style={{ fontFamily: font.black, fontSize: 26 }}>{plan.days.length} days / week</Txt>
              <Txt style={{ color: 'rgba(255,255,255,0.85)', lineHeight: 20 }}>{plan.days.map((d: any) => d.name).join(' · ')}</Txt>
            </LinearGradient>
          </FadeIn>
          <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
            <Ionicons name="swap-horizontal" size={16} color={colors.dim} />
            <Txt style={{ flex: 1, color: colors.dim, fontSize: 13.5, lineHeight: 20 }}>Tap any exercise to swap it for a safe alternative. Swaps here are permanent.</Txt>
          </View>
          {plan.days.map((d: any, di: number) => (
            <FadeIn key={d.name} delay={150 + Math.min(di, 5) * 60}>
            <Card style={{ gap: 2, padding: 14 }}>
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10, marginBottom: 2 }}>
                <View style={{ width: 30, height: 30, borderRadius: 10, backgroundColor: colors.lime + '22', alignItems: 'center', justifyContent: 'center' }}>
                  <Txt style={{ fontFamily: font.black, fontSize: 13, color: colors.lime }}>{di + 1}</Txt>
                </View>
                <Txt style={{ fontFamily: font.bold, fontSize: 16, flex: 1 }}>{d.name}</Txt>
                <Txt style={{ color: colors.faint, fontSize: 12 }}>{d.exercises.length} exercises</Txt>
              </View>
              {d.exercises.map((e: any) => (
                <Pressable key={e.name} accessibilityRole="button" accessibilityLabel={`Swap ${e.name}`} onPress={() => setSwapping({ day: di, name: e.name })}
                  style={({ pressed }) => ({ flexDirection: 'row', alignItems: 'center', gap: 10, minHeight: 44, paddingVertical: 4, borderTopWidth: 1, borderTopColor: colors.border, opacity: pressed ? 0.6 : 1 })}>
                  <Txt style={{ flex: 1, color: colors.text, fontSize: 14.5, lineHeight: 20, textTransform: 'capitalize' }}>{e.name}</Txt>
                  <Ionicons name="swap-horizontal" size={20} color={colors.dim} />
                </Pressable>
              ))}
            </Card>
            </FadeIn>
          ))}
          {plan.excluded?.length > 0 && (
            <Card style={{ borderColor: 'rgba(255,184,77,0.4)', flexDirection: 'row', gap: 12 }}>
              <Ionicons name="shield-checkmark" size={22} color={colors.amber} />
              <Txt style={{ flex: 1, color: colors.dim, lineHeight: 20, fontSize: 13 }}>
                <Txt style={{ color: colors.amber, fontFamily: font.bold }}>Protected your {injuries.join(' & ')}. </Txt>
                Skipped: {plan.excluded.join(', ')}.
              </Txt>
            </Card>
          )}
          <Card style={{ gap: 14 }}>
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 18 }}>
              <Ring size={96} stroke={10} progress={1} from={colors.amber} to={colors.pink}>
                <Txt style={{ fontFamily: font.black, fontSize: 20 }}>{n.calories}</Txt>
                <Txt style={{ color: colors.dim, fontSize: 11 }}>kcal/day</Txt>
              </Ring>
              <Txt style={{ flex: 1, color: colors.dim, fontSize: 13.5, lineHeight: 20 }}>Daily target for {goal === 'bulk' ? 'building muscle' : goal === 'cut' ? 'losing fat' : 'staying where you are'}.</Txt>
            </View>
            {[['Protein', n.protein_g, colors.pink], ['Carbs', n.carbs_g, colors.cyan], ['Fat', n.fat_g, colors.amber]].map(([l, v, c]: any) => (
              <View key={l} style={{ gap: 5 }}>
                <View style={{ flexDirection: 'row', justifyContent: 'space-between' }}>
                  <Txt style={{ fontFamily: font.semibold, fontSize: 14 }}>{l}</Txt><Txt style={{ color: colors.dim, fontSize: 13 }}>{v} g</Txt>
                </View>
                <Bar value={Math.min(1, v / Math.max(n.protein_g, n.carbs_g, n.fat_g))} color={c} />
              </View>
            ))}
          </Card>
          <Card style={{ gap: 4, padding: 0, overflow: 'hidden' }}>
            <Txt style={{ color: colors.dim, fontSize: 13, paddingHorizontal: 16, paddingTop: 12 }}>Not quite right? Change it and rebuild.</Txt>
            {[['Days per week', `${days} days`, 3], ['Goal', goalWord(goal), 2], ['Equipment', eqSummary, 4], ['Injuries to protect', injuries.length ? injuries.map(cap).join(', ') : 'Nothing', 5]].map(([k, v, to]: any, i) => (
              <Pressable key={k} accessibilityRole="button" accessibilityLabel={`Change ${k}`} onPress={() => changePlan(to)}
                style={({ pressed }) => ({ flexDirection: 'row', alignItems: 'center', gap: 12, minHeight: 52, paddingVertical: 10, paddingHorizontal: 16, borderTopWidth: i ? 1 : 0, borderTopColor: colors.border, opacity: pressed ? 0.6 : 1 })}>
                <Txt style={{ fontFamily: font.semibold, fontSize: 14.5, flex: 1 }}>{k}</Txt>
                <Txt style={{ color: colors.dim, fontSize: 13.5, flexShrink: 1, textAlign: 'right' }}>{v}</Txt>
                <Ionicons name="chevron-forward" size={18} color={colors.faint} />
              </Pressable>
            ))}
          </Card>
        </ScrollView>
        <SwapSheet permanent exercise={swapping?.name ?? null} dayIndex={swapping?.day ?? 0} onClose={() => setSwapping(null)} onSwapped={setPlan} />
        <View style={{ padding: 20, paddingTop: 8, gap: 8 }}>
          <GradientButton label="Start training" icon="flash" onPress={onDone} />
          <Txt style={{ color: colors.faint, fontSize: 12.5, textAlign: 'center' }}>You can swap exercises on the Today tab and change your goal, days or equipment in Setup any time.</Txt>
        </View>
      </Animated.View>
    );
  }

  // ---------- steps ----------
  const edit = (to: number) => () => { Haptics.selectionAsync(); setStep(to); };
  const body = [
    // 0 welcome
    <View key="0" style={{ flex: 1, justifyContent: 'center', gap: 22 }}>
      <LinearGradient colors={gradients.lime} style={{ width: 76, height: 76, borderRadius: 26, alignItems: 'center', justifyContent: 'center' }}>
        <Ionicons name="sparkles" size={38} color="#07080B" />
      </LinearGradient>
      {head('Meet your AI coaching team', 'Four specialists work together on your training, nutrition and recovery, and adapt as you do. Setup takes about a minute.')}
      {[['barbell', 'Training', 'Plans and adjusts your lifting', colors.lime], ['nutrition', 'Nutrition', 'Targets that follow your goal', colors.amber],
        ['moon', 'Recovery', 'Protects you when you are run down', colors.violet], ['stats-chart', 'Analyst', 'Spots plateaus before you do', colors.cyan]].map(([i, t, s, c]: any) => (
        <View key={t} style={{ flexDirection: 'row', alignItems: 'center', gap: 14 }}>
          <View style={{ width: 44, height: 44, borderRadius: 14, backgroundColor: c + '22', alignItems: 'center', justifyContent: 'center' }}><Ionicons name={i} size={22} color={c} /></View>
          <View style={{ flex: 1 }}><Txt style={{ fontFamily: font.bold, fontSize: 16 }}>{t}</Txt><Txt style={{ color: colors.dim, fontSize: 14 }}>{s}</Txt></View>
        </View>
      ))}
      <View style={{ flexDirection: 'row', gap: 8, alignItems: 'center' }}>
        <Ionicons name="lock-closed" size={14} color={colors.faint} />
        <Txt style={{ flex: 1, color: colors.faint, fontSize: 12.5, lineHeight: 18 }}>Your answers stay with your coach server. Nothing is posted anywhere.</Txt>
      </View>
    </View>,
    // 1 about you
    <View key="1" style={{ gap: 14 }}>
      {head('About you', 'Used to work out your calories and protein.')}
      <View>
        <Label>What should your coach call you?</Label>
        <TextInput value={name} onChangeText={setName} placeholder="Your first name (optional)" placeholderTextColor={colors.faint} maxLength={30}
          autoCapitalize="words" autoComplete="given-name" returnKeyType="done" accessibilityLabel="Your name"
          style={{ backgroundColor: colors.card, color: colors.text, borderRadius: 18, paddingHorizontal: 18, paddingVertical: 15, fontFamily: font.semibold, fontSize: 17, borderWidth: 1, borderColor: colors.border }} />
      </View>
      <View>
        <Label>Sex (for calorie maths)</Label>
        <View style={{ flexDirection: 'row', gap: 10 }}>
          <Pill flex label="Male" active={sex === 'm'} onPress={() => setSex('m')} /><Pill flex label="Female" active={sex === 'f'} onPress={() => setSex('f')} />
        </View>
      </View>
      <Stepper label="Age" value={age} unit="yrs" min={14} max={80} onChange={setAge} />
      <Stepper label="Height" value={height} unit="cm" min={130} max={220} onChange={setHeight} />
      <Stepper label="Weight" value={weight} unit="kg" step={0.5} min={35} max={200} onChange={setWeight} />
    </View>,
    // 2 goal
    <View key="2" style={{ gap: 12 }}>
      {head("What's your goal?", 'You can change this any time in Profile.')}
      <Choice icon="trending-up" title="Build muscle" sub="Eat a little above maintenance, lift progressively" selected={goal === 'bulk'} onPress={() => setGoal('bulk')} />
      <Choice icon="flame" title="Lose fat" sub="A moderate deficit with high protein to keep muscle" selected={goal === 'cut'} onPress={() => setGoal('cut')} color={colors.pink} />
      <Choice icon="heart" title="Stay fit" sub="Maintain weight, get stronger and healthier" selected={goal === 'maintain'} onPress={() => setGoal('maintain')} color={colors.cyan} />
    </View>,
    // 3 experience + schedule
    <View key="3" style={{ gap: 12 }}>
      {head('Your training', 'Sets your starting volume and split.')}
      <Choice icon="leaf" title="Beginner" sub="Under a year of consistent lifting" selected={exp === 'beginner'} onPress={() => setExp('beginner')} color={colors.mint} />
      <Choice icon="barbell" title="Intermediate" sub="1 to 3 years" selected={exp === 'intermediate'} onPress={() => setExp('intermediate')} />
      <Choice icon="trophy" title="Advanced" sub="3+ years, comfortable near failure" selected={exp === 'advanced'} onPress={() => setExp('advanced')} color={colors.amber} />
      <View style={{ marginTop: 6 }}>
        <Label>Days per week</Label>
        <View style={{ flexDirection: 'row', gap: 10 }}>
          {[3, 4, 5].map((d) => <Pill flex key={d} label={`${d} days`} active={days === d} onPress={() => setDays(d)} color={colors.cyan} />)}
        </View>
        <Txt style={{ color: colors.dim, fontSize: 14, marginTop: 10 }}>Usually: {SPLITS[days].toLowerCase()}. Your coach fits it to you.</Txt>
      </View>
    </View>,
    // 4 equipment
    <View key="4" style={{ gap: 12 }}>
      {head('What can you train with?', 'Your plan only uses equipment you have. Tap everything you can use.')}
      <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 10 }}>
        <Pill label="Full gym" active={fullGym} onPress={() => setEquipment(ALL_EQ)} />
        <Pill label="Home dumbbells" active={!fullGym && equipment.length === 2 && equipment.includes('dumbbell') && equipment.includes('bodyweight')} onPress={() => setEquipment(['dumbbell', 'bodyweight'])} color={colors.cyan} />
        <Pill label="Bodyweight only" active={!fullGym && equipment.length === 1 && equipment[0] === 'bodyweight'} onPress={() => setEquipment(['bodyweight'])} color={colors.mint} />
      </View>
      {EQUIPMENT.map((e) => <Choice key={e.k} icon={e.icon} title={e.title} sub={e.sub} color={e.color} selected={equipment.includes(e.k)} onPress={() => toggleEq(e.k)} />)}
      {equipment.length === 0 && <Txt style={{ color: colors.amber, fontSize: 14 }}>Pick at least one so we can build your plan.</Txt>}
    </View>,
    // 5 injuries
    <View key="5" style={{ gap: 14 }}>
      {head('Anything to protect?', 'Tap any problem areas. Your plan will skip movements that load them.')}
      <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 10 }}>
        {INJURIES.map((i) => <Pill key={i} label={cap(i)} active={injuries.includes(i)} onPress={() => toggleInjury(i)} color={colors.amber} />)}
      </View>
      <Txt style={{ color: colors.dim, fontSize: 14 }}>{injuries.length ? `Protecting: ${injuries.join(', ')}.` : 'Nothing hurts? Great. Just continue.'}</Txt>
    </View>,
    // 6 coach
    <View key="6" style={{ gap: 14 }}>
      {head('Pick your coach', 'They talk you through every session. Tap one to hear them.')}
      <PersonaPicker value={persona} onChange={setPersona} />
    </View>,
    // 7 confirm
    <View key="7" style={{ gap: 12 }}>
      {head('Ready to build', 'Check this over. Tap any line to change it.')}
      <Card style={{ padding: 0, gap: 0, overflow: 'hidden' }}>
        {[[1, 'Body', `${name.trim() ? name.trim() + ', ' : ''}${sex === 'm' ? 'male' : 'female'}, ${age} yrs, ${height} cm, ${weight} kg`],
          [2, 'Goal', goalWord(goal)],
          [3, 'Training', `${cap(exp)}, ${days} days a week`],
          [4, 'Equipment', eqSummary],
          [5, 'Protecting', injuries.length ? injuries.map(cap).join(', ') : 'Nothing'],
          [6, 'Coach', personaById(persona).name]].map(([to, k, v], i) => (
          <Pressable key={k as string} accessibilityRole="button" accessibilityLabel={`Change ${k}`} onPress={edit(to as number)}
            style={({ pressed }) => ({ flexDirection: 'row', alignItems: 'center', gap: 12, minHeight: 56, paddingVertical: 12, paddingHorizontal: 16, borderTopWidth: i ? 1 : 0, borderTopColor: colors.border, opacity: pressed ? 0.6 : 1 })}>
            <Txt style={{ color: colors.dim, width: 84, fontSize: 14 }}>{k as string}</Txt>
            <Txt style={{ fontFamily: font.semibold, flex: 1, fontSize: 15, lineHeight: 21 }}>{v as string}</Txt>
            <Ionicons name="chevron-forward" size={18} color={colors.faint} />
          </Pressable>
        ))}
      </Card>
      {!!error && (
        <Card style={{ borderColor: colors.red + '66', flexDirection: 'row', gap: 10 }}>
          <Ionicons name="cloud-offline" size={22} color={colors.red} />
          <Txt style={{ flex: 1, color: colors.text, lineHeight: 21, fontSize: 14.5 }}>{error}</Txt>
        </Card>
      )}
    </View>,
  ];

  const last = step === STEPS - 1;
  const blocked = step === 4 && equipment.length === 0;
  return (
    <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined} style={{ flex: 1 }}>
      <View style={{ paddingHorizontal: 10, paddingTop: 8, flexDirection: 'row', alignItems: 'center', gap: 6, height: 64 }}>
        {step > 0 ? (
          <Pressable accessibilityRole="button" accessibilityLabel="Back to the previous step" onPress={() => { stopSpeaking(); setStep(step - 1); }} style={{ width: 48, height: 48, alignItems: 'center', justifyContent: 'center' }}>
            <Ionicons name="chevron-back" size={28} color={colors.text} />
          </Pressable>
        ) : <View style={{ width: 48 }} />}
        <View style={{ flex: 1, gap: 6 }}>
          <Txt numberOfLines={1} maxFontSizeMultiplier={1.3} style={{ color: colors.dim, fontSize: 12, letterSpacing: 0.8, fontFamily: font.semibold }}>{STEP_NAMES[step].toUpperCase()}</Txt>
          <View accessibilityRole="progressbar" accessibilityLabel={`Step ${step + 1} of ${STEPS}, ${STEP_NAMES[step]}`} accessibilityValue={{ min: 1, max: STEPS, now: step + 1 }} style={{ flexDirection: 'row', gap: 4 }}>
            {Array.from({ length: STEPS }, (_, i) => <View key={i} style={{ flex: 1, height: 5, borderRadius: 3, backgroundColor: i <= step ? colors.lime : 'rgba(255,255,255,0.10)' }} />)}
          </View>
        </View>
        <Txt maxFontSizeMultiplier={1.3} style={{ color: colors.dim, fontSize: 13, fontFamily: font.semibold, minWidth: 48, textAlign: 'center' }}>{step + 1} of {STEPS}</Txt>
      </View>
      <Animated.View style={{ flex: 1, opacity: fade, transform: [{ translateY: fade.interpolate({ inputRange: [0, 1], outputRange: [14, 0] }) }] }}>
        <ScrollView ref={scroller} keyboardShouldPersistTaps="handled" contentContainerStyle={{ padding: 22, paddingBottom: 12, flexGrow: 1 }} showsVerticalScrollIndicator={false}>{body[step]}</ScrollView>
      </Animated.View>
      <View style={{ padding: 20, paddingTop: 8, opacity: blocked ? 0.4 : 1 }} pointerEvents={blocked ? 'none' : 'auto'}>
        <GradientButton label={step === 0 ? 'Get started' : last ? (error ? 'Try again' : 'Build my plan') : 'Continue'} icon={last ? 'rocket' : undefined}
          onPress={() => { stopSpeaking(); if (last) build(); else setStep(step + 1); }} />
      </View>
    </KeyboardAvoidingView>
  );
}
