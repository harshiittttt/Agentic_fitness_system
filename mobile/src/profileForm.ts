// Pure helpers for the Edit Profile sheet (no React Native imports, so they are testable under plain Node).

export type Form = {
  sex: string; age: number; height_cm: number; weight_kg: number; goal: string; activity: string;
  days_per_week: number; experience: string; injuries: string[];
};

export const INJURIES = ['shoulder', 'lower back', 'knee', 'wrist', 'elbow', 'neck', 'hip'];
export const TRAINING_KEYS = ['days_per_week', 'experience', 'injuries'];

/** Fill in defaults for fields an older/demo profile may lack, so "missing" is never mistaken for "changed". */
export const toForm = (p: any): Form => ({
  sex: p.sex ?? 'm', age: p.age ?? 28, height_cm: p.height_cm ?? 175, weight_kg: p.weight_kg ?? 75, goal: p.goal ?? 'maintain',
  activity: p.activity ?? 'moderate', days_per_week: p.days_per_week ?? 4, experience: p.experience ?? 'intermediate', injuries: [...(p.injuries ?? [])],
});

/** Only the fields that actually changed (injury order doesn't count as a change). */
export function diff(form: Form, base: Form) {
  const out: any = {};
  (Object.keys(form) as (keyof Form)[]).forEach((k) => {
    const a = form[k], b = base[k];
    if (k === 'injuries') {
      if (JSON.stringify([...(a as string[])].sort()) !== JSON.stringify([...(b as string[])].sort())) out[k] = a;
    } else if (a !== b) out[k] = a;
  });
  return out;
}

export const hasInj = (list: string[], k: string) => list.some((i) => i.toLowerCase().includes(k));
export const toggleInj = (list: string[], k: string) => (hasInj(list, k) ? list.filter((i) => !i.toLowerCase().includes(k)) : [...list, k]);
export const customInjuries = (list: string[]) => list.filter((i) => !INJURIES.some((k) => i.toLowerCase().includes(k)));
