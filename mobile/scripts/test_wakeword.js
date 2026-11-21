// Runs the pure wake-word helpers under plain Node: node scripts/test_wakeword.js
const ts = require('typescript'), fs = require('fs'), path = require('path'), assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'wakeWord.ts'), 'utf8');
const js = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2019 } }).outputText;
const m = { exports: {} }; new Function('module', 'exports', js)(m, m.exports);
const { matchWake, createWakeTracker, wakeNames, wakePhrases } = m.exports;

// basic hits, case and punctuation
assert.deepStrictEqual(matchWake('Hey coach'), { hit: true, command: '' });
assert.deepStrictEqual(matchWake('HEY COACH!'), { hit: true, command: '' });
assert.deepStrictEqual(matchWake('Hey coach, bench 80 for 8'), { hit: true, command: 'bench 80 for 8' });
assert.deepStrictEqual(matchWake('okay coach. skip this one'), { hit: true, command: 'skip this one' });
assert.deepStrictEqual(matchWake('hi coach how much time is left'), { hit: true, command: 'how much time is left' });
assert.deepStrictEqual(matchWake('heycoach squat 100 for 5'), { hit: true, command: 'squat 100 for 5' });

// mishearings
assert.ok(matchWake('hey couch').hit);
assert.ok(matchWake('a coach, rest timer').hit);
assert.strictEqual(matchWake('a coach, rest timer').command, 'rest timer');
assert.ok(matchWake('hey Koach').hit);

// persona names
assert.ok(matchWake('hey max', wakeNames('Max')).hit);
assert.ok(!matchWake('hey max').hit);
assert.ok(matchWake('ok ustad bench 60 for 10', wakeNames('Ustaad')).hit);
assert.strictEqual(matchWake('hey pahalwan squat', wakeNames('Pehalwan')).command, 'squat');
assert.ok(matchWake('हे पहलवान', wakeNames('Pehalwan')).hit);

// Hindi
assert.deepStrictEqual(matchWake('हे कोच'), { hit: true, command: '' });
assert.deepStrictEqual(matchWake('हेलो कोच, बेंच 80 आठ'), { hit: true, command: 'बेंच 80 आठ' });

// mentions inside a sentence do not fire
assert.ok(!matchWake('my coach said hey coach is lazy').hit);
assert.ok(!matchWake('i asked a coach about squats').hit);
assert.ok(!matchWake('the coach is great').hit);
assert.ok(!matchWake('hey there').hit);
assert.ok(!matchWake('').hit);
// ...but after a pause marker it does
assert.ok(matchWake('ok so, hey coach, what next').hit);
assert.strictEqual(matchWake('ok so, hey coach, what next').command, 'what next');

// tracker: repeated partials trigger once
let t = createWakeTracker();
let ev = [];
ev.push(...t.feed('hey', false, 0));
ev.push(...t.feed('hey coach', false, 100));
ev.push(...t.feed('hey coach bench', false, 200));
ev.push(...t.feed('hey coach bench 80', false, 300));
assert.deepStrictEqual(ev, [{ kind: 'wake' }]);
ev = t.feed('hey coach bench 80 for 8', true, 400);
assert.deepStrictEqual(ev, [{ kind: 'command', command: 'bench 80 for 8' }]);
assert.deepStrictEqual(t.feed('hey coach bench 80 for 8', true, 450), []);        // duplicate final dropped
assert.deepStrictEqual(t.feed('hey coach bench 80 for 8', false, 500).map((e) => e.kind), ['wake']); // a later new partial is a new utterance...
t.reset();

// tracker: bare wake then command in the next utterance within 6 s
t = createWakeTracker();
assert.deepStrictEqual(t.feed('hey coach', true, 1000).map((e) => e.kind), ['wake']);
assert.ok(t.awaiting(2000));
assert.deepStrictEqual(t.feed('bench 80 for 8', true, 4000), [{ kind: 'command', command: 'bench 80 for 8' }]);
assert.ok(!t.awaiting(4001));
assert.deepStrictEqual(t.feed('something else entirely', true, 4500), []);          // no wake: ignored

// tracker: command window expires
t = createWakeTracker();
t.feed('hey coach', true, 0);
assert.deepStrictEqual(t.feed('bench 80 for 8', true, 7000), []);

// tracker: recognizer ends without a final -> flush dispatches
t = createWakeTracker();
t.feed('hey coach skip', false, 0);
assert.deepStrictEqual(t.flush(500), [{ kind: 'command', command: 'skip' }]);
assert.deepStrictEqual(t.flush(600), []);

// tracker: ordinary speech never fires
t = createWakeTracker();
assert.deepStrictEqual(t.feed('i think the coach is right', true, 0), []);

// follow-up window: the answer to a coach question needs no wake phrase, one shot
t = createWakeTracker(['coach']);
t.expectFollowup(1000, 8000);
assert.ok(t.followingUp(1500));
assert.deepStrictEqual(t.feed('four', false, 2000), []);
assert.deepStrictEqual(t.feed('four eggs', true, 2500), [{ kind: 'command', command: 'four eggs', followup: true }]);
assert.deepStrictEqual(t.feed('and a toast', true, 3000), []);           // one-shot
assert.ok(!t.followingUp(3000));
// expires on silence
t = createWakeTracker(['coach']); t.expectFollowup(0, 8000);
assert.deepStrictEqual(t.feed('four eggs', true, 9000), []);
// no final arrives: flush sends the last interim
t = createWakeTracker(['coach']); t.expectFollowup(0, 8000);
t.feed('three sets of eight', false, 1000);
assert.deepStrictEqual(t.flush(2000), [{ kind: 'command', command: 'three sets of eight', followup: true }]);
// saying the wake phrase inside a follow-up window takes the normal path
t = createWakeTracker(['coach']); t.expectFollowup(0, 8000);
assert.deepStrictEqual(t.feed('hey coach open fuel', true, 1000).filter((e) => e.kind === 'command'), [{ kind: 'command', command: 'open fuel' }]);
// cancel and reset close the window
t = createWakeTracker(['coach']); t.expectFollowup(0, 8000); t.cancelFollowup();
assert.deepStrictEqual(t.feed('four eggs', true, 100), []);
t = createWakeTracker(['coach']); t.expectFollowup(0, 8000); t.reset();
assert.ok(!t.followingUp(100));

// phrases for biasing include the persona
assert.ok(wakePhrases('Max').includes('hey max') && wakePhrases('Max').includes('हे कोच'));

console.log('wake word tests passed');

// v0.3: more mishearings of the greeting / target
assert.ok(matchWake('eh coach').hit);
assert.ok(matchWake('hai coach squat 100').hit);
assert.ok(matchWake('हय कोच').hit);
assert.ok(matchWake('ए कोच bench').hit);
assert.ok(!matchWake('my coach said hey coach is lazy').hit);
console.log('wakeword v0.3 additions ok');
