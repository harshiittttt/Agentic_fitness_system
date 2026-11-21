import React, { useCallback, useEffect, useState } from 'react';
import { View } from 'react-native';
import { LinearGradient } from 'expo-linear-gradient';
import { Ionicons } from '@expo/vector-icons';
import { WorkoutSummaryData, getWorkoutSummary } from '../api';
import { colors, font, gradients } from '../theme';
import { Card, CountUp, FadeIn, GradientButton, SectionTitle, Skeleton, Txt } from '../ui';

const cap = (s: string) => s.replace(/\b\w/g, (c) => c.toUpperCase());
const kg = (n: number | null | undefined) => String(+(n ?? 0).toFixed(2));

function Tile({ value, label, children }: { value?: number; label: string; children?: React.ReactNode }) {
  return (
    <View style={{ flex: 1, alignItems: 'center', gap: 2 }}>
      {children ?? <CountUp value={value ?? 0} style={{ fontFamily: font.black, fontSize: 30, letterSpacing: -0.8 }} />}
      <Txt style={{ color: colors.dim, fontSize: 11.5, letterSpacing: 1, fontFamily: font.semibold }}>{label.toUpperCase()}</Txt>
    </View>
  );
}

/** The "workout complete" moment: what you did, any personal best, and what is next. Reads one summary from the backend. */
export default function WorkoutSummary({ session, accent = colors.lime, coachLine, onDone, doneLabel = 'Done' }: {
  session?: string; accent?: string; coachLine?: string; onDone: () => void; doneLabel?: string;
}) {
  const [d, setD] = useState<WorkoutSummaryData | null>(null);
  const [failed, setFailed] = useState(false);
  const load = useCallback(() => { setFailed(false); setD(null); getWorkoutSummary(session).then(setD).catch(() => setFailed(true)); }, [session]);
  useEffect(() => { load(); }, [load]);

  if (failed) {
    return (
      <View style={{ gap: 14, alignItems: 'center', paddingVertical: 20 }}>
        <Ionicons name="cloud-offline" size={40} color={colors.amber} />
        <Txt style={{ fontFamily: font.bold, fontSize: 18, textAlign: 'center' }}>Your sets are saved</Txt>
        <Txt style={{ color: colors.dim, textAlign: 'center', lineHeight: 21 }}>We just can't load the summary right now. Check your connection and try again.</Txt>
        <View style={{ alignSelf: 'stretch', gap: 10 }}><GradientButton label="Try again" icon="refresh" onPress={load} /><GradientButton label={doneLabel} onPress={onDone} colorsArr={['#2A2F3D', '#1A1E28']} dark={false} /></View>
      </View>
    );
  }
  if (!d) return <View style={{ gap: 14 }}><Skeleton height={120} radius={30} /><Skeleton height={90} /><Skeleton height={140} /></View>;

  const empty = d.sets === 0;
  const complete = d.finished && !empty;
  const volume = d.volume_kg >= 10000 ? { v: +(d.volume_kg / 1000).toFixed(1), unit: 't', dec: 1 } : { v: d.volume_kg, unit: 'kg', dec: 0 };

  return (
    <View style={{ gap: 14 }}>
      <FadeIn>
        <LinearGradient colors={complete ? ['#1B2A12', '#10131A'] : ['#161A24', '#10131A']} style={{ borderRadius: 30, padding: 22, alignItems: 'center', gap: 8, borderWidth: 1, borderColor: complete ? accent + '66' : colors.border }}>
          <View style={{ width: 72, height: 72, borderRadius: 36, alignItems: 'center', justifyContent: 'center', backgroundColor: accent + '24', borderWidth: 2, borderColor: accent + 'cc', shadowColor: accent, shadowOpacity: 0.6, shadowRadius: 18, shadowOffset: { width: 0, height: 0 } }}>
            <Ionicons name={empty ? 'moon' : d.prs.length ? 'trophy' : 'checkmark'} size={36} color={accent} />
          </View>
          <Txt style={{ fontFamily: font.black, fontSize: 28, letterSpacing: -0.5, textAlign: 'center' }}>{empty ? 'Nothing logged yet' : complete ? 'Workout complete' : 'Session saved'}</Txt>
          <Txt style={{ color: colors.dim, fontSize: 14.5, textAlign: 'center', lineHeight: 21 }}>
            {empty ? 'No sets were logged today. Rest days count too.'
              : complete ? `${d.day_name ?? 'Today'}: every planned set is done.`
              : d.done_planned_sets === 0 ? `${d.sets} ${d.sets === 1 ? 'set' : 'sets'} logged, none from the planned ${d.day_name ?? 'workout'}. Whatever you did counts.`
              : `${d.day_name ?? 'Today'}: ${d.done_planned_sets} of ${d.planned_sets} planned sets. Whatever you did counts.`}
          </Txt>
          {!!coachLine && <Txt style={{ color: colors.text, fontFamily: font.semibold, fontSize: 15, lineHeight: 22, textAlign: 'center', marginTop: 6 }}>{coachLine}</Txt>}
        </LinearGradient>
      </FadeIn>

      {d.prs.length > 0 && (
        <FadeIn delay={80}>
          <Card glow={colors.amber} style={{ flexDirection: 'row', alignItems: 'center', gap: 14 }}>
            <Ionicons name="trophy" size={28} color={colors.amber} />
            <View style={{ flex: 1 }}>
              <Txt style={{ color: colors.amber, fontFamily: font.black, fontSize: 12, letterSpacing: 1 }}>NEW PERSONAL BEST</Txt>
              <Txt style={{ fontFamily: font.bold, fontSize: 17, lineHeight: 23 }}>{d.prs.map(cap).join(' and ')}</Txt>
            </View>
          </Card>
        </FadeIn>
      )}

      {!empty && (
        <FadeIn delay={140}>
          <Card style={{ flexDirection: 'row', paddingVertical: 20 }}>
            <Tile value={d.sets} label="Sets" />
            <View style={{ width: 1, backgroundColor: colors.border }} />
            <Tile label="Volume">
              <View style={{ flexDirection: 'row', alignItems: 'baseline', gap: 3 }}>
                <CountUp value={volume.v} decimals={volume.dec} style={{ fontFamily: font.black, fontSize: 30, letterSpacing: -0.8 }} />
                <Txt style={{ color: colors.dim, fontFamily: font.semibold, fontSize: 14 }}>{volume.unit}</Txt>
              </View>
            </Tile>
            <View style={{ width: 1, backgroundColor: colors.border }} />
            <Tile label="Time">
              <View style={{ flexDirection: 'row', alignItems: 'baseline', gap: 3 }}>
                <Txt style={{ fontFamily: font.black, fontSize: 30, letterSpacing: -0.8 }}>{d.duration_min ?? '–'}</Txt>
                {d.duration_min != null && <Txt style={{ color: colors.dim, fontFamily: font.semibold, fontSize: 14 }}>min</Txt>}
              </View>
            </Tile>
          </Card>
        </FadeIn>
      )}

      {d.exercises.length > 0 && (
        <FadeIn delay={200} style={{ gap: 10 }}>
          <SectionTitle>What you did</SectionTitle>
          <Card style={{ padding: 0, gap: 0, overflow: 'hidden' }}>
            {d.exercises.map((e, i) => (
              <View key={e.name} style={{ flexDirection: 'row', alignItems: 'center', gap: 12, paddingVertical: 14, paddingHorizontal: 16, borderTopWidth: i ? 1 : 0, borderTopColor: colors.border }}>
                <View style={{ flex: 1 }}>
                  <Txt style={{ fontFamily: font.semibold, fontSize: 16 }} numberOfLines={1}>{cap(e.name)}</Txt>
                  <Txt style={{ color: colors.dim, fontSize: 13 }}>{e.sets} {e.sets === 1 ? 'set' : 'sets'}{e.volume_kg ? ` · ${e.volume_kg} kg total` : ''}</Txt>
                </View>
                {e.best && <Txt style={{ fontFamily: font.bold, fontSize: 15 }}>{e.best.weight ? `${kg(e.best.weight)} kg × ${e.best.reps}` : `${e.best.reps} reps`}</Txt>}
                {d.prs.includes(e.name) && <Ionicons name="trophy" size={16} color={colors.amber} />}
              </View>
            ))}
          </Card>
        </FadeIn>
      )}

      {d.next && (
        <FadeIn delay={260}>
          <Card style={{ flexDirection: 'row', alignItems: 'center', gap: 14 }}>
            <View style={{ width: 44, height: 44, borderRadius: 14, backgroundColor: colors.cyan + '22', alignItems: 'center', justifyContent: 'center' }}>
              <Ionicons name="calendar" size={22} color={colors.cyan} />
            </View>
            <View style={{ flex: 1 }}>
              <Txt style={{ color: colors.dim, fontSize: 11.5, letterSpacing: 1, fontFamily: font.semibold }}>UP NEXT</Txt>
              <Txt style={{ fontFamily: font.bold, fontSize: 17 }} numberOfLines={1}>{d.next.name}</Txt>
              <Txt style={{ color: colors.dim, fontSize: 13 }}>{d.next.exercises} exercises{d.next.est_minutes ? ` · about ${d.next.est_minutes} min` : ''}</Txt>
            </View>
          </Card>
        </FadeIn>
      )}

      <FadeIn delay={320}>
        <Txt style={{ color: colors.dim, fontSize: 13.5, lineHeight: 20, textAlign: 'center', marginBottom: 4 }}>
          {empty ? 'Come back when you are ready.' : 'Eat, hydrate and sleep well. Your coach will adjust the next session from today.'}
        </Txt>
        <GradientButton label={doneLabel} icon="checkmark" onPress={onDone} colorsArr={gradients.lime} />
      </FadeIn>
    </View>
  );
}
