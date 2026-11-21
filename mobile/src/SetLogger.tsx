import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, Pressable, TextInput, View } from 'react-native';
import { LinearGradient } from 'expo-linear-gradient';
import { Ionicons } from '@expo/vector-icons';
import * as Haptics from './haptics';
import { LoggedSet, Prefill, deleteSet, getPrefill, getSessionSets, getWorkoutSummary, logSet } from './api';
import { Sheet } from './forms';
import { colors, font, gradients } from './theme';
import { FadeIn, Txt } from './ui';
import { Nudge } from './ux/controls';
import WorkoutSummary from './ux/WorkoutSummary';

export type LogTarget = { name: string; sets: number; reps: number; target_rpe?: number };

// Lifts where the number on the bar is EXTRA weight (0 = just your bodyweight), and holds counted in seconds.
const BODYWEIGHT = new Set(['pull up', 'push up', 'dips', 'plank', 'hanging leg raise', 'back extension']);
const HOLD = new Set(['plank']);
const STEPS = [1.25, 2.5, 5, 10];
const RPE: { v: number; hint: string }[] = [{ v: 6, hint: 'Easy' }, { v: 7, hint: 'Solid' }, { v: 8, hint: 'Hard' }, { v: 9, hint: 'Grind' }, { v: 10, hint: 'Max' }];

const fmt = (n: number) => String(+n.toFixed(2));
const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, n));

/** Label, big editable number, −/+ either side. Tap the number to type an exact value. */
export function Dial({ label, unit, value, onChange, step, min, max, decimals }: {
  label: string; unit: string; value: number; onChange: (n: number) => void; step: number; min: number; max: number; decimals: boolean;
}) {
  const [text, setText] = useState(fmt(value));
  const editing = useRef(false);
  useEffect(() => { if (!editing.current) setText(fmt(value)); }, [value]);
  const commit = () => {
    editing.current = false;
    const n = parseFloat(text.replace(',', '.'));
    const v = isNaN(n) ? value : clamp(decimals ? n : Math.round(n), min, max);
    onChange(v); setText(fmt(v));
  };
  const nudge = (d: number) => onChange(clamp(+(value + d).toFixed(2), min, max));
  return (
    <View style={{ gap: 6 }}>
      <Txt style={{ color: colors.dim, fontSize: 12, letterSpacing: 1.2, fontFamily: font.bold }}>{label.toUpperCase()}</Txt>
      <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
        <Nudge icon="remove" label={`Less ${label.toLowerCase()}`} onStep={() => nudge(-step)} />
        <View style={{ alignItems: 'center', flex: 1 }}>
          <TextInput value={text} onChangeText={(x) => { editing.current = true; setText(x.replace(/[^0-9.,]/g, '')); }} onBlur={commit} onSubmitEditing={commit}
            accessibilityLabel={`${label} in ${unit}`} keyboardType="decimal-pad" selectTextOnFocus returnKeyType="done"
            style={{ color: colors.text, fontFamily: font.black, fontSize: 46, letterSpacing: -1.5, textAlign: 'center', minWidth: 130, padding: 0 }} />
          <Txt style={{ color: colors.dim, fontFamily: font.semibold, fontSize: 13, marginTop: -2 }}>{unit}</Txt>
        </View>
        <Nudge icon="add" label={`More ${label.toLowerCase()}`} onStep={() => nudge(step)} />
      </View>
    </View>
  );
}

