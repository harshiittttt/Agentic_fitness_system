import React, { useEffect, useMemo, useState } from 'react';
import { ActivityIndicator, Pressable, TextInput, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import * as Haptics from './haptics';
import type { PortionInfo } from './api';
import { colors, font } from './theme';
import { Card, Txt } from './ui';

export type Macros = { kcal: number; protein: number; carbs: number; fat: number };

/** Compact inline portion card: stepper in natural units, grams field, presets, live macro preview. */
export default function PortionSheet({ name, info, baseGrams, baseMacros, initialGrams, confirmLabel, busy, onConfirm, onCancel }: {
  name: string;
  info: PortionInfo | null; // null while loading or when the lookup failed
  baseGrams: number | null; // the chip's or row's usual grams (defines 1x)
  baseMacros: Macros | null; // macros for baseGrams, used when per100 is unknown
  initialGrams?: number | null;
  confirmLabel: string;
  busy?: boolean;
  onConfirm: (grams: number) => void;
  onCancel: () => void;
}) {
  const unitG = info?.unit_g && info.unit_g > 0 ? info.unit_g : null;
  const unit = info?.unit || 'unit';
  // Count foods (eggs, rotis): only a count. Measured foods (rice, dal, chicken): only an amount in g/ml.
  const count = info?.mode === 'count' && !!unitG;
  const measure = info?.measure ?? 'g';
  const base = baseGrams && baseGrams > 0 ? baseGrams : unitG ?? 100;
  const start = Math.round(initialGrams && initialGrams > 0 ? initialGrams : base);
  const [grams, setGrams] = useState(start);
  const [text, setText] = useState(String(start));
  useEffect(() => { setGrams(start); setText(String(start)); }, [name, start]);

  const set = (g: number) => { const v = Math.max(1, Math.min(99999, Math.round(g))); setGrams(v); setText(String(v)); };
  const step = base >= 400 ? 100 : 50;
  const n = count ? Math.max(1, Math.round(grams / unitG!)) : 0;
  const plural = n === 1 || unit.endsWith('s') ? unit : `${unit}s`;
  const presets = count ? [1, 2, 3, 4, 6] : [100, 150, 200, 250, 300, 400].filter((v) => v !== 0);

  const macros: Macros | null = useMemo(() => {
    const p = info?.per100;
    if (p) return { kcal: p.kcal * grams / 100, protein: p.protein * grams / 100, carbs: p.carbs * grams / 100, fat: p.fat * grams / 100 };
    if (baseMacros && baseGrams && baseGrams > 0) { const f = grams / baseGrams; return { kcal: baseMacros.kcal * f, protein: baseMacros.protein * f, carbs: baseMacros.carbs * f, fat: baseMacros.fat * f }; }
    return null;
  }, [info, grams, baseMacros, baseGrams]);

  const roundBtn = (icon: 'add' | 'remove', label: string, onPress: () => void) => (
    <Pressable onPress={() => { Haptics.selectionAsync(); onPress(); }} accessibilityRole="button" accessibilityLabel={label} hitSlop={4}
      style={({ pressed }) => ({ width: 48, height: 48, borderRadius: 24, alignItems: 'center', justifyContent: 'center', backgroundColor: pressed ? 'rgba(198,255,61,0.22)' : 'rgba(255,255,255,0.08)', transform: [{ scale: pressed ? 0.94 : 1 }] })}>
      <Ionicons name={icon} size={22} color={colors.lime} />
    </Pressable>
  );
  const typed = parseInt(text, 10);

  return (
    <Card style={{ gap: 12, padding: 16 }}>
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
        <Txt style={{ flex: 1, fontFamily: font.bold, fontSize: 16, textTransform: 'capitalize' }} numberOfLines={1}>{name}</Txt>
        {!info && <ActivityIndicator size="small" color={colors.dim} />}
        <Pressable onPress={onCancel} accessibilityRole="button" accessibilityLabel="Close portion picker" style={{ minWidth: 44, minHeight: 44, alignItems: 'center', justifyContent: 'center' }}>
          <Ionicons name="close" size={20} color={colors.dim} />
        </Pressable>
      </View>

      {count ? (
        <>
          <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10 }}>
            {roundBtn('remove', `One fewer ${unit}`, () => set(Math.max(1, n - 1) * unitG!))}
            <View style={{ flex: 1, alignItems: 'center' }}>
              <Txt style={{ fontFamily: font.black, fontSize: 26, letterSpacing: -0.4 }} numberOfLines={1} adjustsFontSizeToFit accessibilityLiveRegion="polite">{n} {plural}</Txt>
              <Txt style={{ color: colors.faint, fontSize: 12 }} numberOfLines={1}>about {Math.round(grams)} g</Txt>
            </View>
            {roundBtn('add', `One more ${unit}`, () => set((n + 1) * unitG!))}
          </View>
          <View style={{ flexDirection: 'row', gap: 8 }}>
            {presets.map((c) => (
              <Pressable key={c} onPress={() => { Haptics.selectionAsync(); set(c * unitG!); }} accessibilityRole="button" accessibilityLabel={`${c} ${unit}`}
                accessibilityState={{ selected: c === n }}
                style={{ flex: 1, minHeight: 48, alignItems: 'center', justifyContent: 'center', borderRadius: 14, borderWidth: 1, borderColor: c === n ? 'rgba(198,255,61,0.7)' : 'transparent', backgroundColor: c === n ? 'rgba(198,255,61,0.18)' : 'rgba(255,255,255,0.08)' }}>
                <Txt style={{ fontFamily: font.bold, fontSize: 15, color: c === n ? colors.lime : colors.text }}>{c}</Txt>
              </Pressable>
            ))}
          </View>
        </>
      ) : (
        <>
          <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10 }}>
            {roundBtn('remove', `${step} ${measure} less`, () => set(grams - step))}
            <View style={{ flex: 1, minHeight: 56, flexDirection: 'row', alignItems: 'center', justifyContent: 'center', borderRadius: 14, backgroundColor: 'rgba(255,255,255,0.06)', borderWidth: 1, borderColor: 'rgba(255,255,255,0.10)', paddingHorizontal: 12 }}>
              <TextInput value={text} keyboardType="number-pad" maxLength={5} selectTextOnFocus accessibilityLabel={`Amount of ${name} in ${measure}`}
                onChangeText={(v) => { const d = v.replace(/[^0-9]/g, ''); setText(d); const x = parseInt(d, 10); if (x > 0) setGrams(x); }}
                onBlur={() => set(parseInt(text, 10) || grams)}
                style={{ minWidth: 70, textAlign: 'center', color: colors.text, fontFamily: font.black, fontSize: 26, paddingVertical: 8 }} />
              <Txt style={{ color: colors.dim, fontFamily: font.semibold, fontSize: 16 }}>{measure}</Txt>
            </View>
            {roundBtn('add', `${step} ${measure} more`, () => set(grams + step))}
          </View>
          <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 6 }}>
            {presets.map((v) => (
              <Pressable key={v} onPress={() => { Haptics.selectionAsync(); set(v); }} accessibilityRole="button" accessibilityLabel={`${v} ${measure}`}
                accessibilityState={{ selected: v === grams }}
                style={{ flexGrow: 1, flexBasis: 44, minHeight: 44, alignItems: 'center', justifyContent: 'center', borderRadius: 14, borderWidth: 1, borderColor: v === grams ? 'rgba(198,255,61,0.7)' : 'transparent', backgroundColor: v === grams ? 'rgba(198,255,61,0.18)' : 'rgba(255,255,255,0.08)' }}>
                <Txt style={{ fontFamily: font.bold, fontSize: 13, color: v === grams ? colors.lime : colors.text }}>{v}</Txt>
              </Pressable>
            ))}
          </View>
        </>
      )}

      {macros ? (
        <View style={{ flexDirection: 'row', alignItems: 'center', flexWrap: 'wrap', columnGap: 14, rowGap: 4, paddingVertical: 2 }} accessible
          accessibilityLabel={`${Math.round(macros.kcal)} calories, ${Math.round(macros.protein)} grams protein, ${Math.round(macros.carbs)} grams carbs, ${Math.round(macros.fat)} grams fat`}>
          <Txt style={{ fontFamily: font.black, fontSize: 18, letterSpacing: -0.3 }}>{Math.round(macros.kcal)}<Txt style={{ color: colors.dim, fontSize: 12, fontFamily: font.semibold }}> kcal</Txt></Txt>
          {([['P', macros.protein, colors.pink], ['C', macros.carbs, colors.cyan], ['F', macros.fat, colors.amber]] as const).map(([l, v, c]) => (
            <View key={l} style={{ flexDirection: 'row', alignItems: 'center', gap: 5 }}>
              <View style={{ width: 7, height: 7, borderRadius: 4, backgroundColor: c }} />
              <Txt style={{ color: colors.dim, fontSize: 13 }}>{l} <Txt style={{ color: colors.text, fontFamily: font.semibold, fontSize: 13 }}>{Math.round(v)} g</Txt></Txt>
            </View>
          ))}
        </View>
      ) : (
        <Txt style={{ color: colors.dim, fontSize: 13 }} numberOfLines={2}>Calories are worked out when you add it.</Txt>
      )}

      <Pressable onPress={() => onConfirm(count ? grams : (typed > 0 ? typed : grams))} disabled={busy}
        accessibilityRole="button" accessibilityLabel={count ? `${confirmLabel} ${n} ${plural} of ${name}` : `${confirmLabel} ${grams} ${measure} of ${name}`}
        accessibilityState={{ disabled: !!busy, busy: !!busy }}
        style={({ pressed }) => ({ minHeight: 48, alignItems: 'center', justifyContent: 'center', borderRadius: 16, backgroundColor: colors.lime, opacity: busy ? 0.6 : pressed ? 0.88 : 1, transform: [{ scale: pressed ? 0.985 : 1 }] })}>
        {busy ? <ActivityIndicator color="#07080B" /> : <Txt style={{ fontFamily: font.bold, fontSize: 15, color: '#07080B' }}>{confirmLabel}</Txt>}
      </Pressable>
    </Card>
  );
}
