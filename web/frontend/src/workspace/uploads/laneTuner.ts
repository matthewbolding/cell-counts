// laneTuner.ts — how many chunks to send at once ("lanes").
//
// One upload stream rarely fills a connection: per-stream flow control, the
// pause while each chunk is written and acknowledged, and Wi-Fi loss all leave
// room that a second or third stream can use. Past some point, more streams
// only split the same bandwidth and make every chunk slower. Where that point
// is depends on the connection and drifts over time, so it's found by
// measuring: hold a count for a few seconds, compare the throughput with the
// count before, step up while that helps by a clear margin, step back when it
// doesn't, and test again every so often. A failed chunk halves the count.
//
// A chunk may not take much over half a minute at the measured speed. That
// keeps slow connections at one or two streams, and every request far from
// Cloudflare's ~100 s limit.

export interface LaneTunerOptions {
  min: number;
  max: number;
  start: number;
  chunkBytes: number;
  /** Most a chunk may take at the measured per-stream speed. */
  maxChunkSeconds: number;
  /** How long each count is measured. */
  epochMs: number;
  /** Ignored after a change, while new streams get up to speed. */
  warmupMs: number;
  /** A step up has to beat the count before it by this fraction to stick. */
  gain: number;
  /** How often a settled count is tested again. */
  reprobeMs: number;
}

export const LANE_DEFAULTS: LaneTunerOptions = {
  min: 1,
  max: 6,
  start: 2,
  chunkBytes: 32 * 1024 * 1024,
  maxChunkSeconds: 30,
  epochMs: 8000,
  warmupMs: 2000,
  gain: 0.1,
  reprobeMs: 60_000,
};

export class LaneTuner {
  lanes: number;
  chunkBytes: number;
  private readonly o: LaneTunerOptions;
  /** The settled count and its latest throughput (bytes/s). */
  private base: { lanes: number; rate: number } | null = null;
  private probe: "up" | "down" | null = null;
  private nextProbe: "up" | "down" = "up";
  private settledAt = Number.NEGATIVE_INFINITY; // probe straight away at the start
  private changedAt = Number.NEGATIVE_INFINITY;
  private epoch: { t: number; bytes: number } | null = null;
  private starved = false;

  constructor(options: Partial<LaneTunerOptions> = {}) {
    this.o = { ...LANE_DEFAULTS, ...options };
    this.lanes = this.o.start;
    this.chunkBytes = this.o.chunkBytes;
  }

  /** Called as bytes go out: the total sent so far (never shrinks within an
   * epoch) and how many chunks are on the wire right now. */
  sample(totalBytes: number, nowMs: number, busy: number): void {
    if (nowMs - this.changedAt < this.o.warmupMs) return;
    if (!this.epoch) {
      this.epoch = { t: nowMs, bytes: totalBytes };
      this.starved = false;
      return;
    }
    if (busy < this.lanes) this.starved = true;
    const elapsed = nowMs - this.epoch.t;
    if (elapsed < this.o.epochMs) return;
    const rate = Math.max(0, totalBytes - this.epoch.bytes) / (elapsed / 1000);
    const starved = this.starved;
    this.epoch = { t: nowMs, bytes: totalBytes };
    this.starved = false;
    // Not every lane had a chunk to send (the end of a batch, a file waiting
    // to be checked): that says nothing about the count.
    if (!starved) this.decide(rate, nowMs);
  }

  /** A chunk failed and will be retried: ease off. */
  backOff(nowMs: number): void {
    this.base = null;
    this.probe = null;
    this.nextProbe = "up";
    this.settledAt = nowMs;
    this.set(Math.max(this.o.min, Math.ceil(this.lanes / 2)), nowMs);
    this.epoch = null; // the stall isn't the new count's fault
    this.changedAt = nowMs;
  }

  /** Measurements so far no longer apply (after a pause, or a file given up on). */
  restart(nowMs: number): void {
    if (this.probe && this.base) {
      this.probe = null;
      this.settledAt = nowMs;
      this.set(this.base.lanes, nowMs);
    }
    this.epoch = null;
    this.changedAt = nowMs;
  }

  private decide(rate: number, now: number): void {
    const n = this.lanes;
    if (!this.fits(n, rate)) {
      // Chunks are taking too long: as many lanes as the speed allows.
      this.base = null;
      this.probe = null;
      this.settledAt = now;
      let fit = n - 1;
      while (fit > this.o.min && !this.fits(fit, rate)) fit--;
      this.set(Math.max(this.o.min, fit), now);
      return;
    }

    if (this.probe === "up" && this.base) {
      if (rate >= this.base.rate * (1 + this.o.gain)) {
        this.base = { lanes: n, rate };
        if (!this.tryUp(rate, now)) this.settle(now);
      } else {
        this.set(this.base.lanes, now);
        this.nextProbe = "down";
        this.settle(now);
      }
      return;
    }
    if (this.probe === "down" && this.base) {
      // Fewer streams for (nearly) the same speed is better.
      if (rate >= this.base.rate * (1 - this.o.gain / 2)) this.base = { lanes: n, rate };
      else this.set(this.base.lanes, now);
      this.nextProbe = "up";
      this.settle(now);
      return;
    }

    this.base = { lanes: n, rate };
    if (now - this.settledAt < this.o.reprobeMs) return;
    const tried = this.nextProbe === "up"
      ? this.tryUp(rate, now) || this.tryDown(now)
      : this.tryDown(now) || this.tryUp(rate, now);
    if (!tried) this.settle(now);
  }

  private tryUp(rate: number, now: number): boolean {
    const n = this.lanes + 1;
    if (n > this.o.max || !this.fits(n, rate)) return false;
    this.probe = "up";
    this.set(n, now);
    return true;
  }

  private tryDown(now: number): boolean {
    const n = this.lanes - 1;
    if (n < this.o.min) return false;
    this.probe = "down";
    this.set(n, now);
    return true;
  }

  /** Whether each of `n` streams would still finish a chunk in time. */
  private fits(n: number, rate: number): boolean {
    return n <= 1 || (rate / n) * this.o.maxChunkSeconds >= this.chunkBytes;
  }

  private settle(now: number): void {
    this.probe = null;
    this.settledAt = now;
  }

  private set(n: number, now: number): void {
    if (n === this.lanes) return;
    this.lanes = n;
    this.changedAt = now;
    this.epoch = null;
  }
}
