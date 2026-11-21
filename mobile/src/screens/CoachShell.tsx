import React from 'react';
import { KeyboardAvoidingView, Modal, Platform, View } from 'react-native';
import { colors, font } from '../theme';
import { IconButton, ToastProvider, Txt } from '../ui';

/** Full-screen modal frame shared by the Agenda, Intake and Research screens (same look as "What I know about you"). */
export default function CoachShell({ visible, onClose, title, subtitle, children }: { visible: boolean; onClose: () => void; title: string; subtitle: string; children: React.ReactNode }) {
  return (
    <Modal visible={visible} animationType="slide" onRequestClose={onClose}>
      <ToastProvider bottom={28}>
        <View style={{ flex: 1, backgroundColor: colors.bg }}>
          <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8, paddingHorizontal: 16, paddingTop: Platform.OS === 'ios' ? 56 : 28, paddingBottom: 8 }}>
              <View style={{ flex: 1 }}>
                <Txt accessibilityRole="header" style={{ fontFamily: font.black, fontSize: 24, letterSpacing: -0.4 }}>{title}</Txt>
                <Txt style={{ color: colors.dim, fontSize: 13 }}>{subtitle}</Txt>
              </View>
              <IconButton icon="close" label="Close" onPress={onClose} size={26} color={colors.text} bg="rgba(255,255,255,0.07)" />
            </View>
            {visible && children}
          </KeyboardAvoidingView>
        </View>
      </ToastProvider>
    </Modal>
  );
}

export const inputStyle = { minHeight: 48, borderRadius: 14, paddingHorizontal: 14, color: colors.text, fontFamily: font.regular, fontSize: 15, backgroundColor: colors.bg2, borderWidth: 1, borderColor: colors.border } as const;
