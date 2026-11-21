import { colors } from './theme';

export type PersonaId = 'hype' | 'mentor' | 'straight' | 'haryanvi' | 'roast';
/** rate/pitch shape the voice; lang picks the phone's text-to-speech voice AND the speech-recognition language. */
export type Tts = { rate: number; pitch: number; lang?: string };

// Mirrors backend/coach/persona.py. Kept on-device so the picker can speak a sample instantly, even offline.
export const PERSONAS: { id: PersonaId; name: string; short: string; blurb: string; icon: string; color: string; tts: Tts; sample: string }[] = [
  {
    id: 'hype', name: 'Max', short: 'Hype', blurb: 'High energy, big reactions, always in your corner.', icon: 'flash', color: colors.lime,
    tts: { rate: 1.05, pitch: 1.05, lang: 'en-US' },
    sample: "Hey, I'm Max! I'll be in your ear the whole session, cheering you on and keeping you honest.",
  },
  {
    id: 'mentor', name: 'Sage', short: 'Mentor', blurb: 'Warm, steady and wise. Explains the why, keeps you calm.', icon: 'leaf', color: colors.cyan,
    tts: { rate: 0.95, pitch: 0.95, lang: 'en-US' },
    sample: "Hi, I'm Sage. I'll be with you through the session, one set at a time.",
  },
  {
    id: 'straight', name: 'Ash', short: 'Straight talker', blurb: 'Dry humour, no fluff, tough love when you need it.', icon: 'skull', color: colors.pink,
    tts: { rate: 1.0, pitch: 0.9, lang: 'en-US' },
    sample: "I'm Ash. I'll tell you what to lift and when. Try to keep up.",
  },
  {
    id: 'haryanvi', name: 'Pehalwan', short: 'Haryanvi', blurb: 'Desi akhada coach. Bolta hai Haryanvi, hausla badhata hai. (हरियाणवी)', icon: 'ribbon', color: colors.amber,
    tts: { rate: 0.95, pitch: 0.9, lang: 'hi-IN' },
    sample: 'राम राम भाई! मैं सूं तेरा पहलवान कोच। पूरे सेशन में तेरे साथ रहूंगा, अर कमी ना छोड़ूंगा।',
  },
  {
    id: 'roast', name: 'Ustaad', short: 'Hindi roaster', blurb: 'Rude, sarcastic Hindi roaster who acts like he could not care less. Roasts your excuses, never you. (हिंदी)', icon: 'flame', color: colors.red,
    tts: { rate: 1.0, pitch: 0.85, lang: 'hi-IN' },
    sample: 'सुन, मैं तेरा उस्ताद हूँ। मुझे तेरी कोई परवाह नहीं है। बस सेट कर और बहाने बंद रख।',
  },
];

export const personaById = (id?: string) => PERSONAS.find((p) => p.id === id) ?? PERSONAS[0];
