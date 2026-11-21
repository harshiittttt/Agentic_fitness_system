import React, { useState } from 'react';
import { View } from 'react-native';
import { colors, font } from '../theme';
import { FadeIn, Segmented, Txt } from '../ui';
import Decisions from './Decisions';
import Progress from './Progress';
import Scorecard from './Scorecard';

type Seg = 'trends' | 'decisions' | 'scorecard';

/** One home for "how am I doing": your numbers, and what your coaching team has decided (including the weekly check). */
export default function Insights({ pending = 0, onPending, initialSeg, onOpenProfile }: { pending?: number; onPending?: (n: number) => void; initialSeg?: Seg | 'team'; onOpenProfile?: () => void }) {
  const [seg, setSeg] = useState<Seg>(() => (initialSeg === 'trends' ? 'trends' : initialSeg === 'scorecard' ? 'scorecard' : initialSeg ? 'decisions' : pending > 0 ? 'decisions' : 'trends'));
  return (
    <View style={{ flex: 1 }}>
      <View style={{ paddingHorizontal: 20, paddingTop: 12, gap: 14 }}>
        <View>
          <Txt style={{ color: colors.dim, fontFamily: font.medium, fontSize: 14 }}>How you're doing</Txt>
          <Txt style={{ fontFamily: font.black, fontSize: 32, letterSpacing: -0.5 }}>Insights</Txt>
        </View>
        <Segmented
          options={[{ key: 'trends', label: 'Your trends' }, { key: 'decisions', label: pending > 0 ? `Your team · ${pending}` : 'Your team' }, { key: 'scorecard', label: 'Scorecard' }]}
          value={seg} onChange={setSeg} />
      </View>
      <FadeIn key={seg} style={{ flex: 1 }} distance={8}>
        {seg === 'trends' ? <Progress embedded onOpenProfile={onOpenProfile} /> : seg === 'scorecard' ? <Scorecard /> : <Decisions onPending={onPending} />}
      </FadeIn>
    </View>
  );
}
