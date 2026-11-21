import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, Animated, Easing, Pressable, StyleProp, StyleSheet, Text, TextProps, TextStyle, View, ViewStyle } from 'react-native';
import { LinearGradient } from 'expo-linear-gradient';
import Svg, { Circle, Defs, LinearGradient as SvgGrad, RadialGradient as SvgRadial, Rect, Stop } from 'react-native-svg';
import { Ionicons } from '@expo/vector-icons';
import { colors, font, gradients, radius } from './theme';
import * as Haptics from './haptics';

export const Txt = ({ style, ...p }: TextProps) => <Text {...p} style={[{ color: colors.text, fontFamily: font.regular }, style]} />;

export const Card = ({ children, style, onPress, glow }: { children: React.ReactNode; style?: StyleProp<ViewStyle>; onPress?: () => void; glow?: string }) => {
  const body = (
    <LinearGradient colors={['rgba(31,36,49,0.96)', 'rgba(17,20,28,0.96)']} start={{ x: 0, y: 0 }} end={{ x: 1, y: 1 }}
      style={[s.card, glow ? { borderColor: glow + '55', shadowColor: glow, shadowOpacity: 0.28, shadowRadius: 22 } : null, style]}>{children}</LinearGradient>
  );
  return onPress ? (
    <Pressable onPress={onPress} style={({ pressed }) => ({ transform: [{ scale: pressed ? 0.98 : 1 }], opacity: pressed ? 0.92 : 1 })}>{body}</Pressable>
  ) : body;
};

export function Chip({ label, active, onPress, color = colors.lime }: { label: string; active?: boolean; onPress?: () => void; color?: string }) {
  return (
    <Pressable onPress={onPress} accessibilityRole="button" accessibilityLabel={label} accessibilityState={{ selected: !!active }} style={[s.chip, active && { backgroundColor: color, borderColor: color }]}>
      <Txt style={{ fontFamily: font.semibold, fontSize: 13, color: active ? '#07080B' : colors.dim }}>{label}</Txt>
    </Pressable>
  );
}

export function GradientButton({ label, onPress, icon, colorsArr = gradients.lime, dark = true }: {
  label: string; onPress: () => void; icon?: string; colorsArr?: readonly [string, string]; dark?: boolean;
}) {
  return (
    <Pressable onPress={onPress} accessibilityRole="button" accessibilityLabel={label} style={({ pressed }) => [{ transform: [{ scale: pressed ? 0.97 : 1 }] }]}>
      <LinearGradient colors={colorsArr as any} start={{ x: 0, y: 0 }} end={{ x: 1, y: 1 }} style={s.gbtn}>
        {icon && <Ionicons name={icon as any} size={20} color={dark ? '#07080B' : '#fff'} style={{ marginRight: 8 }} />}
        <Txt style={{ fontFamily: font.bold, fontSize: 16, color: dark ? '#07080B' : '#fff' }}>{label}</Txt>
      </LinearGradient>
    </Pressable>
  );
}

/** Circular progress ring with gradient stroke and centered content. */
export function Ring({ size = 120, stroke = 10, progress, children, from = colors.lime, to = colors.mint }: {
  size?: number; stroke?: number; progress: number; children?: React.ReactNode; from?: string; to?: string;
}) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const anim = useRef(new Animated.Value(0)).current;
  useEffect(() => {
    Animated.timing(anim, { toValue: Math.max(0, Math.min(1, progress)), duration: 900, easing: Easing.out(Easing.cubic), useNativeDriver: false }).start();
  }, [progress]);
  const AnimatedCircle = Animated.createAnimatedComponent(Circle);
  return (
    <View style={{ width: size, height: size, alignItems: 'center', justifyContent: 'center' }}>
      <Svg width={size} height={size} style={StyleSheet.absoluteFill}>
        <Defs>
          <SvgGrad id="g" x1="0" y1="0" x2="1" y2="1">
            <Stop offset="0" stopColor={from} /><Stop offset="1" stopColor={to} />
          </SvgGrad>
        </Defs>
        <Circle cx={size / 2} cy={size / 2} r={r} stroke="rgba(255,255,255,0.08)" strokeWidth={stroke} fill="none" />
        <AnimatedCircle
          cx={size / 2} cy={size / 2} r={r} stroke="url(#g)" strokeWidth={stroke} fill="none" strokeLinecap="round"
          strokeDasharray={`${c} ${c}`}
          strokeDashoffset={anim.interpolate({ inputRange: [0, 1], outputRange: [c, 0] }) as any}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
        />
      </Svg>
      {children}
    </View>
  );
}

