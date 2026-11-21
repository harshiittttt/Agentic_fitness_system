/** The phone's audio for live voice: microphone (echo canceller + noise suppressor when the phone has them) and a speaker for raw PCM.
 *  Needs the native module (installed app build); returns null in Expo Go / tests, and the app then uses its built-in recogniser instead. */
import type { PcmDevice } from './streamVoice';
import { b64ToBytes, bytesToB64 } from './b64';
import { pcmNative } from './pcmNative';

export const streamAudioAvailable = (): boolean => !!pcmNative();

export function makeDevice(): PcmDevice | null {
  const m = pcmNative();
  if (!m) return null;
  let micSub: any = null;
  let errSub: any = null;
  let lastNativeError = '';
  return {
    async start(onMic) {
      // Only ask when it is not granted yet: asking opens Android's permission screen for a moment even when it IS granted, which took the app out of the
      // foreground and made it end the voice session (found on the real phone: logcat showed GrantPermissionsActivity at every tap).
      let p = await m.getPermission();
      if (!p || p.granted !== true) p = await m.requestPermission();
      if (!p || p.granted !== true) throw new Error('microphone permission is off');
      micSub = m.addListener('mic', (e: { b64?: string }) => { if (e && e.b64) onMic(b64ToBytes(e.b64)); });
      errSub = m.addListener('error', (e: { message?: string }) => { lastNativeError = String(e?.message ?? ''); });
      const why: string = m.start();
      if (why) { try { micSub?.remove?.(); errSub?.remove?.(); } catch { /* ignore */ } micSub = errSub = null; throw new Error(why); }
    },
    stop() {
      try { micSub?.remove?.(); errSub?.remove?.(); } catch { /* ignore */ }
      micSub = errSub = null;
      try { m.stop(); } catch { /* ignore */ }          // gives the phone's normal audio mode and focus back
    },
    play(pcm) { m.play(bytesToB64(pcm)); },
    flush() { try { m.flush(); } catch { /* ignore */ } },
    status() { try { return `${String(m.status())}${lastNativeError ? `;event_error=${lastNativeError}` : ''}`; } catch (e: any) { return `status failed: ${e?.message ?? e}`; } },
  };
}
