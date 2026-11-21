import React from 'react';
import { KeyboardAvoidingView, Modal, Platform, Pressable, ScrollView, View, useWindowDimensions } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import * as Haptics from './haptics';
import { colors, font } from './theme';
import { Card, Txt } from './ui';

export const Label = ({ children }: { children: string }) => (
  <Txt style={{ color: colors.dim, fontSize: 12, letterSpacing: 0.8, fontFamily: font.semibold, marginBottom: 8 }}>{children.toUpperCase()}</Txt>
);

/** Big +/- number control: no keyboard needed. */
export function Stepper({ label, value, unit, step = 1, min, max, onChange }: {
  label: string; value: number; unit: string; step?: number; min: number; max: number; onChange: (n: number) => void;
}) {
  const btn = (icon: string, d: number) => (
    <Pressable onPress={() => { Haptics.selectionAsync(); onChange(Math.max(min, Math.min(max, +(value + d).toFixed(1)))); }}
      style={{ width: 48, height: 48, borderRadius: 16, backgroundColor: colors.bg2, borderWidth: 1, borderColor: colors.border, alignItems: 'center', justifyContent: 'center' }}>
      <Ionicons name={icon as any} size={22} color={colors.text} />
    </Pressable>
  );
  return (
    <Card style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingVertical: 14 }}>
      <View>
        <Txt style={{ color: colors.dim, fontSize: 12, letterSpacing: 0.8, fontFamily: font.semibold }}>{label.toUpperCase()}</Txt>
        <Txt style={{ fontFamily: font.black, fontSize: 30 }}>{value}<Txt style={{ color: colors.dim, fontSize: 14, fontFamily: font.medium }}> {unit}</Txt></Txt>
      </View>
      <View style={{ flexDirection: 'row', gap: 10 }}>{btn('remove', -step)}{btn('add', step)}</View>
    </Card>
  );
}

/** Large selectable card with an icon, used for goals and experience. */
export function Choice({ icon, title, sub, selected, onPress, color = colors.lime }: {
  icon: string; title: string; sub: string; selected: boolean; onPress: () => void; color?: string;
}) {
  return (
    <Pressable onPress={() => { Haptics.selectionAsync(); onPress(); }}>
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 14, padding: 16, borderRadius: 22, backgroundColor: selected ? color + '18' : colors.card, borderWidth: 1.5, borderColor: selected ? color : colors.border }}>
        <View style={{ width: 46, height: 46, borderRadius: 15, backgroundColor: color + '22', alignItems: 'center', justifyContent: 'center' }}>
          <Ionicons name={icon as any} size={24} color={color} />
        </View>
        <View style={{ flex: 1 }}>
          <Txt style={{ fontFamily: font.bold, fontSize: 17 }}>{title}</Txt>
          <Txt style={{ color: colors.dim, fontSize: 13, lineHeight: 18 }}>{sub}</Txt>
        </View>
        <Ionicons name={selected ? 'checkmark-circle' : 'ellipse-outline'} size={24} color={selected ? color : colors.faint} />
      </View>
    </Pressable>
  );
}

/** 1-5 tap scale (soreness, energy). */
export function Scale({ label, value, onChange, icon, color }: { label: string; value: number; onChange: (n: number) => void; icon: string; color: string }) {
  return (
    <View style={{ gap: 8 }}>
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
        <Ionicons name={icon as any} size={16} color={color} />
        <Txt style={{ fontFamily: font.semibold, fontSize: 14 }}>{label}</Txt>
      </View>
      <View style={{ flexDirection: 'row', gap: 8 }}>
        {[1, 2, 3, 4, 5].map((n) => (
          <Pressable key={n} onPress={() => { Haptics.selectionAsync(); onChange(n); }}
            style={{ flex: 1, height: 42, borderRadius: 14, alignItems: 'center', justifyContent: 'center', backgroundColor: n <= value ? color : colors.bg2, borderWidth: 1, borderColor: n <= value ? color : colors.border }}>
            <Txt style={{ fontFamily: font.bold, color: n <= value ? '#07080B' : colors.dim }}>{n}</Txt>
          </Pressable>
        ))}
      </View>
    </View>
  );
}

