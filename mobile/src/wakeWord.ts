// "Hey coach" wake-word matching. Pure functions only (no React / native imports) so it runs under plain Node:
//   node scripts/test_wakeword.js
//
// Speech recognizers give us TEXT, not audio, so "wake word" here means: find a wake phrase in a transcript.
// Rules:
//  - phrase = greeting (hey / hi / ok / okay / hello / हे / हेलो ...) + target (coach / couch / persona name / कोच ...)
//  - it must START the utterance, or follow punctuation (the recognizer's pause marker). A phrase merely mentioned
//    inside a sentence ("my coach said hey coach is lazy") does not fire.
//  - "a coach" (a common mishearing of "hey coach") is only accepted at the very start of the utterance.
//  - the text after the phrase is the command ("hey coach, bench 80 for 8" -> "bench 80 for 8").
//  - the tracker makes sure repeated partial results of one utterance trigger only once.

export type WakeMatch = { hit: boolean; command: string };

const GREET = new Set(['hey', 'hay', 'hi', 'hii', 'ok', 'okay', 'o.k', 'hello', 'hallo', 'yo', 'he', 'hei', 'हे', 'हेलो', 'हैलो', 'हेल्लो', 'ओके', 'हाय', 'हेए']);
// Always a valid target after a greeting (mishearings of "coach").
const TARGETS = ['coach', 'couch', 'coaches', 'coatch', 'koach', 'cooch', 'coch', 'कोच', 'कोचे'];
const WORD = /[A-Za-z0-9'ऀ-ॿ]+/g;
const BREAK = /[.,!?;:—–।…]/;   // punctuation (incl. the Hindi danda) = a pause

type Tok = { w: string; start: number; end: number; afterBreak: boolean };

function tokenize(text: string): Tok[] {
  const out: Tok[] = [];
  let m: RegExpExecArray | null;
  let prevEnd = 0;
  WORD.lastIndex = 0;
  while ((m = WORD.exec(text))) {
    const gap = text.slice(prevEnd, m.index);
    out.push({ w: m[0].toLowerCase().replace(/^'+|'+$/g, ''), start: m.index, end: m.index + m[0].length, afterBreak: out.length === 0 || BREAK.test(gap) });
    prevEnd = m.index + m[0].length;
  }
  return out;
}

/** Extra spellings of a persona name the recognizer may produce. */
export function wakeNames(personaName?: string): string[] {
  const n = (personaName || '').trim().toLowerCase();
  const extra: Record<string, string[]> = {
    pehalwan: ['pehalwan', 'pahalwan', 'pehelwan', 'pahlwan', 'पहलवान'],
    ustaad: ['ustaad', 'ustad', 'ustaat', 'उस्ताद'],
    max: ['max', 'macs', 'mex'],
    sage: ['sage', 'sej'],
    ash: ['ash', 'ashe'],
  };
  return Array.from(new Set([...(extra[n] ?? (n ? [n] : []))]));
}

/** Phrases to bias the recognizer with (contextualStrings). */
export function wakePhrases(personaName?: string): string[] {
  const names = ['coach', ...wakeNames(personaName).slice(0, 2)];
  const out: string[] = [];
  for (const g of ['hey', 'okay']) for (const n of names) out.push(`${g} ${n}`);
  out.push('hello coach', 'हे कोच', 'हेलो कोच');
  return out;
}

export function matchWake(transcript: string, names: string[] = []): WakeMatch {
  const text = transcript || '';
  const toks = tokenize(text);
  const extra = new Set(names.map((n) => n.toLowerCase()));
  const isTarget = (w: string) => TARGETS.includes(w) || extra.has(w);
  for (let i = 0; i < toks.length; i++) {
    const t = toks[i];
    if (!t.afterBreak) continue;
    let targetIdx = -1;
    if (i + 1 < toks.length && GREET.has(t.w) && isTarget(toks[i + 1].w)) targetIdx = i + 1;
    else if (i === 0 && i + 1 < toks.length && t.w === 'a' && TARGETS.includes(toks[1].w)) targetIdx = 1;
    else if (/^(hey|hi|ok|okay|hello)(coach|couch)$/.test(t.w)) targetIdx = i;          // "heycoach"
    else if (/^(हेलो|हे|हैलो)कोच$/.test(t.w)) targetIdx = i;
    if (targetIdx < 0) continue;
    const command = text.slice(toks[targetIdx].end).replace(/^[\s.,!?;:—–।…-]+/, '').trim();
    return { hit: true, command };
  }
  return { hit: false, command: '' };
}

export type WakeEvent = { kind: 'wake' } | { kind: 'command'; command: string; followup?: boolean };

/**
 * Stateful wrapper for the stream of recognizer results.
 *  feed(transcript, isFinal, nowMs) -> events:
 *    'wake'    once per utterance, as soon as the phrase is heard (interim or final): use for haptic / UI
 *    'command' once, when the utterance is final (or flush()ed) and carries text after the phrase, OR the next
 *              final utterance within windowMs after a bare "hey coach".
 */
export function createWakeTracker(names: string[] = [], windowMs = 6000) {
  let armed = false;          // wake heard in the current utterance, command not yet dispatched
  let cmd = '';
  let awaitingUntil = 0;      // bare "hey coach" heard: next final utterance is the command
  let lastDone = '';          // last handled final text, to drop duplicate finals
  let lastDoneAt = 0;
  let followUntil = 0;        // coach asked a question: the next utterance is the answer, no wake phrase needed
  let followText = '';        // latest interim text of that answer (for flush when no final arrives)
  const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9ऀ-ॿ]+/g, ' ').trim();

  function settle(now: number, out: WakeEvent[]) {
    if (!armed) return;
    armed = false;
    if (cmd) { out.push({ kind: 'command', command: cmd }); awaitingUntil = 0; }
    else awaitingUntil = now + windowMs;
    cmd = '';
  }

  return {
    feed(transcript: string, isFinal: boolean, now: number): WakeEvent[] {
      const out: WakeEvent[] = [];
      const key = norm(transcript);
      if (!key) return out;
      if (isFinal && key === lastDone && now - lastDoneAt < 3000 && !armed) return out;
      const m = matchWake(transcript, names);
      if (!m.hit && !armed && followUntil && now <= followUntil) {
        // one-shot answer to the coach's question
        if (!isFinal) { followText = transcript.trim(); return out; }
        followUntil = 0; followText = '';
        lastDone = key; lastDoneAt = now;
        out.push({ kind: 'command', command: transcript.trim(), followup: true });
        return out;
      }
      if (m.hit) {
        followUntil = 0; followText = '';
        if (!armed) out.push({ kind: 'wake' });
        armed = true; cmd = m.command;
        awaitingUntil = 0;
      } else if (isFinal && !armed && awaitingUntil && now <= awaitingUntil) {
        // follow-up command after a bare "hey coach"
        awaitingUntil = 0;
        lastDone = key; lastDoneAt = now;
        out.push({ kind: 'command', command: transcript.trim() });
        return out;
      } else if (armed) {
        cmd = m.command;      // (not reachable: a continuing partial still contains the phrase)
      }
      if (isFinal) { lastDone = key; lastDoneAt = now; settle(now, out); }
      return out;
    },
    /** Call when recognition ends without a final result. */
    flush(now: number): WakeEvent[] {
      const out: WakeEvent[] = [];
      settle(now, out);
      if (followText && followUntil && now <= followUntil + 1500) { out.push({ kind: 'command', command: followText, followup: true }); followUntil = 0; followText = ''; }
      return out;
    },
    /** Open a one-shot window: the next utterance within ms is a command even without the wake phrase. */
    expectFollowup(now: number, ms = 8000) { followUntil = now + ms; followText = ''; armed = false; cmd = ''; awaitingUntil = 0; },
    followingUp(now: number) { return followUntil > now; },
    cancelFollowup() { followUntil = 0; followText = ''; },
    /** True while a bare "hey coach" is waiting for its command. */
    awaiting(now: number) { return awaitingUntil > now; },
    reset() { armed = false; cmd = ''; awaitingUntil = 0; followUntil = 0; followText = ''; },
  };
}
