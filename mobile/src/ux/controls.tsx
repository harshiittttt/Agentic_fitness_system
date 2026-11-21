import React, { useEffect, useRef } from 'react';
import { Pressable } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import * as Haptics from '../haptics';
import { colors, font } from '../theme';
import { Txt } from '../ui';

/** A round minus/plus button. Tap once to nudge, hold to keep going: the way you actually change a number with one thumb. */
export function Nudge({ icon, onStep, label, size = 54 }: { icon: 'remove' | 'add'; onStep: () => void; label: string; size?: number }) {
  const t = useRef<ReturnType<typeof setTimeout> | null>(null);
  const iv = useRef<ReturnType<typeof setInterval> | null>(null);
  const cb = useRef(onStep);
  cb.current = onStep;                                   // the repeat timer must always call the latest step (the value moves under it)
  const stop = () => { if (t.current) clearTimeout(t.current); if (iv.current) clearInterval(iv.current); t.current = null; iv.current = null; };
  useEffect(() => stop, []);
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={label} hitSlop={6}
      onPressIn={() => { Haptics.selectionAsync(); cb.current(); t.current = setTimeout(() => { iv.current = setInterval(() => cb.current(), 110); }, 380); }}
      onPressOut={stop}
      style={({ pressed }) => ({ width: size, height: size, borderRadius: size / 2, alignItems: 'center', justifyContent: 'center', transform: [{ scale: pressed ? 0.92 : 1 }],
        backgroundColor: 'rgba(255,255,255,0.07)', borderWidth: 1, borderColor: 'rgba(255,255,255,0.14)' })}>
      <Ionicons name={icon} size={size * 0.52} color={colors.text} />
    </Pressable>
  );
}

/** A selectable pill at least 48px tall (the shared Chip is about 40px, too small for gym-sweaty thumbs). */
export function Pill({ label, active, onPress, color = colors.lime, flex }: { label: string; active?: boolean; onPress: () => void; color?: string; flex?: boolean }) {
  return (
    <Pressable accessibilityRole="button" accessibilityState={{ selected: !!active }} accessibilityLabel={label} onPress={() => { Haptics.selectionAsync(); onPress(); }}
      style={({ pressed }) => ({ minHeight: 48, minWidth: 64, paddingHorizontal: 18, borderRadius: 999, alignItems: 'center', justifyContent: 'center', flex: flex ? 1 : undefined, opacity: pressed ? 0.8 : 1,
        backgroundColor: active ? color : colors.card, borderWidth: 1, borderColor: active ? color : colors.border })}>
      <Txt style={{ fontFamily: font.semibold, fontSize: 15, color: active ? '#07080B' : colors.dim }}>{label}</Txt>
    </Pressable>
  );
}
