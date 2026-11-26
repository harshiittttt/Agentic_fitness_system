import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, Animated, KeyboardAvoidingView, Platform, Pressable, ScrollView, StatusBar, View } from 'react-native';
import { LinearGradient } from 'expo-linear-gradient';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Ionicons } from '@expo/vector-icons';
import {
  Inter_400Regular, Inter_500Medium, Inter_600SemiBold, Inter_700Bold, Inter_800ExtraBold, useFonts,
} from '@expo-google-fonts/inter';
import * as Haptics from './src/haptics';
import Today from './src/screens/Today';
import Coach from './src/screens/Coach';
import Insights from './src/screens/Insights';
import Nutrition from './src/screens/Nutrition';
import Setup from './src/screens/Setup';
import Onboarding from './src/screens/Onboarding';
import { configurePush, onPushTap } from './src/push';
import { API, AUTH_HELP, flushQueue, getAgentStatus, getCoach, getHealth, getPlan, onAuthFailure } from './src/api';
import { currentServer, installAuthFetch, loadServer, markLoaded } from './src/server';
import ServerForm from './src/screens/ServerForm';
import { loadHaptics } from './src/haptics';
import WakeCoach from './src/WakeCoach';
import { getTalkState, onTalkState, toggleTalk, TalkState } from './src/voiceTalk';
import { dlog, installCrashLog } from './src/diag';
import { onVoiceAction } from './src/voiceBus';
import { getVoiceScreen, setVoiceTab } from './src/voiceScreen';
import { blockedWhileLive } from './src/liveSession';
import { loadVoiceSettings, setRecognitionLang } from './src/voiceSettings';
import { colors, font, gradients } from './src/theme';
import { Ambient, Avatar, Card, FadeIn, GradientButton, OfflineBanner, Orb, OnlineCtx, ToastProvider, Txt, useToast } from './src/ui';

type Tab = 'today' | 'coach' | 'nutrition' | 'insights' | 'setup';
const TABS: { k: Tab; label: string; on: string; off: string }[] = [
  { k: 'today', label: 'Today', on: 'barbell', off: 'barbell-outline' },
  { k: 'coach', label: 'Coach', on: 'chatbubble-ellipses', off: 'chatbubble-ellipses-outline' },
  { k: 'nutrition', label: 'Fuel', on: 'nutrition', off: 'nutrition-outline' },
  { k: 'insights', label: 'Insights', on: 'stats-chart', off: 'stats-chart-outline' },
  { k: 'setup', label: 'Profile', on: 'person', off: 'person-outline' },
];

/** The mic: one tap = one spoken turn with the coach (the reliable voice path). Pulses while listening, spins to amber while the coach thinks. */
function TalkButton() {
  const toast = useToast();
  const [t, setT] = useState<TalkState>(getTalkState());
  const pulse = useRef(new Animated.Value(0)).current;
  useEffect(() => onTalkState(setT), []);
  useEffect(() => {
    if (t.phase === 'listening' || t.phase === 'followup') {
      const loop = Animated.loop(Animated.sequence([Animated.timing(pulse, { toValue: 1, duration: 700, useNativeDriver: true }), Animated.timing(pulse, { toValue: 0, duration: 700, useNativeDriver: true })]));
      loop.start();
      return () => { loop.stop(); pulse.setValue(0); };
    }
    pulse.setValue(0);
  }, [t.phase]);
  const hot = t.phase !== 'idle';
  const tint = t.phase === 'thinking' ? colors.amber : t.phase === 'speaking' ? colors.cyan : colors.lime;
  const label = t.session ? 'Stop the voice conversation' : t.phase === 'idle' ? 'Talk to your coach' : t.phase === 'speaking' ? 'Stop the coach talking' : t.phase === 'thinking' ? 'Cancel' : 'Stop listening';
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={label}
      onPress={() => { const ok = toggleTalk(); dlog('talk_button', { ok, phase: t.phase }); if (!ok) toast('Voice needs the installed app, not Expo Go. Type to your coach instead.', { tone: 'warn' }); }}
      style={({ pressed }) => ({ width: 64, height: 64, marginLeft: 10, alignSelf: 'center', alignItems: 'center', justifyContent: 'center', transform: [{ scale: pressed ? 0.94 : 1 }] })}>
      <Animated.View style={{ position: 'absolute', width: 64, height: 64, borderRadius: 32, backgroundColor: tint, opacity: pulse.interpolate({ inputRange: [0, 1], outputRange: [0, 0.35] }), transform: [{ scale: pulse.interpolate({ inputRange: [0, 1], outputRange: [0.9, 1.25] }) }] }} />
      <View style={{ width: 58, height: 58, borderRadius: 29, alignItems: 'center', justifyContent: 'center', backgroundColor: hot ? tint : colors.lime, shadowColor: tint, shadowOpacity: 0.5, shadowRadius: 12, shadowOffset: { width: 0, height: 4 }, elevation: 10 }}>
        <Ionicons name={t.phase === 'speaking' ? 'volume-high' : t.phase === 'thinking' ? 'ellipsis-horizontal' : t.session ? 'stop' : 'mic'} size={t.session && t.phase !== 'speaking' && t.phase !== 'thinking' ? 24 : 28} color="#07080B" />
      </View>
    </Pressable>
  );
}

