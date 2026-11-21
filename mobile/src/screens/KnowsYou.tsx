import React, { useCallback, useEffect, useRef, useState } from 'react';
import { KeyboardAvoidingView, Modal, Platform, Pressable, RefreshControl, ScrollView, TextInput, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import {
  MemDiscovery, MemDossier, MemEvent, MemItem, MemOverview, MemQuestion,
  ApiError, answerMemoryQuestion, confirmDiscovery, confirmMemoryFact, correctMemoryFact, dismissDiscovery, dismissMemoryQuestion, forgetMemoryFact,
  getMemoryDiscoveries, getMemoryDossier, getMemoryOverview, getMemoryQuestion, getMemoryTimeline, rememberAboutMe,
} from '../api';
import { Sheet } from '../forms';
import * as Haptics from '../haptics';
import { colors, font } from '../theme';
import { Card, EmptyState, ErrorCard, FadeIn, GradientButton, IconButton, Ring, SectionTitle, Segmented, Skeleton, ToastProvider, Txt, useOnline, useToast } from '../ui';
import { Pill } from '../ux/controls';
import ConfirmSheet, { Confirm } from '../ux/ConfirmSheet';

const GUTTER = 16;
const SECTION_ORDER = ['goals', 'training', 'nutrition', 'recovery', 'behavior', 'calibration', 'track_record'] as const;
const SECTION_ICON: Record<string, string> = { goals: 'flag', training: 'barbell', nutrition: 'nutrition', recovery: 'moon', behavior: 'pulse', calibration: 'speedometer', track_record: 'checkmark-done' };
const SECTION_COLOR: Record<string, string> = { goals: colors.lime, training: colors.lime, nutrition: colors.amber, recovery: colors.violet, behavior: colors.cyan, calibration: colors.cyan, track_record: colors.mint };

/** "3 days ago" from an ISO date or timestamp; empty when unknown. */
function ago(iso?: string | null): string {
  if (!iso) return '';
  const t = Date.parse(iso.length <= 10 ? `${iso}T12:00:00` : iso);
  if (isNaN(t)) return '';
  const d = Math.floor((Date.now() - t) / 86400000);
  if (d <= 0) return 'today';
  if (d === 1) return 'yesterday';
  if (d < 30) return `${d} days ago`;
  const m = Math.floor(d / 30);
  return m === 1 ? 'a month ago' : `${m} months ago`;
}

const Tag = ({ label, color = colors.dim }: { label: string; color?: string }) => (
  <View style={{ paddingHorizontal: 10, paddingVertical: 4, borderRadius: 999, backgroundColor: color + '1F', borderWidth: 1, borderColor: color + '55' }}>
    <Txt style={{ fontSize: 12, fontFamily: font.semibold, color }}>{label}</Txt>
  </View>
);

function ActionButton({ label, icon, onPress, color = colors.text, tone }: { label: string; icon: string; onPress: () => void; color?: string; tone?: 'danger' }) {
  const c = tone === 'danger' ? colors.red : color;
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={label} onPress={() => { Haptics.selectionAsync(); onPress(); }}
      style={({ pressed }) => ({ flex: 1, minHeight: 44, borderRadius: 14, flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 6, backgroundColor: c + '1A', borderWidth: 1, borderColor: c + '55', opacity: pressed ? 0.7 : 1 })}>
      <Ionicons name={icon as any} size={16} color={c} />
      <Txt style={{ fontFamily: font.bold, fontSize: 13.5, color: c }}>{label}</Txt>
    </Pressable>
  );
}

