import { useEffect, useRef, useState } from 'react';
import { getRecognitionLang } from './voiceSettings';
import { acquireMic, micOwner, releaseMic } from './micLock';
import { dlog } from './diag';

/** On-device speech-to-text. Falls back gracefully (typed input) if the native
 *  module is unavailable, e.g. in plain Expo Go. */
let SR: any = null;
try { SR = require('expo-speech-recognition'); } catch { SR = null; }

/** Short, human message for a recognizer error code (never empty). */
export function voiceErrorMessage(code?: string, message?: string): string {
  const c = String(code || '').toLowerCase();
  const m = String(message || '').toLowerCase();
  if (c === 'not-allowed' || c === 'service-not-allowed' || m.includes('permission')) return 'Microphone permission is off. Allow it in Settings.';
  if (c === 'no-speech' || c === 'speech-timeout') return "Didn't hear anything. Tap the mic and try again.";
  if (c === 'busy' || m.includes('busy')) return 'The microphone is busy. Try again in a moment.';
  if (c === 'network' || c === 'offline' || m.includes('network') || m.includes('offline')) return 'Speech recognition needs a connection or an offline language pack.';
  if (c === 'language-not-supported' || m.includes('language')) return "This phone can't recognise that language for speech.";
  if (c === 'audio-capture') return "Couldn't use the microphone.";
  if (c === 'aborted') return '';
  return "Couldn't hear that. Try again or type it.";
}

export function useVoice(onFinal: (text: string) => void, onError?: (msg: string) => void) {
  const [listening, setListening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const available = !!SR?.ExpoSpeechRecognitionModule;
  // Native recognizer events are global (the always-on "Hey coach" listener emits them too), so this hook only
  // reacts while IT holds the mic lock. It takes the lock from the wake listener and gives it back on end/error.
  const mine = useRef(false);   // our session is running: react to events
  const held = useRef(false);   // we hold the lock (set slightly before `mine`)
  const gotText = useRef(false);
  const retried = useRef(false);
  const failsafe = useRef<any>(null);
  const cb = useRef({ onFinal, onError });
  cb.current = { onFinal, onError };

  const clearFailsafe = () => { if (failsafe.current) { clearTimeout(failsafe.current); failsafe.current = null; } };
  const done = () => { clearFailsafe(); mine.current = false; if (held.current) { held.current = false; releaseMic('ui'); } setListening(false); };
  const fail = (msg: string) => { dlog('voice_fail', msg); if (!msg) return; setError(msg); try { cb.current.onError?.(msg); } catch { /* ignore */ } };
  useEffect(() => () => { clearFailsafe(); mine.current = false; if (held.current) { held.current = false; releaseMic('ui'); } }, []);

  const begin = () => {
    gotText.current = false;
    mine.current = true;
    dlog('voice_begin', { lang: getRecognitionLang() });
    SR.ExpoSpeechRecognitionModule.start({ lang: getRecognitionLang(), interimResults: false });
    // never stay 'listening' forever if the native side goes silent
    clearFailsafe();
    failsafe.current = setTimeout(() => { if (mine.current) { try { SR.ExpoSpeechRecognitionModule.abort?.(); } catch { /* */ } done(); if (!gotText.current) fail("Didn't hear anything. Tap the mic and try again."); } }, 20000);
  };

  if (available) {
    SR.useSpeechRecognitionEvent?.('result', (e: any) => {
      if (mine.current && e.isFinal && e.results?.[0]?.transcript) { dlog('voice_final', String(e.results[0].transcript).slice(0, 60)); gotText.current = true; cb.current.onFinal(e.results[0].transcript); }
    });
    SR.useSpeechRecognitionEvent?.('end', () => {
      if (!mine.current) return;
      const heard = gotText.current;
      done();
      if (!heard) fail("Didn't hear anything. Tap the mic and try again.");
    });
    SR.useSpeechRecognitionEvent?.('error', (e: any) => {
      if (!mine.current) return;
      dlog('voice_error', { code: e?.error, msg: String(e?.message ?? '').slice(0, 80) });
      const busy = String(e?.error || '').toLowerCase() === 'busy' || String(e?.message || '').toLowerCase().includes('busy');
      if (busy && !retried.current) {   // the wake listener may still be winding down: wait and try once more
        retried.current = true;
        mine.current = false;
        setTimeout(() => { try { begin(); } catch { done(); fail(voiceErrorMessage('busy')); } }, 900);
        return;
      }
      done();
      fail(voiceErrorMessage(e?.error, e?.message));
    });
  }

  const start = async () => {
    setError(null);
    if (!available) { fail('Voice input needs the full app build, not Expo Go. Type it instead.'); return false; }
    try {
      const perm = await SR.ExpoSpeechRecognitionModule.requestPermissionsAsync();
      if (!perm.granted) { fail(voiceErrorMessage('not-allowed')); return false; }
      retried.current = false;
      const evicting = micOwner() === 'wake';
      if (!held.current) { held.current = true; acquireMic('ui'); }
      setListening(true);
      if (evicting) {
        // let the wake listener's session wind down first; its late 'end' event is ignored because `mine` is still false
        try { SR.ExpoSpeechRecognitionModule.abort?.(); } catch { /* nothing running */ }
        await new Promise((r) => setTimeout(r, 700));
      }
      begin();
      return true;
    } catch (e: any) {
      done();
      fail(voiceErrorMessage(e?.code || e?.error, e?.message));
      return false;
    }
  };
  const stop = () => { try { SR?.ExpoSpeechRecognitionModule?.stop(); } catch { done(); } };

  return { listening, available, start, stop, error, clearError: () => setError(null) };
}
