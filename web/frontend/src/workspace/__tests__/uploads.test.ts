import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ScanResult } from "../uploads/scan";

// The upload manager's network and hashing are stubbed; what's under test is
// the bookkeeping behind the progress display.
const server = vi.hoisted(() => ({
  posts: [] as { url: string; body: Record<string, unknown> }[],
  onChunk: null as null | ((url: string, blob: Blob, onProgress: (n: number) => void) => Promise<void>),
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
    putWithProgress: (url: string, blob: Blob, onProgress: (n: number) => void) =>
      server.onChunk ? server.onChunk(url, blob, onProgress) : (onProgress(blob.size), Promise.resolve()),
  };
});

const { RateMeter, UploadManager, formatBytes, formatDuration } = await import("../uploads/uploader");

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

function manager() {
  const events = { changed: vi.fn(), problem: vi.fn(), log: vi.fn(), uploaded: vi.fn(),
    confirmReplace: vi.fn(async () => true), quotaExceeded: vi.fn() };
  return { m: new UploadManager("p1", events), events };
}

const settle = () => new Promise((r) => setTimeout(r, 0));
async function untilIdle(m: InstanceType<typeof UploadManager>) {
  for (let i = 0; i < 200 && m.progress().stage !== "done"; i++) await settle();
}

beforeEach(() => {
  vi.stubGlobal("window", globalThis);
  server.posts = [];
  server.onChunk = null;
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

describe("upload progress", () => {
  it("tracks files and bytes for the batch, including the chunk in flight", async () => {
    const { m, events } = manager();
    let release!: () => void;
    let seen = 0;
    server.onChunk = (_url, blob, onProgress) => {
      seen++;
      if (seen === 2) {
        onProgress(blob.size / 2);
        return new Promise((resolve) => (release = () => {
          onProgress(blob.size);
          resolve();
        }));
      }
      onProgress(blob.size);
      return Promise.resolve();
    };

    m.beginScan();
    expect(m.progress().stage).toBe("scanning");
    await m.add(scanOf(["A1_SNAP.tif", 128], ["A1_CCK.tif", 100]));
    for (let i = 0; i < 50 && !release; i++) await settle();

    // The second 64-byte chunk of A1_SNAP is half sent.
    let p = m.progress();
    expect(p.stage).toBe("working");
    expect(p.filesTotal).toBe(2);
    expect(p.bytesTotal).toBe(228);
    expect(p.bytesDone).toBe(64 + 32);
    expect(p.uploading).toEqual({ name: "A1_SNAP.tif", fraction: 0.75 });

    release();
    await untilIdle(m);
    p = m.progress();
    expect(p.stage).toBe("done");
    expect(p.summary).toBe("Done. 2 uploaded (228 B), 0 already up to date.");
    expect(events.uploaded).toHaveBeenCalledTimes(2);

    m.dismissSummary();
    expect(m.progress().stage).toBe("idle");
  });

  it("drops files the server already has from the total, and counts resumed chunks as done", async () => {
    const { m } = manager();
    server.skip.add("A1_CCK.tif");
    m.beginScan();
    await m.add(scanOf(["A1_CCK.tif", 1000], ["A2_SNAP.tif", 100]));
    await untilIdle(m);
    const chunkPuts = server.posts.filter((p) => p.url.endsWith("/complete"));
    expect(chunkPuts).toHaveLength(1); // only A2_SNAP was uploaded
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
