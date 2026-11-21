import { Platform } from 'react-native';
import Constants from 'expo-constants';
import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';
import { API, apiError } from './api';

/** Push registration + backend calls. Everything degrades to a plain message; nothing here may crash the app. */

export type PushCategory = 'nudge' | 'approval' | 'report' | 'safety';
export type PushStatus = {
  enabled: boolean; registered: boolean; categories: Record<PushCategory, boolean>; category_labels: Record<PushCategory, string>;
  quiet_hours: { start: number; end: number; now: boolean }; daily_cap: number; sent_last_24h: number;
};
export type EnableResult = { ok: boolean; reason?: 'simulator' | 'denied' | 'no_project' | 'expo_go' | 'error'; message?: string };

export const isExpoGo = Constants.executionEnvironment === 'storeClient';
const projectId = (): string | undefined => (Constants.expoConfig as any)?.extra?.eas?.projectId ?? (Constants as any).easConfig?.projectId;

let configured = false;
/** Show notifications while the app is open, and create the Android channel. Safe to call twice. */
export function configurePush() {
  if (configured) return;
  configured = true;
  try {
    Notifications.setNotificationHandler({
      handleNotification: async () => ({ shouldShowAlert: true, shouldPlaySound: false, shouldSetBadge: false }),
    });
    if (Platform.OS === 'android') Notifications.setNotificationChannelAsync('default', { name: 'Coach', importance: Notifications.AndroidImportance.DEFAULT }).catch(() => {});
  } catch { /* module unavailable (e.g. web) */ }
}

/** Ask permission, fetch the Expo push token and register it with the backend. */
export async function enablePush(): Promise<EnableResult> {
  try {
    configurePush();
    if (Platform.OS === 'web') return { ok: false, reason: 'error', message: 'Push is not available on web.' };
    if (!Device.isDevice) return { ok: false, reason: 'simulator', message: 'Push needs a real phone. Simulators cannot receive it.' };
    let { status } = await Notifications.getPermissionsAsync();
    if (status !== 'granted') status = (await Notifications.requestPermissionsAsync()).status;
    if (status !== 'granted') return { ok: false, reason: 'denied', message: 'Notifications are off for this app. Turn them on in your phone settings, then try again.' };
    const pid = projectId();
    if (!pid) return { ok: false, reason: 'no_project', message: 'Push needs a development build with an EAS project id. It cannot work in this setup yet.' };
    const tok = (await Notifications.getExpoPushTokenAsync({ projectId: pid })).data;
    const r = await fetch(`${API}/push/register`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: tok, platform: Platform.OS }) });
    if (!r.ok) throw await apiError(r, 'push');
    return { ok: true };
  } catch (e: any) {
    const expoGo = isExpoGo ? ' Expo Go does not support push; a development build is needed.' : '';
    return { ok: false, reason: isExpoGo ? 'expo_go' : 'error', message: `Could not turn on push.${expoGo} ${String(e?.message ?? '')}`.trim() };
  }
}

export const disablePush = () => fetch(`${API}/push/register`, { method: 'DELETE' }).then((r) => r.ok).catch(() => false);
export const getPushStatus = (): Promise<PushStatus> => fetch(`${API}/push/settings`).then(async (r) => { if (!r.ok) throw await apiError(r, 'push'); return r.json(); });
export const setPushPrefs = (body: { enabled?: boolean; categories?: Partial<Record<PushCategory, boolean>> }): Promise<PushStatus> =>
  fetch(`${API}/push/settings`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(async (r) => { if (!r.ok) throw await apiError(r, 'push'); return r.json(); });
export const sendTestPush = (): Promise<{ sent: boolean; error?: string }> =>
  fetch(`${API}/push/test`, { method: 'POST' }).then(async (r) => { if (!r.ok) throw await apiError(r, 'push'); return r.json(); });

/** Calls `onScreen(screen)` when the athlete taps a notification (also when it launched the app). Returns an unsubscribe. */
export function onPushTap(onScreen: (screen: string, data: Record<string, any>) => void): () => void {
  try {
    const handle = (resp: Notifications.NotificationResponse | null) => {
      const d = (resp?.notification?.request?.content?.data ?? {}) as Record<string, any>;
      if (d.screen) onScreen(String(d.screen), d);
    };
    Notifications.getLastNotificationResponseAsync().then(handle).catch(() => {});
    const sub = Notifications.addNotificationResponseReceivedListener(handle);
    return () => sub.remove();
  } catch { return () => {}; }
}
