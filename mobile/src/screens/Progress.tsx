import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { RefreshControl, ScrollView, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { InsightsSummary, getInsightsSummary, getProgress, getSeries } from '../api';
import { BarChart, Heatmap, LineChart, weekStreak, weeklyBuckets } from '../Chart';
import { shortDate } from '../dates';
import { colors, font } from '../theme';
import { Card, CountUp, EmptyState, ErrorCard, FadeIn, Segmented, SectionTitle, Skeleton, Txt, useOnline } from '../ui';

const LIFT_COLOR: Record<string, string> = {
  squat: colors.lime, 'bench press': colors.cyan, deadlift: colors.pink, 'overhead press': colors.amber, 'barbell row': colors.violet,
};
const TREND: Record<string, { icon: string; color: string; label: string }> = {
  up: { icon: 'trending-up', color: colors.mint, label: 'Improving' },
  flat: { icon: 'remove', color: colors.amber, label: 'Plateau' },
  down: { icon: 'trending-down', color: colors.red, label: 'Dropping' },
};
const READINESS_BANDS = [
  { from: 75, to: 100, color: colors.mint }, { from: 50, to: 75, color: colors.amber }, { from: 0, to: 50, color: colors.red },
];
type Range = '8' | '20' | 'all';

function Stat({ label, value, unit, sub }: { label: string; value: number; unit?: string; sub?: string }) {
  return (
    <Card style={{ flex: 1, padding: 14, gap: 2 }}>
      <Txt style={{ color: colors.dim, fontSize: 11, letterSpacing: 0.8, fontFamily: font.semibold }} numberOfLines={1} accessibilityLabel={`${label}: ${value}${unit ? ' ' + unit : ''}${sub ? ', ' + sub : ''}`}>{label.toUpperCase()}</Txt>
      <View style={{ flexDirection: 'row', alignItems: 'baseline' }}>
        <CountUp value={value} style={{ fontFamily: font.black, fontSize: 26, letterSpacing: -0.5 }} />
        {!!unit && <Txt style={{ color: colors.dim, fontSize: 13, fontFamily: font.medium }} numberOfLines={1}> {unit}</Txt>}
      </View>
      {!!sub && <Txt style={{ color: colors.dim, fontSize: 11.5 }} numberOfLines={1}>{sub}</Txt>}
    </Card>
  );
}

function Delta({ value, unit, decimals = 0 }: { value: number; unit: string; decimals?: number }) {
  const flat = Math.abs(value) < 0.05;
  const up = value >= 0;
  const c = flat ? colors.amber : up ? colors.mint : colors.red;
  return (
    <View style={{ flexDirection: 'row', alignItems: 'center', gap: 3 }}>
      <Ionicons name={(flat ? 'remove' : up ? 'caret-up' : 'caret-down') as any} size={12} color={c} />
      <Txt style={{ color: c, fontFamily: font.bold, fontSize: 13 }}>{up ? '+' : ''}{value.toFixed(decimals)} {unit}</Txt>
    </View>
  );
}

/** One plain sentence under a chart: what it shows and whether that is good. */
const Explain = ({ children }: { children: React.ReactNode }) => (
  <View style={{ flexDirection: 'row', gap: 8, alignItems: 'flex-start' }}>
    <Ionicons name="information-circle-outline" size={16} color={colors.dim} style={{ marginTop: 2 }} />
    <Txt style={{ flex: 1, color: colors.dim, fontSize: 13, lineHeight: 19 }}>{children}</Txt>
  </View>
);

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`;
const niceDate = (iso: string) => shortDate(iso);

function liftSentence(info: any, gain: number, n: number): string {
  if (!info) return n < 3 ? 'A few more sessions and your coach can say which way this lift is heading.' : '';
  if (info.trend === 'up') return `Going the right way: up ${Math.abs(gain).toFixed(1)} kg since the start of this view.`;
  if (info.trend === 'flat') return 'Flat for a while. That is normal now and then. Your coach watches it and will adjust the plan if it sticks.';
  return 'Dipping lately. Often that is tiredness or a rough week, so check your sleep and recovery. Your coach is keeping an eye on it.';
}

export default function Progress({ embedded = false, onOpenProfile }: { embedded?: boolean; onOpenProfile?: () => void }) {
  const { epoch } = useOnline();
  const [series, setSeries] = useState<any>(null);
  const [report, setReport] = useState<any>(null);
  const [summary, setSummary] = useState<InsightsSummary | null>(null);
  const [failed, setFailed] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [range, setRange] = useState<Range>('20');
  const load = useCallback(async () => {
    setRefreshing(true);
    try {
      const [s, r] = await Promise.all([getSeries(), getProgress()]);
      setSeries(s); setReport(r); setFailed(false);
      getInsightsSummary().then(setSummary).catch(() => {});
    } catch { setFailed(true); }
    setRefreshing(false);
  }, []);
  useEffect(() => { load(); }, [load, epoch]);

  const lifts = Object.entries(series?.lifts ?? {}) as [string, number[]][];
  const empty = series && lifts.length === 0;
  const buckets = useMemo(() => weeklyBuckets(series?.activity ?? [], 12), [series]);
  const streak = weekStreak(buckets);
  const thisWeek = buckets[buckets.length - 1]?.sessions ?? 0;
  const target = summary?.week.target;
  const totalT = buckets.reduce((a, b) => a + b.tonnage, 0) / 1000;
  const take = (arr: any[]) => (range === 'all' ? arr : arr.slice(-Number(range)));
  const bw: number[] = series?.bodyweight ?? [];
  const rd: number[] = series?.readiness ?? [];

  // The one-line verdict at the top: say how things are going in words.
  const headline = useMemo(() => {
    if (!series || lifts.length === 0) return null;
    const ups = Object.entries(report?.lifts ?? {}).filter(([, v]: any) => v.trend === 'up').map(([k]) => k);
    const flats = (report?.plateaus ?? []) as string[];
    const downs = Object.entries(report?.lifts ?? {}).filter(([, v]: any) => v.trend === 'down').map(([k]) => k);
    if (downs.length) return { icon: 'alert-circle', color: colors.amber, text: `Your ${downs[0]} has dipped lately. Worth a look at sleep and recovery.` };
    if (ups.length && !flats.length) return { icon: 'trending-up', color: colors.mint, text: `You're getting stronger. ${ups.length > 1 ? `${plural(ups.length, 'lift')} are` : `Your ${ups[0]} is`} climbing.` };
    if (ups.length && flats.length) return { icon: 'trending-up', color: colors.mint, text: `Mostly going well: ${ups.slice(0, 2).join(' and ')} climbing, ${flats[0]} flat for now.` };
    if (flats.length) return { icon: 'remove', color: colors.amber, text: `Steady, with ${flats[0]} on a plateau. Your coach is watching it.` };
    return { icon: 'sparkles', color: colors.cyan, text: 'Nice start. A few more sessions and trends will show up.' };
  }, [series, report]);

  return (
    <ScrollView contentContainerStyle={{ padding: 20, gap: 16, paddingBottom: 30 }}
      refreshControl={<RefreshControl refreshing={refreshing && !!series} onRefresh={load} tintColor={colors.lime} />}>
      {!embedded && (
        <View>
          <Txt style={{ color: colors.dim, fontFamily: font.medium, fontSize: 14 }}>Your trends</Txt>
          <Txt style={{ fontFamily: font.black, fontSize: 32, letterSpacing: -0.5 }}>Progress</Txt>
        </View>
      )}

      {!series && !failed && (
        <>
          <View style={{ flexDirection: 'row', gap: 10 }}><Skeleton height={92} width="31%" radius={26} style={{ flex: 1 }} /><Skeleton height={92} width="31%" radius={26} style={{ flex: 1 }} /><Skeleton height={92} width="31%" radius={26} style={{ flex: 1 }} /></View>
          <Skeleton height={64} /><Skeleton height={220} /><Skeleton height={220} />
        </>
      )}
      {!series && failed && <ErrorCard message="Couldn't load your trends" onRetry={load} />}

      {series && (
        <FadeIn>
          <View style={{ flexDirection: 'row', gap: 10 }}>
            <Stat label="Sessions" value={series.sessions ?? 0} sub="all time" />
            <Stat label="This week" value={thisWeek} unit={target ? `/ ${target}` : undefined} sub={target ? (thisWeek >= target ? 'target hit' : `${target - thisWeek} to go`) : 'sessions'} />
            <Stat label="Streak" value={streak} unit="wk" sub="2+ per week" />
          </View>
        </FadeIn>
      )}

      {headline && (
        <FadeIn delay={40}>
          <Card glow={headline.color} style={{ flexDirection: 'row', alignItems: 'center', gap: 12, padding: 16 }}>
            <Ionicons name={headline.icon as any} size={26} color={headline.color} />
            <Txt style={{ flex: 1, fontFamily: font.semibold, fontSize: 15.5, lineHeight: 22 }}>{headline.text}</Txt>
          </Card>
        </FadeIn>
      )}

      {empty && (
        <EmptyState icon="bar-chart" tint={colors.cyan} title="Your trends start with your first session"
          body={'Log a workout with your coach and this page fills in: strength charts, personal bests, weekly volume and consistency. Charts need about 3 sessions of a lift to show a trend. A daily check-in adds your readiness and bodyweight.'}
          action={onOpenProfile ? 'Open Profile to load demo data' : undefined} onAction={onOpenProfile} />
      )}

      {summary && summary.prs.length > 0 && (
        <>
          <SectionTitle>Personal bests</SectionTitle>
          <Card style={{ gap: 0, padding: 4 }}>
            {summary.prs.map((p, i) => (
              <View key={p.lift} accessible accessibilityLabel={`${p.lift}: estimated one-rep max ${Math.round(p.best_e1rm)} kilograms. ${p.at_best ? 'At your best.' : `Now ${Math.round(p.latest_e1rm)} kilograms.`}`}
                style={{ flexDirection: 'row', alignItems: 'center', gap: 12, padding: 14, minHeight: 64, borderTopWidth: i ? 1 : 0, borderTopColor: colors.border }}>
                <View style={{ width: 8, height: 36, borderRadius: 4, backgroundColor: LIFT_COLOR[p.lift] ?? colors.lime }} />
                <View style={{ flex: 1, gap: 2 }}>
                  <Txt style={{ fontFamily: font.semibold, fontSize: 15, textTransform: 'capitalize' }} numberOfLines={1}>{p.lift}</Txt>
                  <Txt style={{ color: colors.dim, fontSize: 12.5 }} numberOfLines={1}>Heaviest: {p.heaviest.weight} kg × {p.heaviest.reps} · {niceDate(p.heaviest.date)}</Txt>
                </View>
                <View style={{ alignItems: 'flex-end', gap: 2 }}>
                  <Txt style={{ fontFamily: font.black, fontSize: 20, letterSpacing: -0.3 }}>{Math.round(p.best_e1rm)}<Txt style={{ color: colors.dim, fontSize: 12, fontFamily: font.medium }}> kg</Txt></Txt>
                  {p.at_best
                    ? <View style={{ backgroundColor: colors.mint + '22', borderRadius: 999, paddingHorizontal: 8, paddingVertical: 2 }}><Txt style={{ color: colors.mint, fontFamily: font.bold, fontSize: 11 }}>At your best</Txt></View>
                    : <Txt style={{ color: colors.dim, fontSize: 11.5 }}>now {Math.round(p.latest_e1rm)} kg</Txt>}
                </View>
              </View>
            ))}
          </Card>
          <Explain>The big number is your estimated one-rep max, the most you could likely lift once, worked out from your normal sets. It goes up as you get stronger.</Explain>
        </>
      )}

      {lifts.length > 0 && (
        <>
          <SectionTitle>Strength over time</SectionTitle>
          <Segmented<Range> options={[{ key: '8', label: 'Last 8' }, { key: '20', label: 'Last 20' }, { key: 'all', label: 'All' }]} value={range} onChange={setRange} />
          <Explain>One line per lift, one dot per session. Up and to the right is good. Counting sessions, not days.</Explain>
        </>
      )}
      {lifts.map(([name, allVals], li) => {
        const vals = take(allVals);
        const dates = take(series.lift_dates?.[name] ?? []);
        const info = report?.lifts?.[name];
        const t = info ? TREND[info.trend] : null;
        const gain = vals[vals.length - 1] - vals[0];
        const sentence = liftSentence(info, gain, allVals.length);
        return (
          <FadeIn key={name} delay={li * 90}>
            <Card style={{ gap: 12 }}>
              <View style={{ flexDirection: 'row', justifyContent: 'space-between', alignItems: 'flex-start', gap: 8 }}>
                <View style={{ gap: 2, flexShrink: 1 }}>
                  <Txt style={{ fontFamily: font.semibold, fontSize: 15, textTransform: 'capitalize', color: colors.dim }}>{name}</Txt>
                  <CountUp value={vals[vals.length - 1]} style={{ fontFamily: font.black, fontSize: 28 }} suffix=" kg" />
                  {vals.length > 1 && <Delta value={gain} unit="kg" />}
                </View>
                {t && (
                  <View accessible accessibilityLabel={`Trend: ${t.label}`} style={{ flexDirection: 'row', alignItems: 'center', gap: 5, backgroundColor: t.color + '22', paddingHorizontal: 10, paddingVertical: 6, borderRadius: 999 }}>
                    <Ionicons name={t.icon as any} size={14} color={t.color} />
                    <Txt style={{ color: t.color, fontSize: 12, fontFamily: font.bold }}>{t.label}</Txt>
                  </View>
                )}
              </View>
              <LineChart data={vals} labels={dates} color={LIFT_COLOR[name] ?? colors.lime} id={name.replace(/\s/g, '')} unit=" kg" markPeak window={vals.length > 10 ? 8 : undefined} />
              {!!sentence && <Txt style={{ color: info?.trend === 'down' ? colors.amber : colors.dim, fontSize: 13, lineHeight: 19 }}>{sentence}</Txt>}
              {vals.length > 10 && <Txt style={{ color: colors.dim, fontSize: 11.5 }}>Drag on the chart to inspect. The shaded band is the last 8 sessions your coach judges trends over.</Txt>}
            </Card>
          </FadeIn>
        );
      })}

      {series && buckets.some((b) => b.tonnage > 0) && (
        <>
          <SectionTitle>Weekly volume</SectionTitle>
          <Card style={{ gap: 10 }}>
            <View style={{ flexDirection: 'row', alignItems: 'baseline' }}>
              <CountUp value={totalT} decimals={1} style={{ fontFamily: font.black, fontSize: 26 }} />
              <Txt style={{ color: colors.dim, fontSize: 13, fontFamily: font.medium }}> tonnes lifted in 12 weeks</Txt>
            </View>
            <BarChart data={buckets.map((b) => b.tonnage / 1000)} labels={buckets.map((b) => b.start)} format={(v) => `${v.toFixed(1)}t`} />
            <Explain>Total weight you moved each week (sets × reps × kg), in tonnes. Steady or slowly rising is the goal. A sudden spike or a big drop is worth noticing.</Explain>
          </Card>

          <SectionTitle>Consistency</SectionTitle>
          <Card style={{ gap: 12 }}>
            <Heatmap activity={series.activity} />
            <View style={{ flexDirection: 'row', flexWrap: 'wrap', columnGap: 18, rowGap: 4 }}>
              <Txt style={{ color: colors.dim, fontSize: 12.5 }}><Txt style={{ fontFamily: font.bold, color: colors.text }}>{buckets.reduce((a, b) => a + b.sessions, 0)}</Txt> sessions in 12 weeks</Txt>
              <Txt style={{ color: colors.dim, fontSize: 12.5 }}><Txt style={{ fontFamily: font.bold, color: colors.text }}>{Math.max(...buckets.map((b) => b.sessions))}</Txt> best week</Txt>
              <Txt style={{ color: colors.dim, fontSize: 12.5 }}><Txt style={{ fontFamily: font.bold, color: colors.text }}>{streak}</Txt> week streak</Txt>
            </View>
            <Explain>Each square is a day you trained; darker means more work. A streak counts weeks with 2 or more sessions. Showing up matters more than any single big day.</Explain>
          </Card>
        </>
      )}

      {summary && summary.recent.length > 0 && (
        <>
          <SectionTitle>Recent sessions</SectionTitle>
          <Card style={{ gap: 0, padding: 4 }}>
            {summary.recent.slice(0, 6).map((s, i) => (
              <View key={s.date} style={{ padding: 14, gap: 6, borderTopWidth: i ? 1 : 0, borderTopColor: colors.border }}>
                <View style={{ flexDirection: 'row', justifyContent: 'space-between', alignItems: 'baseline' }}>
                  <Txt style={{ fontFamily: font.bold, fontSize: 15 }}>{niceDate(s.date)}</Txt>
                  <Txt style={{ color: colors.dim, fontSize: 12.5 }}>{plural(s.sets, 'set')} · {(s.tonnage / 1000).toFixed(1)} t</Txt>
                </View>
                <Txt style={{ color: colors.dim, fontSize: 13, lineHeight: 19, textTransform: 'capitalize' }}>
                  {s.exercises.slice(0, 4).map((e) => `${e.name}${e.top ? ` ${e.top.weight}×${e.top.reps}` : ''}`).join(' · ')}
                </Txt>
              </View>
            ))}
          </Card>
        </>
      )}

      {bw.length > 1 && (
        <>
          <SectionTitle>Bodyweight</SectionTitle>
          <Card style={{ gap: 10 }}>
            <View style={{ flexDirection: 'row', alignItems: 'baseline', justifyContent: 'space-between' }}>
              <CountUp value={bw[bw.length - 1]} decimals={1} style={{ fontFamily: font.black, fontSize: 26 }} suffix=" kg" />
              <Delta value={bw[bw.length - 1] - bw[0]} unit="kg" decimals={1} />
            </View>
            <LineChart data={bw} labels={series.bodyweight_dates} color={colors.amber} id="bw" unit=" kg" decimals={1} height={150} />
            <Explain>From your check-ins. Daily swings of a kilo are just water and food, so look at the direction over a few weeks, not any single day.</Explain>
          </Card>
        </>
      )}

      {rd.length > 1 && (
        <>
          <SectionTitle>Readiness</SectionTitle>
          <Card style={{ gap: 10 }}>
            <View style={{ flexDirection: 'row', alignItems: 'baseline' }}>
              <CountUp value={rd[rd.length - 1]} style={{ fontFamily: font.black, fontSize: 26 }} />
              <Txt style={{ color: colors.dim, fontSize: 13, fontFamily: font.medium }}> / 100 at your last check-in</Txt>
            </View>
            <LineChart data={rd} labels={series.readiness_dates} color={colors.violet} id="rd" height={150} bands={READINESS_BANDS} />
            <Explain>How ready your body is, from sleep, soreness and energy. Green: good to push. Amber: take it steady. Red: go easy and recover.</Explain>
          </Card>
        </>
      )}
    </ScrollView>
  );
}