/** Big mic button with an expanding pulse while listening. */
export function MicButton({ listening, onPress, disabled }: { listening: boolean; onPress: () => void; disabled?: boolean }) {
  const pulse = useRef(new Animated.Value(0)).current;
  useEffect(() => {
    if (!listening) { pulse.setValue(0); return; }
    const loop = Animated.loop(Animated.timing(pulse, { toValue: 1, duration: 1300, easing: Easing.out(Easing.quad), useNativeDriver: true }));
    loop.start();
    return () => loop.stop();
  }, [listening]);
  const ringStyle = {
    opacity: pulse.interpolate({ inputRange: [0, 1], outputRange: [0.5, 0] }),
    transform: [{ scale: pulse.interpolate({ inputRange: [0, 1], outputRange: [1, 2] }) }],
  };
  return (
    <View style={{ alignItems: 'center', justifyContent: 'center', height: 96, width: 96 }}>
      {listening && <Animated.View style={[s.pulse, ringStyle]} />}
      <Pressable onPress={onPress} disabled={disabled} accessibilityRole="button" accessibilityLabel={listening ? 'Stop listening' : 'Start voice input'}>
        <LinearGradient colors={listening ? gradients.warm : gradients.lime} style={s.mic}>
          <Ionicons name={listening ? 'stop' : 'mic'} size={34} color="#07080B" />
        </LinearGradient>
      </Pressable>
    </View>
  );
}

export function Bar({ value, color = colors.lime }: { value: number; color?: string }) {
  return (
    <View style={s.barTrack}>
      <View style={{ width: `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`, height: '100%', backgroundColor: color, borderRadius: 4 }} />
    </View>
  );
}

