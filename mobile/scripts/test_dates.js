// Runs the pure date helpers under plain Node: node scripts/test_dates.js
const ts = require('typescript'), fs = require('fs'), path = require('path'), assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'dates.ts'), 'utf8');
const js = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2019 } }).outputText;
const m = { exports: {} }; new Function('module', 'exports', js)(m, m.exports);
const { mondayOf, isoOf, weeklyBuckets, weekStreak, shortDate, isIso } = m.exports;
const D = (s) => new Date(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10));

// Monday-based weeks, including Sunday (which belongs to the PREVIOUS Monday) and month/year boundaries
assert.strictEqual(isoOf(mondayOf(D('2026-01-05'))), '2026-01-05');   // Monday
assert.strictEqual(isoOf(mondayOf(D('2026-01-11'))), '2026-01-05');   // Sunday
assert.strictEqual(isoOf(mondayOf(D('2026-01-01'))), '2025-12-29');   // crosses the year
assert.strictEqual(isoOf(mondayOf(D('2026-03-30'))), '2026-03-30');   // DST week in many zones

assert.strictEqual(shortDate('2026-01-05'), 'Jan 5');
assert.strictEqual(shortDate('s3'), 's3');
assert.ok(isIso('2026-01-05') && !isIso('s3') && !isIso('2026-1-5'));

const now = D('2026-03-11'); // a Wednesday
const act = [
  { date: '2026-03-02', sets: 12, tonnage: 4000 }, { date: '2026-03-04', sets: 12, tonnage: 4200 },   // week of Mar 2: 2 sessions
  { date: '2026-03-09', sets: 12, tonnage: 3000 },                                                    // this week: 1 session so far
  { date: '2026-02-23', sets: 12, tonnage: 3900 }, { date: '2026-02-27', sets: 12, tonnage: 4100 },   // week of Feb 23: 2 sessions
  { date: '2025-01-01', sets: 12, tonnage: 9999 }, { date: 'not-a-date', sets: 1, tonnage: 1 },        // outside window / junk: ignored
];
const b = weeklyBuckets(act, 12, now);
assert.strictEqual(b.length, 12);
assert.strictEqual(b[11].start, '2026-03-09');
assert.strictEqual(b[0].start, '2025-12-22');
assert.deepStrictEqual([b[9].sessions, b[10].sessions, b[11].sessions], [2, 2, 1]);
assert.strictEqual(b[10].tonnage, 8200);
assert.strictEqual(b.reduce((a, x) => a + x.sessions, 0), 5);          // junk and out-of-window rows excluded

// Streak: current week (1 session, still in progress) must not break it; two full weeks before it count
assert.strictEqual(weekStreak(b), 2);
assert.strictEqual(weekStreak([{ sessions: 3 }, { sessions: 0 }, { sessions: 2 }]), 1);      // a gap resets it
assert.strictEqual(weekStreak([{ sessions: 2 }, { sessions: 2 }, { sessions: 2 }]), 3);      // current week counted when it already qualifies
assert.strictEqual(weekStreak([{ sessions: 0 }, { sessions: 0 }]), 0);
assert.strictEqual(weekStreak([]), 0);
console.log('dates: all assertions passed');
