import { useEffect, useState } from 'react';

// The coach can change the plan from chat. Screens that show the plan subscribe here and reload when it moves.
let epoch = 0;
const listeners = new Set<(n: number) => void>();

export const bumpPlan = () => { epoch += 1; listeners.forEach((l) => l(epoch)); };

export function usePlanEpoch(): number {
  const [n, setN] = useState(epoch);
  useEffect(() => { listeners.add(setN); setN(epoch); return () => { listeners.delete(setN); }; }, []);
  return n;
}
