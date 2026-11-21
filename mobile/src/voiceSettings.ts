// Which lines get the premium (ElevenLabs) voice. The free plan has a small monthly budget, so the default is
// "moments": openers, PRs, pain warnings, wrap-ups and open conversation use ElevenLabs; routine cues use the phone voice.
import AsyncStorage from '@react-native-async-storage/async-storage';

export type VoiceMode = 'all' | 'moments' | 'phone';
const KEY = 'voice_mode_v1';
let mode: VoiceMode = 'moments';

export const getVoiceMode = () => mode;

export async function loadVoiceSettings(): Promise<VoiceMode> {
  try {
    const v = await AsyncStorage.getItem(KEY);
    if (v === 'all' || v === 'moments' || v === 'phone') mode = v;
  } catch { /* keep default */ }
  return mode;
}

export async function setVoiceMode(m: VoiceMode): Promise<void> {
  mode = m;
  try { await AsyncStorage.setItem(KEY, m); } catch { /* in-memory value still applies this session */ }
}

// Speech-recognition language follows the coach: Pehalwan listens for Hindi (hi-IN), everyone else English.
let recogLang = 'en-US';
export const getRecognitionLang = () => recogLang;
export const setRecognitionLang = (lang?: string) => { recogLang = lang || 'en-US'; };

/** `moment` comes from the server: true for lines worth spending credits on. */
export const shouldUsePremium = (moment: boolean) => mode === 'all' || (mode === 'moments' && moment);
