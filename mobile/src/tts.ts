import * as Speech from 'expo-speech';
import { Audio, AVPlaybackStatus } from 'expo-av';
import * as FileSystem from 'expo-file-system';
import { API } from './api';
import type { Tts } from './personas';

export type SpeakOpts = {
  /** Coach persona id, so the right ElevenLabs voice is used. */
  persona?: string;
  /** Try the premium (ElevenLabs) voice first; any failure quietly falls back to the phone voice. */
  premium?: boolean;
  /** Specialist agent id (training, nutrition...): the backend speaks it with that agent's own voice settings. */
  agent?: string;
};

const PREMIUM_TIMEOUT_MS = 9000;
const voiceByLang: Record<string, string | undefined> = {};
const resolvedLang: Record<string, boolean> = {};
let token = 0;                                   // each speak() call supersedes the previous one
let current: Audio.Sound | null = null;
let activeFinish: (() => void) | null = null;
let audioModeSet = false;

/** Prefer the best-sounding English voice the phone offers (enhanced / network / neural). */
async function pickVoice(lang = 'en-US'): Promise<string | undefined> {
  if (resolvedLang[lang]) return voiceByLang[lang];
  resolvedLang[lang] = true;
  const prefix = lang.slice(0, 2).toLowerCase();
  try {
    const voices = await Speech.getAvailableVoicesAsync();
    const score = (v: any) =>
      (v.quality === 'Enhanced' ? 3 : 0) +
      (/network|neural|enhanced|premium|natural/i.test(`${v.identifier} ${v.name}`) ? 2 : 0) +
      (v.language?.replace('_', '-').toLowerCase() === lang.toLowerCase() ? 1 : 0);
    voiceByLang[lang] = voices
      .filter((v: any) => v.language?.toLowerCase().startsWith(prefix))
      .sort((a: any, b: any) => score(b) - score(a))[0]?.identifier;
  } catch {
    /* fall back to the system default voice */
  }
  return voiceByLang[lang];
}

/** Does this phone have a text-to-speech voice for the language? (Hindi often needs a one-time download.) */
export async function hasVoiceFor(lang: string): Promise<boolean> {
  try {
    const prefix = lang.slice(0, 2).toLowerCase();
    return (await Speech.getAvailableVoicesAsync()).some((v: any) => v.language?.toLowerCase().startsWith(prefix));
  } catch { return true; /* can't tell: don't scare the user */ }
}

function hash(s: string) {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  return (h >>> 0).toString(36);
}

/** Download (or reuse the phone-cached copy of) the line from the backend and play it. Returns false if it could not. */
async function playPremium(text: string, persona: string, mine: number, finish: () => void, agent = ''): Promise<boolean> {
  try {
    if (!audioModeSet) {
      await Audio.setAudioModeAsync({ playsInSilentModeIOS: true, shouldDuckAndroid: true, staysActiveInBackground: false });
      audioModeSet = true;
    }
    const file = `${FileSystem.cacheDirectory}coach_${persona}${agent ? '_' + agent : ''}_${hash(text)}.mp3`;
    const info = await FileSystem.getInfoAsync(file);
    if (!info.exists) {
      const url = `${API}/tts?persona=${encodeURIComponent(persona)}${agent ? `&agent=${encodeURIComponent(agent)}` : ''}&text=${encodeURIComponent(text)}`;
      const got = await Promise.race([
        FileSystem.downloadAsync(url, file),
        new Promise<null>((r) => setTimeout(() => r(null), PREMIUM_TIMEOUT_MS)),
      ]);
      if (!got || got.status !== 200) {            // 503 = backend says "use the phone voice"; the file then holds an error body
        await FileSystem.deleteAsync(file, { idempotent: true });
        return false;
      }
    }
    if (mine !== token) { finish(); return true; }  // superseded while downloading: stay silent, the newer line wins
    const { sound } = await Audio.Sound.createAsync({ uri: file }, { shouldPlay: true });
    current = sound;
    sound.setOnPlaybackStatusUpdate((st: AVPlaybackStatus) => {
      if (st.isLoaded && st.didJustFinish) {
        sound.unloadAsync().catch(() => {});
        if (current === sound) current = null;
        finish();
      }
    });
    return true;
  } catch {
    return false;
  }
}

/** Speak a line. onDone fires exactly once: when speech finishes, is interrupted, or fails. */
export async function speak(text: string, tts: Tts, onDone?: () => void, opts: SpeakOpts = {}) {
  const mine = ++token;
  const prev = activeFinish;
  activeFinish = null;
  await stopCurrent();
  prev?.();                                    // the interrupted line's onDone must still fire once
  let finished = false;
  const finish = () => {
    if (!finished) { finished = true; if (activeFinish === finish) activeFinish = null; onDone?.(); }
  };
  if (!text) { finish(); return; }
  activeFinish = finish;

  if (opts.premium && (await playPremium(text, opts.persona ?? 'hype', mine, finish, opts.agent))) return;
  if (mine !== token) { finish(); return; }

  const voice = await pickVoice(tts.lang);
  if (mine !== token) { finish(); return; }
  Speech.speak(text, { voice, language: tts.lang, rate: tts.rate, pitch: tts.pitch, onDone: finish, onStopped: finish, onError: finish });
}

async function stopCurrent() {
  Speech.stop();
  const s = current;
  current = null;
  if (s) { try { await s.stopAsync(); await s.unloadAsync(); } catch { /* already gone */ } }
}

export const stopSpeaking = () => {
  token++;
  const fin = activeFinish;
  stopCurrent().finally(() => fin?.());
};
