// Runs the voice bus under plain Node: node scripts/test_voicebus.js
const ts = require('typescript'), fs = require('fs'), path = require('path'), assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'voiceBus.ts'), 'utf8');
const js = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2019 } }).outputText;
const m = { exports: {} }; new Function('module', 'exports', js)(m, m.exports);
const { parseAction, onVoiceAction, emitVoiceAction, emitVoiceActions, takePendingVoiceActions, _resetVoiceBus } = m.exports;

// parseAction: valid
assert.deepStrictEqual(parseAction({ type: 'navigate', tab: 'nutrition' }), { type: 'navigate', tab: 'nutrition' });
assert.deepStrictEqual(parseAction({ type: 'open', screen: 'agenda' }), { type: 'open', screen: 'agenda' });
assert.deepStrictEqual(parseAction({ type: 'close' }), { type: 'close' });
assert.deepStrictEqual(parseAction({ type: 'rest', seconds: 90.4 }), { type: 'rest', seconds: 90 });
assert.deepStrictEqual(parseAction({ type: 'nutrition_day', day: '2026-11-20' }), { type: 'nutrition_day', day: '2026-11-20' });
assert.deepStrictEqual(parseAction({ type: 'refresh' }), { type: 'refresh', what: ['all'] });
assert.deepStrictEqual(parseAction({ type: 'refresh', what: ['today', 'bogus'] }), { type: 'refresh', what: ['today'] });
assert.deepStrictEqual(parseAction({ type: 'setting', key: 'wake', value: false }), { type: 'setting', key: 'wake', value: false });
// parseAction: invalid never throws
for (const bad of [null, undefined, 5, 'x', {}, { type: 5 }, { type: 'nope' }, { type: 'navigate', tab: 'admin' }, { type: 'open', screen: 'x' },
  { type: 'rest', seconds: 0 }, { type: 'rest', seconds: 5000 }, { type: 'rest', seconds: 'a' }, { type: 'rest_add', seconds: 999 },
  { type: 'nutrition_day', day: 'yesterday' }, { type: 'insights_segment', seg: '' }, { type: 'setting', key: 'x', value: 1 }, { type: 'setting', key: 'wake', value: 3 }]) {
  assert.strictEqual(parseAction(bad), null, JSON.stringify(bad));
}

// handled actions are not queued; unhandled wait; takePending drains only what matches
_resetVoiceBus();
const seen = [];
const off = onVoiceAction((a) => { seen.push(a.type); return a.type === 'navigate'; });
emitVoiceAction({ type: 'navigate', tab: 'setup' }, 0);
emitVoiceAction({ type: 'open', screen: 'agenda' }, 0);
assert.deepStrictEqual(seen, ['navigate', 'open']);
assert.deepStrictEqual(takePendingVoiceActions((a) => a.type === 'navigate', 100), []);
assert.deepStrictEqual(takePendingVoiceActions((a) => a.type === 'open', 100), [{ type: 'open', screen: 'agenda' }]);
assert.deepStrictEqual(takePendingVoiceActions((a) => a.type === 'open', 100), []);   // drained
off();

// TTL: 12 s
_resetVoiceBus();
emitVoiceAction({ type: 'rest_skip' }, 0);
assert.strictEqual(takePendingVoiceActions(() => true, 11999).length, 1);
emitVoiceAction({ type: 'rest_skip' }, 0);
assert.strictEqual(takePendingVoiceActions(() => true, 12000).length, 0);
// expiry is also applied when a later emit happens
emitVoiceAction({ type: 'end' }, 0); emitVoiceAction({ type: 'close' }, 20000);
assert.deepStrictEqual(takePendingVoiceActions(() => true, 20001), [{ type: 'close' }]);

// a throwing listener does not block others; order is kept; emitVoiceActions skips junk and counts valid
_resetVoiceBus();
const order = [];
onVoiceAction(() => { throw new Error('boom'); });
onVoiceAction((a) => { order.push(a.type); return true; });
assert.strictEqual(emitVoiceActions([{ type: 'close' }, { type: 'zzz' }, null, { type: 'end' }], 0), 2);
assert.deepStrictEqual(order, ['close', 'end']);
assert.strictEqual(emitVoiceActions('nope'), 0);
assert.strictEqual(takePendingVoiceActions(() => true, 1).length, 0);       // handled -> nothing queued

console.log('voice bus tests passed');