export const SectionTitle = ({ children, right }: { children: string; right?: React.ReactNode }) => (
  <View style={{ flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', marginTop: 10 }}>
    <View style={{ flexDirection: 'row', alignItems: 'center', gap: 9 }}>
      <View style={{ width: 4, height: 18, borderRadius: 2, backgroundColor: colors.lime }} />
      <Txt style={{ fontFamily: font.bold, fontSize: 18, letterSpacing: -0.2 }}>{children}</Txt>
    </View>{right}
  </View>
);

/** One soft radial glow: a real radial gradient, so it melts into the background with no rim or banding. */
function Glow({ color, size, x, y, strength = 0.2 }: { color: string; size: number; x: number; y: number; strength?: number }) {
  const id = 'glow' + color.replace('#', '') + size;
  return (
    <Svg width={size} height={size} pointerEvents="none" style={{ position: 'absolute', left: x - size / 2, top: y - size / 2 }}>
      <Defs>
        <SvgRadial id={id} cx="50%" cy="50%" r="50%">
          <Stop offset="0" stopColor={color} stopOpacity={strength} />
          <Stop offset="0.45" stopColor={color} stopOpacity={strength * 0.4} />
          <Stop offset="1" stopColor={color} stopOpacity={0} />
        </SvgRadial>
      </Defs>
      <Rect width={size} height={size} fill={`url(#${id})`} />
    </Svg>
  );
}

/** Soft colour glows behind everything: gives the flat black depth and a sense of place. Put it first inside a root View. */
export function Ambient() {
  return (
    <View pointerEvents="none" style={[StyleSheet.absoluteFill, { overflow: 'hidden' }]}>
      <Glow color="#C6FF3D" size={520} x={380} y={40} strength={0.20} />
      <Glow color="#8B6CFF" size={560} x={-20} y={620} strength={0.20} />
      <Glow color="#38D3FF" size={480} x={360} y={980} strength={0.12} />
    </View>
  );
}

/** An agent's face: a glowing coloured disc with its icon. The team should feel like people, not rows. */
export function Avatar({ color, icon, size = 44, ring = false, dim = false }: { color: string; icon: string; size?: number; ring?: boolean; dim?: boolean }) {
  return (
    <View style={{ width: size, height: size, borderRadius: size / 2, alignItems: 'center', justifyContent: 'center', opacity: dim ? 0.45 : 1,
      backgroundColor: color + '24', borderWidth: ring ? 2 : 1, borderColor: color + (ring ? 'cc' : '55'), shadowColor: color, shadowOpacity: ring ? 0.55 : 0.25, shadowRadius: ring ? 14 : 8, shadowOffset: { width: 0, height: 0 } }}>
      <Ionicons name={icon as any} size={size * 0.46} color={color} />
    </View>
  );
}

/** A big number with a small label under it. */
export function Stat({ value, label, color = colors.text }: { value: string | number; label: string; color?: string }) {
  return (
    <View style={{ gap: 2 }}>
      <Txt style={{ fontFamily: font.black, fontSize: 26, letterSpacing: -0.6, color }}>{value}</Txt>
      <Txt style={{ fontFamily: font.semibold, fontSize: 11, letterSpacing: 1, color: colors.dim }}>{label.toUpperCase()}</Txt>
    </View>
  );
}

/** The coach's presence: breathes when idle, ripples when listening, pulses quickly when speaking. */
export function Orb({ state, size = 190, color = colors.lime }: { state: 'idle' | 'listening' | 'thinking' | 'speaking'; size?: number; color?: string }) {
  const breath = useRef(new Animated.Value(0)).current;
  useEffect(() => {
    const dur = state === 'speaking' ? 520 : state === 'listening' ? 1000 : state === 'thinking' ? 450 : 2400;
    breath.setValue(0);
    const loop = Animated.loop(Animated.sequence([
      Animated.timing(breath, { toValue: 1, duration: dur, easing: Easing.inOut(Easing.quad), useNativeDriver: true }),
      Animated.timing(breath, { toValue: 0, duration: dur, easing: Easing.inOut(Easing.quad), useNativeDriver: true }),
    ]));
    loop.start();
    return () => loop.stop();
  }, [state]);
  const amp = state === 'speaking' ? 0.14 : state === 'listening' ? 0.09 : state === 'thinking' ? 0.05 : 0.04;
  const core = state === 'listening' ? ['#38D3FF', '#8B6CFF'] : state === 'thinking' ? ['#FFB84D', '#FF5C9E'] : [color, '#34E3A4'];
  const scale = breath.interpolate({ inputRange: [0, 1], outputRange: [1, 1 + amp] });
  const halo = breath.interpolate({ inputRange: [0, 1], outputRange: [1.05, 1.35] });
  const haloOp = breath.interpolate({ inputRange: [0, 1], outputRange: [0.28, 0.06] });
  const icon = state === 'listening' ? 'mic' : state === 'thinking' ? 'ellipsis-horizontal' : state === 'speaking' ? 'volume-high' : 'sparkles';
  return (
    <View style={{ width: size * 1.5, height: size * 1.5, alignItems: 'center', justifyContent: 'center' }}>
      <Animated.View style={{ position: 'absolute', width: size, height: size, borderRadius: size / 2, backgroundColor: core[0], opacity: haloOp, transform: [{ scale: halo }] }} />
      <Animated.View style={{ transform: [{ scale }] }}>
        <LinearGradient colors={core as any} start={{ x: 0.1, y: 0 }} end={{ x: 0.9, y: 1 }}
          style={{ width: size * 0.78, height: size * 0.78, borderRadius: size, alignItems: 'center', justifyContent: 'center' }}>
          <Ionicons name={icon as any} size={size * 0.28} color="#07080B" />
        </LinearGradient>
      </Animated.View>
    </View>
  );
}

/** Fades and slides content in on mount. Stagger a list by passing delay={index * 60}. */
export function FadeIn({ children, delay = 0, style, distance = 14 }: { children: React.ReactNode; delay?: number; style?: StyleProp<ViewStyle>; distance?: number }) {
  const v = useRef(new Animated.Value(0)).current;
  useEffect(() => {
    Animated.timing(v, { toValue: 1, duration: 420, delay, easing: Easing.out(Easing.cubic), useNativeDriver: true }).start();
  }, []);
  return (
    <Animated.View style={[style, { opacity: v, transform: [{ translateY: v.interpolate({ inputRange: [0, 1], outputRange: [distance, 0] }) }] }]}>
      {children}
    </Animated.View>
  );
}

/** A number that counts up to its value (and re-animates when the value changes). */
export function CountUp({ value, style, duration = 800, decimals = 0, suffix = '' }: { value: number; style?: StyleProp<TextStyle>; duration?: number; decimals?: number; suffix?: string }) {
  const v = useRef(new Animated.Value(0)).current;
  const [text, setText] = useState((0).toFixed(decimals));
  useEffect(() => {
    const id = v.addListener(({ value: x }) => setText(x.toFixed(decimals)));
    Animated.timing(v, { toValue: value, duration, easing: Easing.out(Easing.cubic), useNativeDriver: false }).start();
    return () => v.removeListener(id);
  }, [value]);
  return <Txt style={style}>{text}{suffix}</Txt>;
}

/** Pulsing placeholder shown while data loads, instead of a blank screen. */
export function Skeleton({ height = 80, width = '100%', radius = 22, style }: { height?: number; width?: number | string; radius?: number; style?: StyleProp<ViewStyle> }) {
  const v = useRef(new Animated.Value(0.35)).current;
  useEffect(() => {
    const loop = Animated.loop(Animated.sequence([
      Animated.timing(v, { toValue: 0.85, duration: 800, useNativeDriver: true }),
      Animated.timing(v, { toValue: 0.35, duration: 800, useNativeDriver: true }),
    ]));
    loop.start();
    return () => loop.stop();
  }, []);
  return <Animated.View style={[{ height, width: width as any, borderRadius: radius, backgroundColor: colors.cardHi, opacity: v }, style]} />;
}

const CONFETTI_COLORS = [colors.lime, colors.mint, colors.cyan, colors.violet, colors.pink, colors.amber];

/** Celebration burst. Increment `burst` to fire it. Renders over its parent and ignores touches. */
export function Confetti({ burst }: { burst: number }) {
  const N = 30;
  const parts = useRef(Array.from({ length: N }, (_, i) => ({
    v: new Animated.Value(0), angle: (Math.PI * 2 * i) / N + Math.random() * 0.5, dist: 110 + Math.random() * 170,
    spin: Math.random() * 6 - 3, color: CONFETTI_COLORS[i % CONFETTI_COLORS.length], size: 7 + Math.random() * 8,
  }))).current;
  useEffect(() => {
    if (!burst) return;
    parts.forEach((p) => p.v.setValue(0));
    Animated.stagger(8, parts.map((p) => Animated.timing(p.v, { toValue: 1, duration: 1200, easing: Easing.out(Easing.cubic), useNativeDriver: true }))).start();
  }, [burst]);
  if (!burst) return null;
  return (
    <View pointerEvents="none" style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, alignItems: 'center', justifyContent: 'center' }}>
      {parts.map((p, i) => {
        const tx = p.v.interpolate({ inputRange: [0, 1], outputRange: [0, Math.cos(p.angle) * p.dist] });
        const ty = p.v.interpolate({ inputRange: [0, 0.55, 1], outputRange: [0, Math.sin(p.angle) * p.dist - 50, Math.sin(p.angle) * p.dist + 140] });
        const op = p.v.interpolate({ inputRange: [0, 0.08, 0.8, 1], outputRange: [0, 1, 1, 0] });
        const rot = p.v.interpolate({ inputRange: [0, 1], outputRange: ['0deg', `${Math.round(p.spin * 180)}deg`] });
        return <Animated.View key={i} style={{ position: 'absolute', width: p.size, height: p.size * 0.55, borderRadius: 2, backgroundColor: p.color, opacity: op, transform: [{ translateX: tx }, { translateY: ty }, { rotate: rot }] }} />;
      })}
    </View>
  );
}

