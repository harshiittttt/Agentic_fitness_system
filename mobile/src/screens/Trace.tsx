import React, { useCallback, useEffect, useState } from 'react';
import { Pressable, RefreshControl, ScrollView, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { TraceRun, TraceSpan, getTraceRun, getTraceRuns } from '../api';
import * as Haptics from '../haptics';
import { colors, font } from '../theme';
import { Card, EmptyState, ErrorCard, FadeIn, Skeleton, Txt, useOnline } from '../ui';
import CoachShell from './CoachShell';

const KIND_ICON: Record<string, string> = { event: 'flash', agent: 'person', tool: 'construct', llm: 'sparkles', critic: 'shield-checkmark', policy: 'git-compare' };
const KIND_COLOR: Record<string, string> = { event: colors.lime, agent: colors.cyan, tool: colors.amber, llm: colors.violet, critic: colors.mint, policy: colors.pink };

const outcomeColor = (o: string) => (o === 'ok' || o === 'applied' ? colors.mint : o === 'error' || o === 'blocked' ? colors.red : colors.amber);

/** "850 ms" / "7.3 s" from milliseconds; empty when unknown. */
function dur(ms: number | null | undefined): string {
  if (ms == null || !isFinite(ms)) return '';
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
}

/** "3 min ago" from an ISO timestamp; empty when unknown. */
function ago(iso: string): string {
  const t = Date.parse(iso);
  if (isNaN(t)) return '';
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

const tokens = (a: number | null | undefined, b: number | null | undefined) => (a || b ? `${a ?? 0} in · ${b ?? 0} out tokens` : '');

function SpanRow({ s, depth }: { s: TraceSpan; depth: number }) {
  const color = KIND_COLOR[s.kind] ?? colors.dim;
  const meta = [dur(s.dur_ms), tokens(s.tokens_in, s.tokens_out)].filter(Boolean).join(' · ');
  const model = typeof s.attrs?.model === 'string' ? (s.attrs.model as string) : '';
  return (
    <View>
      <View style={{ flexDirection: 'row', alignItems: 'flex-start', gap: 8, paddingLeft: Math.min(depth, 6) * 14, paddingVertical: 5 }}>
        <Ionicons name={(KIND_ICON[s.kind] ?? 'ellipse') as any} size={15} color={color} style={{ marginTop: 2 }} />
        <View style={{ flex: 1 }}>
          <View style={{ flexDirection: 'row', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
            <Txt style={{ fontFamily: font.semibold, fontSize: 14 }}>{s.name}</Txt>
            <View style={{ width: 7, height: 7, borderRadius: 4, backgroundColor: outcomeColor(s.outcome) }} />
            <Txt style={{ color: colors.dim, fontSize: 12 }}>{s.outcome}</Txt>
          </View>
          {!!(meta || model) && <Txt style={{ color: colors.dim, fontSize: 12.5 }}>{[meta, model].filter(Boolean).join(' · ')}</Txt>}
          {!!s.error && <Txt style={{ color: colors.red, fontSize: 12.5, lineHeight: 17 }}>{s.error}</Txt>}
        </View>
      </View>
      {s.children?.map((c) => <SpanRow key={c.id} s={c} depth={depth + 1} />)}
    </View>
  );
}

function RunCard({ run, open, onToggle }: { run: TraceRun; open: boolean; onToggle: () => void }) {
  const [tree, setTree] = useState<TraceSpan[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    if (!open || tree) return;
    let live = true;
    getTraceRun(run.run_id).then((r) => { if (live) setTree(r.tree); }).catch((e: any) => { if (live) setErr(e?.message ?? 'Could not load this run'); });
    return () => { live = false; };
  }, [open, tree, run.run_id]);
  const color = KIND_COLOR[run.kind] ?? colors.dim;
  const meta = [dur(run.dur_ms), run.spans > 1 ? `${run.spans} steps` : '', tokens(run.tokens_in, run.tokens_out)].filter(Boolean).join(' · ');
  return (
    <Card style={{ gap: 8 }}>
      <Pressable accessibilityRole="button" accessibilityState={{ expanded: open }} accessibilityLabel={`${run.name}, ${run.outcome}, ${ago(run.started)}`}
        onPress={() => { Haptics.selectionAsync(); onToggle(); }} style={{ flexDirection: 'row', alignItems: 'center', gap: 10 }}>
        <View style={{ width: 34, height: 34, borderRadius: 12, alignItems: 'center', justifyContent: 'center', backgroundColor: color + '22' }}>
          <Ionicons name={(KIND_ICON[run.kind] ?? 'ellipse') as any} size={18} color={color} />
        </View>
        <View style={{ flex: 1 }}>
          <View style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
            <Txt style={{ fontFamily: font.bold, fontSize: 15 }}>{run.name}</Txt>
            <View style={{ width: 8, height: 8, borderRadius: 4, backgroundColor: outcomeColor(run.outcome) }} />
            {run.errors > 0 && <Txt style={{ color: colors.red, fontSize: 12, fontFamily: font.semibold }}>{run.errors} error{run.errors > 1 ? 's' : ''}</Txt>}
          </View>
          <Txt style={{ color: colors.dim, fontSize: 12.5 }}>{[ago(run.started), meta].filter(Boolean).join(' · ')}</Txt>
        </View>
        <Ionicons name={open ? 'chevron-up' : 'chevron-down'} size={18} color={colors.dim} />
      </Pressable>
      {open && (
        <View style={{ borderTopWidth: 1, borderTopColor: colors.border, paddingTop: 6 }}>
          {err ? <Txt style={{ color: colors.amber, fontSize: 13 }}>{err}</Txt>
            : !tree ? <Skeleton height={44} radius={12} />
            : tree.map((s) => <SpanRow key={s.id} s={s} depth={0} />)}
        </View>
      )}
    </Card>
  );
}

export default function Trace({ visible, onClose }: { visible: boolean; onClose: () => void }) {
  const { epoch } = useOnline();
  const [runs, setRuns] = useState<TraceRun[] | null>(null);
  const [enabled, setEnabled] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    setErr(null);
    try { const r = await getTraceRuns(30); setRuns(r.runs); setEnabled(r.enabled); } catch (e: any) { setErr(e?.message ?? 'Could not load the trace'); }
  }, []);
  useEffect(() => { if (visible) load(); }, [visible, load, epoch]);

  return (
    <CoachShell visible={visible} onClose={onClose} title="Agent trace" subtitle="What your coaches did, step by step. Stays on your device.">
      <ScrollView contentContainerStyle={{ padding: 16, gap: 12, paddingBottom: 40 }}
        refreshControl={<RefreshControl refreshing={refreshing} tintColor={colors.lime} onRefresh={async () => { setRefreshing(true); await load(); setRefreshing(false); }} />}>
        {err ? <ErrorCard message={err} onRetry={load} />
          : !runs ? <><Skeleton height={64} /><Skeleton height={64} /><Skeleton height={64} /></>
          : !enabled ? <EmptyState icon="eye-off" tint={colors.amber} title="Tracing is off" body="Set FITNESS_TRACE=1 on the backend (or remove FITNESS_TRACE=0) to record agent runs." />
          : runs.length === 0 ? <EmptyState icon="pulse" tint={colors.cyan} title="No runs yet" body="When a coach runs, you will see each step here with its timing and token use." />
          : [
            <FadeIn key="sum">
              <Card style={{ flexDirection: 'row', gap: 0, paddingVertical: 14 }}>
                {[{ l: 'Runs', v: String(runs.length), c: colors.text }, { l: 'With errors', v: String(runs.filter((r) => r.errors > 0).length), c: runs.some((r) => r.errors > 0) ? colors.red : colors.mint },
                  { l: 'Typical time', v: dur(runs.map((r) => r.dur_ms ?? 0).sort((x, y) => x - y)[Math.floor(runs.length / 2)]) || '-', c: colors.text }].map((x, i) => (
                  <View key={x.l} accessible accessibilityLabel={`${x.l}: ${x.v}`} style={{ flex: 1, alignItems: 'center', gap: 2, borderLeftWidth: i ? 1 : 0, borderLeftColor: colors.border }}>
                    <Txt maxFontSizeMultiplier={1.3} style={{ fontFamily: font.black, fontSize: 20, color: x.c }}>{x.v}</Txt>
                    <Txt maxFontSizeMultiplier={1.3} style={{ color: colors.dim, fontSize: 11.5, letterSpacing: 0.6, fontFamily: font.semibold }}>{x.l.toUpperCase()}</Txt>
                  </View>
                ))}
              </Card>
            </FadeIn>,
            ...runs.map((r, i) => (
            <FadeIn key={r.run_id} delay={Math.min(i, 6) * 30}>
              <RunCard run={r} open={openId === r.run_id} onToggle={() => setOpenId(openId === r.run_id ? null : r.run_id)} />
            </FadeIn>
          ))]}
        {!!runs?.length && <Txt style={{ color: colors.faint, fontSize: 12, textAlign: 'center' }}>Only timings, counts and short ids are stored. Your messages are never recorded.</Txt>}
      </ScrollView>
    </CoachShell>
  );
}
