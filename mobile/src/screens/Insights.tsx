import React, { useEffect, useRef, useState } from 'react';
import { View } from 'react-native';
import { colors, font } from '../theme';
import { FadeIn, Segmented, Txt } from '../ui';
import Decisions from './Decisions';
import Progress from './Progress';
import Scorecard from './Scorecard';
import { onVoiceAction, takePendingVoiceActions, VoiceAction } from '../voiceBus';

type Seg = 'trends' | 'decisions' | 'scorecard';

/** One home for "how am I doing": your numbers, and what your coaching team has decided (including the weekly check). */
export default function Insights({ pending = 0, onPending, initialSeg, onOpenProfile }: { pending?: number; onPending?: (n: number) => void; initialSeg?: Seg | 'team'; onOpenProfile?: () => void }) {
  const [seg, setSeg] = useState<Seg>(() => (initialSeg === 'trends' ? 'trends' : initialSeg === 'scorecard' ? 'scorecard' : initialSeg ? 'decisions' : pending > 0 ? 'decisions' : 'trends'));
  const [rev, setRev] = useState(0);   // bumping it remounts the segment, which reloads its data
  // Voice control: "Hey coach, show my scorecard". Also drains actions queued before this tab mounted.
  const apply = useRef((a: VoiceAction): boolean => false);
  apply.current = (a) => {
    if (a.type === 'insights_segment') {
      const k = String(a.seg).toLowerCase();
      const next: Seg | null = k.startsWith('trend') || k === 'progress' ? 'trends' : k.startsWith('score') ? 'scorecard' : k === 'decisions' || k === 'team' ? 'decisions' : null;
      if (!next) return false;
      setSeg(next); return true;
    }
    if (a.type === 'refresh' && (!a.what || a.what.includes('all'))) { setRev((r) => r + 1); return true; }
    return false;
  };
  useEffect(() => {
    const mine = (x: VoiceAction) => x.type === 'insights_segment';
    takePendingVoiceActions(mine).forEach((a) => apply.current(a));
    return onVoiceAction((a) => apply.current(a));
  }, []);
  return (
    <View style={{ flex: 1 }}>
      <View style={{ paddingHorizontal: 20, paddingTop: 12, gap: 14 }}>
        <View>
          <Txt style={{ color: colors.dim, fontFamily: font.medium, fontSize: 14 }}>How you're doing</Txt>
          <Txt accessibilityRole="header" style={{ fontFamily: font.black, fontSize: 32, letterSpacing: -0.5 }} numberOfLines={1} adjustsFontSizeToFit>Insights</Txt>
        </View>
        <Segmented
          options={[{ key: 'trends', label: 'Your trends' }, { key: 'decisions', label: pending > 0 ? `Your team · ${pending}` : 'Your team' }, { key: 'scorecard', label: 'Scorecard' }]}
          value={seg} onChange={setSeg} />
      </View>
      <FadeIn key={`${seg}-${rev}`} style={{ flex: 1 }} distance={8}>
        {seg === 'trends' ? <Progress embedded onOpenProfile={onOpenProfile} /> : seg === 'scorecard' ? <Scorecard /> : <Decisions onPending={onPending} />}
      </FadeIn>
    </View>
  );
}
