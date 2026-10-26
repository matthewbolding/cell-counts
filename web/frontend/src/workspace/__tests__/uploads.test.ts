import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ScanResult } from "../uploads/scan";

// The upload manager's network and hashing are stubbed; what's under test is
// the bookkeeping behind the progress display and how chunks are scheduled.
// In manual mode every chunk PUT waits until the test settles it.
interface Call {
  url: string;
  signal?: AbortSignal;
  settled: boolean;
  progress(loaded: number): void;
  ok(): void;
  fail(err: unknown): void;
}

const server = vi.hoisted(() => ({
  posts: [] as { url: string; body: Record<string, unknown> }[],
  manual: false,
  calls: [] as Call[],
  skip: new Set<string>(),
}));

vi.mock("../uploads/hasher", () => ({
  hashFile: async (file: File, onProgress?: (f: number) => void) => {
    onProgress?.(0.5);
    return `hash-of-${file.name}`;
  },
}));

vi.mock("../api", async (importOriginal) => {
  const real = await importOriginal<typeof import("../api")>();
  return {
    ...real,
    sleep: async () => {},
    post: async (url: string, body: Record<string, unknown> = {}) => {
      server.posts.push({ url, body });
      if (url.endsWith("/uploads/plan")) {
        return { files: (body.files as { name: string }[]).map((f) => ({ name: f.name, decision: "needs_hash",
          image_id: null, sha256: null })) };
      }
      if (url.endsWith("/uploads/init")) {
        const name = body.filename as string;
        if (server.skip.has(name)) return { action: "skip", image: { id: `img-${name}` } };
        const size = body.size as number;
        return { action: "upload", upload_id: `up-${name}`, chunk_size: 64, total_chunks: Math.ceil(size / 64),
          chunks_present: name === "A2_SNAP.tif" ? [0] : [] };
      }
      if (url.endsWith("/complete")) return { image: { id: "img" } };
      throw new Error(`unexpected POST ${url}`);
    },
    putWithProgress: (url: string, blob: Blob, onProgress: (n: number) => void, signal?: AbortSignal) => {
      if (!server.manual) {
        onProgress(blob.size);
        return Promise.resolve();
      }
      return new Promise<void>((resolve, reject) => {
        const call: Call = {
          url, signal, settled: false,
          progress: (n) => onProgress(n),
          ok: () => {
            call.settled = true;
            onProgress(blob.size);
            resolve();
          },
          fail: (err) => {
            call.settled = true;
            reject(err);
          },
        };
        signal?.addEventListener("abort", () => {
          if (call.settled) return;
          call.settled = true;
          reject(new real.ApiError(0, "aborted", "cancelled"));
        });
        server.calls.push(call);
      });
    },
  };
});

const { ApiError } = await import("../api");
const { LaneTuner } = await import("../uploads/laneTuner");
const { RateMeter, UploadManager, formatBytes, formatDuration } = await import("../uploads/uploader");

const MB = 1024 * 1024;

function scanOf(...files: [string, number][]): ScanResult {
  return {
    recognized: files.map(([name, size]) => {
      const [prefix, channel] = name.replace(".tif", "").split("_");
      return { file: new File([new Uint8Array(size)], name), name, prefix, channel: channel as "SNAP", desktop: null };
    }),
    skipped: [],
    duplicates: [],
    desktopFolders: 0,
  };
}

function manager(tuner = new LaneTuner()) {
  const events = { changed: vi.fn(), problem: vi.fn(), log: vi.fn(), uploaded: vi.fn(),
    confirmReplace: vi.fn(async () => true), quotaExceeded: vi.fn() };
  return { m: new UploadManager("p1", events, tuner), events };
}

const settle = () => new Promise((r) => setTimeout(r, 0));
async function until(check: () => boolean) {
  for (let i = 0; i < 200 && !check(); i++) await settle();
  expect(check()).toBe(true);
}
const untilDone = (m: InstanceType<typeof UploadManager>) => until(() => m.progress().stage === "done");
const open = () => server.calls.filter((c) => !c.settled);
const label = (c: Call) => c.url.split("/up-")[1];

