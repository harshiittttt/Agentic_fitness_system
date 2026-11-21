import React, { useCallback, useEffect, useState } from 'react';
import { ActivityIndicator, Pressable, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import * as Haptics from './haptics';
import { getAlternatives, swapExercise } from './api';
import { Sheet } from './forms';
import { colors, font } from './theme';
import { Segmented, Skeleton, Txt } from './ui';

const cap = (s: string) => s.replace(/\b\w/g, (c) => c.toUpperCase());

/** Bottom sheet: pick an injury-safe like-for-like replacement, for today only or for good. */
export default function SwapSheet({ exercise, dayIndex, onClose, onSwapped, permanent }: {
  exercise: string | null; dayIndex: number; onClose: () => void; onSwapped: (plan: any) => void; permanent?: boolean; // permanent: no 'just today' option (onboarding)
}) {
  const [alts, setAlts] = useState<any[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [scope, setScope] = useState<'today' | 'plan'>(permanent ? 'plan' : 'today');
  const [busy, setBusy] = useState('');
  const [err, setErr] = useState('');

  const load = useCallback(() => {
    if (!exercise) return;
    setAlts(null); setFailed(false); setErr('');
    getAlternatives(exercise).then((a) => setAlts(Array.isArray(a) ? a : [])).catch(() => setFailed(true));
  }, [exercise]);
  useEffect(() => { if (exercise) setScope(permanent ? 'plan' : 'today'); load(); }, [exercise]);

  const apply = async (name: string) => {
    if (!exercise) return;
    setBusy(name); setErr('');
    try {
      const plan = await swapExercise(dayIndex, exercise, name, scope);
      Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
      onSwapped(plan); onClose();
    } catch { setErr("Couldn't swap that. Check your connection and try again."); }
    setBusy('');
  };

  return (
    <Sheet visible={!!exercise} onClose={onClose} title="Swap exercise">
      <Txt style={{ fontFamily: font.black, fontSize: 20, marginBottom: -4 }}>{exercise ? cap(exercise) : ''}</Txt>
      {!permanent && <Segmented<'today' | 'plan'> options={[{ key: 'today', label: 'Just today' }, { key: 'plan', label: 'From now on' }]} value={scope} onChange={setScope} />}
      <Txt style={{ color: colors.dim, fontSize: 14, lineHeight: 20 }}>
        {scope === 'today' ? 'Only like-for-like moves that suit your injuries. Your plan stays the same after today.' : 'Your plan changes for good and your coach will remember this preference.'}
      </Txt>
      {!!err && (
        <View style={{ flexDirection: 'row', gap: 10, padding: 12, borderRadius: 16, backgroundColor: colors.red + '1C', borderWidth: 1, borderColor: colors.red + '55' }}>
          <Ionicons name="alert-circle" size={20} color={colors.red} />
          <Txt style={{ flex: 1, color: colors.red, fontFamily: font.semibold, fontSize: 14, lineHeight: 20 }}>{err}</Txt>
        </View>
      )}
      {alts === null && !failed && <View style={{ gap: 10 }}><Skeleton height={68} radius={18} /><Skeleton height={68} radius={18} /><Skeleton height={68} radius={18} /></View>}
      {failed && (
        <View style={{ alignItems: 'center', gap: 10, paddingVertical: 12 }}>
          <Ionicons name="cloud-offline" size={32} color={colors.amber} />
          <Txt style={{ color: colors.dim, textAlign: 'center', lineHeight: 21 }}>Couldn't load the options. Check your connection.</Txt>
          <Pressable accessibilityRole="button" onPress={load} style={{ minHeight: 48, paddingHorizontal: 24, borderRadius: 24, borderWidth: 1.5, borderColor: colors.lime, alignItems: 'center', justifyContent: 'center' }}>
            <Txt style={{ color: colors.lime, fontFamily: font.bold, fontSize: 15 }}>Try again</Txt>
          </Pressable>
        </View>
      )}
      {alts?.length === 0 && (
        <View style={{ alignItems: 'center', gap: 6, paddingVertical: 12 }}>
          <Ionicons name="shield-checkmark" size={32} color={colors.amber} />
          <Txt style={{ fontFamily: font.bold, fontSize: 16 }}>No safe swap for this one</Txt>
          <Txt style={{ color: colors.dim, textAlign: 'center', lineHeight: 21 }}>Nothing else matches this movement without loading your problem areas. Ask your coach in the Coach tab.</Txt>
        </View>
      )}
      {alts?.map((a) => (
        <Pressable key={a.name} accessibilityRole="button" accessibilityLabel={`Swap to ${a.name}`} onPress={() => apply(a.name)} disabled={!!busy}
          style={({ pressed }) => ({ flexDirection: 'row', alignItems: 'center', gap: 12, minHeight: 68, paddingHorizontal: 16, paddingVertical: 12, borderRadius: 20, backgroundColor: colors.card, borderWidth: 1, borderColor: colors.border, opacity: pressed || (!!busy && busy !== a.name) ? 0.6 : 1 })}>
          <View style={{ flex: 1 }}>
            <Txt style={{ fontFamily: font.bold, fontSize: 17 }}>{cap(a.name)}</Txt>
            <Txt style={{ color: colors.dim, fontSize: 13.5, textTransform: 'capitalize' }}>{a.muscle}{a.compound ? ' · compound' : ' · isolation'}</Txt>
          </View>
          {busy === a.name ? <ActivityIndicator color={colors.lime} /> : <Ionicons name="swap-horizontal" size={22} color={colors.lime} />}
        </Pressable>
      ))}
    </Sheet>
  );
}
