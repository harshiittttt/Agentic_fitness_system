export const colors = {
  bg: '#07080B',
  bg2: '#0D0F14',
  card: '#12151C',
  cardHi: '#1A1E28',
  border: 'rgba(255,255,255,0.07)',
  text: '#F5F7FA',
  dim: '#A9B2C4',
  faint: '#7F8A9F',
  lime: '#C6FF3D',
  mint: '#34E3A4',
  cyan: '#38D3FF',
  violet: '#8B6CFF',
  pink: '#FF5C9E',
  amber: '#FFB84D',
  red: '#FF5A5F',
};

export const gradients = {
  hero: ['#2B5CFF', '#7A3DFF'] as const,
  lime: ['#C6FF3D', '#34E3A4'] as const,
  warm: ['#FF8A3D', '#FF3D7F'] as const,
  card: ['#161A24', '#10131A'] as const,
};

export const agentStyle: Record<string, { color: string; icon: string }> = {
  'Progress Analyst': { color: colors.cyan, icon: 'stats-chart' },
  Training: { color: colors.lime, icon: 'barbell' },
  Recovery: { color: colors.violet, icon: 'moon' },
  Nutrition: { color: colors.amber, icon: 'nutrition' },
};

export const radius = { sm: 12, md: 18, lg: 26, xl: 34 };
export const font = {
  regular: 'Inter_400Regular',
  medium: 'Inter_500Medium',
  semibold: 'Inter_600SemiBold',
  bold: 'Inter_700Bold',
  black: 'Inter_800ExtraBold',
};

/** The team, by the ids the backend uses in the decision ledger. One place so every screen gives each agent the same face. */
export const TEAM: Record<string, { label: string; role: string; color: string; icon: string }> = {
  training: { label: 'Training', role: 'Sessions & progression', color: colors.lime, icon: 'barbell' },
  nutrition: { label: 'Nutrition', role: 'Calories & macros', color: colors.amber, icon: 'nutrition' },
  recovery: { label: 'Recovery', role: 'Readiness & rest', color: colors.violet, icon: 'moon' },
  progress: { label: 'Progress', role: 'Trends & plateaus', color: colors.cyan, icon: 'stats-chart' },
  sleep: { label: 'Sleep & Stress', role: 'Sleep debt & wind-down', color: colors.violet, icon: 'bed' },
  injury: { label: 'Injury & Rehab', role: 'Pain triage & substitutions', color: colors.pink, icon: 'medkit' },
  adherence: { label: 'Adherence', role: 'Habits & nudges', color: colors.cyan, icon: 'chatbubble-ellipses' },
  moderator: { label: 'Moderator', role: 'Runs the roundtable', color: colors.mint, icon: 'git-compare' },
  orchestrator: { label: 'Head coach', role: 'Reconciles the team', color: colors.mint, icon: 'git-compare' },
  safety_critic: { label: 'Safety critic', role: 'Veto power', color: colors.red, icon: 'shield-checkmark' },
  watchdog: { label: 'Watchdog', role: 'Always on', color: colors.red, icon: 'eye' },
  you: { label: 'You', role: '', color: colors.text, icon: 'person' },
};
