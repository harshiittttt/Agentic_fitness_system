/** The native module in modules/pcm-audio (Android). null in Expo Go, on web and in tests (they replace this file). */
let cached: any;
export function pcmNative(): any {
  if (cached !== undefined) return cached;
  try { cached = require('expo-modules-core').requireNativeModule('PcmAudio'); } catch { cached = null; }
  return cached;
}
