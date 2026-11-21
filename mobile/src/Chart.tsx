import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Animated, Easing, PanResponder, Pressable, StyleSheet, View } from 'react-native';
import Svg, { Circle, Defs, G, Line, LinearGradient, Path, Rect, Stop, Text as SvgText } from 'react-native-svg';
import * as Haptics from './haptics';
import { colors, font } from './theme';
import { Txt } from './ui';
import { Activity, DAYS, MONTHS, isIso, isoOf, mondayOf, parseIso, shortDate } from './dates';

export { isIso, shortDate, parseIso, isoOf, mondayOf, weeklyBuckets, weekStreak } from './dates';
export type { Activity } from './dates';

// ---------------------------------------------------------------- line chart
function smoothPath(pts: [number, number][], top: number, bottom: number) {
  let d = `M ${pts[0][0]} ${pts[0][1]}`;
  const clamp = (y: number) => Math.max(top, Math.min(bottom, y));
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] ?? pts[i], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] ?? p2;
    const t = 0.18;
    d += ` C ${p1[0] + (p2[0] - p0[0]) * t} ${clamp(p1[1] + (p2[1] - p0[1]) * t)} ${p2[0] - (p3[0] - p1[0]) * t} ${clamp(p2[1] - (p3[1] - p1[1]) * t)} ${p2[0]} ${p2[1]}`;
  }
  return d;
}

const PAD = { l: 40, r: 12, t: 12, b: 22 };

type LineProps = {
  data: number[]; labels?: string[]; color?: string; height?: number; id?: string; unit?: string; decimals?: number;
  /** Shade the most recent N points (the window the coach judges trends over). */
  window?: number;
  /** Horizontal value zones, e.g. readiness green/amber/red. */
  bands?: { from: number; to: number; color: string }[];
  markPeak?: boolean;
};

