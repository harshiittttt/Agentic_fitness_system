import React from 'react';
import { Pressable, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import * as Haptics from './haptics';
import { PERSONAS, PersonaId } from './personas';
import { speak } from './tts';
import { setRecognitionLang, shouldUsePremium } from './voiceSettings';
import { colors, font } from './theme';
import { Txt } from './ui';

/** Pick a coach. Selecting one makes them say hello, so you hear the personality before committing. */
export default function PersonaPicker({ value, onChange }: { value: PersonaId; onChange: (id: PersonaId) => void }) {
  return (
    <View accessibilityRole="radiogroup" style={{ gap: 12 }}>
      {PERSONAS.map((p) => {
        const on = value === p.id;
        return (
          <Pressable key={p.id} accessibilityRole="radio" accessibilityState={{ selected: on }} accessibilityLabel={`${p.name}. ${p.blurb}`} accessibilityHint="Plays a short hello in their voice" style={({ pressed }) => ({ opacity: pressed ? 0.85 : 1, transform: [{ scale: pressed ? 0.985 : 1 }] })} onPress={() => { Haptics.selectionAsync(); onChange(p.id); setRecognitionLang(p.tts.lang); speak(p.sample, p.tts, undefined, { persona: p.id, premium: shouldUsePremium(true) }); }}>
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 14, padding: 16, borderRadius: 22, backgroundColor: on ? p.color + '18' : colors.card, borderWidth: 1.5, borderColor: on ? p.color : colors.border }}>
              <View style={{ width: 48, height: 48, borderRadius: 16, backgroundColor: p.color + '22', alignItems: 'center', justifyContent: 'center' }}>
                <Ionicons name={p.icon as any} size={24} color={p.color} />
              </View>
              <View style={{ flex: 1 }}>
                <Txt style={{ fontFamily: font.bold, fontSize: 17 }}>{p.name}</Txt>
                <Txt style={{ color: colors.dim, fontSize: 13, lineHeight: 18 }}>{p.blurb}</Txt>
              </View>
              <View style={{ alignItems: 'center', gap: 2 }}>
                <Ionicons name={on ? 'checkmark-circle' : 'play-circle-outline'} size={26} color={on ? p.color : colors.faint} />
                <Txt maxFontSizeMultiplier={1.2} style={{ fontSize: 10.5, color: on ? p.color : colors.faint, fontFamily: font.semibold }}>{on ? 'Chosen' : 'Hear'}</Txt>
              </View>
            </View>
          </Pressable>
        );
      })}
    </View>
  );
}
