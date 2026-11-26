const fs = require('fs'), vm = require('vm');
const html = fs.readFileSync('deploy/local/voice_web.html', 'utf8');
const js = html.match(/<script>([\s\S]*)<\/script>/)[1];
function run(reported, actual) {
  const els = {}; const el = id => els[id] || (els[id] = { textContent: '', className: '', style: {}, appendChild() {}, set onclick(f) {} });
  let clock = 0;
  const win = { __COACH_TEST__: true, performance: { now: () => clock }, location: { hash: '', protocol: 'https:', host: 'x', pathname: '/' }, navigator: {}, history: { replaceState() {} },
    document: { getElementById: el, createElement: () => el('n'), addEventListener() {} } };
  win.window = win; win.localStorage = { getItem() { return ''; }, setItem() {} };
  vm.createContext(win); vm.runInContext(js, win);
  const T = win.__COACH_TEST__;
  const sent = []; T.setWs({ readyState: 1, send: b => sent.push(new Int16Array(b)) });
  T.setCtx({ sampleRate: reported });
  T.resetMeasure();
  // the real mic delivers `actual` samples per real second in buffers of 2048; a 440 Hz tone in real time
  let n = 0; const dur = 4;
  while (n / actual < dur) {
    const buf = new Float32Array(2048);
    for (let i = 0; i < 2048; i++) buf[i] = 0.5 * Math.sin(2 * Math.PI * 440 * (n + i) / actual);
    n += 2048; clock = n / actual * 1000;
    T.capture({ inputBuffer: { getChannelData: () => buf } });
  }
  const all = []; sent.forEach(a => a.forEach(v => all.push(v)));
  // frequency by zero crossings of what would be played back at 16 kHz
  let zc = 0; for (let i = 1; i < all.length; i++) if (all[i - 1] < 0 && all[i] >= 0) zc++;
  const secs = all.length / 16000, f = zc / secs;
  return { reported, actual, rate: T.m.rate, sentSeconds: +secs.toFixed(2), tone: Math.round(f) };
}
for (const [r, a] of [[16000, 16000], [48000, 48000], [16000, 32000], [44100, 44100], [48000, 16000], [32000, 32000], [16000, 48000]]) console.log(JSON.stringify(run(r, a)));
