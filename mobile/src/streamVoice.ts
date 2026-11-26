/**
 * Live voice over a WebSocket to the coach's voice agent (voice_agent/server.py on the PC).
 *
 *   microphone (PCM16 16 kHz, echo-cancelled) ---> server: turn detection + speech-to-text + coach brain + voice
 *   speaker    (PCM16 16 kHz)                 <--- server: the answer as it is spoken
 *
 * Pure logic with the audio device and the socket injected, so it is tested without a phone (see __tests__/stream.test.ts).
 * Interrupting: when you start talking the server sends {"t":"clear"}; the audio module cannot flush what it was already given, so
 * playback is fed in small, paced pieces (LEAD_MS ahead) and "clear" simply stops feeding: the tail that is already queued is short.
 */
export type PcmDevice = {
  start(onMic: (pcm: Uint8Array) => void): Promise<void> | void;
  stop(): void;
  play(pcm: Uint8Array): void;
  flush?(): void;                                    // drop what is queued / playing right now (interrupting)
  status?(): string;                                  // readable state of the native audio, for diagnostics
};

export type Phase = 'listening' | 'thinking' | 'speaking';
export type StreamEvents = {
  onUser?(text: string): void;
  onBot?(result: any): void;                         // the same result object /voice/command returns (reply, intent, actions, expects, ...)
  onPhase?(p: Phase): void;
  onClose?(reason: 'closed' | 'unavailable' | 'denied'): void;
  onFirstMic?(msAfterOpen: number): void;            // the first microphone audio arrived
  onNoMic?(status: string): void;                    // connected, but NO microphone audio within NO_MIC_MS: the app should use another engine
};

const BYTES_PER_MS = 32;                              // 16 kHz * 2 bytes
export const LEAD_MS = 260;                           // how far ahead of the speaker we feed audio (= the longest tail after an interruption)
const SLICE_MS = 120;
const OPEN_TIMEOUT_MS = 6000;
export const NO_MIC_MS = 2500;

export class VoiceStream {
  private ws: any = null;
  private queue: Uint8Array[] = [];
  private aheadUntil = 0;                             // when the audio already handed to the speaker has been played
  private feeder: any = null;
  private phase: Phase = 'listening';
  private muteUntil = 0;
  private closing = false;
  private opened = false;
  private pre: Uint8Array[] | null = null;             // microphone audio captured while the socket was still connecting
  private preBytes = 0;
  private micFrames = 0;
  private openedAt = 0;
  private watchdog: any = null;

  constructor(private url: string, private dev: PcmDevice, private ev: StreamEvents = {}, private WS: any = (globalThis as any).WebSocket, private now: () => number = Date.now) {}

  /** Start the microphone AND connect at the same time: what you say while the socket is still opening is kept and sent first, so the first words are never lost.
   *  Rejects (and reports 'unavailable') when the voice agent is not reachable, so the app can fall back. */
  async open(screen: string, bargeIn = true, lang?: string): Promise<void> {
    this.pre = [];
    this.preBytes = 0;
    const micReady = Promise.resolve(this.dev.start((pcm) => this.onMic(pcm)));
    const sockReady = new Promise<void>((resolve, reject) => {
      let settled = false;
      const done = (err?: any) => { if (settled) return; settled = true; clearTimeout(timer); err ? reject(err) : resolve(); };
      const timer = setTimeout(() => done(new Error('voice agent did not answer')), OPEN_TIMEOUT_MS);
      let ws: any;
      try { ws = new this.WS(this.url); } catch (e) { done(e); return; }
      this.ws = ws;
      ws.binaryType = 'arraybuffer';
      ws.onopen = () => { this.opened = true; done(); };
      ws.onerror = () => done(new Error('voice socket error'));
      ws.onclose = (e: any) => {
        if (!this.opened) { done(new Error(`closed ${e?.code ?? ''}`)); return; }
        this.finish(e?.code === 4401 ? 'denied' : 'closed');
      };
      ws.onmessage = (m: any) => this.onMessage(m.data);
    });
    try {
      await Promise.all([micReady, sockReady]);
    } catch (e) { this.pre = null; this.teardown(); this.ev.onClose?.('unavailable'); throw e; }
    this.send(JSON.stringify({ type: 'screen', screen }));
    this.send(JSON.stringify({ type: 'config', barge_in: bargeIn, ...(lang ? { lang } : {}) }));   // lang: 'en' (English / Hinglish, the default) or 'hi' (Hindi)
    const held = this.pre ?? [];
    this.pre = null;
    for (const c of held) this.send(c);
    this.feeder = setInterval(() => this.feed(), 40);
    this.openedAt = this.now();
    this.watchdog = setTimeout(() => {
      this.watchdog = null;
      if (this.micFrames === 0 && !this.closing) this.ev.onNoMic?.(this.micStatus());
    }, NO_MIC_MS);
    this.setPhase('listening');
  }

