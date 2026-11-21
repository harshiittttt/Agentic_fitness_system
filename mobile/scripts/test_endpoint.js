// node scripts/test_endpoint.js : the end-of-speech decisions, with phrases taken from a real session log.
const ts = require('typescript'), fs = require('fs'), path = require('path'), assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'voiceEndpoint.ts'), 'utf8');
const js = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
const m = { exports: {} }; new Function('module', 'exports', js)(m, m.exports);
const { cleanSpeech, judge, holdFor, Endpointer, joinSegments, tuning } = m.exports;

// cleaning
assert.strictEqual(cleanSpeech('I did like uhh one set of overhead press 15 wraps 20 kg'), 'I did like one set of overhead press 15 reps 20 kg');
assert.strictEqual(cleanSpeech('10 raps'), '10 reps');
assert.strictEqual(cleanSpeech('um so I did uh bench'), 'so I did bench');
assert.strictEqual(cleanSpeech('wrap it up'), 'wrap it up');
assert.strictEqual(cleanSpeech('  hmm  '), '');

// judging, from the user's real transcripts
const incomplete = ['can you just tell me', 'should I go till', 'okay can you tell me', 'what is the', 'how do I', 'give me', 'I did like', 'I did like uhh', 'and then', 'so I', 'I just', 'my', 'I did the', 'bench press with', 'okay so'];
const complete = ['can you skip this', 'tell me how much protein', 'skip', 'next', 'done', 'what is next', "what's my protein left", 'bench press 60 for 8', 'I did 10 reps', 'ten reps', 'forty five kilos', 'sixty',
  'I did one set of overhead press 15 reps 20 kg', 'how much protein do I have left', 'change the time to 1 minute'];
const maybe = ['I did bench press', 'I did a set of lat pulldown', 'done with that set'];
for (const t of incomplete) assert.strictEqual(judge(t), 'incomplete', t);
for (const t of complete) assert.strictEqual(judge(t), 'complete', t);
for (const t of maybe) assert.strictEqual(judge(t), 'maybe', t);

// hold times: finished sentences go fast, unfinished ones wait
assert.ok(holdFor('skip') <= 800 && holdFor('I did like') >= 3000 && holdFor('I did bench press') > holdFor('skip') && holdFor('I did bench press') < holdFor('I did like'));
assert.ok(holdFor('I did like', true) < holdFor('I did like', false));

// the endpointer: "I did like [thinks] ... one set of bench 60 for 8 reps" stays ONE utterance
let e = new Endpointer();
e.feed('I did like', 0);
assert.strictEqual(e.ready(1500), null, 'waits through a thinking pause');
assert.strictEqual(e.ready(3000), null);
e.feed('I did like uhh', 3200);                      // filler only: text unchanged after cleaning, clock keeps running
e.feed('I did like one set of bench', 3500);
assert.strictEqual(e.ready(4500), null);            // "set of bench" has no numbers yet: maybe
e.feed('I did like one set of bench 60 for 8 reps', 5000);
assert.strictEqual(e.ready(5300), null);
assert.strictEqual(e.ready(5800), 'I did like one set of bench 60 for 8 reps');
assert.strictEqual(e.ready(9000), null, 'nothing is sent twice');

// a complete command is sent quickly
e = new Endpointer(); e.feed('skip this', 0);
assert.strictEqual(e.ready(500), null); assert.strictEqual(e.ready(750), 'skip this');

// a dangling fragment is finally sent if nothing ever follows (after the long hold), never lost
e = new Endpointer(); e.feed('I did like', 0);
assert.strictEqual(e.ready(3000), null); assert.strictEqual(e.ready(4000), 'I did like');

// a very long ramble is cut at the hard limit
e = new Endpointer(); let t = 0; let words = 'so I did and then';
for (let i = 0; i < 40; i++) { t += 700; words += ' and'; e.feed(words, t); }
assert.ok(e.ready(t + 100) !== null || e.ready(t + 5000) !== null);

// time scaling for tests
tuning.scale = 0.1; e = new Endpointer(); e.feed('skip', 0); assert.strictEqual(e.ready(100), 'skip'); tuning.scale = 1;

assert.strictEqual(joinSegments('I did like', 'one set'), 'I did like one set');
assert.strictEqual(joinSegments('', 'hello'), 'hello');
console.log('endpoint tests passed');
