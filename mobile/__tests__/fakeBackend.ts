// A small in-memory stand-in for the coach server, with the same response SHAPES as the real endpoints (checked against backend/main.py).
export function makeBackend(opts: { delayMs?: number; fragments?: string[]; failVoice?: boolean } = {}) {
  const calls: { path: string; method: string; body: any }[] = [];
  const names = ['bench press', 'lat pulldown', 'overhead press'];
  const ex = names.map((n) => ({ name: n, sets: 3, reps: 8, target_rpe: 8, done: 0, skipped: false, target: null }));
  const st = () => {
    const cur = ex.find((e) => !e.skipped && e.done < e.sets);
    return { day: 'Upper A', date: '2026-11-21', day_index: 1, split_len: 4, exercises: ex.map((e) => ({ ...e })),
      current: cur ? { name: cur.name, set_no: cur.done + 1, sets: cur.sets, reps: cur.reps, target_rpe: 8, target: null } : null,
      remaining_sets: ex.filter((e) => !e.skipped).reduce((a, e) => a + (e.sets - e.done), 0), finished: !cur };
  };
  const live = (intent: string, reply: string, extra: any = {}) => ({ intent, reply, saved: [], next_set: null, pr: false, safety_flag: null, actions: [], moment: false, state: st(), ...extra });
  const handlers: Record<string, (b: any, url: string) => any> = {
    'GET /health': () => ({ ok: true }),
    'GET /coach': () => ({ persona: 'mentor', coach_name: 'Sage', user_name: 'Golu', tts: { rate: 1, pitch: 1, lang: 'en-US' } }),
    'GET /live/state': () => st(),
    'POST /live/start': () => live('start', 'Morning, Golu. Today is Upper A with bench press first. Warm up well. No rush at all.', { moment: true }),
    'POST /live/log': (b) => { const e = ex.find((x) => x.name === (b.exercise ?? st().current?.name))!; e.done += 1;
      return live('set', `Nice ${b.weight} for ${b.reps}.`, { saved: [{ exercise: e.name, weight: b.weight, reps: b.reps }], next_set: { weight: b.weight + 2.5, reps: b.reps }, actions: [{ type: 'rest', seconds: 120 }] }); },
    'POST /live/say': (b) => {
      if (/skip/.test(b.transcript)) { const c = ex.find((x) => !x.skipped && x.done < x.sets); if (c) c.skipped = true; return live('skip', 'Skipped. Moving on.'); }
      return live('chat', `Heard: ${b.transcript}`);
    },
    'GET /plan': () => ({ today_index: 1, sessions_done: 0, days: [{ name: 'Lower A', est_minutes: 40, exercises: [] }, { name: 'Upper A', est_minutes: 45, exercises: ex.map((e) => ({ name: e.name, sets: e.sets, reps: e.reps, target_rpe: e.target_rpe })) }], nutrition: { calories: 2600 } }),
    'GET /insights/today': () => ({ has_plan: true, trained_today: false, sets_today: 0, tonnage_today: 0, week_done: false, days_since_last: null, next: { name: 'Upper A', lifts: 3, sets: 9, minutes: 45, exercises: names }, week: { sessions: 0, target: 4 } }),
    'GET /readiness': () => ({ readiness: { score: 80, label: 'good' }, safety: null }),
    'GET /profile': () => ({ profile: { name: 'Golu' } }),
    'GET /agents/decisions': () => [],
    'GET /agents/nudges': () => [],
    'GET /agents/status': () => ({ pending: [] }),
    // assumed shape of the server's un-skip (workstream A): POST /live/unskip {exercise?} -> a live reply with the new state
    'POST /live/unskip': (b) => {
      const sk = ex.filter((x) => x.skipped);
      const e = b.exercise ? sk.find((x) => x.name === b.exercise) : sk[sk.length - 1];
      if (!e) return live('unskip', 'Nothing was skipped.');
      e.skipped = false; return live('unskip', `Put ${e.name} back.`);
    },
    'POST /live/cue': () => live('cue', 'Rest is over. Next set.'),
    'POST /voice/command': (b) => {
      if (opts.fragments?.includes(b.transcript)) return { intent: 'fragment', reply: '', actions: [], expects: 'answer', moment: false, pr: false, safety_flag: null, state: null, saved: [] };
      const m = /(\d+(?:\.\d+)?)\s*(?:kg\s*)?for\s*(\d+)/.exec(b.transcript);
      if (b.screen === 'live' && m) {
        const e = ex.find((x) => !x.skipped && x.done < x.sets)!; e.done += 1;
        return live('set', `Logged ${m[1]} for ${m[2]}.`, { saved: [{ exercise: e.name, weight: +m[1], reps: +m[2] }], actions: [{ type: 'rest', seconds: 90 }] });
      }
      return { intent: 'router', reply: `You said ${b.transcript}.`, actions: [], expects: null, moment: false, pr: false, safety_flag: null, state: null, saved: [] };
    },
    'GET /exercise/prefill': () => ({ last: null, suggest: null, increment: 2.5 }),
    'POST /diag/log': () => ({ ok: true }),
  };
  const fetchFn = jest.fn(async (url: any, init: any = {}) => {
    const u = String(url);
    const path = '/' + u.replace(/^https?:\/\/[^/]+\//, '').split('?')[0];
    const method = (init.method || 'GET').toUpperCase();
    const body = init.body ? JSON.parse(init.body) : undefined;
    calls.push({ path, method, body });
    if (opts.delayMs && path === '/voice/command') await new Promise((r) => setTimeout(r, opts.delayMs));
    if (opts.failVoice && path === '/voice/command') return { ok: false, status: 500, json: async () => ({ detail: 'boom' }) } as any;
    const h = handlers[`${method} ${path}`];
    if (!h) return { ok: false, status: 404, json: async () => ({ detail: `no fake for ${method} ${path}` }) } as any;
    const data = h(body, u);
    return { ok: true, status: 200, json: async () => data } as any;
  });
  return { fetch: fetchFn, calls, ex, count: (p: string) => calls.filter((c) => c.path === p).length };
}