  micStatus(): string { try { return this.dev.status?.() ?? ''; } catch { return ''; } }

  setScreen(screen: string) { this.send(JSON.stringify({ type: 'screen', screen })); }

  /** Ignore the microphone for a while (the app is about to speak by itself). */
  mute(ms: number) { this.muteUntil = Math.max(this.muteUntil, this.now() + ms); }

  /** The user cut the coach short (a tap): stop feeding the speaker and tell the server to stop too. */
  interrupt() {
    this.queue = [];
    try { this.dev.flush?.(); } catch { /* nothing to flush */ }
    this.send(JSON.stringify({ type: 'interrupt' }));
  }

  close() {
    this.closing = true;
    this.teardown();
  }

  get speaking() { return this.queue.length > 0 || this.aheadUntil > this.now(); }

  // ---------------------------------------------------------------- internals
  private send(data: any) {
    try { if (this.ws && this.ws.readyState === 1) this.ws.send(data); } catch { /* a closing socket: onclose reports it */ }
  }

  private onMic(pcm: Uint8Array) {
    if (this.micFrames++ === 0) this.ev.onFirstMic?.(this.openedAt ? this.now() - this.openedAt : 0);
    if (this.closing || this.now() < this.muteUntil) return;
    if (this.pre) { if (this.preBytes < 8 * 16000 * 2) { this.pre.push(pcm.slice()); this.preBytes += pcm.length; } return; }       // still connecting: keep up to 8 s
    this.send(pcm.byteOffset === 0 && pcm.byteLength === pcm.buffer.byteLength ? pcm : pcm.slice().buffer);
  }

  private onMessage(data: any) {
    if (typeof data === 'string') {
      let d: any; try { d = JSON.parse(data); } catch { return; }
      if (d.t === 'user') { this.ev.onUser?.(String(d.text ?? '')); }
      else if (d.t === 'bot') { try { this.ev.onBot?.(d.result ?? { reply: d.text, intent: d.intent, actions: d.actions ?? [] }); } catch { /* a UI listener bug must not stop the audio */ } if (!d.text) this.setPhase('listening'); }
      else if (d.t === 'clear') { this.queue = []; try { this.dev.flush?.(); } catch { /* nothing to flush */ } }
      else if (d.t === 'state') { if (d.s === 'thinking' && !this.speaking) this.setPhase('thinking'); else if (d.s === 'listening' && !this.speaking) this.setPhase('listening'); }
      return;
    }
    const buf = data instanceof ArrayBuffer ? new Uint8Array(data) : new Uint8Array(data.buffer ?? data);
    if (buf.length < 3 || buf[0] !== 1) return;
    const pcm = buf.subarray(1);
    const step = SLICE_MS * BYTES_PER_MS;
    for (let i = 0; i < pcm.length; i += step) this.queue.push(pcm.slice(i, Math.min(pcm.length, i + step)));
    this.feed();
  }

  private feed() {
    const t = this.now();
    if (this.aheadUntil < t) this.aheadUntil = t;
    while (this.queue.length && this.aheadUntil - t < LEAD_MS) {
      const c = this.queue.shift() as Uint8Array;
      try { this.dev.play(c); } catch { /* the speaker is gone: keep the session alive */ }
      this.aheadUntil += c.length / BYTES_PER_MS;
      this.setPhase('speaking');
    }
    if (!this.queue.length && this.aheadUntil <= t && this.phase === 'speaking') this.setPhase('listening');
  }

  private setPhase(p: Phase) {
    if (p === this.phase) return;
    this.phase = p;
    this.ev.onPhase?.(p);
  }

  private teardown() {
    if (this.feeder) { clearInterval(this.feeder); this.feeder = null; }
    if (this.watchdog) { clearTimeout(this.watchdog); this.watchdog = null; }
    this.queue = [];
    try { this.dev.stop(); } catch { /* already stopped */ }
    try { if (this.ws) { this.ws.onclose = null; this.ws.onmessage = null; this.ws.close(); } } catch { /* already closed */ }
    this.ws = null;
  }

  private finish(reason: 'closed' | 'denied') {
    if (this.closing) return;
    this.closing = true;
    this.teardown();
    this.ev.onClose?.(reason);
  }
}
