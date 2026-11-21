// Deciding when the user has FINISHED speaking. Pure TypeScript (no React / native imports): node scripts/test_endpoint.js
//
// The phone's recognizer ends an utterance after ~1 s of silence, so "I did like uhhh ... one set of bench" becomes two broken sentences. We keep the
// recognizer running and make the decision ourselves, from what was said:
//   - a sentence that ends on "like", "and", "I did", a bare "so" ... is NOT finished: wait up to ~4 s for the rest
//   - "skip", "next", "bench 60 for 8 reps", a question: finished: send after ~0.7 s
//   - in between (a set was reported but no numbers yet): wait ~1.6 s
// Fillers (uh, um, hmm) never count as words and are removed from what is sent.

export type Completeness = 'complete' | 'maybe' | 'incomplete';

/** Milliseconds of silence to wait, by how finished the sentence looks. `scale` shrinks all of them (tests). */
export const tuning = { complete: 700, maybe: 1600, incomplete: 3800, hardMax: 22000, scale: 1 };

const FILLER = /(?<![A-Za-z])(?:u+m+|u+h+|e+r+m*|h+m+|m{2,}|a+h+|eh+)(?![A-Za-z])[,.]?\s*/gi;
const NUM = '(?:\\d+(?:\\.\\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty)';
const REPS_AFTER_NUM = new RegExp(`\\b(${NUM})\\s+(?:wraps?|raps?|rapes?|reaps?)\\b`, 'gi');
const REPS_PLURAL = /\b(?:wraps|raps|rapes|reaps)\b/gi;

const DANGLING = new Set(['like', 'and', 'so', 'then', 'the', 'a', 'an', 'my', 'of', 'with', 'for', 'at', 'to', 'i', "i'm", 'im', 'just', 'about', 'was', 'is', 'have', 'had',
  'did', 'do', 'that', 'which', 'also', 'plus', 'but', 'because', 'when', 'if', 'or', 'on', 'in', 'it', 'its', "it's", 'some', 'around', 'kind', 'sort', 'basically',
  'actually', 'well', 'okay', 'ok', 'yeah', 'by', 'from', 'than', 'let', "let's", 'lets', 'can', 'could', 'should', 'would', 'will', 'what', 'how', 'where', 'who', 'which', 'need', 'needs', 'want', 'wanna', 'gonna', 'supposed', 'have', 'why', 'whether', 'while', 'tell', 'explain', 'between', 'after', 'before']);
// endings that mean "and the rest is coming": "can you just tell me", "should I go till", "what is the"
const OPEN_END = /(?:\b(?:tell|give|show|let|help|ask|get|take|teach|remind)\s+me|\b(?:can|could|would|should|will|do|does|did|am|is|are|was|were)\s+(?:you|i|we|it|that|this)(?:\s+just)?|\b(?:till|until|between|towards?|against|instead of|because of|according to|how do i|what is the|what's the|where is the))$/i;
const SHORT_COMMANDS = /^(?:skip|skip this|next|next one|done|finished|finish|stop|yes|no|yeah|nope|okay|ok|cancel|undo|repeat|again|ready|go|start|pause|resume|continue|sure|thanks|thank you)\b/i;
const QUESTION = /^(?:what|how|should|can|could|why|is|are|do|does|when|where|which|who|am i|any|tell me|give me|show me|open)\b/i;
const SET_WORDS = /\b(?:did|done|finished|completed|logged|set|sets|had|got)\b/i;
const NUMBERISH = new RegExp(`\\b${NUM}\\b`, 'i');
// a real quantity (not just 'one set'): digits, or a number word other than 'one'
const QUANTITY = /\d|\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred)\b/i;
const UNIT_END = /(?:\d|\b(?:reps?|rep|kg|kgs|kilos?|kilograms?|lbs?|pounds?|times|rpe|seconds?|minutes?|mins?|sets?)\b)\s*$/i;

/** Fillers removed, 'wraps / raps' (a systematic mishearing of 'reps') repaired. */
export function cleanSpeech(text: string): string {
  return (text || '').replace(FILLER, ' ').replace(REPS_AFTER_NUM, (_m, n) => `${n} reps`).replace(REPS_PLURAL, 'reps').replace(/\s{2,}/g, ' ').trim();
}

const NUMWORD = /^(?:\d+(?:\.\d+)?|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|point|half|and)$/;
const wordsOf = (t: string) => (t.toLowerCase().match(/[a-z']+|\d+(?:\.\d+)?/g) ?? []);

export function judge(text: string): Completeness {
  const t = cleanSpeech(text);
  const w = wordsOf(t);
  if (!w.length) return 'incomplete';
  const last = w[w.length - 1];
  if (DANGLING.has(last)) return 'incomplete';
  if (w.length <= 8 && OPEN_END.test(t.toLowerCase())) return 'incomplete';
  if (w.length <= 3 && SHORT_COMMANDS.test(t)) return 'complete';
  if (w.length <= 4 && w.every((x) => NUMWORD.test(x)) && w.some((x) => x !== 'and')) return 'complete';       // "sixty", "eighty five": an answer to "how heavy?"
  if (UNIT_END.test(t) && NUMBERISH.test(t)) return 'complete';
  if (QUESTION.test(t) && w.length >= 3) return 'complete';
  if (SET_WORDS.test(t) && !QUANTITY.test(t) && w.length < 9) return 'maybe';       // "I did bench press": the numbers may still come
  if (/^\d+(?:\.\d+)?$/.test(last) && w.length <= 3) return 'complete';             // "sixty", "bench 60" as an answer
  if (w.length === 1) return 'maybe';
  return w.length >= 5 ? 'complete' : 'maybe';
}

export function holdFor(text: string, segmentFinal = false): number {
  const base = tuning[judge(text)] * tuning.scale;
  return segmentFinal ? Math.max(350 * tuning.scale, base * 0.6) : base;                // the recognizer itself thinks it is done: trust it a little
}

/** Feed it the whole utterance so far; ask it when to send. */
export class Endpointer {
  private text = '';
  private changedAt = 0;
  private startedAt = 0;
  private final = false;

  feed(text: string, now: number, segmentFinal = false): void {
    const t = cleanSpeech(text);
    if (!t) return;
    if (!this.startedAt) this.startedAt = now;
    if (t !== this.text) { this.text = t; this.changedAt = now; }
    this.final = segmentFinal;
  }
  /** The utterance to send now, or null if it should keep waiting. */
  ready(now: number): string | null {
    if (!this.text) return null;
    const waited = now - this.changedAt;
    if (waited >= holdFor(this.text, this.final) || now - this.startedAt >= tuning.hardMax * tuning.scale + holdFor(this.text, true)) {
      return this.take();
    }
    return null;
  }
  /** Send what is there regardless (the recognizer died / the user tapped). */
  take(): string | null { const t = this.text; this.reset(); return t || null; }
  peek(): string { return this.text; }
  reset(): void { this.text = ''; this.changedAt = 0; this.startedAt = 0; this.final = false; }
}

/** Join the text of finished segments and the current partial one. */
export const joinSegments = (committed: string, partial: string) => [committed, partial].filter((x) => x && x.trim()).join(' ').replace(/\s{2,}/g, ' ').trim();

/** A few words the coach can say at once (phone voice, no network) while a slow answer is still being worked out. */
export const FILLERS = ['One sec.', 'Okay, checking.', 'Mm, let me see.', 'Right, one moment.', 'Got it, hold on.'];
export const pickFiller = (n: number) => FILLERS[Math.abs(n) % FILLERS.length];
