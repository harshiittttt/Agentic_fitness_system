import React, { useState } from 'react';
import { ActivityIndicator, Alert, Pressable, TextInput, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import * as Clipboard from 'expo-clipboard';
import { clearServer, currentServer, saveServer, testConnection } from '../server';
import { colors, font } from '../theme';
import { Card, GradientButton, Txt } from '../ui';
import { inputStyle } from './CoachShell';

/** Server address + access key form, used by the first-run screen and by Setup > Server. */
export default function ServerForm({ onSaved, notice }: { onSaved: () => void; notice?: string }) {
  const start = currentServer();
  const [url, setUrl] = useState(start.saved ? start.url : '');
  const [token, setToken] = useState(start.saved ? start.token : '');
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<{ ok: boolean; message: string } | null>(null);

  const paste = async (set: (s: string) => void) => {
    try { const t = await Clipboard.getStringAsync(); if (t) { set(t.trim()); setRes(null); } } catch { /* clipboard unavailable */ }
  };
  const test = async () => { setBusy(true); setRes(null); setRes(await testConnection(url, token)); setBusy(false); };
  const save = async () => {
    try { await saveServer(url, token); onSaved(); } catch (e: any) { setRes({ ok: false, message: String(e?.message ?? e) }); }
  };
  const disconnect = () => Alert.alert('Disconnect from this coach?', 'The saved address and access key are removed from this phone. Your data stays on the server.', [
    { text: 'Cancel', style: 'cancel' },
    { text: 'Disconnect', style: 'destructive', onPress: async () => { await clearServer(); onSaved(); } },
  ]);

  const [focus, setFocus] = useState<'url' | 'key' | null>(null);
  const ready = url.trim().length > 0;
  const fieldBox = (k: 'url' | 'key') => ({ flex: 1, flexDirection: 'row' as const, alignItems: 'center' as const, minHeight: 52, borderRadius: 16, backgroundColor: colors.bg2, borderWidth: 1.5, borderColor: focus === k ? colors.lime + 'aa' : colors.border, paddingLeft: 14, paddingRight: 4 });
  const inner = { flex: 1, color: colors.text, fontFamily: font.regular, fontSize: 15, paddingVertical: 12 } as const;
  const label = (t: string, hint?: string) => (
    <View style={{ gap: 2 }}>
      <Txt style={{ color: colors.dim, fontSize: 12, letterSpacing: 0.8, fontFamily: font.semibold }}>{t}</Txt>
      {!!hint && <Txt style={{ color: colors.faint, fontSize: 12.5, lineHeight: 17 }}>{hint}</Txt>}
    </View>
  );
  const PasteBtn = ({ onPress, what }: { onPress: () => void; what: string }) => (
    <Pressable accessibilityRole="button" accessibilityLabel={`Paste ${what} from clipboard`} onPress={onPress} hitSlop={4}
      style={({ pressed }) => ({ minHeight: 44, minWidth: 44, paddingHorizontal: 10, flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 4, opacity: pressed ? 0.6 : 1 })}>
      <Ionicons name="clipboard-outline" size={18} color={colors.lime} />
      <Txt maxFontSizeMultiplier={1.3} style={{ color: colors.lime, fontFamily: font.bold, fontSize: 13 }}>Paste</Txt>
    </Pressable>
  );

  return (
    <View style={{ gap: 16 }}>
      {!!notice && (
        <Card glow={colors.amber} style={{ flexDirection: 'row', gap: 10, alignItems: 'flex-start' }}>
          <Ionicons name="alert-circle" size={20} color={colors.amber} style={{ marginTop: 1 }} />
          <Txt accessibilityRole="alert" style={{ flex: 1, color: colors.text, lineHeight: 21, fontSize: 14.5 }}>{notice}</Txt>
        </Card>
      )}
      <View style={{ gap: 8 }}>
        {label('SERVER ADDRESS', 'Starts with https:// or http://')}
        <View style={{ flexDirection: 'row', alignItems: 'center' }}>
          <View style={fieldBox('url')}>
            <TextInput value={url} onChangeText={(t) => { setUrl(t); setRes(null); }} placeholder="https://my-coach.example.com" placeholderTextColor={colors.faint}
              onFocus={() => setFocus('url')} onBlur={() => setFocus(null)} returnKeyType="next"
              autoCapitalize="none" autoCorrect={false} keyboardType="url" accessibilityLabel="Server address" style={inner} />
            <PasteBtn what="address" onPress={() => paste(setUrl)} />
          </View>
        </View>
      </View>
      <View style={{ gap: 8 }}>
        {label('ACCESS KEY', 'Hidden while you type. Never shown in the app unless you tap the eye.')}
        <View style={fieldBox('key')}>
          <TextInput value={token} onChangeText={(t) => { setToken(t); setRes(null); }} placeholder="The key your coach host gave you" placeholderTextColor={colors.faint}
            onFocus={() => setFocus('key')} onBlur={() => setFocus(null)} returnKeyType="done"
            autoCapitalize="none" autoCorrect={false} secureTextEntry={!show} accessibilityLabel="Access key" style={inner} />
          <Pressable accessibilityRole="button" accessibilityLabel={show ? 'Hide access key' : 'Show access key'} onPress={() => setShow(!show)} hitSlop={4}
            style={{ width: 44, height: 44, alignItems: 'center', justifyContent: 'center' }}>
            <Ionicons name={show ? 'eye-off' : 'eye'} size={22} color={colors.dim} />
          </Pressable>
          <PasteBtn what="access key" onPress={() => paste(setToken)} />
        </View>
      </View>

      {busy ? (
        <View accessibilityLiveRegion="polite" style={{ flexDirection: 'row', gap: 10, alignItems: 'center', justifyContent: 'center', minHeight: 54, borderRadius: 26, backgroundColor: '#2A2F3D' }}>
          <ActivityIndicator color={colors.lime} />
          <Txt style={{ fontFamily: font.semibold, color: colors.dim }}>Contacting your coach...</Txt>
        </View>
      ) : <GradientButton label="Test connection" icon="pulse" onPress={test} colorsArr={['#2A2F3D', '#2A2F3D']} dark={false} />}
      {!!res && (
        <View accessibilityRole="alert" accessibilityLiveRegion="polite" style={{ flexDirection: 'row', gap: 10, alignItems: 'flex-start', padding: 14, borderRadius: 16,
          backgroundColor: (res.ok ? colors.mint : colors.amber) + '18', borderWidth: 1, borderColor: (res.ok ? colors.mint : colors.amber) + '66' }}>
          <Ionicons name={res.ok ? 'checkmark-circle' : 'alert-circle'} size={22} color={res.ok ? colors.mint : colors.amber} />
          <View style={{ flex: 1, gap: 2 }}>
            <Txt style={{ fontFamily: font.bold, fontSize: 14.5, color: res.ok ? colors.mint : colors.amber }}>{res.ok ? 'Connected' : 'Not connected'}</Txt>
            <Txt style={{ color: colors.text, lineHeight: 20, fontSize: 14 }}>{res.message}</Txt>
          </View>
        </View>
      )}
      <View style={{ opacity: ready ? 1 : 0.5 }} pointerEvents={ready ? 'auto' : 'none'}>
        <GradientButton label={start.saved ? 'Save changes' : 'Save and connect'} icon="checkmark" onPress={save} />
      </View>
      {!ready && <Txt style={{ color: colors.faint, fontSize: 12.5, textAlign: 'center', marginTop: -6 }}>Enter the server address to continue.</Txt>}
      {start.saved && (
        <Pressable accessibilityRole="button" accessibilityLabel="Disconnect from this coach" onPress={disconnect} style={({ pressed }) => ({ alignItems: 'center', justifyContent: 'center', minHeight: 48, opacity: pressed ? 0.6 : 1 })}>
          <Txt style={{ color: colors.red, fontFamily: font.semibold }}>Disconnect</Txt>
        </Pressable>
      )}
    </View>
  );
}