/** One belief: what I think, where it came from and how sure I am. Tap it to confirm, correct or forget it. */
function Fact({ item, open, onToggle, onConfirm, onCorrect, onForget }: {
  item: MemItem; open: boolean; onToggle: () => void; onConfirm: () => void; onCorrect: () => void; onForget: () => void;
}) {
  const confirmed = item.user_state === 'confirmed' || item.user_state === 'corrected';
  const unsure = item.confidence < 0.6 && !confirmed;
  const quote = item.evidence.find((e) => e.kind === 'quote');
  const meta = [item.source_label, ago(item.since)].filter(Boolean).join(' · ');
  return (
    <View style={{ borderTopWidth: 1, borderTopColor: colors.border }}>
      <Pressable accessibilityRole="button" accessibilityState={{ expanded: open }} accessibilityLabel={`${item.text}. ${confirmed ? 'You confirmed this.' : unsure ? 'Not sure yet.' : ''} Tap for options.`} onPress={onToggle}
        style={({ pressed }) => ({ minHeight: 56, paddingVertical: 12, paddingHorizontal: 16, flexDirection: 'row', alignItems: 'center', gap: 10, opacity: pressed ? 0.7 : 1 })}>
        <View style={{ flex: 1, gap: 3 }}>
          <Txt style={{ fontSize: 15, lineHeight: 21, fontFamily: font.medium }}>{item.text}</Txt>
          <Txt style={{ fontSize: 12.5, color: unsure ? colors.amber : colors.faint }}>{meta}{unsure ? ' · not sure yet' : ''}</Txt>
        </View>
        {confirmed && <Ionicons name="checkmark-circle" size={20} color={colors.lime} />}
        <Ionicons name={open ? 'chevron-up' : 'chevron-down'} size={18} color={colors.faint} />
      </Pressable>
      {open && (
        <View style={{ paddingHorizontal: 16, paddingBottom: 14, gap: 10 }}>
          {quote && <Txt style={{ color: colors.dim, fontSize: 13, lineHeight: 18 }}>You said: “{quote.text}”</Txt>}
          {!quote && item.evidence[0] && <Txt style={{ color: colors.dim, fontSize: 13 }}>Based on: {item.evidence[0].text}</Txt>}
          <View style={{ flexDirection: 'row', gap: 8 }}>
            {!confirmed && <ActionButton label="That's right" icon="checkmark" color={colors.lime} onPress={onConfirm} />}
            <ActionButton label="Correct" icon="create" color={colors.cyan} onPress={onCorrect} />
            <ActionButton label="Forget" icon="trash" tone="danger" onPress={onForget} />
          </View>
        </View>
      )}
    </View>
  );
}

function Collapsible({ title, icon, color, count, open, onToggle, children, subtitle }: { title: string; icon: string; color: string; count: number; open: boolean; onToggle: () => void; children: React.ReactNode; subtitle?: string }) {
  return (
    <Card style={{ padding: 0, gap: 0, overflow: 'hidden' }}>
      <Pressable accessibilityRole="button" accessibilityState={{ expanded: open }} accessibilityLabel={`${title}, ${count} item${count === 1 ? '' : 's'}`} onPress={() => { Haptics.selectionAsync(); onToggle(); }}
        style={({ pressed }) => ({ minHeight: 56, paddingHorizontal: 16, flexDirection: 'row', alignItems: 'center', gap: 12, opacity: pressed ? 0.7 : 1 })}>
        <View style={{ width: 34, height: 34, borderRadius: 11, backgroundColor: color + '22', alignItems: 'center', justifyContent: 'center' }}><Ionicons name={icon as any} size={18} color={color} /></View>
        <View style={{ flex: 1 }}>
          <Txt style={{ fontFamily: font.semibold, fontSize: 15.5 }}>{title}</Txt>
          {!!subtitle && <Txt style={{ color: colors.faint, fontSize: 12 }}>{subtitle}</Txt>}
        </View>
        <Txt style={{ color: colors.dim, fontSize: 14, fontFamily: font.semibold }}>{count}</Txt>
        <Ionicons name={open ? 'chevron-up' : 'chevron-down'} size={18} color={colors.faint} />
      </Pressable>
      {open && children}
    </Card>
  );
}

