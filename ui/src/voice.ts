// Microphone capture + playback. Recording produces a webm/opus Blob that the
// backend transcribes with faster-whisper. An AnalyserNode exposes a live level
// (0..1) so the hologram can pulse with your voice.

export class Recorder {
  private media: MediaRecorder | null = null;
  private chunks: Blob[] = [];
  private stream: MediaStream | null = null;
  private audioCtx: AudioContext | null = null;
  private analyser: AnalyserNode | null = null;
  private data: Uint8Array | null = null;

  get level(): number {
    if (!this.analyser || !this.data) return 0;
    this.analyser.getByteTimeDomainData(this.data);
    let sum = 0;
    for (let i = 0; i < this.data.length; i++) {
      const v = (this.data[i] - 128) / 128;
      sum += v * v;
    }
    return Math.min(1, Math.sqrt(sum / this.data.length) * 3);
  }

  async start(): Promise<void> {
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    this.chunks = [];
    this.media = new MediaRecorder(this.stream, { mimeType: "audio/webm" });
    this.media.ondataavailable = (e) => e.data.size > 0 && this.chunks.push(e.data);
    this.media.start();

    this.audioCtx = new AudioContext();
    const src = this.audioCtx.createMediaStreamSource(this.stream);
    this.analyser = this.audioCtx.createAnalyser();
    this.analyser.fftSize = 512;
    this.data = new Uint8Array(this.analyser.fftSize);
    src.connect(this.analyser);
  }

  async stop(): Promise<Blob> {
    return new Promise((resolve) => {
      if (!this.media) return resolve(new Blob());
      this.media.onstop = () => {
        const blob = new Blob(this.chunks, { type: "audio/webm" });
        this.cleanup();
        resolve(blob);
      };
      this.media.stop();
    });
  }

  private cleanup() {
    this.stream?.getTracks().forEach((t) => t.stop());
    this.audioCtx?.close().catch(() => {});
    this.stream = null;
    this.audioCtx = null;
    this.analyser = null;
    this.media = null;
  }
}

// Decode recorded webm/opus and re-encode as 16kHz mono PCM WAV, so server-side
// STT (faster-whisper) decodes it reliably without needing opus codecs.
export async function toWav16k(blob: Blob): Promise<Blob> {
  const arr = await blob.arrayBuffer();
  const AC: typeof AudioContext = (window as any).AudioContext || (window as any).webkitAudioContext;
  const ctx = new AC();
  const decoded = await ctx.decodeAudioData(arr);
  ctx.close().catch(() => {});
  const target = 16000;
  const len = decoded.length;
  const ch = decoded.numberOfChannels;
  const mono = new Float32Array(len);
  for (let c = 0; c < ch; c++) {
    const d = decoded.getChannelData(c);
    for (let i = 0; i < len; i++) mono[i] += d[i] / ch;
  }
  const ratio = decoded.sampleRate / target;
  const outLen = Math.max(1, Math.floor(len / ratio));
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const idx = i * ratio, i0 = Math.floor(idx), i1 = Math.min(i0 + 1, len - 1), fr = idx - i0;
    out[i] = mono[i0] * (1 - fr) + mono[i1] * fr;
  }
  const buf = new ArrayBuffer(44 + outLen * 2);
  const view = new DataView(buf);
  const ws = (o: number, s: string) => { for (let i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i)); };
  ws(0, "RIFF"); view.setUint32(4, 36 + outLen * 2, true); ws(8, "WAVE"); ws(12, "fmt ");
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, target, true); view.setUint32(28, target * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  ws(36, "data"); view.setUint32(40, outLen * 2, true);
  let off = 44;
  for (let i = 0; i < outLen; i++) { const s = Math.max(-1, Math.min(1, out[i])); view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true); off += 2; }
  return new Blob([view], { type: "audio/wav" });
}

// Interruptible, sentence-streaming speech. `cancel()` stops current playback
// (barge-in); `stream()` synthesizes sentence-by-sentence and plays the first as
// soon as it's ready while prefetching the next. A monotonic token invalidates
// any in-flight playback when a newer utterance starts or cancel() is called.
export class SpeechController {
  private token = 0;
  private audio: HTMLAudioElement | null = null;
  private ctx: AudioContext | null = null;   // a single warm context, reused across clips

  constructor(
    private synth: (t: string) => Promise<Blob | null>,
    private onLevel: (l: number) => void,
    private onCaption?: (s: string) => void
  ) {}

  private audioCtx(): AudioContext | null {
    try {
      if (!this.ctx) this.ctx = new (window.AudioContext || (window as any).webkitAudioContext)();
    } catch {
      return null;
    }
    return this.ctx;
  }

  cancel() {
    this.token++;
    this.queue = [];
    this.busy = false;
    if (this.audio) { try { this.audio.pause(); } catch {} this.audio = null; }
    this.onLevel(0);  // keep this.ctx warm so the next clip's first word isn't clipped
  }

