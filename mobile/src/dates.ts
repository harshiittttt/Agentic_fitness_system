// Pure date + bucketing helpers for the charts. No React Native imports, so they can be tested under plain Node.
// Deliberately avoids Intl so output is identical on every device.

export const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
export const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

export type Activity = { date: string; sets: number; tonnage: number };

export const isIso = (s: string) => /^\d{4}-\d{2}-\d{2}$/.test(s);
export const shortDate = (s: string) => (isIso(s) ? `${MONTHS[+s.slice(5, 7) - 1]} ${+s.slice(8, 10)}` : s);
export const parseIso = (s: string) => new Date(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10));
export const isoOf = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

/** The Monday of the week containing d, at local midnight. */
export function mondayOf(d: Date) {
  const x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  x.setDate(x.getDate() - ((x.getDay() + 6) % 7));
  return x;
}

/** Weekly buckets for the last `weeks` weeks (oldest first, current week last). `now` is injectable for tests. */
export function weeklyBuckets(activity: Activity[], weeks = 12, now: Date = new Date()) {
  const thisMonday = mondayOf(now);
  const buckets = Array.from({ length: weeks }, (_, i) => {
    const d = new Date(thisMonday.getFullYear(), thisMonday.getMonth(), thisMonday.getDate() - (weeks - 1 - i) * 7);
    return { start: isoOf(d), sessions: 0, tonnage: 0 };
  });
  const index = new Map(buckets.map((b, i) => [b.start, i]));
  for (const a of activity) {
    if (!isIso(a.date)) continue;
    const i = index.get(isoOf(mondayOf(parseIso(a.date))));
    if (i !== undefined) {
      buckets[i].sessions += 1;
      buckets[i].tonnage += a.tonnage;
    }
  }
  return buckets;
}

/** Consecutive weeks (ending now) with 2+ sessions. The current week is not held against you until it ends. */
export function weekStreak(buckets: { sessions: number }[]) {
  let streak = 0;
  for (let i = buckets.length - 1; i >= 0; i--) {
    if (buckets[i].sessions >= 2) streak++;
    else if (i === buckets.length - 1) continue;
    else break;
  }
  return streak;
}
