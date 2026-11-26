// Runs the live-voice lifecycle rules under plain Node: node scripts/test_livesession.js
// (ERROR_LEDGER E-007 / E-008: the workout screen and its voice session must survive Back, a misheard "open Fuel", the app being switched away and a dropped connection.)
const ts = require('typescript'), fs = require('fs'), path = require('path'), assert = require('assert');
const load = (f) => {
  const src = fs.readFileSync(path.join(__dirname, '..', 'src', f), 'utf8');
  const js = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2019 } }).outputText;
  const m = { exports: {} }; new Function('module', 'exports', 'require', js)(m, m.exports, require); return m.exports;
};
const L = load('liveSession.ts');
const B = load('liveBack.ts');
const T = load('voiceTalk.ts');

// ---- app state: only really leaving ends the session
const on = { session: true, stream: true, starting: false };
assert.strictEqual(L.appStateEffect('background', on), 'pause');
assert.strictEqual(L.appStateEffect('inactive', on), 'keep');                                  // a permission screen / the notification shade
assert.strictEqual(L.appStateEffect('active', on), 'keep');
assert.strictEqual(L.appStateEffect('inactive', { session: true, stream: false, starting: false }), 'pause');   // phone-recogniser session: stricter
assert.strictEqual(L.appStateEffect('inactive', { session: true, stream: false, starting: true }), 'keep');     // connecting: a permission screen may flash
assert.strictEqual(L.appStateEffect('background', { session: false, stream: false, starting: false }), 'keep'); // nothing running: nothing to pause

// ---- coming back
const t0 = 1_000_000;
assert.strictEqual(L.resumePlan(null, t0), 'none');
assert.strictEqual(L.resumePlan(t0, t0 + 20_000), 'auto');
assert.strictEqual(L.resumePlan(t0, t0 + L.AUTO_RESUME_MS), 'auto');
assert.strictEqual(L.resumePlan(t0, t0 + L.AUTO_RESUME_MS + 1), 'manual');
assert.strictEqual(L.resumePlan(t0, t0 + L.RESUME_GIVE_UP_MS + 1), 'none');
assert.strictEqual(L.resumePlan(t0, t0 - 5), 'auto');                                           // a clock that went backwards must not strand the user

// ---- navigation under the workout is dropped (it would unmount the workout and the voice with it)
assert.strictEqual(L.blockedWhileLive('live', { type: 'navigate', tab: 'nutrition' }), true);
assert.strictEqual(L.blockedWhileLive('live', { type: 'open', screen: 'diet' }), true);
assert.strictEqual(L.blockedWhileLive('live', { type: 'open', screen: 'live' }), false);
assert.strictEqual(L.blockedWhileLive('live', { type: 'rest', seconds: 90 }), false);
assert.strictEqual(L.blockedWhileLive('live', { type: 'refresh' }), false);
assert.strictEqual(L.blockedWhileLive('today', { type: 'navigate', tab: 'nutrition' }), false);   // outside a workout navigation works as before
assert.strictEqual(L.blockedWhileLive('nutrition', { type: 'open', screen: 'agenda' }), false);

// ---- leaving asks only when it would cost something
assert.strictEqual(L.leaveNeedsConfirm({ voiceOn: true, ended: false, setsDone: 0, finished: false }), true);
assert.strictEqual(L.leaveNeedsConfirm({ voiceOn: false, ended: false, setsDone: 3, finished: false }), true);
assert.strictEqual(L.leaveNeedsConfirm({ voiceOn: false, ended: false, setsDone: 0, finished: false }), false);
assert.strictEqual(L.leaveNeedsConfirm({ voiceOn: true, ended: true, setsDone: 5, finished: false }), false);
assert.strictEqual(L.leaveNeedsConfirm({ voiceOn: true, ended: false, setsDone: 9, finished: true }), false);

// ---- the banner always says what is going on, and why a session ended
assert.deepStrictEqual(L.voiceBanner({ session: true, phase: 'listening', endReason: '', connecting: true }), { kind: 'connecting', text: 'Connecting voice…' });
assert.strictEqual(L.voiceBanner({ session: true, phase: 'thinking', endReason: '' }).kind, 'thinking');
assert.strictEqual(L.voiceBanner({ session: true, phase: 'speaking', endReason: '' }).kind, 'speaking');
assert.strictEqual(L.voiceBanner({ session: true, phase: 'followup', endReason: '' }).kind, 'listening');
assert.strictEqual(L.voiceBanner({ session: false, phase: 'idle', endReason: '' }).kind, 'off');
const ended = L.voiceBanner({ session: false, phase: 'idle', endReason: L.endText('background') });
assert.strictEqual(ended.kind, 'ended');
assert.ok(/left the app/.test(ended.text));
for (const k of ['background', 'quiet', 'dropped', 'denied', 'screen']) assert.ok(L.endText(k).length > 10, k);
assert.strictEqual(L.endText('error', 'Mic broke'), 'Mic broke');
assert.strictEqual(L.endText('user'), '');

// ---- reconnect: two quick tries, then tell the user
assert.strictEqual(L.reconnectDelay(0), 700);
assert.strictEqual(L.reconnectDelay(1), 2500);
assert.strictEqual(L.reconnectDelay(2), null);

// ---- the back handler: the workout screen claims Back while open, a closed one lets it through
assert.strictEqual(B.handleLiveBack(), false);
let calls = 0; const off = B.setLiveBackHandler(() => { calls++; return true; });
assert.strictEqual(B.handleLiveBack(), true); assert.strictEqual(calls, 1);
const off2 = B.setLiveBackHandler(() => false);                                                  // a newer screen replaced it
off();                                                                                           // the old one unregistering must not remove the new one
assert.strictEqual(B.handleLiveBack(), false);
off2();
assert.strictEqual(B.handleLiveBack(), false);
B.setLiveBackHandler(() => { throw new Error('boom'); });
assert.strictEqual(B.handleLiveBack(), false);                                                   // a broken handler never traps the user

// ---- voiceTalk: the end reason is part of the state; stop passes WHY
let seen = [];
T.registerTalk(() => {}, (why) => seen.push(why));
T.setTalkState({ session: true, phase: 'listening' });
T.toggleTalk(); T.stopTalk(); T.stopTalk('custom');
assert.deepStrictEqual(seen, ['tap', 'screen', 'custom']);
let n = 0; const un = T.onTalkState(() => n++);
T.setTalkState({ endReason: 'x' }); T.setTalkState({ endReason: 'x' }); T.setTalkState({ connecting: true });
assert.strictEqual(n, 2);                                                                        // identical updates do not re-render
un();
assert.strictEqual(T.getTalkState().endReason, 'x');
assert.strictEqual(T.resumeTalk(), true);

console.log('livesession ok');