/** Two-option pill switch with a sliding highlight. */
export function Segmented<T extends string>({ options, value, onChange }: { options: { key: T; label: string }[]; value: T; onChange: (k: T) => void }) {
  const [w, setW] = useState(0);
  const x = useRef(new Animated.Value(0)).current;
  const idx = Math.max(0, options.findIndex((o) => o.key === value));
  useEffect(() => {
    if (w) Animated.spring(x, { toValue: idx * (w / options.length), useNativeDriver: true, damping: 18, stiffness: 220, mass: 0.7 }).start();
  }, [idx, w]);
  return (
    <View style={{ backgroundColor: colors.card, borderRadius: 16, padding: 4, borderWidth: 1, borderColor: colors.border }}>
      <View onLayout={(e) => setW(e.nativeEvent.layout.width)} style={{ flexDirection: 'row' }}>
        {w > 0 && <Animated.View style={{ position: 'absolute', top: 0, bottom: 0, width: w / options.length, borderRadius: 12, backgroundColor: colors.cardHi, transform: [{ translateX: x }] }} />}
        {options.map((o) => (
          <Pressable key={o.key} onPress={() => onChange(o.key)} accessibilityRole="tab" accessibilityLabel={o.label} accessibilityState={{ selected: o.key === value }} style={{ flex: 1, paddingVertical: 12, minHeight: 44, alignItems: 'center', justifyContent: 'center' }}>
            <Txt numberOfLines={1} style={{ fontFamily: font.bold, fontSize: 14, color: o.key === value ? colors.text : colors.dim }}>{o.label}</Txt>
          </Pressable>
        ))}
      </View>
    </View>
  );
}

