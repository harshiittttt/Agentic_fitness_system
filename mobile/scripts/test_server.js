// Runs the pure server helpers under plain Node: node scripts/test_server.js
const ts = require('typescript'), fs = require('fs'), path = require('path'), assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'serverCore.ts'), 'utf8');
const js = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2019 } }).outputText;
const m = { exports: {} }; new Function('module', 'exports', js)(m, m.exports);
const { normalizeUrl: n, cleanToken, isSameBase, makeAuthFetch } = m.exports;

// scheme: http for localhost / LAN, https for anything public
assert.strictEqual(n('localhost:8000'), 'http://localhost:8000');
assert.strictEqual(n('192.168.1.8'), 'http://192.168.1.8');
assert.strictEqual(n('192.168.1.8:8000/'), 'http://192.168.1.8:8000');
assert.strictEqual(n('10.0.2.2:8000'), 'http://10.0.2.2:8000');
assert.strictEqual(n('172.20.1.1'), 'http://172.20.1.1');
assert.strictEqual(n('172.32.1.1'), 'https://172.32.1.1');          // outside 172.16/12
assert.strictEqual(n('my-coach.example.com'), 'https://my-coach.example.com');
assert.strictEqual(n('  my-coach.fly.dev/  '), 'https://my-coach.fly.dev');
assert.strictEqual(n('mypc.local:8000'), 'http://mypc.local:8000');
// explicit scheme is respected, scheme lower-cased, trailing slashes / query stripped
assert.strictEqual(n('HTTPS://Coach.Example.com///'), 'https://Coach.Example.com');
assert.strictEqual(n('http://coach.example.com/'), 'http://coach.example.com');
assert.strictEqual(n('https://a.com/api/?x=1#y'), 'https://a.com/api');
// unusable
for (const bad of ['', '   ', null, undefined, 'ftp://a.com', 'javascript://x', 'https://']) assert.strictEqual(n(bad), '', String(bad));
assert.strictEqual(cleanToken(' abc\n def '), 'abcdef');

// host check: never a prefix lookalike
assert.ok(isSameBase('https://a.com/plan', 'https://a.com'));
assert.ok(isSameBase('https://a.com', 'https://a.com'));
assert.ok(isSameBase('https://a.com?x=1', 'https://a.com'));
assert.ok(isSameBase('HTTPS://A.COM/plan', 'https://a.com'));
assert.ok(!isSameBase('https://a.com.evil.io/plan', 'https://a.com'));
assert.ok(!isSameBase('https://a.com@evil.io/plan', 'https://a.com'));
assert.ok(!isSameBase('http://a.com/plan', 'https://a.com'));
assert.ok(!isSameBase('https://other.com/plan', 'https://a.com'));
assert.ok(!isSameBase('https://a.com/plan', ''));

// the wrapper
(async () => {
  const calls = []; let status = 200, fired = 0;
  const inner = async (input, init) => { calls.push({ input, init }); return { status }; };
  const f = makeAuthFetch(inner, () => 'https://a.com', () => 'sekret', () => { fired++; });
  const auth = (c) => c.init && c.init.headers && new Headers(c.init.headers).get('authorization');

  await f('https://a.com/plan');
  assert.strictEqual(auth(calls.at(-1)), 'Bearer sekret');
  await f('https://a.com/log', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  assert.strictEqual(auth(calls.at(-1)), 'Bearer sekret');
  assert.strictEqual(calls.at(-1).init.headers.get('content-type'), 'application/json');
  assert.strictEqual(calls.at(-1).init.method, 'POST');
  await f('https://a.com/x', { headers: { Authorization: 'Bearer mine' } });          // explicit header wins
  assert.strictEqual(auth(calls.at(-1)), 'Bearer mine');
  await f('https://api.elevenlabs.io/v1/tts');                                         // other host: untouched
  assert.strictEqual(calls.at(-1).init, undefined);
  await f('https://a.com.evil.io/plan', { headers: { x: '1' } });                     // lookalike host: no token
  assert.ok(!auth(calls.at(-1)));
  await f(new URL('https://a.com/plan'));
  assert.strictEqual(auth(calls.at(-1)), 'Bearer sekret');

  assert.strictEqual(fired, 0);
  status = 401;
  await f('https://a.com/plan'); assert.strictEqual(fired, 1);
  await f('https://other.com/plan'); assert.strictEqual(fired, 1);                     // another host's 401 is not ours

  const noTok = makeAuthFetch(inner, () => 'https://a.com', () => '', () => { fired++; });
  await noTok('https://a.com/plan'); assert.ok(!auth(calls.at(-1)));                   // no key: no header, 401 still reported
  assert.strictEqual(fired, 2);
  console.log('test_server: all assertions passed');
})().catch((e) => { console.error(e); process.exit(1); });