beforeEach(() => {
  vi.stubGlobal("window", globalThis);
  server.posts = [];
  server.manual = false;
  server.calls = [];
  server.skip = new Set();
});
afterEach(() => vi.unstubAllGlobals());

describe("RateMeter", () => {
  it("averages over its window and waits for 2 s of data", () => {
    const meter = new RateMeter(20_000);
    meter.record(0, 0);
    meter.record(1_000_000, 1000);
    expect(meter.rate()).toBeNull();
    meter.record(4_000_000, 4000);
    expect(meter.rate()).toBe(1_000_000);
    meter.record(4_000_000, 30_000); // a stall: the window slides past the old samples
    expect(meter.rate()).toBe(0);
    meter.reset();
    expect(meter.rate()).toBeNull();
  });
});

describe("LaneTuner", () => {
  /** Simulated uploading: `speed` gives bytes/s for a lane count; every lane always busy. */
  function drive(tuner: InstanceType<typeof LaneTuner>, speed: (lanes: number) => number, seconds: number,
    clock = { t: 0, bytes: 0 }) {
    const seen: number[] = [];
    for (const end = clock.t + seconds * 1000; clock.t < end; clock.t += 250) {
      clock.bytes += speed(tuner.lanes) * 0.25;
      tuner.sample(clock.bytes, clock.t, tuner.lanes);
      seen.push(tuner.lanes);
    }
    return seen;
  }

  it("adds lanes while each one helps, then returns to the best count", () => {
    const tuner = new LaneTuner();
    const speed = (lanes: number) => [0, 10, 15, 20, 21, 21, 21][lanes] * MB;
    const clock = { t: 0, bytes: 0 };
    drive(tuner, speed, 40, clock);
    expect(tuner.lanes).toBe(3);
    // Later checks (one more, one fewer) don't stick either.
    const seen = drive(tuner, speed, 300, clock);
    expect(tuner.lanes).toBe(3);
    expect(Math.max(...seen)).toBe(4);
    expect(seen.filter((n) => n === 3).length / seen.length).toBeGreaterThan(0.8);
  });

  it("keeps a slow connection to one lane so chunks finish in time", () => {
    const tuner = new LaneTuner();
    const seen = drive(tuner, () => 1 * MB, 300); // 32 MB chunks take 32 s even alone
    expect(tuner.lanes).toBe(1);
    expect(Math.max(...seen.slice(40))).toBe(1);
  });

  it("settles on fewer lanes when more don't make it faster", () => {
    const tuner = new LaneTuner();
    drive(tuner, () => 12 * MB, 300);
    expect(tuner.lanes).toBe(1);
  });

  it("halves on a failed chunk and ignores stretches where lanes sat idle", () => {
    const tuner = new LaneTuner({ start: 6 });
    tuner.backOff(0);
    expect(tuner.lanes).toBe(3);
    tuner.backOff(1);
    tuner.backOff(2);
    tuner.backOff(3);
    expect(tuner.lanes).toBe(1);

    const idle = new LaneTuner({ start: 2 });
    for (let t = 0, bytes = 0; t < 120_000; t += 250) {
      bytes += 20 * MB * 0.25;
      idle.sample(bytes, t, 1); // only one of the two lanes has work
    }
    expect(idle.lanes).toBe(2);
  });
});