  private play(blob: Blob | null, token: number, text = ""): Promise<void> {
    return new Promise((res) => {
      if (token !== this.token) return res();
      if (!blob) { this.onCaption?.(text); return res(); }   // no TTS: show text statically
      const url = URL.createObjectURL(blob);
      const a = new Audio(url);
      a.preload = "auto";
      this.audio = a;
      let raf = 0;
      const done = () => {
        cancelAnimationFrame(raf);
        if (this.audio === a) this.audio = null;
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        res();
      };
      const ctx = this.audioCtx();
      if (ctx) {
        try {
          const src = ctx.createMediaElementSource(a);
          const an = ctx.createAnalyser();
          an.fftSize = 256;
          const buf = new Uint8Array(an.fftSize);
          src.connect(an);
          an.connect(ctx.destination);
          const tick = () => {
            if (token !== this.token) { done(); return; }   // barge-in -> resolve
            an.getByteTimeDomainData(buf);
            let s = 0;
            for (let i = 0; i < buf.length; i++) { const v = (buf[i] - 128) / 128; s += v * v; }
            this.onLevel(Math.min(1, Math.sqrt(s / buf.length) * 3));
            raf = requestAnimationFrame(tick);
          };
          tick();
        } catch {}
      }
      a.onended = done;
      a.onerror = done;
      const start = () => { if (token !== this.token) return done(); a.play().catch(done); };
      if (ctx && ctx.state === "suspended") ctx.resume().then(start).catch(start);
      else start();
    });
  }

  // Karaoke-style "tape": advance a small window of words over the clip duration.
  private startTape(text: string, duration: number): any {
    if (!this.onCaption) return 0;
    const words = (text || "").split(/\s+/).filter(Boolean);
    if (!words.length) return 0;
    const per = Math.max(0.13, ((duration && isFinite(duration)) ? duration : words.length * 0.38) / words.length);
    let i = 0;
    const render = () => {
      const lo = Math.max(0, i - 2), hi = Math.min(words.length, i + 3);
      this.onCaption!(words.slice(lo, hi).join(" "));
      i++; if (i > words.length) clearInterval(id);
    };
    render();
    const id = setInterval(render, per * 1000);
    return id;
  }

  split(t: string): string[] {
    return (t.match(/[^.!?]+[.!?]+|\S[^.!?]*$/g) || [t]).map((s) => s.trim()).filter(Boolean);
  }

  // FIFO queue: enqueue sentences as they stream in; they synthesize+play in order
  // (next synthesized while current plays). cancel() clears the queue.
  private queue: string[] = [];
  private busy = false;
  private onIdle: (() => void) | null = null;

  setOnIdle(cb: () => void) { this.onIdle = cb; }

  enqueue(text: string) {
    if (text && text.trim()) {
      this.queue.push(text.trim());
      if (!this.busy) void this.drain();
    }
  }

  private async drain() {
    this.busy = true;
    const token = this.token;
    let curText: string | null = this.queue.length ? this.queue.shift()! : null;
    let pending = curText != null ? this.synth(curText).catch(() => null) : null;
    while (pending) {
      if (token !== this.token) { this.busy = false; return; }
      const blob = await pending;
      const text = curText ?? "";
      curText = this.queue.length ? this.queue.shift()! : null;
      pending = curText != null ? this.synth(curText).catch(() => null) : null;
      if (token !== this.token) { this.busy = false; return; }
      await this.play(blob, token, text);
    }
    this.busy = false;
    if (token === this.token) { this.onLevel(0); this.onIdle?.(); }
  }

  get speaking() { return this.busy || this.queue.length > 0; }
}

// Play WAV bytes; resolves when playback ends. onLevel reports a coarse level so
// the hologram can pulse while speaking.
export function playWav(blob: Blob, onLevel?: (lvl: number) => void): Promise<void> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    let raf = 0;
    if (onLevel) {
      const ctx = new AudioContext();
      const src = ctx.createMediaElementSource(audio);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      const buf = new Uint8Array(analyser.fftSize);
      src.connect(analyser);
      analyser.connect(ctx.destination);
      const tick = () => {
        analyser.getByteTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) {
          const v = (buf[i] - 128) / 128;
          sum += v * v;
        }
        onLevel(Math.min(1, Math.sqrt(sum / buf.length) * 3));
        raf = requestAnimationFrame(tick);
      };
      tick();
      audio.onended = () => {
        cancelAnimationFrame(raf);
        ctx.close().catch(() => {});
        URL.revokeObjectURL(url);
        onLevel(0);
        resolve();
      };
    } else {
      audio.onended = () => {
        URL.revokeObjectURL(url);
        resolve();
      };
    }
    audio.play().catch(() => resolve());
  });
}
