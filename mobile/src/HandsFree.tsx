// Profile > Voice: how voice works, the opt-in "Hey coach" switch, and live diagnostics so a miss is explainable (what the recognizer heard).
import React, { useEffect, useState } from 'react';
import { Pressable, Switch, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { getWakeState, onWakeState, resetDiag, setWakeEnabled, WakeState } from './wakeSettings';
import { colors, font } from './theme';
import { Card, Txt } from './ui';

const STATUS: Record<string, { label: string; color: string }> = {
  off: { label: 'Off', color: colors.faint },
  unavailable: { label: 'Not available in this build', color: colors.amber },
  denied: { label: 'Microphone permission refused', color: colors.red },
  listening: { label: 'Listening for the phrase', color: colors.lime },
  paused: { label: 'Waiting (the coach is talking, another screen has the mic, or the app is in the background)', color: colors.amber },
};

const ago = (t: number) => { if (!t) return 'never'; const s = Math.round((Date.now() - t) / 1000); return s < 5 ? 'just now' : s < 90 ? `${s}s ago` : `${Math.round(s / 60)} min ago`; };

export default function HandsFree() {
  const [ws, setWs] = useState<WakeState>(getWakeState());
  const [, tick] = useState(0);
  useEffect(() => onWakeState(setWs), []);
  useEffect(() => { const t = setInterval(() => tick((n) => n + 1), 2000); return () => clearInterval(t); }, []);
  const st = STATUS[ws.status] ?? STATUS.off;
  const d = ws.diag;
  const silent = ws.enabled && ws.status === 'listening' && d.sessions >= 2 && d.results === 0;
  return (
    <Card style={{ gap: 12 }}>
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10 }}>
        <Ionicons name="mic" size={22} color={colors.lime} />
        <View style={{ flex: 1 }}>
          <Txt style={{ fontFamily: font.bold, fontSize: 16 }}>Talking to your coach</Txt>
          <Txt style={{ color: colors.dim, fontSize: 13.5, lineHeight: 19 }}>Tap the green mic next to the tabs, say it, done. It shows your words as you speak and the coach answers out loud.</Txt>
        </View>
      </View>
      <View style={{ height: 1, backgroundColor: colors.border }} />
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10 }}>
        <View style={{ flex: 1 }}>
          <Txt style={{ fontFamily: font.semibold, fontSize: 15 }}>Hands-free "Hey coach" (experimental)</Txt>
          <Txt style={{ color: colors.dim, fontSize: 13, lineHeight: 18 }}>Keeps the speech recognizer on while the app is open. It is not a real wake word, so it can miss, restart with a beep and use battery. Off by default.</Txt>
        </View>
        <Switch accessibilityLabel="Hey coach hands-free" value={ws.enabled} trackColor={{ false: colors.cardHi, true: colors.lime }} thumbColor="#fff" onValueChange={(on) => setWakeEnabled(on)} />
      </View>
      {ws.enabled && (
        <View style={{ gap: 8, padding: 12, borderRadius: 14, backgroundColor: 'rgba(255,255,255,0.04)' }}>
          <View style={{ flexDirection: 'row', gap: 8, alignItems: 'center' }}>
            <View style={{ width: 8, height: 8, borderRadius: 4, backgroundColor: st.color }} />
            <Txt style={{ flex: 1, color: st.color, fontFamily: font.semibold, fontSize: 13.5 }}>{st.label}</Txt>
          </View>
          <Txt style={{ color: colors.dim, fontSize: 12.5, lineHeight: 18 }}>Sessions started: {d.sessions}   ·   Results heard: {d.results}</Txt>
          <Txt style={{ color: colors.dim, fontSize: 12.5, lineHeight: 18 }}>Last heard ({ago(d.lastHeardAt)}): <Txt style={{ color: colors.text, fontSize: 12.5 }}>{d.lastHeard ? `“${d.lastHeard}”` : 'nothing yet'}</Txt></Txt>
          <Txt style={{ color: colors.dim, fontSize: 12.5 }}>Phrase matched: {ago(d.lastMatch)}{d.lastError ? `   ·   last error: ${d.lastError}` : ''}</Txt>
          {silent && <Txt style={{ color: colors.amber, fontSize: 12.5, lineHeight: 18 }}>The recognizer starts but never returns words on this phone, so the phrase cannot work here. Use the mic button.</Txt>}
          {!!ws.note && <Txt style={{ color: colors.faint, fontSize: 12, lineHeight: 17 }}>{ws.note}</Txt>}
          <Pressable accessibilityRole="button" accessibilityLabel="Reset counters" onPress={resetDiag} style={{ minHeight: 40, justifyContent: 'center' }}>
            <Txt style={{ color: colors.cyan, fontFamily: font.semibold, fontSize: 13 }}>Reset counters</Txt>
          </Pressable>
        </View>
      )}
    </Card>
  );
}