const s = StyleSheet.create({
  card: { borderRadius: radius.lg, padding: 18, borderWidth: 1, borderColor: 'rgba(255,255,255,0.08)', borderTopColor: 'rgba(255,255,255,0.16)', gap: 6,
    shadowColor: '#000', shadowOpacity: 0.4, shadowRadius: 18, shadowOffset: { width: 0, height: 10 }, elevation: 5 },
  chip: { paddingHorizontal: 16, paddingVertical: 10, minHeight: 44, justifyContent: 'center', borderRadius: 999, borderWidth: 1, borderColor: colors.border, backgroundColor: colors.card },
  gbtn: { flexDirection: 'row', alignItems: 'center', justifyContent: 'center', paddingVertical: 17, borderRadius: radius.lg },
  mic: { width: 80, height: 80, borderRadius: 40, alignItems: 'center', justifyContent: 'center' },
  pulse: { position: 'absolute', width: 80, height: 80, borderRadius: 40, backgroundColor: colors.red },
  barTrack: { height: 6, borderRadius: 4, backgroundColor: 'rgba(255,255,255,0.08)', overflow: 'hidden' },
});

/** A round icon-only button with a 44px touch target and a spoken label (required: icon-only buttons need one). */
export function IconButton({ icon, label, onPress, color = colors.dim, size = 22, bg, disabled }: { icon: string; label: string; onPress: () => void; color?: string; size?: number; bg?: string; disabled?: boolean }) {
  return (
    <Pressable onPress={onPress} disabled={disabled} accessibilityRole="button" accessibilityLabel={label} hitSlop={6}
      style={({ pressed }) => ({ width: 44, height: 44, borderRadius: 22, alignItems: 'center', justifyContent: 'center', backgroundColor: bg ?? 'transparent', opacity: disabled ? 0.4 : pressed ? 0.7 : 1 })}>
      <Ionicons name={icon as any} size={size} color={color} />
    </Pressable>
  );
}

/** A calm "nothing here yet" panel: say what this is, why it is empty, and what to do next. */
export function EmptyState({ icon, title, body, action, onAction, tint = colors.dim }: { icon: string; title: string; body: string; action?: string; onAction?: () => void; tint?: string }) {
  return (
    <Card style={{ alignItems: 'center', gap: 10, padding: 26 }}>
      <Avatar size={56} color={tint} icon={icon} />
      <Txt style={{ fontFamily: font.bold, fontSize: 17, textAlign: 'center' }}>{title}</Txt>
      <Txt style={{ color: colors.dim, textAlign: 'center', lineHeight: 21, fontSize: 14 }}>{body}</Txt>
      {!!action && !!onAction && <View style={{ alignSelf: 'stretch', marginTop: 6 }}><GradientButton label={action} onPress={onAction} /></View>}
    </Card>
  );
}

/** A load failure that never dead-ends: what happened in plain words, and a retry. */
export function ErrorCard({ message = "Couldn't load this", onRetry }: { message?: string; onRetry: () => void }) {
  return (
    <Card glow={colors.amber} style={{ gap: 10 }}>
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10 }}>
        <Ionicons name="cloud-offline" size={22} color={colors.amber} />
        <Txt style={{ fontFamily: font.bold, fontSize: 16, flex: 1 }}>{message}</Txt>
      </View>
      <Txt style={{ color: colors.dim, lineHeight: 20, fontSize: 14 }}>Your coach can't be reached right now. Anything you log is kept on your phone and sent when you're back online.</Txt>
      <Pressable onPress={onRetry} accessibilityRole="button" accessibilityLabel="Try again" style={({ pressed }) => ({ minHeight: 44, borderRadius: 14, alignItems: 'center', justifyContent: 'center', backgroundColor: colors.amber, opacity: pressed ? 0.8 : 1 })}>
        <Txt style={{ color: '#07080B', fontFamily: font.bold, fontSize: 15 }}>Try again</Txt>
      </Pressable>
    </Card>
  );
}

