// One switch for all vibration. Every screen imports this instead of expo-haptics, so turning it off really is off.
// Default is OFF; the Profile tab has the toggle. The choice is remembered on the phone.
import * as H from 'expo-haptics';
import AsyncStorage from '@react-native-async-storage/async-storage';

const KEY = 'haptics_enabled_v1';
let enabled = false;

export const hapticsEnabled = () => enabled;

/** Read the saved choice (call once at app start). Anything unreadable means "off". */
export async function loadHaptics(): Promise<boolean> {
  try { enabled = (await AsyncStorage.getItem(KEY)) === '1'; } catch { enabled = false; }
  return enabled;
}

export async function setHapticsEnabled(on: boolean): Promise<void> {
  enabled = on;
  try { await AsyncStorage.setItem(KEY, on ? '1' : '0'); } catch { /* the in-memory value still applies this session */ }
}

export const ImpactFeedbackStyle = H.ImpactFeedbackStyle;
export const NotificationFeedbackType = H.NotificationFeedbackType;

export const impactAsync = (style?: H.ImpactFeedbackStyle) => (enabled ? H.impactAsync(style) : Promise.resolve());
export const notificationAsync = (type?: H.NotificationFeedbackType) => (enabled ? H.notificationAsync(type) : Promise.resolve());
export const selectionAsync = () => (enabled ? H.selectionAsync() : Promise.resolve());
