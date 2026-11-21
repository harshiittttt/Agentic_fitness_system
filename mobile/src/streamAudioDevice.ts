/** The phone's audio for live voice: microphone with Android's echo canceller + noise suppressor, and a speaker that plays raw PCM.
 *  Needs the native module (installed app build); returns null in Expo Go / tests, and the app then uses its built-in recogniser instead. */
import type { PcmDevice } from './streamVoice';

let mod: any;
function native(): any {
  if (mod !== undefined) return mod;
  try { mod = require('@speechmatics/expo-two-way-audio'); } catch { mod = null; }
  return mod;
}

export const streamAudioAvailable = (): boolean => !!native();

export function makeDevice(): PcmDevice | null {
  const m = native();
  if (!m) return null;
  let sub: any = null;
  return {
    async start(onMic) {
      const p = await m.requestMicrophonePermissionsAsync();
      if (p && p.granted === false) throw new Error('microphone permission is off');
      const ok = await m.initialize();
      if (ok === false) throw new Error('audio engine did not start');
      sub = m.addExpoTwoWayAudioEventListener('onMicrophoneData', (e: { data: Uint8Array }) => onMic(e.data instanceof Uint8Array ? e.data : new Uint8Array(e.data as any)));
      m.toggleRecording(true);
    },
    stop() {
      try { sub?.remove?.(); } catch { /* ignore */ }
      sub = null;
      try { m.toggleRecording(false); } catch { /* ignore */ }
      try { m.tearDown(); } catch { /* ignore */ }       // gives the phone's normal audio mode and focus back
    },
    play(pcm) { m.playPCMData(pcm); },
  };
}