/** Slim strip shown across every tab while the coach can't be reached. */
export function OfflineBanner({ onRetry, retrying }: { onRetry: () => void; retrying?: boolean }) {
  return (
    <View accessibilityRole="alert" style={{ marginHorizontal: 14, marginTop: 6, flexDirection: 'row', alignItems: 'center', gap: 10, paddingLeft: 14, paddingRight: 6, minHeight: 48, borderRadius: 16, backgroundColor: 'rgba(255,184,77,0.14)', borderWidth: 1, borderColor: 'rgba(255,184,77,0.45)' }}>
      <Ionicons name="cloud-offline" size={18} color={colors.amber} />
      <View style={{ flex: 1, paddingVertical: 6 }}>
        <Txt style={{ fontFamily: font.bold, fontSize: 13, color: colors.amber }}>You're offline</Txt>
        <Txt style={{ fontSize: 12, color: colors.dim, lineHeight: 16 }}>Sets you log are saved here and sent later.</Txt>
      </View>
      <Pressable onPress={onRetry} accessibilityRole="button" accessibilityLabel="Retry connection" style={{ minHeight: 44, paddingHorizontal: 14, justifyContent: 'center' }}>
        {retrying ? <ActivityIndicator color={colors.amber} /> : <Txt style={{ fontFamily: font.bold, fontSize: 14, color: colors.amber }}>Retry</Txt>}
      </Pressable>
    </View>
  );
}

// ---- Toast: a short confirmation with an optional action ("Meal removed. Undo") ----
type ToastOpts = { action?: { label: string; onPress: () => void }; ms?: number; lift?: number; tone?: 'ok' | 'warn' };
const ToastCtx = createContext<(msg: string, o?: ToastOpts) => void>(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children, bottom = 96 }: { children: React.ReactNode; bottom?: number }) {
  const [t, setT] = useState<{ id: number; msg: string; o: ToastOpts } | null>(null);
  const v = useRef(new Animated.Value(0)).current;
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hide = useCallback(() => {
    Animated.timing(v, { toValue: 0, duration: 180, useNativeDriver: true }).start(({ finished }) => { if (finished) setT(null); });
  }, [v]);
  const show = useCallback((msg: string, o: ToastOpts = {}) => {
    if (timer.current) clearTimeout(timer.current);
    setT({ id: Date.now(), msg, o });
    v.setValue(0);
    Animated.timing(v, { toValue: 1, duration: 220, easing: Easing.out(Easing.cubic), useNativeDriver: true }).start();
    Haptics.selectionAsync();
    timer.current = setTimeout(hide, o.ms ?? (o.action ? 5000 : 2600));
  }, [v, hide]);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  const warn = t?.o.tone === 'warn';
  return (
    <ToastCtx.Provider value={show}>
      {children}
      {t && (
        <Animated.View pointerEvents="box-none" style={{ position: 'absolute', left: 16, right: 16, bottom: bottom + (t.o.lift ?? 0), opacity: v, transform: [{ translateY: v.interpolate({ inputRange: [0, 1], outputRange: [16, 0] }) }] }}>
          <View accessibilityLiveRegion="polite" style={{ flexDirection: 'row', alignItems: 'center', gap: 10, minHeight: 52, paddingLeft: 16, paddingRight: t.o.action ? 4 : 16, borderRadius: 18, backgroundColor: '#222838', borderWidth: 1, borderColor: warn ? colors.amber + '88' : 'rgba(198,255,61,0.35)',
            shadowColor: '#000', shadowOpacity: 0.5, shadowRadius: 16, shadowOffset: { width: 0, height: 8 }, elevation: 10 }}>
            <Ionicons name={warn ? 'alert-circle' : 'checkmark-circle'} size={20} color={warn ? colors.amber : colors.lime} />
            <Txt style={{ flex: 1, fontFamily: font.semibold, fontSize: 14, lineHeight: 19 }} numberOfLines={2}>{t.msg}</Txt>
            {t.o.action && (
              <Pressable onPress={() => { const a = t.o.action!; hide(); a.onPress(); }} accessibilityRole="button" accessibilityLabel={t.o.action.label} style={{ minHeight: 48, minWidth: 64, paddingHorizontal: 14, alignItems: 'center', justifyContent: 'center' }}>
                <Txt style={{ fontFamily: font.black, fontSize: 14, color: colors.lime }}>{t.o.action.label}</Txt>
              </Pressable>
            )}
          </View>
        </Animated.View>
      )}
    </ToastCtx.Provider>
  );
}

// ---- Connectivity: one shared answer to "can the app reach the coach?", plus a counter that bumps on reconnect ----
export const OnlineCtx = createContext<{ online: boolean; epoch: number; recheck: () => void }>({ online: true, epoch: 0, recheck: () => {} });
export const useOnline = () => useContext(OnlineCtx);