// ---------------------------------------------------------------- settings-style list
export function Row({ icon, color = colors.lime, title, value, onPress, right, danger, divider }: {
  icon: string; color?: string; title: string; value?: string; onPress?: () => void; right?: React.ReactNode; danger?: boolean; divider?: boolean;
}) {
  const tint = danger ? colors.red : color;
  const body = (
    <View style={{ flexDirection: 'row', alignItems: 'center', gap: 14, paddingVertical: 13, paddingHorizontal: 16, borderTopWidth: divider ? 1 : 0, borderTopColor: colors.border }}>
      <View style={{ width: 34, height: 34, borderRadius: 11, backgroundColor: tint + '22', alignItems: 'center', justifyContent: 'center' }}>
        <Ionicons name={icon as any} size={18} color={tint} />
      </View>
      <Txt style={{ flex: 1, fontFamily: font.semibold, fontSize: 15.5, color: danger ? colors.red : colors.text }} numberOfLines={1}>{title}</Txt>
      {!!value && <Txt style={{ color: colors.dim, fontSize: 13.5, maxWidth: '50%', textAlign: 'right' }} numberOfLines={1}>{value}</Txt>}
      {right ?? (onPress ? <Ionicons name="chevron-forward" size={18} color={colors.faint} /> : null)}
    </View>
  );
  return onPress ? <Pressable onPress={() => { Haptics.selectionAsync(); onPress(); }} style={({ pressed }) => ({ opacity: pressed ? 0.6 : 1 })}>{body}</Pressable> : body;
}

/** A titled card of rows with hairline dividers between them. */
export function Group({ title, children, footer }: { title?: string; children: React.ReactNode; footer?: string }) {
  const rows = React.Children.toArray(children).filter(Boolean);
  return (
    <View style={{ gap: 8 }}>
      {!!title && <Txt style={{ color: colors.dim, fontSize: 12, letterSpacing: 1, fontFamily: font.semibold, marginLeft: 6 }}>{title.toUpperCase()}</Txt>}
      <Card style={{ padding: 0, gap: 0, overflow: 'hidden' }}>
        {rows.map((r, i) => (React.isValidElement(r) && (r.type as any) === Row ? React.cloneElement(r as React.ReactElement<any>, { divider: i > 0 }) : r))}
      </Card>
      {!!footer && <Txt style={{ color: colors.faint, fontSize: 12, lineHeight: 17, marginLeft: 6 }}>{footer}</Txt>}
    </View>
  );
}

// ---------------------------------------------------------------- bottom sheet
export function Sheet({ visible, onClose, title, children, footer }: {
  visible: boolean; onClose: () => void; title: string; children: React.ReactNode; footer?: React.ReactNode;
}) {
  // A percentage maxHeight does not resolve against an auto-height parent, so tall sheets ran off the screen and hid their
  // footer button. Use the real window height instead: the body scrolls, the footer always stays on screen.
  const { height } = useWindowDimensions();
  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose}>
      <Pressable style={{ flex: 1, backgroundColor: 'rgba(0,0,0,0.6)' }} onPress={onClose} />
      <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
        <View style={{ backgroundColor: colors.bg2, borderTopLeftRadius: 30, borderTopRightRadius: 30, borderTopWidth: 1, borderColor: colors.border, maxHeight: Math.round(height * 0.92) }}>
          <View style={{ alignSelf: 'center', width: 40, height: 4, borderRadius: 2, backgroundColor: colors.faint, marginTop: 10 }} />
          <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingHorizontal: 22, paddingTop: 14, paddingBottom: 6 }}>
            <Txt style={{ fontFamily: font.black, fontSize: 24 }}>{title}</Txt>
            <Pressable onPress={onClose} hitSlop={12}><Ionicons name="close-circle" size={28} color={colors.faint} /></Pressable>
          </View>
          <ScrollView style={{ maxHeight: Math.max(220, Math.round(height * 0.92) - (footer ? 200 : 110)) }} contentContainerStyle={{ padding: 22, paddingTop: 12, gap: 14 }} keyboardShouldPersistTaps="handled">{children}</ScrollView>
          {!!footer && <View style={{ padding: 18, paddingTop: 8, gap: 10, borderTopWidth: 1, borderTopColor: colors.border }}>{footer}</View>}
        </View>
      </KeyboardAvoidingView>
    </Modal>
  );
}
