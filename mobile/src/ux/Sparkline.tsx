import React from 'react';
import Svg, { Circle, Defs, LinearGradient, Path, Stop } from 'react-native-svg';
import { colors } from '../theme';

/** A tiny trend line with a dot on the latest value. No axes: it only answers "going up, down or flat?". */
export default function Sparkline({ values, width = 120, height = 36, color = colors.cyan }: { values: number[]; width?: number; height?: number; color?: string }) {
  if (values.length < 2) return null;
  const lo = Math.min(...values), hi = Math.max(...values);
  const span = Math.max(hi - lo, 0.4);                        // a flat line stays flat instead of zooming into noise
  const pad = 5;
  const pts = values.map((v, i) => [pad + (i / (values.length - 1)) * (width - pad * 2), pad + (1 - (v - lo) / span) * (height - pad * 2)]);
  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ');
  const last = pts[pts.length - 1];
  return (
    <Svg width={width} height={height}>
      <Defs><LinearGradient id="spk" x1="0" y1="0" x2="1" y2="0"><Stop offset="0" stopColor={color} stopOpacity={0.35} /><Stop offset="1" stopColor={color} stopOpacity={1} /></LinearGradient></Defs>
      <Path d={d} stroke="url(#spk)" strokeWidth={2.5} fill="none" strokeLinecap="round" strokeLinejoin="round" />
      <Circle cx={last[0]} cy={last[1]} r={4} fill={color} />
    </Svg>
  );
}
