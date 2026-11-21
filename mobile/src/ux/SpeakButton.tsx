import React, { useEffect, useRef, useState } from 'react';
import { Pressable } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { speak, stopSpeaking } from '../tts';
import { shouldUsePremium } from '../voiceSettings';
import { colors, font } from '../theme';
import { Txt } from '../ui';

/**
 * Tap to hear an agent message in that agent's own voice; tap again to stop.
 * Never plays on its own (credit saving). Audio is cached on the phone by tts.ts, so a repeat tap costs nothing.
 * `agent` is a backend AGENT_VOICES id (training, nutrition, intake, research, logistics, periodization...).
 * Starting another line (anywhere) silences this one: tts.ts fires this button's onDone when it is superseded.
 */
export default function SpeakButton({ text, agent, color = colors.lime, label, name }: { text: string; agent: string; color?: string; label?: boolean; name?: string }) {
  const [on, setOn] = useState(false);
  const mounted = useRef(true);
  const mine = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; if (mine.current) stopSpeaking(); };
  }, []);

  const press = () => {
    if (on) { stopSpeaking(); setOn(false); mine.current = false; return; }
    if (!text.trim()) return;
    setOn(true); mine.current = true;
    speak(text, { rate: 1, pitch: 1, lang: 'en-US' }, () => { mine.current = false; if (mounted.current) setOn(false); }, { persona: 'mentor', agent, premium: shouldUsePremium(true) });
  };
  const who = name ?? 'this message';
  return (
    <Pressable onPress={press} accessibilityRole="button" accessibilityLabel={on ? `Stop reading ${who}` : `Hear ${who}`} hitSlop={10}
      style={({ pressed }) => ({ flexDirection: 'row', alignItems: 'center', gap: 6, alignSelf: 'flex-start', minHeight: 32, opacity: pressed ? 0.6 : 1 })}>
      <Ionicons name={on ? 'stop-circle' : 'volume-high'} size={20} color={color} />
      {!!label && <Txt style={{ color, fontFamily: font.semibold, fontSize: 13.5 }}>{on ? 'Stop' : 'Hear it'}</Txt>}
    </Pressable>
  );
}