/** Touch-scrubbable line chart: drag along it to read any point. Axes, gridlines, draw-in animation. */
export function LineChart({ data, labels, color = colors.lime, height = 170, id = 'c', unit = '', decimals = 0, window: win, bands, markPeak }: LineProps) {
  const [w, setW] = useState(0);
  const [active, setActive] = useState<number | null>(null);
  const reveal = useRef(new Animated.Value(0)).current;
  const hideTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastIdx = useRef(-1);
  const geo = useRef({ w: 0, n: 0 });
  const n = data.length;
  geo.current = { w, n };

  useEffect(() => {
    if (w <= 0) return;
    reveal.setValue(0);
    Animated.timing(reveal, { toValue: 1, duration: 700, easing: Easing.out(Easing.cubic), useNativeDriver: true }).start(() => reveal.setValue(1));
    // Safety net: whatever happens to the animation, the chart must never stay invisible.
    const t = setTimeout(() => reveal.setValue(1), 1100);
    return () => clearTimeout(t);
  }, [w, n, data.join(',')]);
  useEffect(() => () => { if (hideTimer.current) clearTimeout(hideTimer.current); }, []);

  const pick = (x: number) => {
    const { w: ww, n: nn } = geo.current;
    if (nn < 1 || ww <= PAD.l + PAD.r) return;
    const i = Math.max(0, Math.min(nn - 1, Math.round(((x - PAD.l) / (ww - PAD.l - PAD.r)) * (nn - 1))));
    if (i !== lastIdx.current) { lastIdx.current = i; Haptics.selectionAsync(); setActive(i); }
  };
  const release = () => {
    if (hideTimer.current) clearTimeout(hideTimer.current);
    hideTimer.current = setTimeout(() => { lastIdx.current = -1; setActive(null); }, 1600);
  };
  const pan = useRef(PanResponder.create({
    onStartShouldSetPanResponder: () => true,
    onMoveShouldSetPanResponder: () => true,
    onPanResponderTerminationRequest: () => true, // let the page scroll if the finger moves vertically
    onPanResponderGrant: (e) => { if (hideTimer.current) clearTimeout(hideTimer.current); pick(e.nativeEvent.locationX); },
    onPanResponderMove: (e) => pick(e.nativeEvent.locationX),
    onPanResponderRelease: release,
    onPanResponderTerminate: release,
  })).current;

  if (n < 2) return <View style={{ height }} onLayout={(e) => setW(e.nativeEvent.layout.width)} />;

  const hi0 = Math.max(...data), lo0 = Math.min(...data);
  const span = hi0 - lo0 || Math.max(1, Math.abs(hi0) * 0.1);
  const lo = lo0 - span * 0.15, hi = hi0 + span * 0.15;
  const iw = w - PAD.l - PAD.r, ih = height - PAD.t - PAD.b;
  const X = (i: number) => PAD.l + (i / (n - 1)) * iw;
  const Y = (v: number) => PAD.t + (1 - (v - lo) / (hi - lo)) * ih;
  const fmt = (v: number) => (decimals ? v.toFixed(decimals) : String(Math.round(v)));
  const pts = data.map((v, i) => [X(i), Y(v)] as [number, number]);
  const line = w > 0 ? smoothPath(pts, PAD.t, PAD.t + ih) : '';
  const area = w > 0 ? `${line} L ${X(n - 1)} ${PAD.t + ih} L ${X(0)} ${PAD.t + ih} Z` : '';
  const ticks = [hi0, (hi0 + lo0) / 2, lo0];
  const peak = markPeak ? data.indexOf(hi0) : -1;
  const xLabelIdx = labels ? Array.from(new Set([0, Math.round((n - 1) / 2), n - 1])) : [];
  const tipW = 96;

  return (
    <View style={{ height }} onLayout={(e) => setW(e.nativeEvent.layout.width)}>
      {w > 0 && (
        <>
          <Animated.View pointerEvents="none" style={{ opacity: reveal }}>
          <Svg width={w} height={height}>
            <Defs>
              <LinearGradient id={`a${id}`} x1="0" y1="0" x2="0" y2="1">
                <Stop offset="0" stopColor={color} stopOpacity={0.32} />
                <Stop offset="1" stopColor={color} stopOpacity={0} />
              </LinearGradient>
            </Defs>

            {bands?.map((b, i) => {
              const y1 = Math.max(PAD.t, Y(Math.min(hi, b.to))), y2 = Math.min(PAD.t + ih, Y(Math.max(lo, b.from)));
              return y2 > y1 ? <Rect key={i} x={PAD.l} y={y1} width={iw} height={y2 - y1} fill={b.color} opacity={0.09} /> : null;
            })}

            {win && n > win && (
              <>
                <Rect x={X(n - win)} y={PAD.t} width={X(n - 1) - X(n - win)} height={ih} fill="rgba(255,255,255,0.045)" rx={8} />
                <SvgText x={X(n - win) + 6} y={PAD.t + ih - 6} fontSize={9.5} fill={colors.dim} fontFamily={font.semibold}>COACH WINDOW</SvgText>
              </>
            )}

            {ticks.map((t, i) => (
              <G key={i}>
                <Line x1={PAD.l} x2={w - PAD.r} y1={Y(t)} y2={Y(t)} stroke="rgba(255,255,255,0.07)" strokeWidth={1} strokeDasharray="2 5" />
                <SvgText x={PAD.l - 8} y={Y(t) + 3.5} fontSize={10} fill={colors.faint} textAnchor="end" fontFamily={font.medium}>{fmt(t)}</SvgText>
              </G>
            ))}

            <Path d={area} fill={`url(#a${id})`} />
            <Path d={line} stroke={color} strokeWidth={2.75} fill="none" strokeLinecap="round" strokeLinejoin="round" />

            {peak >= 0 && peak !== n - 1 && (
              <>
                <Circle cx={X(peak)} cy={Y(hi0)} r={5.5} fill={colors.bg} stroke={color} strokeWidth={2} />
                <SvgText x={X(peak)} y={Y(hi0) - 10} fontSize={9} fill={color} textAnchor="middle" fontFamily={font.bold}>BEST</SvgText>
              </>
            )}

            {active === null && (
              <>
                <Circle cx={X(n - 1)} cy={Y(data[n - 1])} r={9} fill={color} opacity={0.22} />
                <Circle cx={X(n - 1)} cy={Y(data[n - 1])} r={4.5} fill={color} />
              </>
            )}

            {xLabelIdx.map((i) => (
              <SvgText key={i} x={X(i)} y={height - 5} fontSize={10} fill={colors.faint} fontFamily={font.medium}
                textAnchor={i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle'}>{shortDate(labels![i] ?? '')}</SvgText>
            ))}

            {active !== null && (
              <>
                <Line x1={X(active)} x2={X(active)} y1={PAD.t} y2={PAD.t + ih} stroke="rgba(255,255,255,0.28)" strokeWidth={1} strokeDasharray="3 4" />
                <Circle cx={X(active)} cy={Y(data[active])} r={10} fill={color} opacity={0.22} />
                <Circle cx={X(active)} cy={Y(data[active])} r={5} fill={colors.bg} stroke={color} strokeWidth={2.5} />
              </>
            )}
          </Svg>
          </Animated.View>

          <View style={StyleSheet.absoluteFill} {...pan.panHandlers} />

          {active !== null && (
            <View pointerEvents="none" style={{
              position: 'absolute', width: tipW, alignItems: 'center', paddingVertical: 6, borderRadius: 12, backgroundColor: colors.cardHi,
              borderWidth: 1, borderColor: colors.border, left: Math.max(0, Math.min(w - tipW, X(active) - tipW / 2)), top: Math.max(0, Y(data[active]) - 58),
            }}>
              <Txt style={{ fontFamily: font.bold, fontSize: 15 }}>{fmt(data[active])}{unit}</Txt>
              {!!labels?.[active] && <Txt style={{ color: colors.dim, fontSize: 11 }}>{shortDate(labels[active])}</Txt>}
            </View>
          )}
        </>
      )}
    </View>
  );
}

// ---------------------------------------------------------------- bar chart
/** Tap a bar to read it. Bars grow in on mount; the selected bar is highlighted. */
export function BarChart({ data, labels, color = colors.cyan, height = 130, format }: { data: number[]; labels?: string[]; color?: string; height?: number; format?: (v: number) => string }) {
  const [sel, setSel] = useState(data.length - 1);
  const grow = useRef(new Animated.Value(0)).current;
  useEffect(() => {
    setSel(data.length - 1);
    grow.setValue(0);
    Animated.timing(grow, { toValue: 1, duration: 700, easing: Easing.out(Easing.cubic), useNativeDriver: false }).start();
  }, [data.join(',')]);
  const max = Math.max(...data, 1);
  const fmt = format ?? ((v: number) => String(Math.round(v)));
  const step = data.length > 8 ? 3 : 1;
  return (
    <View>
      <View style={{ height, flexDirection: 'row', gap: 6 }}>
        {data.map((v, i) => (
          <Pressable key={i} onPress={() => { Haptics.selectionAsync(); setSel(i); }} style={{ flex: 1, justifyContent: 'flex-end', alignItems: 'center' }}>
            {sel === i && <Txt style={{ fontSize: 10.5, fontFamily: font.bold, marginBottom: 4 }}>{fmt(v)}</Txt>}
            <Animated.View style={{
              width: '100%', borderRadius: 7, backgroundColor: sel === i ? color : color + '3A',
              height: grow.interpolate({ inputRange: [0, 1], outputRange: [0, Math.max(4, (v / max) * (height - 22))] }),
            }} />
          </Pressable>
        ))}
      </View>
      {labels && (
        <View style={{ flexDirection: 'row', gap: 6, marginTop: 6 }}>
          {data.map((_, i) => (
            <View key={i} style={{ flex: 1, alignItems: 'center' }}>
              {(i % step === (data.length - 1) % step) && <Txt style={{ color: colors.dim, fontSize: 10.5, width: 60, textAlign: 'center' }} numberOfLines={1}>{shortDate(labels[i])}</Txt>}
            </View>
          ))}
        </View>
      )}
    </View>
  );
}

// ---------------------------------------------------------------- consistency heatmap
/** 12 weeks x 7 days, brighter = more volume. Tap a day for details. */
export function Heatmap({ activity, weeks = 12, color = colors.lime }: { activity: Activity[]; weeks?: number; color?: string }) {
  const [w, setW] = useState(0);
  const [sel, setSel] = useState<string | null>(null);
  const byDate = useMemo(() => {
    const m: Record<string, Activity> = {};
    for (const a of activity) if (isIso(a.date)) m[a.date] = a;
    return m;
  }, [activity]);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const start = mondayOf(new Date(today.getFullYear(), today.getMonth(), today.getDate() - (weeks - 1) * 7));
  const gap = 4, labelW = 24;
  const cell = w ? Math.max(8, Math.floor((w - labelW - gap * (weeks - 1)) / weeks)) : 0;
  const max = Math.max(1, ...Object.values(byDate).map((a) => a.tonnage));
  const level = (t: number) => (t <= 0 ? 0 : t < max * 0.34 ? 1 : t < max * 0.67 ? 2 : 3);
  const alpha = [0.07, 0.35, 0.65, 1];
  const cols = Array.from({ length: weeks }, (_, c) => Array.from({ length: 7 }, (_, r) => new Date(start.getFullYear(), start.getMonth(), start.getDate() + c * 7 + r)));
  const todayIso = isoOf(today);
  const selA = sel ? byDate[sel] : undefined;

  return (
    <View onLayout={(e) => setW(e.nativeEvent.layout.width)} style={{ gap: 8 }}>
      {w > 0 && (
        <>
          <View style={{ flexDirection: 'row', marginLeft: labelW }}>
            {cols.map((col, c) => (
              <View key={c} style={{ width: cell, marginRight: c < weeks - 1 ? gap : 0 }}>
                {(c === 0 || col[0].getMonth() !== cols[c - 1][0].getMonth()) && (
                  <Txt style={{ color: colors.faint, fontSize: 10.5, width: 40 }} numberOfLines={1}>{MONTHS[col[0].getMonth()]}</Txt>
                )}
              </View>
            ))}
          </View>
          <View style={{ flexDirection: 'row' }}>
            <View style={{ width: labelW, justifyContent: 'space-between' }}>
              {DAYS.map((d, r) => (
                <View key={d} style={{ height: cell, marginBottom: r < 6 ? gap : 0, justifyContent: 'center' }}>
                  {r % 2 === 0 && <Txt style={{ color: colors.faint, fontSize: 10 }}>{d[0]}</Txt>}
                </View>
              ))}
            </View>
            {cols.map((col, c) => (
              <View key={c} style={{ marginRight: c < weeks - 1 ? gap : 0 }}>
                {col.map((d, r) => {
                  const iso = isoOf(d);
                  const a = byDate[iso];
                  const future = d > today;
                  const lv = a ? level(a.tonnage) : 0;
                  const isSel = sel === iso;
                  return (
                    <Pressable key={r} disabled={future} onPress={() => { Haptics.selectionAsync(); setSel(isSel ? null : iso); }}
                      style={{
                        width: cell, height: cell, borderRadius: Math.max(3, cell * 0.26), marginBottom: r < 6 ? gap : 0,
                        backgroundColor: a ? color : '#FFFFFF', opacity: future ? 0.03 : a ? alpha[lv] : 0.07,
                        borderWidth: isSel || iso === todayIso ? 1.5 : 0, borderColor: isSel ? '#fff' : colors.lime,
                      }} />
                  );
                })}
              </View>
            ))}
          </View>
          <Txt style={{ color: colors.dim, fontSize: 12, minHeight: 16 }}>
            {sel ? `${DAYS[(parseIso(sel).getDay() + 6) % 7]}, ${shortDate(sel)} · ${selA ? `${selA.sets} sets · ${(selA.tonnage / 1000).toFixed(1)} t moved` : 'rest day'}` : 'Tap a day for details'}
          </Txt>
        </>
      )}
    </View>
  );
}