describe("uploads", () => {
  it("keeps as many chunks going as the tuner says, in queue order, across files", async () => {
    server.manual = true;
    const { m, events } = manager(new LaneTuner({ start: 3 }));
    m.beginScan();
    expect(m.progress().stage).toBe("scanning");
    await m.add(scanOf(["A1_SNAP.tif", 256], ["A1_CCK.tif", 64]));
    await until(() => open().length === 3);
    expect(open().map(label)).toEqual(["A1_SNAP.tif/chunks/0", "A1_SNAP.tif/chunks/1", "A1_SNAP.tif/chunks/2"]);

    server.calls[1].progress(32);
    let p = m.progress();
    expect(p.filesTotal).toBe(2);
    expect(p.bytesTotal).toBe(320);
    expect(p.bytesDone).toBe(32);
    expect(p.uploading).toEqual({ name: "A1_SNAP.tif", fraction: 32 / 256, others: 0 });

    server.calls[1].ok(); // chunks can land out of order
    await until(() => server.calls.length === 4);
    expect(label(server.calls[3])).toBe("A1_SNAP.tif/chunks/3");
    expect(open()).toHaveLength(3);

    server.calls[0].ok(); // nothing left of A1_SNAP to start: the next file begins
    await until(() => server.calls.length === 5);
    expect(label(server.calls[4])).toBe("A1_CCK.tif/chunks/0");
    p = m.progress();
    expect(p.bytesDone).toBe(128);
    expect(p.uploading).toMatchObject({ name: "A1_SNAP.tif", others: 1 });

    for (const c of open()) c.ok();
    await untilDone(m);
    expect(m.progress().summary).toBe("Done. 2 uploaded (320 B), 0 already up to date.");
    expect(server.posts.filter((post) => post.url.endsWith("/complete"))).toHaveLength(2);
    expect(events.uploaded).toHaveBeenCalledTimes(2);

    m.dismissSummary();
    expect(m.progress().stage).toBe("idle");
  });

  it("cancels a file's other chunks when one fails for good", async () => {
    server.manual = true;
    const { m, events } = manager(new LaneTuner({ start: 3 }));
    await m.add(scanOf(["A1_SNAP.tif", 256]));
    await until(() => open().length === 3);
    const others = server.calls.slice(1, 3);

    server.calls[0].fail(new ApiError(400, "chunk_length", "chunk 0 should be 64 bytes"));
    await untilDone(m);
    expect(others.every((c) => c.signal?.aborted)).toBe(true);
    expect(server.calls).toHaveLength(3); // nothing more was sent for it
    expect(m.progress().summary).toContain("1 failed");
    expect(events.log).toHaveBeenCalledWith(expect.stringContaining("ERROR uploading A1_SNAP.tif"));
  });

  it("retries a dropped chunk with fewer lanes", async () => {
    server.manual = true;
    const tuner = new LaneTuner({ start: 4 });
    const { m } = manager(tuner);
    await m.add(scanOf(["A1_SNAP.tif", 256]));
    await until(() => open().length === 4);

    server.calls[2].fail(new ApiError(0, "network", "Could not reach the server."));
    await until(() => server.calls.length === 5);
    expect(label(server.calls[4])).toBe("A1_SNAP.tif/chunks/2"); // the same chunk again
    expect(tuner.lanes).toBe(2);

    for (const c of open()) c.ok();
    await untilDone(m);
    expect(m.progress().summary).toBe("Done. 1 uploaded (256 B), 0 already up to date.");
  });

  it("drops files the server already has from the total, and counts resumed chunks as done", async () => {
    const { m } = manager();
    server.skip.add("A1_CCK.tif");
    m.beginScan();
    await m.add(scanOf(["A1_CCK.tif", 1000], ["A2_SNAP.tif", 100]));
    await untilDone(m);
    const completes = server.posts.filter((p) => p.url.endsWith("/complete"));
    expect(completes).toHaveLength(1); // only A2_SNAP was uploaded
    expect(m.progress().summary).toBe("Done. 1 uploaded (100 B), 1 already up to date.");
  });

  it("formats sizes and times the way the strip shows them", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(277 * 1024 ** 2)).toBe("277 MB");
    expect(formatBytes(28.4 * 1024 ** 3)).toBe("28 GB");
    expect(formatDuration(30)).toBe("less than a minute");
    expect(formatDuration(9 * 60)).toBe("about 9 min");
    expect(formatDuration(2 * 3600 + 10 * 60)).toBe("about 2 h 10 min");
  });
});