/** Floating pill tab bar; the highlight springs to the active tab. */
function TabBar({ tab, onChange, badges = {} }: { tab: Tab; onChange: (t: Tab) => void; badges?: Partial<Record<Tab, number>> }) {
  const [w, setW] = useState(0);
  const x = useRef(new Animated.Value(0)).current;
  const idx = TABS.findIndex((t) => t.k === tab);
  useEffect(() => {
    if (w) Animated.spring(x, { toValue: idx * (w / TABS.length), useNativeDriver: true, damping: 16, stiffness: 200, mass: 0.7 }).start();
  }, [idx, w]);
  return (
    <View style={{ paddingHorizontal: 14, paddingTop: 6, paddingBottom: 8, flexDirection: 'row' }}>
      <View accessibilityRole="tablist" style={{ flex: 1, backgroundColor: colors.card, borderRadius: 30, borderWidth: 1, borderColor: 'rgba(255,255,255,0.10)', padding: 6, shadowColor: '#000', shadowOpacity: 0.45, shadowRadius: 14, shadowOffset: { width: 0, height: 6 }, elevation: 8 }}>
        <View onLayout={(e) => setW(e.nativeEvent.layout.width)} style={{ flexDirection: 'row' }}>
          {w > 0 && (
            <Animated.View style={{ position: 'absolute', top: 0, bottom: 0, width: w / TABS.length, borderRadius: 24, backgroundColor: 'rgba(198,255,61,0.14)', borderWidth: 1, borderColor: 'rgba(198,255,61,0.28)', transform: [{ translateX: x }] }} />
          )}
          {TABS.map((t) => {
            const active = tab === t.k;
            return (
              <Pressable key={t.k} accessibilityRole="tab" accessibilityLabel={badges[t.k] ? `${t.label}, ${badges[t.k]} waiting` : t.label} accessibilityState={{ selected: active }} style={{ flex: 1, alignItems: 'center', gap: 2, paddingVertical: 9, minHeight: 56 }}
                onPress={() => { if (!active) { Haptics.selectionAsync(); onChange(t.k); } }}>
                <View>
                  <Ionicons name={(active ? t.on : t.off) as any} size={22} color={active ? colors.lime : colors.faint} />
                  {!!badges[t.k] && (
                    <View style={{ position: 'absolute', top: -4, right: -9, minWidth: 16, height: 16, borderRadius: 8, paddingHorizontal: 4, backgroundColor: colors.red, alignItems: 'center', justifyContent: 'center' }}>
                      <Txt maxFontSizeMultiplier={1.1} style={{ fontFamily: font.bold, fontSize: 10, color: '#fff' }}>{badges[t.k]}</Txt>
                    </View>
                  )}
                </View>
                <Txt numberOfLines={1} maxFontSizeMultiplier={1.15} style={{ fontSize: 11, fontFamily: active ? font.bold : font.medium, color: active ? colors.lime : colors.faint }}>{t.label}</Txt>
              </Pressable>
            );
          })}
        </View>
      </View>
      <TalkButton />
    </View>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>('today');
  const [loaded] = useFonts({ Inter_400Regular, Inter_500Medium, Inter_600SemiBold, Inter_700Bold, Inter_800ExtraBold });
  const [gate, setGate] = useState<'loading' | 'offline' | 'connect' | 'onboard' | 'ready'>('loading');
  const [notice, setNotice] = useState('');
  const authFailed = useRef(false);
  const booted = useRef(false);
  const [err, setErr] = useState('');
  const [pending, setPending] = useState(0);
  const [insightsSeg, setInsightsSeg] = useState<'trends' | 'decisions' | 'team' | undefined>(undefined);
  const check = useCallback(async () => {
    setGate('loading');
    authFailed.current = false;
    if (!booted.current) { installAuthFetch(); markLoaded(await loadServer()); booted.current = true; }
    try {
      const plan = await getPlan();
      if (authFailed.current) return;               // a 401 already sent us to the connect screen
      if (plan) getCoach().then((c) => setRecognitionLang(c.tts?.lang)).catch(() => {});
      setGate(plan ? 'ready' : 'onboard');
    } catch (e: any) {
      if (authFailed.current) return;
      setErr(String(e?.message ?? e));
      // Nothing saved and the default (local) address is not answering: ask for the server instead of a dead end.
      if (!currentServer().saved) { setNotice(''); setGate('connect'); } else setGate('offline');
    }
  }, []);
  // Any 401 from the server (wrong or missing access key) opens the connect screen with a plain message.
  useEffect(() => onAuthFailure(() => { authFailed.current = true; setNotice(AUTH_HELP); setGate('connect'); }), []);
  useEffect(() => { installCrashLog(); loadHaptics(); loadVoiceSettings(); check(); }, [check]);
  // A red badge on Insights when an agent is waiting for the athlete's OK (polled; cheap, and silent on failure).
  useEffect(() => {
    if (gate !== 'ready') return;
    let live = true;
    const poll = () => getAgentStatus().then((s) => { if (live) setPending(s.pending.length); }).catch(() => {});
    poll();
    const t = setInterval(poll, 20000);
    return () => { live = false; clearInterval(t); };
  }, [gate]);

  // Tapping a push notification opens the screen named in its data (today | coach | decisions | insights | nutrition | setup).
  useEffect(() => {
    if (gate !== 'ready') return;
    configurePush();
    return onPushTap((screen) => {
      if (screen === 'decisions') { setInsightsSeg('decisions'); setTab('insights'); return; }
      if ((['today', 'coach', 'nutrition', 'insights', 'setup'] as string[]).includes(screen)) setTab(screen as Tab);
    });
  }, [gate]);

  // Voice control ("Hey coach, open Fuel"): the active tab is shared with WakeCoach, and navigate/close/open come back here.
  useEffect(() => { setVoiceTab(tab); }, [tab]);
  useEffect(() => {
    if (gate !== 'ready') return;
    return onVoiceAction((a) => {
      // The full-screen workout covers the tab bar: switching the tab under it would unmount it and end the voice session (E-008). A misheard "open Fuel" is dropped.
      if (blockedWhileLive(getVoiceScreen(), a as any)) { dlog('live_nav_blocked', { type: a.type, to: (a as any).tab ?? (a as any).screen }); return true; }
      if (a.type === 'navigate') { if (a.tab !== 'insights') setInsightsSeg(undefined); setTab(a.tab as Tab); return true; }
      if (a.type === 'open' && a.screen === 'decisions') { setInsightsSeg('decisions'); setTab('insights'); return true; }
      if (a.type === 'open' && a.screen === 'roundtable') { setInsightsSeg('team'); setTab('insights'); return true; }
      if (a.type === 'open' && a.screen === 'diet') { setTab('nutrition'); return false; }   // Fuel owns the Diet screen: it takes the action when it mounts
      if (a.type === 'close') return true;      // App owns no full-screen; the screen that has one listens too. Handled so it is not queued.
      return false;
    });
  }, [gate]);

  if (!loaded || gate === 'loading') return (
    <View accessibilityLabel="Loading your coach" accessibilityLiveRegion="polite" style={{ flex: 1, backgroundColor: colors.bg, alignItems: 'center', justifyContent: 'center', gap: 16 }}>
      <StatusBar barStyle="light-content" />
      <Orb state="idle" size={120} />
      {loaded && <Txt style={{ color: colors.dim, fontSize: 14, fontFamily: font.medium, marginTop: -20 }}>Waking your coach...</Txt>}
      {!loaded && <ActivityIndicator color={colors.lime} />}
    </View>
  );

  if (gate === 'offline') return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={['top', 'bottom']}>
      <StatusBar barStyle="light-content" />
      <ScrollView contentContainerStyle={{ padding: 24, gap: 16, flexGrow: 1, justifyContent: 'center' }}>
        <FadeIn style={{ gap: 14 }}>
          <Avatar color={colors.amber} icon="cloud-offline" size={64} ring />
          <Txt accessibilityRole="header" style={{ fontFamily: font.black, fontSize: 28, letterSpacing: -0.5 }}>Can't reach your coach</Txt>
          <Txt style={{ color: colors.dim, lineHeight: 22 }}>Nothing is lost. Your plan and history are safe on the server, and anything you log is kept on this phone until you are back online.</Txt>
          <Card style={{ gap: 4 }}>
            <Txt style={{ color: colors.dim, fontSize: 12, letterSpacing: 0.8, fontFamily: font.semibold }}>LOOKING FOR YOUR COACH AT</Txt>
            <Txt selectable style={{ fontFamily: font.bold, color: colors.lime, fontSize: 15 }}>{API}</Txt>
            {!!err && <Txt style={{ color: colors.amber, fontSize: 12.5, lineHeight: 18, marginTop: 4 }}>Error: {err}</Txt>}
          </Card>
          <Card style={{ gap: 8 }}>
            <Txt style={{ fontFamily: font.bold, fontSize: 15 }}>Things to check</Txt>
            {['The coach server is running and reachable.', 'Your phone has a connection (same Wi-Fi if the server is on your computer).', 'The address above is right. If not, change it below.'].map((t) => (
              <View key={t} style={{ flexDirection: 'row', gap: 10 }}>
                <Ionicons name="ellipse" size={6} color={colors.faint} style={{ marginTop: 8 }} />
                <Txt style={{ flex: 1, color: colors.dim, lineHeight: 21, fontSize: 14 }}>{t}</Txt>
              </View>
            ))}
          </Card>
          <GradientButton label="Try again" icon="refresh" onPress={check} />
          <GradientButton label="Change server" icon="server" onPress={() => { setNotice(''); setGate('connect'); }} colorsArr={['#2A2F3D', '#2A2F3D']} dark={false} />
        </FadeIn>
      </ScrollView>
    </SafeAreaView>
  );

  if (gate === 'connect') return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={['top', 'bottom']}>
      <StatusBar barStyle="light-content" />
      <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined} style={{ flex: 1 }}>
        <ScrollView contentContainerStyle={{ padding: 24, gap: 16, flexGrow: 1, justifyContent: 'center' }} keyboardShouldPersistTaps="handled">
          <FadeIn style={{ gap: 14 }}>
            <LinearGradient colors={gradients.lime} style={{ width: 64, height: 64, borderRadius: 22, alignItems: 'center', justifyContent: 'center' }}>
              <Ionicons name="link" size={30} color="#07080B" />
            </LinearGradient>
            <Txt accessibilityRole="header" style={{ fontFamily: font.black, fontSize: 30, letterSpacing: -0.6, lineHeight: 36 }}>Connect to your coach</Txt>
            <Txt style={{ color: colors.dim, lineHeight: 22, fontSize: 15.5 }}>Your coaching team runs on a server you control. Enter its address and the access key you were given. You only do this once on this phone.</Txt>
            <View style={{ flexDirection: 'row', gap: 8, alignItems: 'center' }}>
              <Ionicons name="lock-closed" size={14} color={colors.faint} />
              <Txt style={{ color: colors.faint, fontSize: 12.5, flex: 1, lineHeight: 18 }}>The key is stored on this phone only and sent only to your server.</Txt>
            </View>
          </FadeIn>
          <FadeIn delay={120}><ServerForm notice={notice} onSaved={check} /></FadeIn>
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );

  if (gate === 'onboard') return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={['top', 'bottom']}>
      <StatusBar barStyle="light-content" />
      <Onboarding onDone={() => setGate('ready')} />
    </SafeAreaView>
  );

  const openTeam = () => { setInsightsSeg('decisions'); setTab('insights'); };
  return (
    <View style={{ flex: 1, backgroundColor: colors.bg }}>
      <Ambient />
      <SafeAreaView style={{ flex: 1, backgroundColor: 'transparent' }} edges={['top', 'bottom']}>
        <StatusBar barStyle="light-content" />
        <ToastProvider>
          <Shell>
            <FadeIn key={tab} style={{ flex: 1 }} distance={8}>
              {tab === 'today' && <Today onOpenTeam={openTeam} onOpenProfile={() => setTab("setup")} />}{tab === 'coach' && <Coach onOpenTeam={openTeam} />}{tab === 'nutrition' && <Nutrition />}
              {tab === 'insights' && <Insights key={insightsSeg ?? 'auto'} initialSeg={insightsSeg} pending={pending} onPending={setPending} onOpenProfile={() => setTab("setup")} />}{tab === 'setup' && <Setup onReset={check} />}
            </FadeIn>
            <TabBar tab={tab} onChange={(t) => { if (t !== 'insights') setInsightsSeg(undefined); setTab(t); }} badges={{ insights: pending }} />
          </Shell>
          <WakeCoach />
        </ToastProvider>
      </SafeAreaView>
    </View>
  );
}