export default function SetLogger({ target, done, session, onClose, onLogged, onUndone }: {
  target: LogTarget | null; done: number; session: string; onClose: () => void; onLogged: (result: any, loggedWeight: number) => void;
  /** Called after a set is removed, so the screen behind can lower its own count. */
  onUndone?: (exercise: string, remaining: number) => void;
}) {
  const visible = !!target;
  const name = target?.name ?? '';
  const bw = BODYWEIGHT.has(name.toLowerCase());
  const hold = HOLD.has(name.toLowerCase());
  const [pre, setPre] = useState<Prefill | null>(null);
  const [loading, setLoading] = useState(false);
  const [weight, setWeight] = useState(0);
  const [reps, setReps] = useState(5);
  const [rpe, setRpe] = useState<number | null>(null);
  const [step, setStep] = useState(2.5);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; tone: 'ok' | 'warn'; undo?: boolean } | null>(null);
  const [logged, setLogged] = useState<LoggedSet[] | null>(null);   // this lift, today, from the server (null = not loaded / offline)
  const [queued, setQueued] = useState(0);                          // sets saved on the phone only, not undoable until they sync
  const [workoutDone, setWorkoutDone] = useState(false);
  const [summary, setSummary] = useState(false);
  const total = target?.sets ?? 3;
  const count = (logged ? logged.length : done) + queued;
  const setNo = count + 1;
  const finished = setNo > total;

  const refresh = useCallback(async () => {
    if (!target) return;
    try { setLogged(await getSessionSets(session, target.name)); } catch { /* offline: fall back to the count we were given */ }
    getWorkoutSummary(session).then((s) => setWorkoutDone(s.finished)).catch(() => {});
  }, [target?.name, session]);

  // Open: start from your last set (or the autoregulated suggestion), never from a made-up number.
  useEffect(() => {
    if (!target) return;
    setRpe(null); setMsg(null); setPre(null); setLoading(true); setLogged(null); setQueued(0); setSummary(false); setWorkoutDone(false);
    setReps(target.reps); setWeight(bw ? 0 : 20); setStep(hold ? 5 : 2.5);
    let live = true;
    refresh();
    getPrefill(target.name, target.reps, target.target_rpe ?? 8).then((p) => {
      if (!live) return;
      setPre(p); setStep(hold ? 5 : p.increment || 2.5);
      const w = p.suggest?.weight ?? p.last?.weight;
      if (w != null) setWeight(+w);
      setReps(p.suggest?.reps ?? p.last?.reps ?? target.reps);
    }).catch(() => {}).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [target?.name]);

  const submit = async () => {
    if (!target || busy) return;
    setBusy(true); setMsg(null);
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium);
    try {
      const r = await logSet(session, target.name, weight, reps, rpe ?? undefined);
      onLogged(r, weight);
      const wasLast = setNo >= total;
      if (r.queued) setQueued((q) => q + 1);
      if (r.safety_flag) { setMsg({ text: 'Pain noted. Skip this movement and tell your coach where it hurts.', tone: 'warn' }); }
      else if (r.queued) { setMsg({ text: 'Saved on your phone. It will sync when you are back online.', tone: 'ok' }); }
      else if (wasLast) { setMsg({ text: 'Logged. That was your last set of this lift.', tone: 'ok', undo: true }); }
      else if (r.next_set) { setMsg({ text: `Logged. Next: ${fmt(r.next_set.weight)} kg × ${r.next_set.reps}. ${r.next_set.reason}`, tone: 'ok', undo: true }); }
      else setMsg({ text: 'Logged.', tone: 'ok', undo: true });
      if (r.next_set) { setWeight(+r.next_set.weight); setReps(r.next_set.reps); }
      setRpe(null);
      if (!r.queued) await refresh();
    } catch (e: any) { setMsg({ text: e?.name === 'ApiError' ? `Could not save that set: ${e.message}` : 'Could not save that set. Try again.', tone: 'warn' }); }
    setBusy(false);
  };

  /** Remove a set and put its numbers back in the dials, so a wrong tap is fixed with one tap and a re-log. */
  const undo = async (s: LoggedSet) => {
    if (busy) return;
    setBusy(true);
    try {
      await deleteSet(s.id);
      Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning);
      if (s.weight != null) setWeight(+s.weight);
      if (s.reps) setReps(s.reps);
      setRpe(s.rpe ?? null);
      setMsg({ text: `Removed ${fmt(+(s.weight ?? 0))} kg × ${s.reps}. Fix the numbers and log it again.`, tone: 'ok' });
      const rest = (logged ?? []).filter((x) => x.id !== s.id);
      setLogged(rest); onUndone?.(s.exercise, rest.length);
      getWorkoutSummary(session).then((x) => setWorkoutDone(x.finished)).catch(() => {});
    } catch { setMsg({ text: 'Could not undo that set. Check your connection and try again.', tone: 'warn' }); }
    setBusy(false);
  };

  const hint = pre?.last
    ? `Last time: ${bw && !+pre.last.weight ? 'bodyweight' : `${fmt(+pre.last.weight)} kg`} × ${pre.last.reps}${pre.last.rpe ? ` @ RPE ${pre.last.rpe}` : ''}`
    : pre ? (bw ? 'Add weight only if you want to. 0 is just your bodyweight.' : 'First time on this lift: pick a weight you could do for a few more reps.')
    : loading ? '' : 'Offline: set your weight by hand.';

  const cta = (label: string, onPress: () => void, icon: string, disabled?: boolean) => (
    <Pressable accessibilityRole="button" accessibilityLabel={label} onPress={onPress} disabled={disabled} style={({ pressed }) => ({ opacity: pressed || disabled ? 0.8 : 1, transform: [{ scale: pressed ? 0.98 : 1 }] })}>
      <LinearGradient colors={gradients.lime} start={{ x: 0, y: 0 }} end={{ x: 1, y: 1 }} style={{ borderRadius: 20, minHeight: 60, flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 10, paddingHorizontal: 12 }}>
        {busy ? <ActivityIndicator color="#07080B" /> : <Ionicons name={icon as any} size={22} color="#07080B" />}
        <Txt numberOfLines={1} style={{ color: '#07080B', fontFamily: font.black, fontSize: 17, flexShrink: 1 }}>{label}</Txt>
      </LinearGradient>
    </Pressable>
  );

  const lastSet = logged && logged.length ? logged[logged.length - 1] : null;

  return (
    <Sheet visible={visible} onClose={onClose} title={summary ? 'Workout complete' : name ? name.replace(/\b\w/g, (c) => c.toUpperCase()) : ''}
      footer={summary ? null : finished
        ? cta(workoutDone ? 'Finish workout' : `All ${total} sets done`, workoutDone ? () => setSummary(true) : onClose, workoutDone ? 'trophy' : 'checkmark')
        : cta(`Log set ${setNo} of ${total}`, submit, 'checkmark-circle', busy || loading)}>
      {summary ? <WorkoutSummary session={session} onDone={onClose} /> : (
        <>
          <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
            <View style={{ backgroundColor: 'rgba(198,255,61,0.14)', borderRadius: 999, paddingHorizontal: 12, paddingVertical: 6 }}>
              <Txt style={{ color: colors.lime, fontFamily: font.bold, fontSize: 13 }}>Target {total} × {target?.reps}{hold ? ' s' : ''}{target?.target_rpe ? ` @ RPE ${target.target_rpe}` : ''}</Txt>
            </View>
            <View accessible accessibilityRole="progressbar" accessibilityLabel={`${Math.min(count, total)} of ${total} sets logged`} style={{ flexDirection: 'row', gap: 5, alignItems: 'center' }}>
              {Array.from({ length: Math.min(total, 12) }).map((_, k) => (
                <View key={k} style={{ width: 10, height: 10, borderRadius: 5, backgroundColor: k < count ? colors.lime : k === count && !finished ? 'transparent' : 'rgba(255,255,255,0.14)', borderWidth: k === count && !finished ? 2 : 0, borderColor: colors.lime }} />
              ))}
            </View>
            {loading ? <ActivityIndicator size="small" color={colors.lime} /> : !!hint && <Txt style={{ color: colors.dim, fontSize: 13.5, flexShrink: 1 }}>{hint}</Txt>}
          </View>

          {!!msg && (
            <FadeIn key={msg.text} distance={6} style={{ flexDirection: 'row', alignItems: 'center', gap: 8, padding: 12, borderRadius: 16, backgroundColor: (msg.tone === 'ok' ? colors.mint : colors.red) + '1C', borderWidth: 1, borderColor: (msg.tone === 'ok' ? colors.mint : colors.red) + '55' }}>
              <Ionicons name={msg.tone === 'ok' ? 'checkmark-circle' : 'alert-circle'} size={20} color={msg.tone === 'ok' ? colors.mint : colors.red} />
              <Txt accessibilityLiveRegion="polite" style={{ flex: 1, color: msg.tone === 'ok' ? colors.mint : colors.red, fontFamily: font.semibold, fontSize: 14, lineHeight: 20 }}>{msg.text}</Txt>
              {msg.undo && lastSet && (
                <Pressable accessibilityRole="button" accessibilityLabel="Wrong set? Undo it" onPress={() => undo(lastSet)} disabled={busy}
                  style={({ pressed }) => ({ minHeight: 48, minWidth: 76, paddingHorizontal: 14, borderRadius: 14, borderWidth: 1.5, borderColor: colors.amber, alignItems: 'center', justifyContent: 'center', flexDirection: 'row', gap: 6, opacity: pressed || busy ? 0.6 : 1 })}>
                  <Ionicons name="arrow-undo" size={18} color={colors.amber} />
                  <Txt style={{ color: colors.amber, fontFamily: font.bold, fontSize: 15 }}>Undo</Txt>
                </Pressable>
              )}
            </FadeIn>
          )}

          {finished && (
            <View style={{ alignItems: 'center', gap: 4, paddingVertical: 6 }}>
              <Ionicons name="checkmark-circle" size={44} color={colors.lime} />
              <Txt style={{ fontFamily: font.black, fontSize: 20 }}>All {total} sets logged</Txt>
              <Txt style={{ color: colors.dim, fontSize: 14, textAlign: 'center' }}>{workoutDone ? 'That was the last of the workout.' : 'Pick your next exercise on the Today screen.'}</Txt>
            </View>
          )}

          {!finished && (
            <>
              <Dial label={bw ? 'Added weight' : 'Weight'} unit="kg" value={weight} onChange={setWeight} step={step} min={0} max={600} decimals />
              <View style={{ flexDirection: 'row', gap: 8, justifyContent: 'center' }}>
                {STEPS.map((x) => (
                  <Pressable key={x} accessibilityRole="button" accessibilityLabel={`Change weight in steps of ${x} kilos`} accessibilityState={{ selected: step === x }} onPress={() => { Haptics.selectionAsync(); setStep(x); }}>
                    <View style={{ minHeight: 44, minWidth: 64, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 12, borderRadius: 999, borderWidth: 1, backgroundColor: step === x ? colors.lime : 'rgba(255,255,255,0.05)', borderColor: step === x ? colors.lime : 'rgba(255,255,255,0.12)' }}>
                      <Txt style={{ fontFamily: font.bold, fontSize: 14, color: step === x ? '#07080B' : colors.dim }}>±{x}</Txt>
                    </View>
                  </Pressable>
                ))}
              </View>
              <View style={{ height: 1, backgroundColor: colors.border }} />
              <Dial label={hold ? 'Seconds' : 'Reps'} unit={hold ? 'seconds' : 'reps'} value={reps} onChange={setReps} step={hold ? 5 : 1} min={1} max={hold ? 600 : 100} decimals={false} />
              <View style={{ height: 1, backgroundColor: colors.border }} />
              <View style={{ gap: 10 }}>
                <Txt style={{ color: colors.dim, fontSize: 12, letterSpacing: 1.2, fontFamily: font.bold }}>HOW HARD WAS IT? (OPTIONAL)</Txt>
                <View style={{ flexDirection: 'row', gap: 8 }}>
                  {RPE.map((r) => {
                    const on = rpe === r.v;
                    return (
                      <Pressable key={r.v} accessibilityRole="button" accessibilityLabel={`Effort ${r.v}, ${r.hint}`} accessibilityState={{ selected: on }} onPress={() => { Haptics.selectionAsync(); setRpe(on ? null : r.v); }} style={{ flex: 1 }}>
                        <View style={{ alignItems: 'center', justifyContent: 'center', minHeight: 56, borderRadius: 16, borderWidth: 1, backgroundColor: on ? colors.lime + '22' : 'rgba(255,255,255,0.05)', borderColor: on ? colors.lime : 'rgba(255,255,255,0.10)' }}>
                          <Txt style={{ fontFamily: font.black, fontSize: 18, color: on ? colors.lime : colors.text }}>{r.v}</Txt>
                          <Txt style={{ fontSize: 11, color: on ? colors.lime : colors.dim, fontFamily: font.semibold }}>{r.hint}</Txt>
                        </View>
                      </Pressable>
                    );
                  })}
                </View>
              </View>
            </>
          )}

          {(logged?.length || queued) ? (
            <View style={{ gap: 8, marginTop: 4 }}>
              <Txt style={{ color: colors.dim, fontSize: 12, letterSpacing: 1.2, fontFamily: font.bold }}>LOGGED TODAY</Txt>
              <View style={{ borderRadius: 18, borderWidth: 1, borderColor: colors.border, backgroundColor: 'rgba(255,255,255,0.03)', overflow: 'hidden' }}>
                {(logged ?? []).map((s, i) => (
                  <View key={s.id} style={{ flexDirection: 'row', alignItems: 'center', gap: 12, paddingLeft: 16, paddingRight: 6, minHeight: 52, borderTopWidth: i ? 1 : 0, borderTopColor: colors.border }}>
                    <View style={{ width: 26, height: 26, borderRadius: 13, backgroundColor: colors.lime + '22', alignItems: 'center', justifyContent: 'center' }}>
                      <Txt style={{ fontFamily: font.black, fontSize: 13, color: colors.lime }}>{i + 1}</Txt>
                    </View>
                    <Txt style={{ flex: 1, fontFamily: font.bold, fontSize: 16 }} numberOfLines={2}>
                      {bw && !s.weight ? 'Bodyweight' : `${fmt(+(s.weight ?? 0))} kg`} × {s.reps}{hold ? ' s' : ''}
                      {s.rpe ? <Txt style={{ color: colors.dim, fontFamily: font.medium, fontSize: 13 }}>{`  RPE ${s.rpe}`}</Txt> : null}
                    </Txt>
                    <Pressable accessibilityRole="button" accessibilityLabel={`Remove set ${i + 1}`} onPress={() => undo(s)} disabled={busy}
                      style={({ pressed }) => ({ width: 48, height: 48, alignItems: 'center', justifyContent: 'center', opacity: pressed || busy ? 0.5 : 1 })}>
                      <Ionicons name="trash-outline" size={20} color={colors.faint} />
                    </Pressable>
                  </View>
                ))}
                {queued > 0 && (
                  <View style={{ paddingHorizontal: 16, minHeight: 48, justifyContent: 'center', borderTopWidth: logged?.length ? 1 : 0, borderTopColor: colors.border }}>
                    <Txt style={{ color: colors.amber, fontSize: 13.5 }}>{queued} {queued === 1 ? 'set' : 'sets'} saved on your phone, waiting for a connection</Txt>
                  </View>
                )}
              </View>
              {lastSet && (
                <Pressable accessibilityRole="button" accessibilityLabel="Undo last set" onPress={() => undo(lastSet)} disabled={busy}
                  style={({ pressed }) => ({ minHeight: 52, borderRadius: 16, borderWidth: 1.5, borderColor: colors.amber + '88', backgroundColor: colors.amber + '12', flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 10, opacity: pressed || busy ? 0.6 : 1 })}>
                  <Ionicons name="arrow-undo" size={20} color={colors.amber} />
                  <Txt style={{ color: colors.amber, fontFamily: font.bold, fontSize: 15.5 }}>Undo last set ({fmt(+(lastSet.weight ?? 0))} kg × {lastSet.reps}{hold ? ' s' : ''})</Txt>
                </Pressable>
              )}
            </View>
          ) : null}
        </>
      )}
    </Sheet>
  );
}
