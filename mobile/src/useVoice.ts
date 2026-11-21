import { useEffect, useRef, useState } from 'react';
import { getRecognitionLang } from './voiceSettings';
import { acquireMic, micOwner, releaseMic } from './micLock';

/** On-device speech-to-text. Falls back gracefully (typed input) if the native
 *  module is unavailable, e.g. in plain Expo Go. */
let SR: any = null;
try { SR = require('expo-speech-recognition'); } catch { SR = null; }

export function useVoice(onFinal: (text: string) => void) {
  const [listening, setListening] = useState(false);
  const available = !!SR?.ExpoSpeechRecognitionModule;
  // Native recognizer events are global (the always-on "Hey coach" listener emits them too), so this hook only
  // reacts while IT holds the mic lock. It takes the lock from the wake listener and gives it back on end/error.
  const mine = useRef(false);   // our session is running: react to events
  const held = useRef(false);   // we hold the lock (set slightly before `mine`)
  const done = () => { mine.current = false; if (held.current) { held.current = false; releaseMic('ui'); } setListening(false); };
  useEffect(() => () => { mine.current = false; if (held.current) { held.current = false; releaseMic('ui'); } }, []);

  if (available) {
    SR.useSpeechRecognitionEvent?.('result', (e: any) => {
      if (mine.current && e.isFinal && e.results?.[0]?.transcript) onFinal(e.results[0].transcript);
    });
    SR.useSpeechRecognitionEvent?.('end', () => { if (mine.current) done(); });
    SR.useSpeechRecognitionEvent?.('error', () => { if (mine.current) done(); });
  }

  const start = async () => {
    if (!available) return false;
    const perm = await SR.ExpoSpeechRecognitionModule.requestPermissionsAsync();
    if (!perm.granted) return false;
    const evicting = micOwner() === 'wake';
    if (!held.current) { held.current = true; acquireMic('ui'); }
    setListening(true);
    if (evicting) {
      // let the wake listener's session wind down first; its late 'end' event is ignored because `mine` is still false
      try { SR.ExpoSpeechRecognitionModule.abort?.(); } catch { /* nothing running */ }
      await new Promise((r) => setTimeout(r, 350));
    }
    mine.current = true;
    SR.ExpoSpeechRecognitionModule.start({ lang: getRecognitionLang(), interimResults: false });
    return true;
  };
  const stop = () => SR?.ExpoSpeechRecognitionModule?.stop();

  return { listening, available, start, stop };
}
