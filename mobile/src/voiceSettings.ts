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
  await loadRecognitionLang();
  return mode;
}

export async function setVoiceMode(m: VoiceMode): Promise<void> {
  mode = m;
  try { await AsyncStorage.setItem(KEY, m); } catch { /* in-memory value still applies this session */ }
}

// Speech-recognition language is what YOU speak, not what the coach speaks. Default Indian English (en-IN): it handles an Indian accent and English
// gym words ("bench", "reps"). The coach's own voice can still be Hindi. Switch to Hindi in Profile > Talking to your coach if you speak Hindi.
// (Before 0.3.4 a Hindi-speaking coach forced hi-IN, which turned English speech into garbled Devanagari.)
export type RecogLang = 'en-IN' | 'hi-IN';
const LANG_KEY = 'recog_lang_v1';
let recogLang: RecogLang = 'en-IN';
export const getRecognitionLang = () => recogLang;
/** The coach's own language no longer decides what we listen for; kept so existing callers need no change. */
export const setRecognitionLang = (_coachLang?: string) => { /* intentionally ignored */ };
export async function loadRecognitionLang(): Promise<RecogLang> {
  try { const v = await AsyncStorage.getItem(LANG_KEY); if (v === 'en-IN' || v === 'hi-IN') recogLang = v; } catch { /* keep default */ }
  return recogLang;
}
export async function chooseRecognitionLang(l: RecogLang): Promise<void> {
  recogLang = l;
  try { await AsyncStorage.setItem(LANG_KEY, l); } catch { /* applies for this session */ }
}

/** `moment` comes from the server: true for lines worth spending credits on. */
export const shouldUsePremium = (moment: boolean) => mode === 'all' || (mode === 'moments' && moment);
