// Which screen the user is looking at, for "Hey coach" commands ("log this set" means something different on Live than on Fuel).
// App.tsx sets the tab; a full-screen (Live, push-to-talk, etc.) may call setVoiceOverlay('live') on mount and setVoiceOverlay(null) on unmount.
// Plain module state, no React, so WakeCoach can read it at the moment a command is sent.
export type VoiceScreen = 'today' | 'coach' | 'nutrition' | 'insights' | 'setup' | 'live' | 'unknown';
let tab: VoiceScreen = 'unknown';
let overlay: VoiceScreen | null = null;
let day: string | null = null;
export const setVoiceTab = (t: VoiceScreen) => { tab = t; };
export const setVoiceOverlay = (o: VoiceScreen | null) => { overlay = o; };
/** Fuel (Nutrition) reports the day it is showing, YYYY-MM-DD, or null. */
export const setVoiceDay = (d: string | null) => { day = d; };
export const getVoiceScreen = (): VoiceScreen => overlay ?? tab;
export const getVoiceDay = (): string | null => day;
