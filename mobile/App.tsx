import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, Animated, Pressable, StatusBar, View } from 'react-native';
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
import { API, flushQueue, getAgentStatus, getCoach, getHealth, getPlan } from './src/api';
import { loadHaptics } from './src/haptics';
import { loadVoiceSettings, setRecognitionLang } from './src/voiceSettings';
import { colors, font } from './src/theme';
import { Ambient, FadeIn, GradientButton, OfflineBanner, OnlineCtx, ToastProvider, Txt, useToast } from './src/ui';

type Tab = 'today' | 'coach' | 'nutrition' | 'insights' | 'setup';
const TABS: { k: Tab; label: string; on: string; off: string }[] = [
  { k: 'today', label: 'Today', on: 'barbell', off: 'barbell-outline' },
  { k: 'coach', label: 'Coach', on: 'chatbubble-ellipses', off: 'chatbubble-ellipses-outline' },
  { k: 'nutrition', label: 'Fuel', on: 'nutrition', off: 'nutrition-outline' },
  { k: 'insights', label: 'Insights', on: 'stats-chart', off: 'stats-chart-outline' },
  { k: 'setup', label: 'Profile', on: 'person', off: 'person-outline' },
];

/** Floating pill tab bar; the highlight springs to the active tab. */
function TabBar({ tab, onChange, badges = {} }: { tab: Tab; onChange: (t: Tab) => void; badges?: Partial<Record<Tab, number>> }) {
  const [w, setW] = useState(0);
  const x = useRef(new Animated.Value(0)).current;
  const idx = TABS.findIndex((t) => t.k === tab);
  useEffect(() => {
    if (w) Animated.spring(x, { toValue: idx * (w / TABS.length), useNativeDriver: true, damping: 16, stiffness: 200, mass: 0.7 }).start();
  }, [idx, w]);
  return (
    <View style={{ paddingHorizontal: 14, paddingTop: 6, paddingBottom: 8 }}>
      <View style={{ backgroundColor: colors.card, borderRadius: 30, borderWidth: 1, borderColor: colors.border, padding: 6 }}>
        <View onLayout={(e) => setW(e.nativeEvent.layout.width)} style={{ flexDirection: 'row' }}>
          {w > 0 && (
            <Animated.View style={{ position: 'absolute', top: 0, bottom: 0, width: w / TABS.length, borderRadius: 24, backgroundColor: 'rgba(198,255,61,0.14)', transform: [{ translateX: x }] }} />
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
                      <Txt style={{ fontFamily: font.bold, fontSize: 10, color: '#fff' }}>{badges[t.k]}</Txt>
                    </View>
                  )}
                </View>
                <Txt style={{ fontSize: 11, fontFamily: active ? font.bold : font.medium, color: active ? colors.lime : colors.faint }}>{t.label}</Txt>
              </Pressable>
            );
          })}
        </View>
      </View>
    </View>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>('today');
  const [loaded] = useFonts({ Inter_400Regular, Inter_500Medium, Inter_600SemiBold, Inter_700Bold, Inter_800ExtraBold });
  const [gate, setGate] = useState<'loading' | 'offline' | 'onboard' | 'ready'>('loading');
  const [err, setErr] = useState('');
  const [pending, setPending] = useState(0);
  const [insightsSeg, setInsightsSeg] = useState<'trends' | 'decisions' | 'team' | undefined>(undefined);
  const check = useCallback(async () => {
    setGate('loading');
    try {
      const plan = await getPlan();
      if (plan) getCoach().then((c) => setRecognitionLang(c.tts?.lang)).catch(() => {});
      setGate(plan ? 'ready' : 'onboard');
    } catch (e: any) { setErr(String(e?.message ?? e)); setGate('offline'); }
  }, []);
  useEffect(() => { loadHaptics(); loadVoiceSettings(); check(); }, [check]);
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

  if (!loaded || gate === 'loading') return <View style={{ flex: 1, backgroundColor: colors.bg, justifyContent: 'center' }}><ActivityIndicator color={colors.lime} /></View>;

  if (gate === 'offline') return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg, justifyContent: 'center', padding: 28, gap: 14 }}>
      <StatusBar barStyle="light-content" />
      <Ionicons name="cloud-offline" size={44} color={colors.amber} />
      <Txt style={{ fontFamily: font.black, fontSize: 28 }}>Can't reach your coach</Txt>
      <Txt style={{ color: colors.dim, lineHeight: 22 }}>The app is looking for the backend at:</Txt>
      <Txt style={{ fontFamily: font.bold, color: colors.lime, fontSize: 15 }}>{API}</Txt>
      {!!err && <Txt style={{ color: colors.amber, fontSize: 12.5, lineHeight: 18 }}>Error: {err}</Txt>}
      <Txt style={{ color: colors.dim, lineHeight: 22 }}>Make sure the backend is running, your phone is on the same Wi-Fi, and EXPO_PUBLIC_API_URL in mobile/.env is your computer's address. Then restart Expo with a cleared cache.</Txt>
      <GradientButton label="Try again" icon="refresh" onPress={check} />
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
