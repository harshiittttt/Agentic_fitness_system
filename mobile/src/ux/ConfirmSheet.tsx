import React from 'react';
import { ActivityIndicator, Pressable, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { Sheet } from '../forms';
import { colors, font } from '../theme';
import { GradientButton, Txt } from '../ui';

export type Confirm = { title: string; message: string; action: string; danger?: boolean; icon?: string; onYes: () => void | Promise<void> };

/** Our own "are you sure": same look on every platform (Alert.alert does nothing on web) and the safe choice is the big one. */
export default function ConfirmSheet({ confirm, busy, onClose }: { confirm: Confirm | null; busy?: boolean; onClose: () => void }) {
  const danger = !!confirm?.danger;
  return (
    <Sheet visible={!!confirm} onClose={onClose} title={confirm?.title ?? ''}
      footer={confirm ? (
        <>
          {danger ? (
            <Pressable accessibilityRole="button" accessibilityLabel={confirm.action} disabled={busy} onPress={() => confirm.onYes()}
              style={({ pressed }) => ({ opacity: pressed || busy ? 0.7 : 1, minHeight: 54, borderRadius: 20, alignItems: 'center', justifyContent: 'center', flexDirection: 'row', gap: 8,
                backgroundColor: colors.red + '22', borderWidth: 1.5, borderColor: colors.red })}>
              {busy ? <ActivityIndicator color={colors.red} /> : <Ionicons name={(confirm.icon ?? 'trash') as any} size={20} color={colors.red} />}
              <Txt style={{ color: colors.red, fontFamily: font.black, fontSize: 16 }}>{confirm.action}</Txt>
            </Pressable>
          ) : <GradientButton label={busy ? 'Working…' : confirm.action} onPress={() => confirm.onYes()} />}
          <Pressable accessibilityRole="button" onPress={onClose} style={{ minHeight: 48, alignItems: 'center', justifyContent: 'center' }}>
            <Txt style={{ fontFamily: font.bold, fontSize: 16, color: colors.text }}>Cancel</Txt>
          </Pressable>
        </>
      ) : null}>
      <View style={{ flexDirection: 'row', gap: 12 }}>
        <Ionicons name={danger ? 'warning' : 'help-circle'} size={24} color={danger ? colors.red : colors.cyan} style={{ marginTop: 1 }} />
        <Txt style={{ flex: 1, color: colors.dim, fontSize: 15, lineHeight: 23 }}>{confirm?.message}</Txt>
      </View>
    </Sheet>
  );
}
