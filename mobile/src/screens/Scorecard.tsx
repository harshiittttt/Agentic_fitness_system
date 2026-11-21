import React, { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshControl, ScrollView, Switch, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { Scorecard as Score, TrustRow, getScorecard, getTrust, setStrictNewTypes } from '../api';
import { colors, font } from '../theme';
import { Card, ErrorCard, FadeIn, Ring, SectionTitle, Skeleton, Txt, useOnline, useToast } from '../ui';
import AnimBar from '../ux/f_AnimBar';

const pct = (x: number | null | undefined) => (x == null ? '–' : `${Math.round(x * 100)}%`);
const LEVEL: Record<TrustRow['level'], { label: string; color: string; hint: string }> = {
  earned: { label: 'Trusted', color: colors.lime, hint: 'You keep accepting these, so they apply without a wait.' },
  neutral: { label: 'Building', color: colors.dim, hint: 'Not enough history yet to change how it works.' },
  tightened: { label: 'Stricter', color: colors.amber, hint: 'You undid some recently, so these now wait for your veto window.' },
  strict: { label: 'Asks first', color: colors.red, hint: 'You pushed back several times, so these need your OK.' },
};
const TYPE_LABEL: Record<string, string> = { 'volume:cut': 'Easing volume', 'volume:increase': 'Adding volume', 'calories:cut': 'Lowering calories', 'calories:increase': 'Raising calories', swap: 'Exercise swaps' };

function Stat2({ value, label, color = colors.text }: { value: string | number; label: string; color?: string }) {
  return (
    <View style={{ flex: 1, gap: 2 }} accessible accessibilityLabel={`${value} ${label}`}>
      <Txt style={{ fontFamily: font.black, fontSize: 26, letterSpacing: -0.5, color }} numberOfLines={1} adjustsFontSizeToFit>{value}</Txt>
      <Txt style={{ color: colors.dim, fontSize: 12.5, lineHeight: 16 }}>{label}</Txt>
    </View>
  );
}

/** The success metrics from the plan: are you training, is the team's autonomy earned, and is safety holding. One hero number, each fact once. */
export default function Scorecard() {
  const [s, setS] = useState<Score | null>(null);
  const [trust, setTrust] = useState<{ types: TrustRow[]; strict_new_types: boolean } | null>(null);
  const [err, setErr] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const alive = useRef(true);
  const { epoch } = useOnline();
  const toast = useToast();

  const load = useCallback(async () => {
    try {
      const [a, t] = await Promise.all([getScorecard(), getTrust()]);
      if (!alive.current) return;
      setS(a); setTrust(t); setErr(false);
    } catch { if (alive.current) setErr(true); }
    finally { if (alive.current) setRefreshing(false); }
  }, []);
  useEffect(() => { alive.current = true; load(); return () => { alive.current = false; }; }, [load, epoch]);

  const toggleStrict = async (on: boolean) => {
    if (trust) setTrust({ ...trust, strict_new_types: on });
    try { await setStrictNewTypes(on); toast(on ? 'New kinds of change will start stricter' : 'Back to the normal start'); } catch { toast("Couldn't change that", { tone: 'warn' }); load(); }
  };

  if (!s && !err) return <ScrollView contentContainerStyle={{ padding: 20, gap: 14 }}><Skeleton height={150} radius={26} /><Skeleton height={28} width="40%" radius={8} /><Skeleton height={110} radius={26} /><Skeleton height={28} width="55%" radius={8} /><Skeleton height={90} radius={26} /></ScrollView>;
  if (!s) return <View style={{ padding: 20 }}><ErrorCard message="Couldn't load your scorecard" onRetry={load} /></View>;

  const a = s.adherence;
  const rate = a.rate_14d ?? 0;
  const au = s.autonomy;
  const decided = au.accepted + au.overridden;
  const safe = s.safety.false_negatives === 0;
  const gp = s.goal_progress.lifts_trending;

  return (
    <ScrollView contentContainerStyle={{ padding: 20, gap: 14, paddingBottom: 40 }}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={() => { setRefreshing(true); load(); }} tintColor={colors.lime} />}>

      {s.disruptions.active && (
        <Card glow={colors.amber} style={{ gap: 4 }}>
          <Txt style={{ fontFamily: font.bold, color: colors.amber }}>Lighter-week mode is on</Txt>
          <Txt style={{ color: colors.dim, lineHeight: 20 }}>Your team noticed {s.disruptions.active.kind.replace('_', ' ')} and eased your sessions. They'll bring your volume back gradually once you train again.</Txt>
        </Card>
      )}

      <FadeIn>
        <Card glow={colors.lime} style={{ flexDirection: 'row', alignItems: 'center', gap: 18 }}>
          <Ring size={108} stroke={11} progress={rate}>
            <Txt style={{ fontFamily: font.black, fontSize: 26 }} accessibilityLabel={`Consistency ${pct(a.rate_14d)}`}>{pct(a.rate_14d)}</Txt>
          </Ring>
          <View style={{ flex: 1, gap: 4 }}>
            <Txt style={{ fontFamily: font.black, fontSize: 18 }}>Consistency</Txt>
            <Txt style={{ color: rate >= 0.8 ? colors.lime : colors.dim, fontFamily: font.semibold, fontSize: 13.5, lineHeight: 19 }}>
              {a.planned_14d === 0 ? 'No sessions planned yet.' : rate >= 0.8 ? 'Strong rhythm. Keep it going.' : rate >= 0.5 ? 'A decent base. Every session counts.' : 'Life happens. Your team will fit the plan around you.'}
            </Txt>
            <Txt style={{ color: colors.dim, fontSize: 14, lineHeight: 20 }}>{a.sessions_14d} of {a.planned_14d} planned sessions in the last 2 weeks.</Txt>
            <Txt style={{ color: colors.faint, fontSize: 12.5 }}>4 weeks: {a.sessions_28d} of {a.planned_28d} ({pct(a.rate_28d)}){a.days_since_last_session != null ? ` · last session ${a.days_since_last_session < 1 ? 'today' : `${Math.round(a.days_since_last_session)}d ago`}` : ''}</Txt>
          </View>
        </Card>
      </FadeIn>

      <SectionTitle>Safety</SectionTitle>
      <Card glow={safe ? undefined : colors.red} style={{ gap: 10 }}>
        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10 }}>
          <Ionicons name={safe ? 'shield-checkmark' : 'warning'} size={26} color={safe ? colors.lime : colors.red} />
          <View style={{ flex: 1 }}>
            <Txt accessibilityRole="header" style={{ fontFamily: font.bold, fontSize: 16, color: safe ? colors.lime : colors.red }}>{safe ? 'No warning signs missed' : `${s.safety.false_negatives} warning sign${s.safety.false_negatives === 1 ? '' : 's'} not acted on`}</Txt>
            <Txt style={{ color: colors.dim, fontSize: 13, lineHeight: 18 }}>
              {safe ? 'Every red flag or worrying note in the last 30 days led to a pause.' : 'Something you wrote needs a qualified professional. Open Your team to see what is paused.'}
            </Txt>
          </View>
        </View>
        <View style={{ flexDirection: 'row', gap: 12 }}>
          <Stat2 value={s.safety.escalations_30d} label="safety pauses (30 days)" />
          <Stat2 value={au.blocked_by_policy} label={au.blocked_by_policy === 1 ? 'change stopped by safety rules' : 'changes stopped by safety rules'} />
        </View>
      </Card>

      <SectionTitle>Your team's changes</SectionTitle>
      <Card style={{ gap: 10 }}>
        {decided === 0 ? (
          <Txt style={{ color: colors.dim, lineHeight: 21 }}>No automatic changes decided yet. When your team adjusts your plan, this shows how many you kept versus undid.</Txt>
        ) : (
          <>
            <Txt style={{ fontSize: 15.5, lineHeight: 22 }}>You kept <Txt style={{ fontFamily: font.bold, color: colors.lime }}>{au.accepted}</Txt> of {decided} changes{au.overridden ? ` and undid ${au.overridden}` : ''} in the last {au.window_days} days.</Txt>
            <AnimBar value={au.acceptance_rate ?? 0} color={colors.lime} height={8} />
          </>
        )}
        {au.waiting > 0 && <Txt style={{ color: colors.amber, fontSize: 13.5 }}>{au.waiting} waiting for you in Your team.</Txt>}
      </Card>

      {gp && s.goal_progress.available && (
        <>
          <SectionTitle>Your lifts</SectionTitle>
          <Card style={{ flexDirection: 'row', gap: 12 }}>
            <Stat2 value={gp.up} label="going up" color={colors.lime} />
            <Stat2 value={gp.flat} label="holding steady" />
            <Stat2 value={gp.down} label="going down" color={gp.down ? colors.amber : colors.text} />
          </Card>
        </>
      )}

      {trust && (
        <>
          <SectionTitle>Earned trust</SectionTitle>
          <Card style={{ gap: 12 }}>
            <Txt style={{ color: colors.dim, fontSize: 13.5, lineHeight: 20 }}>The more you accept a kind of change, the less your team makes you wait. Undo or stop some, and it gets stricter. Big changes and safety limits always need you.</Txt>
            {trust.types.map((t) => {
              const lv = LEVEL[t.level];
              return (
                <View key={t.type} style={{ gap: 4 }} accessibilityLabel={`${TYPE_LABEL[t.type] ?? t.type}: ${lv.label}. ${lv.hint}`}>
                  <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
                    <Txt style={{ flex: 1, fontFamily: font.semibold, fontSize: 15 }}>{TYPE_LABEL[t.type] ?? t.type}</Txt>
                    <View style={{ paddingHorizontal: 10, paddingVertical: 4, minHeight: 26, justifyContent: 'center', borderRadius: 999, backgroundColor: lv.color + '22', borderWidth: 1, borderColor: lv.color + '66' }}>
                      <Txt style={{ color: lv.color, fontFamily: font.bold, fontSize: 12 }}>{lv.label}</Txt>
                    </View>
                  </View>
                  <Txt style={{ color: colors.faint, fontSize: 12.5 }}>{t.decisions ? `${t.accepted} kept, ${t.pushed_back} undone or stopped` : 'No history yet'}</Txt>
                </View>
              );
            })}
            <View style={{ height: 1, backgroundColor: colors.border }} />
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12, minHeight: 48 }}>
              <View style={{ flex: 1 }}>
                <Txt style={{ fontFamily: font.semibold, fontSize: 15 }}>New kinds of change start stricter</Txt>
                <Txt style={{ color: colors.dim, fontSize: 12.5, lineHeight: 17 }}>A first-ever change waits for your veto window until it has a track record. Slower, safer.</Txt>
              </View>
              <Switch accessibilityLabel="New kinds of change start stricter" value={trust.strict_new_types} onValueChange={toggleStrict} trackColor={{ false: colors.cardHi, true: colors.lime }} thumbColor="#fff" />
            </View>
          </Card>
        </>
      )}

      {s.disruptions.count > 0 && (
        <>
          <SectionTitle>Bouncing back</SectionTitle>
          <Card style={{ flexDirection: 'row', gap: 12 }}>
            <Stat2 value={s.disruptions.count} label="disruptions so far" />
            <Stat2 value={s.disruptions.avg_days_to_recover != null ? `${s.disruptions.avg_days_to_recover}d` : '–'} label="average time to get back" />
          </Card>
        </>
      )}

      <Txt style={{ color: colors.faint, fontSize: 12, textAlign: 'center' }}>AI calls since the server started: {s.cost.model_calls_since_start}</Txt>
    </ScrollView>
  );
}