function Body() {
  const [view, setView] = useState<'now' | 'history'>('now');
  const [ov, setOv] = useState<MemOverview | null>(null);
  const [dos, setDos] = useState<MemDossier | null>(null);
  const [disc, setDisc] = useState<MemDiscovery[]>([]);
  const [q, setQ] = useState<MemQuestion | null>(null);
  const [events, setEvents] = useState<MemEvent[] | null>(null);
  const [err, setErr] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [openSec, setOpenSec] = useState<Record<string, boolean>>({});
  const [openFact, setOpenFact] = useState<number | null>(null);
  const [more, setMore] = useState(false);               // "something else" on a question with answer chips
  const [text, setText] = useState('');                  // free-text answer
  const [teach, setTeach] = useState('');                // "tell me something"
  const [busy, setBusy] = useState(false);
  const [fixing, setFixing] = useState<MemItem | null>(null);
  const [fixText, setFixText] = useState('');
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const alive = useRef(true);
  const toast = useToast();
  const { epoch } = useOnline();

  const load = useCallback(async () => {
    try {
      const [o, d, ds, qs] = await Promise.all([getMemoryOverview(), getMemoryDossier(), getMemoryDiscoveries(), getMemoryQuestion()]);
      if (!alive.current) return;
      setOv(o); setDos(d); setDisc(ds.discoveries); setQ(qs.questions[0] ?? null); setErr(false);
    } catch { if (alive.current) setErr(true); }
    finally { if (alive.current) setRefreshing(false); }
  }, []);
  useEffect(() => { alive.current = true; load(); return () => { alive.current = false; }; }, [load, epoch]);
  useEffect(() => {
    if (view !== 'history' || events) return;
    getMemoryTimeline(40).then((r) => alive.current && setEvents(r.events)).catch(() => alive.current && setEvents([]));
  }, [view, events]);

  const fail = (msg = "Couldn't save that. Check your connection and try again.") => toast(msg, { tone: 'warn' });
  const refreshAfter = async () => { setEvents(null); await load(); };

  // ---- the question of the day
  const answer = async (a: string) => {
    if (!q || busy || !a.trim()) return;
    setBusy(true);
    try {
      const r = await answerMemoryQuestion(q.id, a.trim());
      toast(r.understood ? `Got it${r.learned[0]?.body ? `: ${r.learned[0].body}` : ''}` : "Thanks. I couldn't turn that into something I can use yet.");
      setText(''); setMore(false);
      await refreshAfter();
    } catch { fail("I couldn't use that answer. Try a few plain words."); }
    setBusy(false);
  };
  const skipQuestion = async () => {
    if (!q) return;
    try { await dismissMemoryQuestion(q.id); setQ(null); toast("OK, I won't ask that for a while"); } catch { fail(); }
  };

  // ---- discoveries
  const rate = async (d: MemDiscovery, yes: boolean) => {
    Haptics.selectionAsync();
    try {
      await (yes ? confirmDiscovery(d.id) : dismissDiscovery(d.id));
      setDisc((all) => (yes ? all.map((x) => (x.id === d.id ? { ...x, pending: false, user_state: 'confirmed' } : x)) : all.filter((x) => x.id !== d.id)));
      toast(yes ? (d.status === 'candidate' ? "Thanks, that helps me check it" : "Thanks, I'll lean on that") : "OK, I won't use that");
    } catch { fail(); }
  };

  // ---- facts
  const confirmFact = async (it: MemItem) => { try { await confirmMemoryFact(it.fact_id!); toast('Locked in'); await refreshAfter(); } catch { fail(); } };
  const startFix = (it: MemItem) => { setFixing(it); setFixText(''); };
  const saveFix = async () => {
    if (!fixing || !fixText.trim() || busy) return;
    setBusy(true);
    try {
      const r = await correctMemoryFact(fixing.fact_id!, fixText.trim());
      setFixing(null);
      const p = r?.profile;
      const hasChange = !!p && (Array.isArray(p.changed) ? p.changed.length > 0 : !!p.changed && Object.keys(p.changed).length > 0);
      if (p && hasChange) {
        const parts = ['Updated your profile'];
        if (p.nutrition_updated) parts.push('targets recalculated');
        if (p.plan_rebuilt) parts.push('plan rebuilt');
        toast(parts.join(', '));
      } else toast('Corrected. Thanks for telling me');
      await refreshAfter();
    } catch (e) {
      // a 400 carries a plain-language reason from the server; keep the sheet open so the value can be fixed
      const msg = e instanceof ApiError && e.status === 400 && e.message ? e.message : 'Use a few plain words, like "5 days" or "left shoulder".';
      fail(msg);
    }
    setBusy(false);
  };
  const askForget = (it: MemItem) => setConfirm({
    title: 'Forget this?', message: `I'll stop using "${it.text}" and I won't bring it back on my own. You can always tell me again.`, action: 'Forget it', danger: true, icon: 'trash',
    onYes: async () => { try { const r = await forgetMemoryFact(it.fact_id!); setConfirm(null); toast(r?.note || 'Forgotten'); await refreshAfter(); } catch { setConfirm(null); fail(); } },
  });
  const teachMe = async () => {
    if (!teach.trim() || busy) return;
    setBusy(true);
    try {
      const r = await rememberAboutMe(teach.trim());
      toast(r.understood ? `Noted: ${r.learned[0]?.body ?? 'got it'}` : "I couldn't turn that into something specific. Try: I can only train 4 days a week.", r.understood ? undefined : { tone: 'warn' });
      if (r.understood) { setTeach(''); await refreshAfter(); }
    } catch { fail("I can only remember plain facts about you, not instructions."); }
    setBusy(false);
  };

  if (!ov && !err) return <ScrollView contentContainerStyle={{ padding: GUTTER, gap: 14 }}><Skeleton height={170} /><Skeleton height={150} /><Skeleton height={90} /><Skeleton height={90} /></ScrollView>;
  if (!ov || !dos) return <View style={{ padding: GUTTER }}><ErrorCard message="Couldn't load what I know about you" onRetry={() => { setErr(false); load(); }} /></View>;

  // each fact appears once: patterns live in the "I noticed" cards, health items in "Health notes", the rest in their section
  const health: MemItem[] = [];
  const by: Record<string, MemItem[]> = {};
  Object.entries(dos.sections).forEach(([sec, items]) => {
    const own = items.filter((i) => i.fact_id || !i.discovery_id);
    if (sec === 'risk') own.forEach((i) => health.push(i));
    else {
      by[sec] = own.filter((i) => { if (i.sensitive && i.fact_id) { health.push(i); return false; } return true; });
    }
  });
  const sections = SECTION_ORDER.filter((s) => (by[s] ?? []).length > 0);
  const isNew = ov.stage === 'new';
  const diff = dos.diff;
  const changed = [...(diff.used_to ?? []).filter((u) => !u.sensitive).map((u) => `Before: ${u.before}. Now: ${u.now}.`), ...(diff.added ?? []).filter((a) => a.section !== 'risk').slice(0, 3).map((a) => `New: ${a.text}`)].slice(0, 5);
  const secOpen = (s: string, idx: number) => openSec[s] ?? idx < 2;
  const factProps = (it: MemItem) => ({ item: it, open: openFact === it.fact_id, onToggle: () => setOpenFact(openFact === it.fact_id ? null : it.fact_id ?? null), onConfirm: () => confirmFact(it), onCorrect: () => startFix(it), onForget: () => askForget(it) });
  const nothingYet = sections.length === 0 && health.length === 0 && disc.length === 0;

  return (
    <>
      <ScrollView contentContainerStyle={{ padding: GUTTER, gap: 14, paddingBottom: 48 }} keyboardShouldPersistTaps="handled"
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={() => { setRefreshing(true); refreshAfter(); }} tintColor={colors.lime} />}>
        <Segmented options={[{ key: 'now', label: 'What I know' }, { key: 'history', label: 'How I learned it' }]} value={view} onChange={setView} />

        {view === 'now' ? (
          <>
            <FadeIn>
              <Card glow={colors.lime} style={{ flexDirection: 'row', alignItems: 'center', gap: 18 }}>
                <Ring size={112} stroke={11} progress={ov.coverage}>
                  <Txt style={{ fontFamily: font.black, fontSize: 26 }} accessibilityLabel={`I know ${ov.coverage_pct} percent of the basics about you`}>{ov.coverage_pct}%</Txt>
                </Ring>
                <View style={{ flex: 1, gap: 4 }}>
                  <Txt style={{ fontFamily: font.black, fontSize: 18 }}>{isNew ? "I'm still getting to know you" : 'The basics I know'}</Txt>
                  <Txt style={{ color: colors.dim, fontSize: 14, lineHeight: 20 }}>{ov.narrative}</Txt>
                  <Txt style={{ color: colors.faint, fontSize: 12.5 }}>{ov.slots_known} of {ov.slots_total} basics{ov.changed ? ` · ${ov.changed}` : ''}</Txt>
                </View>
              </Card>
            </FadeIn>

            {q && (
              <FadeIn delay={60}>
                <SectionTitle>A question for you</SectionTitle>
                <Card glow={colors.cyan} style={{ gap: 12, marginTop: 10 }}>
                  <Txt style={{ fontFamily: font.bold, fontSize: 17, lineHeight: 23 }}>{q.question}</Txt>
                  {!!q.why && <Txt style={{ color: colors.dim, fontSize: 13.5, lineHeight: 19 }}>{q.why}</Txt>}
                  {q.options.length > 0 && (
                    <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8 }}>
                      {q.options.map((o) => <Pill key={o} label={o} onPress={() => answer(o)} color={colors.cyan} />)}
                      <Pill label="Something else" active={more} onPress={() => setMore((m) => !m)} color={colors.cyan} />
                    </View>
                  )}
                  {(q.free_text || more) && (
                    <View style={{ flexDirection: 'row', gap: 8, alignItems: 'center' }}>
                      <TextInput value={text} onChangeText={setText} placeholder="Type your answer" placeholderTextColor={colors.faint} maxLength={200} accessibilityLabel="Your answer" returnKeyType="send" onSubmitEditing={() => answer(text)}
                        style={{ flex: 1, minHeight: 48, borderRadius: 14, paddingHorizontal: 14, color: colors.text, fontFamily: font.regular, fontSize: 15, backgroundColor: colors.bg2, borderWidth: 1, borderColor: colors.border }} />
                      <IconButton icon="send" label="Send answer" onPress={() => answer(text)} color="#07080B" bg={colors.cyan} disabled={!text.trim() || busy} />
                    </View>
                  )}
                  <Pressable accessibilityRole="button" accessibilityLabel="Not now" onPress={skipQuestion} style={{ minHeight: 44, justifyContent: 'center', alignSelf: 'flex-start' }}>
                    <Txt style={{ color: colors.faint, fontSize: 13.5, fontFamily: font.semibold }}>Not now</Txt>
                  </Pressable>
                </Card>
              </FadeIn>
            )}

            {disc.length > 0 && (
              <FadeIn delay={100} style={{ gap: 10 }}>
                <SectionTitle>I noticed</SectionTitle>
                {disc.map((d) => (
                  <Card key={d.id} style={{ gap: 10 }} glow={d.pending ? colors.violet : undefined}>
                    <View style={{ flexDirection: 'row' }}>
                      {d.status === 'candidate' ? <Tag label="Still learning" color={colors.amber} />
                        : d.status === 'weakening' || d.weakening ? <Tag label="Weakening" color={colors.amber} />
                        : <Tag label="Confirmed pattern" color={colors.violet} />}
                    </View>
                    <Txt style={{ fontFamily: font.bold, fontSize: 16 }}>{d.title ?? 'A pattern in your data'}</Txt>
                    <Txt style={{ fontSize: 15, lineHeight: 22 }}>{d.text}</Txt>
                    {d.chips.length > 0 && <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 6 }}>{d.chips.map((c) => <Tag key={c} label={c} color={c.startsWith('may be') ? colors.amber : colors.violet} />)}</View>}
                    {!!d.recommendation && <Txt style={{ color: colors.dim, fontSize: 13.5, lineHeight: 19 }}>{d.recommendation}</Txt>}
                    {d.pending ? (
                      <View style={{ flexDirection: 'row', gap: 8 }}>
                        <ActionButton label={d.status === 'candidate' ? 'Seems right' : "Yes, that's me"} icon="checkmark" color={colors.lime} onPress={() => rate(d, true)} />
                        <ActionButton label="Not really" icon="close" color={colors.dim} onPress={() => rate(d, false)} />
                      </View>
                    ) : <View style={{ flexDirection: 'row' }}><Tag label="You confirmed this" color={colors.lime} /></View>}
                  </Card>
                ))}
              </FadeIn>
            )}

            {changed.length > 0 && (
              <FadeIn delay={140} style={{ gap: 10 }}>
                <SectionTitle>Changed recently</SectionTitle>
                <Card style={{ gap: 8 }}>
                  {changed.map((c, i) => <Txt key={i} style={{ fontSize: 14.5, lineHeight: 21 }}>{c}</Txt>)}
                </Card>
              </FadeIn>
            )}

            {nothingYet ? (
              <EmptyState icon="sparkles" tint={colors.violet} title="Nothing here yet" body="Answer the question above, or just tell me about your training below. Everything I learn shows up here, and you can correct or delete any of it." />
            ) : (
              <>
                <SectionTitle>What I know</SectionTitle>
                <Txt style={{ color: colors.faint, fontSize: 13, lineHeight: 18, marginTop: -6 }}>Tap any line to confirm it, correct it or have me forget it.</Txt>
                {sections.map((s, idx) => (
                  <Collapsible key={s} title={dos.section_titles[s] ?? s} icon={SECTION_ICON[s] ?? 'ellipse'} color={SECTION_COLOR[s] ?? colors.lime} count={by[s].length} open={secOpen(s, idx)} onToggle={() => setOpenSec((o) => ({ ...o, [s]: !secOpen(s, idx) }))}>
                    {by[s].map((it, i) => it.fact_id ? <Fact key={it.fact_id} {...factProps(it)} /> : (
                      <View key={`t${i}`} style={{ borderTopWidth: 1, borderTopColor: colors.border, padding: 16, gap: 3 }}>
                        <Txt style={{ fontSize: 15, lineHeight: 21 }}>{it.text}</Txt>
                        <Txt style={{ fontSize: 12.5, color: colors.faint }}>{it.source_label}</Txt>
                      </View>
                    ))}
                  </Collapsible>
                ))}
                {health.length > 0 && (
                  <Collapsible title="Health notes" subtitle="Only you and the coaches who need them see these" icon="shield-checkmark" color={colors.amber} count={health.length} open={!!openSec.health} onToggle={() => setOpenSec((o) => ({ ...o, health: !o.health }))}>
                    {health.map((it, i) => it.fact_id ? <Fact key={it.fact_id} {...factProps(it)} /> : <View key={`h${i}`} style={{ borderTopWidth: 1, borderTopColor: colors.border, padding: 16 }}><Txt>{it.text}</Txt></View>)}
                  </Collapsible>
                )}
              </>
            )}

            <FadeIn delay={180} style={{ gap: 10 }}>
              <SectionTitle>Tell me something</SectionTitle>
              <Card style={{ gap: 10 }}>
                <Txt style={{ color: colors.dim, fontSize: 13.5, lineHeight: 19 }}>The more I know, the better I coach. Say it like you'd say it to a friend, in English or Hindi.</Txt>
                <View style={{ flexDirection: 'row', gap: 8, alignItems: 'center' }}>
                  <TextInput value={teach} onChangeText={setTeach} placeholder="e.g. I can only train 4 days a week" placeholderTextColor={colors.faint} maxLength={300} accessibilityLabel="Tell me something about you" returnKeyType="send" onSubmitEditing={teachMe}
                    style={{ flex: 1, minHeight: 48, borderRadius: 14, paddingHorizontal: 14, color: colors.text, fontFamily: font.regular, fontSize: 15, backgroundColor: colors.bg2, borderWidth: 1, borderColor: colors.border }} />
                  <IconButton icon="send" label="Send" onPress={teachMe} color="#07080B" bg={colors.lime} disabled={!teach.trim() || busy} />
                </View>
              </Card>
            </FadeIn>
          </>
        ) : (
          <View style={{ gap: 10 }}>
            {events === null ? <><Skeleton height={64} /><Skeleton height={64} /><Skeleton height={64} /></> : events.length === 0 ? (
              <EmptyState icon="time" title="Nothing yet" body="As I learn things about you, each one shows up here with the date, so you can see how my picture of you changed." />
            ) : (
              <Card style={{ padding: 0, gap: 0, overflow: 'hidden' }}>
                {events.map((e, i) => (
                  <View key={`${e.ts}${i}`} accessibilityLabel={`${e.text}, ${ago(e.ts)}`} style={{ paddingVertical: 12, paddingHorizontal: 16, gap: 2, borderTopWidth: i ? 1 : 0, borderTopColor: colors.border }}>
                    <Txt style={{ fontSize: 14.5, lineHeight: 20 }}>{e.sensitive ? 'A health note was updated' : e.text}</Txt>
                    <Txt style={{ fontSize: 12, color: colors.faint }}>{ago(e.ts)}</Txt>
                  </View>
                ))}
              </Card>
            )}
          </View>
        )}
      </ScrollView>

      <Sheet visible={!!fixing} onClose={() => setFixing(null)} title="Correct this"
        footer={<GradientButton label={busy ? 'Saving…' : 'Save correction'} icon="checkmark" onPress={saveFix} />}>
        <Txt style={{ color: colors.dim, fontSize: 14, lineHeight: 20 }}>I have: {fixing?.text}</Txt>
        <TextInput value={fixText} onChangeText={setFixText} autoFocus placeholder="What's right? A few plain words" placeholderTextColor={colors.faint} maxLength={60} accessibilityLabel="The correct value"
          style={{ minHeight: 52, borderRadius: 14, paddingHorizontal: 14, color: colors.text, fontFamily: font.regular, fontSize: 16, backgroundColor: colors.bg, borderWidth: 1, borderColor: colors.border }} />
        <Txt style={{ color: colors.faint, fontSize: 12.5 }}>Your correction wins over anything I work out myself.</Txt>
      </Sheet>
      <ConfirmSheet confirm={confirm} onClose={() => setConfirm(null)} />
    </>
  );
}

/** "What I know about you": the athlete model, readable and correctable. One hero number (how much of the basics is known), everything else is a fact you can fix. */
export default function KnowsYou({ visible, onClose }: { visible: boolean; onClose: () => void }) {
  return (
    <Modal visible={visible} animationType="slide" onRequestClose={onClose}>
      <ToastProvider bottom={28}>
        <View style={{ flex: 1, backgroundColor: colors.bg }}>
          <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8, paddingHorizontal: GUTTER, paddingTop: Platform.OS === 'ios' ? 56 : 28, paddingBottom: 8 }}>
              <View style={{ flex: 1 }}>
                <Txt accessibilityRole="header" style={{ fontFamily: font.black, fontSize: 24, letterSpacing: -0.4 }}>What I know about you</Txt>
                <Txt style={{ color: colors.dim, fontSize: 13 }}>Correct anything that's wrong. Forgotten stays forgotten.</Txt>
              </View>
              <IconButton icon="close" label="Close" onPress={onClose} size={26} color={colors.text} bg="rgba(255,255,255,0.07)" />
            </View>
            {visible && <Body />}
          </KeyboardAvoidingView>
        </View>
      </ToastProvider>
    </Modal>
  );
}
