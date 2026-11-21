// node scripts/test_profileform.js : runs the pure Edit-Profile helpers under plain Node.
const ts = require('typescript'), fs = require('fs'), path = require('path'), assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'profileForm.ts'), 'utf8');
const js = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2019 } }).outputText;
const m = { exports: {} }; new Function('module', 'exports', js)(m, m.exports);
const { toForm, diff, hasInj, toggleInj, customInjuries, TRAINING_KEYS } = m.exports;

const stored = { age: 28, sex: 'm', height_cm: 178, weight_kg: 82, goal: 'maintain', days_per_week: 4, activity: 'moderate', injuries: [] }; // like the demo profile: NO experience key
const base = toForm(stored);
assert.strictEqual(base.experience, 'intermediate');                       // default filled in
assert.deepStrictEqual(diff(toForm(stored), base), {});                    // untouched form = no changes, so no false "rebuild?" prompt

assert.deepStrictEqual(diff({ ...base, weight_kg: 85 }, base), { weight_kg: 85 });
const t = diff({ ...base, days_per_week: 5, weight_kg: 85 }, base);
assert.ok(Object.keys(t).some((k) => TRAINING_KEYS.includes(k)));          // days is a training change
assert.ok(!Object.keys(diff({ ...base, weight_kg: 85, goal: 'cut', age: 30 }, base)).some((k) => TRAINING_KEYS.includes(k))); // body/goal alone never asks to rebuild

// injuries: order irrelevant, free text preserved, keyword toggling
const withInj = toForm({ ...stored, injuries: ['left shoulder', 'shoulder', 'something odd'] });
assert.deepStrictEqual(diff({ ...withInj, injuries: ['something odd', 'shoulder', 'left shoulder'] }, withInj), {}); // reordered = unchanged
assert.ok(hasInj(withInj.injuries, 'shoulder') && !hasInj(withInj.injuries, 'knee'));
assert.deepStrictEqual(toggleInj(['left shoulder', 'knee'], 'shoulder'), ['knee']);                  // removes every shoulder entry
assert.deepStrictEqual(toggleInj(['knee'], 'wrist'), ['knee', 'wrist']);
assert.deepStrictEqual(customInjuries(['left shoulder', 'something odd', 'knee pain']), ['something odd']);
assert.deepStrictEqual(Object.keys(diff({ ...base, injuries: ['knee'] }, base)), ['injuries']);
// the form never mutates the stored profile
const src2 = { ...stored, injuries: ['knee'] }; const f = toForm(src2); f.injuries.push('hip'); assert.deepStrictEqual(src2.injuries, ['knee']);
console.log('profileForm: all assertions passed');
