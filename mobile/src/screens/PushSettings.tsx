import React, { useCallback, useEffect, useState } from 'react';
import { Switch } from 'react-native';
import { colors } from '../theme';
import { Group, Row } from '../forms';
import { PushCategory, PushStatus, disablePush, enablePush, getPushStatus, sendTestPush, setPushPrefs } from '../push';

const hh = (h: number) => `${String(h).padStart(2, '0')}:00`;

/** Notifications group for the Profile tab: master switch (off by default), per-category switches, quiet hours, test button. */
export default function PushSettings() {
  const [st, setSt] = useState<PushStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const load = useCallback(() => { getPushStatus().then(setSt).catch(() => setSt(null)); }, []);
  useEffect(load, [load]);
  if (!st) return null;
  const on = st.enabled && st.registered;

  const toggle = async (v: boolean) => {
    setBusy(true); setMsg('');
    try {
      if (v) {
        const r = await enablePush();
        if (!r.ok) { setMsg(r.message ?? 'Could not turn on notifications.'); return; }
        setSt(await setPushPrefs({ enabled: true }));
      } else {
        await disablePush();
        setSt(await setPushPrefs({ enabled: false }));
      }
    } catch (e: any) { setMsg(String(e?.message ?? 'Something went wrong.')); }
    finally { setBusy(false); load(); }
  };
  const cat = async (k: PushCategory, v: boolean) => {
    setSt({ ...st, categories: { ...st.categories, [k]: v } });
    try { setSt(await setPushPrefs({ categories: { [k]: v } })); } catch { load(); }
  };
  const test = async () => {
    setBusy(true); setMsg('');
    try { const r = await sendTestPush(); setMsg(r.sent ? 'Sent. It should arrive in a few seconds.' : `Could not send${r.error ? `: ${r.error}` : ''}.`); }
    catch (e: any) { setMsg(String(e?.message ?? 'Could not send.')); }
    finally { setBusy(false); load(); }
  };
  const q = st.quiet_hours;
  return (
    <Group title="Notifications" footer={msg || `Off until you turn it on. Never during quiet hours (${hh(q.start)} to ${hh(q.end)}), except a safety alert. At most ${st.daily_cap} a day.`}>
      <Row icon="notifications" color={colors.lime} title="Push notifications" value={on ? 'On' : 'Off'}
        right={<Switch accessibilityLabel="Push notifications" disabled={busy} value={on} trackColor={{ false: colors.cardHi, true: colors.lime }} thumbColor="#fff" onValueChange={toggle} />} />
      {on && (Object.keys(st.category_labels) as PushCategory[]).map((k) => (
        <Row key={k} divider icon="ellipse" color={colors.dim} title={st.category_labels[k]}
          right={<Switch accessibilityLabel={st.category_labels[k]} disabled={k === 'safety'} value={st.categories[k]} trackColor={{ false: colors.cardHi, true: colors.lime }} thumbColor="#fff" onValueChange={(v) => cat(k, v)} />} />
      ))}
      {on && <Row divider icon="moon" color={colors.violet} title="Quiet hours" value={`${hh(q.start)} to ${hh(q.end)}${q.now ? ' (now)' : ''}`} />}
      {st.registered && <Row divider icon="paper-plane" color={colors.cyan} title="Send a test notification" onPress={busy ? undefined : test} />}
    </Group>
  );
}