/** Watches whether the coach can be reached. Shows one banner on every tab, tells screens to reload when it comes back, and re-sends anything queued offline. */
function Shell({ children }: { children: React.ReactNode }) {
  const toast = useToast();
  const [online, setOnline] = useState(true);
  const [epoch, setEpoch] = useState(0);
  const [retrying, setRetrying] = useState(false);
  const fails = useRef(0);
  const was = useRef(true);
  const probe = useCallback(async (manual = false) => {
    if (manual) setRetrying(true);
    let ok = false;
    try { ok = await getHealth(); } catch { ok = false; }
    fails.current = ok ? 0 : fails.current + 1;
    const next = ok || (!manual && fails.current < 2) ? (ok ? true : was.current) : false;
    if (next !== was.current) {
      was.current = next; setOnline(next);
      if (next) { setEpoch((e) => e + 1); flushQueue().then((n) => toast(n > 0 ? `Back online. Sent ${n} saved ${n === 1 ? 'entry' : 'entries'}.` : 'Back online.')).catch(() => toast('Back online.')); }
    }
    if (manual) { setRetrying(false); if (!ok) toast("Still can't reach your coach.", { tone: 'warn' }); }
  }, [toast]);
  useEffect(() => {
    const t = setInterval(() => probe(), 10000);
    return () => clearInterval(t);
  }, [probe]);
  return (
    <OnlineCtx.Provider value={{ online, epoch, recheck: () => probe(true) }}>
      {!online && <OfflineBanner onRetry={() => probe(true)} retrying={retrying} />}
      {children}
    </OnlineCtx.Provider>
  );
}
