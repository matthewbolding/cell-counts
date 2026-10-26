// uploader.ts — File › Add Images…, end to end (DESIGN.md, "Upload flow").
//
// Mirrors the desktop client's two concurrent halves: a hashing loop (plan → hash
// → init) and the uploading (chunks → complete) run side by side, so the first
// file starts uploading while later ones are still being hashed. The plan step
// is the desktop's fast path: a file whose name, size and modification time
// match what's already on the server is trusted without hashing.
//
// Several chunks go up at once, each in its own "lane"; LaneTuner picks how
// many for this connection. Lanes take chunks in queue order, so the tail of
// one file overlaps the start of the next, and a file is completed as soon as
// its last chunk lands.
//
// Desktop review work rides along: if the folder has the desktop app's
// cellcounts.json and .cells.json files and a file's recorded sha256 matches,
// its cells go up with it and it isn't re-segmented. If the file is already on
// the server (uploaded earlier without them), the cells are imported into it,
// unless someone has already reviewed that image in the web app.
//
// Folders can be tens of gigabytes, so progress is tracked for the whole batch —
// files and bytes done, what's being hashed and uploaded right now, speed and
// time left — and published through progress() for the Uploads panel.
//
// Interruptions are expected — closing the laptop, flaky Wi-Fi, the tab
// closing. Every chunk is retried with backoff; anything not finished when the
// page goes away resumes the next time the folder is added, because `init`
// reports which chunks the server already has.

import { ApiError, errorMessage, post, putWithProgress, request, sleep } from "../api";
import type { Channel } from "../constants";
import { moveSelection, type Move } from "../queueOrder";
import type { ImageInfo } from "../types";
import { hashFile } from "./hasher";
import { LaneTuner } from "./laneTuner";
import { desktopMatch, readDesktopCells, type DesktopRef, type Recognized, type ScanResult } from "./scan";

const CHUNK_RETRY_MS = [1000, 2000, 5000, 10000, 20000, 30000];
const MAX_CHUNK_ATTEMPTS = 8;
const RATE_WINDOW_MS = 20_000; // speed is averaged over this much recent uploading
const NOTIFY_MS = 200; // at most this often for progress-only updates

type Phase = "hash" | "hashing" | "conflict" | "ready" | "uploading";

export interface UploadItem {
  key: string;
  name: string;
  prefix: string;
  channel: Channel;
  file: File;
  phase: Phase;
  replace: boolean;
  sha256?: string;
  uploadId?: string;
  chunkSize?: number;
  totalChunks?: number;
  present?: Set<number>;
  /** Chunks not handed to a lane yet, in order. */
  queue?: number[];
  /** Cancels this file's chunk requests when it's given up on. */
  cancel?: AbortController;
  /** Bumped whenever the file's upload is given up on or started over, so
   * requests still finishing for the old attempt are ignored. */
  attempt: number;
  desktop: DesktopRef | null;
  /** 0..1 — hashing progress while hashing, upload progress while uploading. */
  fraction: number;
  /** Bytes of this file already on the server (finished chunks). */
  sent: number;
}

export interface UploadProgress {
  stage: "idle" | "scanning" | "checking" | "working" | "done";
  paused: boolean;
  filesTotal: number; // files in this batch that needed checking
  filesDone: number; // ...uploaded, found identical, left as they were, or failed
  failed: number;
  bytesTotal: number; // bytes this batch has to upload (shrinks as identical files turn up)
  bytesDone: number;
  hashed: number;
  hashing: { name: string; fraction: number } | null;
  /** The first file going up, and how many others are going up alongside it. */
  uploading: { name: string; fraction: number; others: number } | null;
  bytesPerSecond: number | null;
  etaSeconds: number | null;
  summary: string | null; // set once a batch finishes
}

interface PlanFile {
  name: string;
  decision: "unchanged" | "needs_hash";
  image_id: string | null;
  sha256: string | null;
}

interface InitResponse {
  action: "upload" | "skip";
  upload_id?: string;
  chunk_size?: number;
  total_chunks?: number;
  chunks_present?: number[];
  image?: ImageInfo;
}

interface ImportSummary {
  cells: number;
  skipped: number;
}

interface ImportResponse {
  result: "imported" | "unchanged";
  cells?: number;
  skipped?: number;
  image?: ImageInfo;
}

export interface UploadEvents {
  /** Something changed: items, phases, or just progress (throttled). */
  changed(): void;
  /** Something the person has to know about (shown as a dialog). Progress is
   * never reported this way: it's all in progress(), shown by the upload strip. */
  problem(title: string, message: string): void;
  log(message: string): void;
  uploaded(image: ImageInfo): void;
  /** Ask once about every file whose content differs from what's on the server. */
  confirmReplace(names: string[]): Promise<boolean>;
  quotaExceeded(message: string): void;
}

interface Batch {
  filesTotal: number;
  filesDone: number;
  bytesTotal: number;
  bytesDone: number; // finished chunks, including ones the server already had
  hashed: number;
  upToDate: number;
  uploaded: number;
  failed: number;
  kept: number;
  desktop: number;
}

const newBatch = (): Batch => ({
  filesTotal: 0, filesDone: 0, bytesTotal: 0, bytesDone: 0, hashed: 0, upToDate: 0, uploaded: 0, failed: 0,
  kept: 0, desktop: 0,
});

export class UploadManager {
  items: UploadItem[] = [];
  paused = false;
  private readonly projectId: string;
  private readonly events: UploadEvents;
  private hashing = false;
  private lanesRunning = 0;
  private completing = 0;
  private importing = 0;
  private stage: UploadProgress["stage"] = "idle";
  private summary: string | null = null;
  private batch = newBatch();
  /** Chunk requests on the wire, by file, attempt and index: bytes sent so far. */
  private readonly wire = new Map<string, { item: UploadItem; loaded: number }>();
  private readonly meter = new RateMeter(RATE_WINDOW_MS);
  private readonly tuner: LaneTuner;
  private sentThisSession = 0; // bytes actually sent (excludes resumed chunks), for the speed
  private resumeWaiters: (() => void)[] = [];
  private notifyTimer = 0;

  constructor(projectId: string, events: UploadEvents, tuner = new LaneTuner()) {
    this.projectId = projectId;
    this.events = events;
    this.tuner = tuner;
  }

  get active(): boolean {
    return this.items.length > 0 || this.stage === "scanning" || this.stage === "checking";
  }

  /** For the readiness dots: a file for this slot is waiting or uploading. */
  isPending(prefix: string, channel: Channel): boolean {
    return this.items.some((i) => i.prefix === prefix && i.channel === channel);
  }

  progress(): UploadProgress {
    const b = this.batch;
    const hashingItem = this.items.find((i) => i.phase === "hashing");
    const uploadingItems = this.items.filter((i) => i.phase === "uploading");
    const first = uploadingItems[0];
    const rate = this.meter.rate();
    const bytesDone = Math.min(b.bytesTotal, b.bytesDone + this.inFlightBytes());
    const remaining = Math.max(0, b.bytesTotal - bytesDone);
    return {
      stage: this.stage,
      paused: this.paused,
      filesTotal: b.filesTotal,
      filesDone: b.filesDone,
      failed: b.failed,
      bytesTotal: b.bytesTotal,
      bytesDone,
      hashed: b.hashed,
      hashing: hashingItem ? { name: hashingItem.name, fraction: hashingItem.fraction } : null,
      uploading: first ? { name: first.name, fraction: first.fraction, others: uploadingItems.length - 1 } : null,
      bytesPerSecond: rate,
      etaSeconds: rate && !this.paused ? remaining / rate : null,
      summary: this.summary,
    };
  }

  /** The Uploads panel's Close button after a batch has finished. */
  dismissSummary(): void {
    if (this.stage !== "done") return;
    this.stage = "idle";
    this.summary = null;
    this.events.changed();
  }

  /** The folder picker returned; reading a big folder's entries takes a moment. */
  beginScan(): void {
    if (this.stage === "idle" || this.stage === "done") {
      this.batch = newBatch();
      this.summary = null;
    }
    this.stage = "scanning";
    this.events.changed();
  }

  pause(): void {
    this.paused = true;
    this.events.changed();
  }

  resume(): void {
    this.paused = false;
    this.meter.reset(); // don't average the pause into the speed
    this.tuner.restart(performance.now());
    for (const wake of this.resumeWaiters.splice(0)) wake();
    this.events.changed();
    this.kick();
  }

  /** ▲/▼ in the Queue panel for local rows. Files already uploading stay put. */
  reorder(keys: Set<string>, how: Move): void {
    const fixed = this.items.filter((i) => i.phase === "uploading");
    const movable = this.items.filter((i) => i.phase !== "uploading");
    this.items = [...fixed, ...moveSelection(movable, (i) => keys.has(i.key), how)];
    this.events.changed();
  }

  // ------------------------------------------------------------------ //
  async add(scan: ScanResult): Promise<void> {
    const { recognized, skipped, duplicates } = scan;
    if (skipped.length) {
      this.events.log(`Skipped ${skipped.length} file(s) with unrecognized names:`);
      for (const name of skipped) this.events.log(`  - ${name}`);
    }
    for (const name of duplicates) this.events.log(`Skipped ${name}: another file in this folder has the same sample and channel.`);
    if (!recognized.length) {
      this.stage = this.items.length ? "working" : "idle";
      this.events.problem("Add Images", "There are no image files named like A1_SNAP.tif, A1_CCK.tif or A1_CHR.tif "
        + "in that folder.");
      this.events.changed();
      return;
    }

    const already = new Set(this.items.map((i) => i.name));
    const fresh = recognized.filter((r) => !already.has(r.name));
    this.stage = "checking";
    this.events.changed();
    let plan: { files: PlanFile[] };
    try {
      plan = await post(`/api/projects/${this.projectId}/uploads/plan`, {
        files: fresh.map((r) => ({ name: r.name, size: r.file.size, last_modified: r.file.lastModified })),
      });
    } catch (err) {
      this.stage = this.items.length ? "working" : "idle";
      this.events.problem("Add Images", `The upload couldn't start: ${errorMessage(err)}`);
      this.events.changed();
      return;
    }

    const decisions = new Map(plan.files.map((f) => [f.name, f]));
    let unchanged = 0;
    const toImport: { name: string; imageId: string; sha256: string; ref: DesktopRef }[] = [];
    for (const r of fresh) {
      const d = decisions.get(r.name);
      if (d?.decision === "unchanged") {
        unchanged++;
        // Already on the server, so it won't be hashed: match desktop review
        // data against the server's copy of the hash instead.
        const ref = desktopMatch(r.desktop, d.sha256);
        if (ref && d.image_id && d.sha256) toImport.push({ name: r.name, imageId: d.image_id, sha256: d.sha256, ref });
        continue;
      }
      this.items.push(this.newItem(r));
      this.batch.filesTotal++;
      this.batch.bytesTotal += r.file.size;
    }
    this.batch.upToDate += unchanged;
    if (scan.desktopFolders) {
      this.events.log(`Found desktop review data (${scan.desktopFolders} cellcounts.json file(s)); it will be `
        + "brought in for every image whose file hasn't changed since.");
    }
    this.events.log(`Found ${recognized.length} recognized image(s); ${unchanged} already up to date, `
      + `${fresh.length - unchanged} to check (${formatBytes(this.batch.bytesTotal)}).`);
    this.stage = "working";
    this.events.changed();
    this.importing++;
    this.kick();
    try {
      for (const task of toImport) await this.importIntoExisting(task.name, task.imageId, task.sha256, task.ref);
    } finally {
      this.importing--;
      this.finishIfIdle();
    }
  }

  private newItem(r: Recognized): UploadItem {
    return { key: `upload:${r.name}`, name: r.name, prefix: r.prefix, channel: r.channel, file: r.file,
      phase: "hash", replace: false, attempt: 0, desktop: r.desktop, fraction: 0, sent: 0 };
  }

  private kick(): void {
    if (!this.hashing) void this.hashLoop();
    this.pump();
  }

  /** Progress-only change: coalesce bursts (chunk progress fires constantly). */
  private notify(): void {
    if (this.notifyTimer) return;
    this.notifyTimer = window.setTimeout(() => {
      this.notifyTimer = 0;
      this.events.changed();
    }, NOTIFY_MS);
  }

  // ------------------------------------------------------------------ //
  // Hashing half: hash → init
  // ------------------------------------------------------------------ //
  private async hashLoop(): Promise<void> {
    this.hashing = true;
    try {
      for (;;) {
        const item = this.items.find((i) => i.phase === "hash");
        if (item) {
          await this.hashAndInit(item);
          continue;
        }
        // Everything hashed: ask once about all the files whose content
        // differs from the server's copy, then carry on with the ones to replace.
        const conflicts = this.items.filter((i) => i.phase === "conflict");
        if (!conflicts.length) break;
        const replace = await this.events.confirmReplace(conflicts.map((i) => i.name));
        for (const c of conflicts) {
          if (replace) {
            c.replace = true;
            c.phase = "hash";
          } else {
            this.batch.kept++;
            this.settle(c);
          }
        }
        this.events.changed();
      }
    } finally {
      this.hashing = false;
      this.finishIfIdle();
    }
  }

  private async hashAndInit(item: UploadItem): Promise<void> {
    if (!item.sha256) {
      item.phase = "hashing";
      item.fraction = 0;
      this.batch.hashed++;
      this.events.changed();
      try {
        item.sha256 = await hashFile(item.file, (f) => {
          item.fraction = f;
          this.notify();
        });
      } catch (err) {
        this.fail(item, `couldn't read the file (${errorMessage(err)})`);
        return;
      }
      item.phase = "hash";
      item.fraction = 0;
    }
    await this.init(item);
  }

  private async init(item: UploadItem): Promise<void> {
    try {
      const r = await post<InitResponse>(`/api/projects/${this.projectId}/uploads/init`, {
        filename: item.name, size: item.file.size, sha256: item.sha256,
        last_modified: item.file.lastModified, replace: item.replace,
      });
      if (r.action === "skip") {
        this.batch.upToDate++;
        this.settle(item);
        const ref = desktopMatch(item.desktop, item.sha256);
        if (ref && r.image) await this.importIntoExisting(item.name, r.image.id, item.sha256!, ref);
        return;
      }
      item.uploadId = r.upload_id;
      item.chunkSize = r.chunk_size;
      item.totalChunks = r.total_chunks;
      item.present = new Set(r.chunks_present ?? []);
      // Chunks the server already has (an interrupted upload resuming) count as done.
      const resumed = [...item.present].reduce((sum, i) => sum + this.chunkLength(item, i), 0);
      this.batch.bytesDone += resumed - item.sent;
      item.sent = resumed;
      item.fraction = item.file.size ? resumed / item.file.size : 0;
      item.phase = "ready";
      this.events.changed();
      this.pump();
    } catch (err) {
      if (err instanceof ApiError && err.code === "changed") {
        item.phase = "conflict";
        this.events.changed();
      } else if (err instanceof ApiError && err.code === "quota_exceeded") {
        this.stopAll(err.message);
      } else if (err instanceof ApiError && err.isRetryable) {
        item.phase = "hash"; // sha256 is kept; just try init again shortly
        await sleep(5000);
      } else {
        this.fail(item, errorMessage(err));
      }
    }
  }

  // ------------------------------------------------------------------ //
  // Upload half: chunks → complete, several chunks at a time
  // ------------------------------------------------------------------ //
  /** Start lanes until there are as many as the tuner wants, or no more work. */
  private pump(): void {
    while (this.lanesRunning < this.tuner.lanes && this.hasWork()) {
      this.lanesRunning++;
      void this.lane();
    }
  }

  private hasWork(): boolean {
    return this.items.some((i) => i.phase === "ready" || (i.phase === "uploading" && i.queue!.length > 0));
  }

  /** One chunk at a time, until there's nothing left or the tuner wants fewer lanes. */
  private async lane(): Promise<void> {
    try {
      for (;;) {
        if (this.paused) await this.waitForResume();
        if (this.lanesRunning > this.tuner.lanes) return;
        const next = this.nextChunk();
        if (!next) return;
        await this.sendChunk(next.item, next.index);
      }
    } finally {
      this.lanesRunning--;
      this.finishIfIdle();
    }
  }

  /** The next chunk in queue order: the files already going up first, then the next file that's ready. */
  private nextChunk(): { item: UploadItem; index: number } | null {
    for (const item of this.items) {
      if (item.phase === "ready") this.startItem(item);
      if (item.phase === "uploading" && item.queue!.length) return { item, index: item.queue!.shift()! };
    }
    return null;
  }

  private startItem(item: UploadItem): void {
    item.phase = "uploading";
    item.cancel = new AbortController();
    item.queue = [];
    for (let i = 0; i < item.totalChunks!; i++) if (!item.present!.has(i)) item.queue.push(i);
    this.tuner.chunkBytes = item.chunkSize!;
    this.events.changed();
    // Every chunk arrived before an interruption: only the completion is left.
    if (!item.queue.length) void this.finishItem(item);
  }

  private waitForResume(): Promise<void> {
    this.events.changed();
    return new Promise((resolve) => this.resumeWaiters.push(resolve));
  }

  private chunkLength(item: UploadItem, index: number): number {
    const size = item.chunkSize!;
    return Math.max(0, Math.min(size, item.file.size - index * size));
  }

  private async sendChunk(item: UploadItem, index: number): Promise<void> {
    const attempt = item.attempt;
    const size = item.chunkSize!;
    const blob = item.file.slice(index * size, Math.min(item.file.size, (index + 1) * size));
    const key = `${item.key}:${attempt}:${index}`;
    for (let tries = 0; ; tries++) {
      this.wire.set(key, { item, loaded: 0 });
      try {
        await putWithProgress(`/api/uploads/${item.uploadId}/chunks/${index}`, blob, (loaded) => {
          if (item.attempt !== attempt) return;
          this.wire.set(key, { item, loaded });
          this.progressed(item);
        }, item.cancel!.signal);
      } catch (err) {
        this.wire.delete(key);
        if (item.attempt !== attempt) return; // the file was given up on meanwhile
        if (!(err instanceof ApiError) || !err.isRetryable || tries + 1 >= MAX_CHUNK_ATTEMPTS) {
          this.abandon(item, err);
          return;
        }
        this.tuner.backOff(performance.now());
        this.meter.reset(); // the strip shows "waiting for the connection" meanwhile
        await sleep(CHUNK_RETRY_MS[Math.min(tries, CHUNK_RETRY_MS.length - 1)]);
        if (item.attempt !== attempt) return;
        if (this.paused) await this.waitForResume();
        continue;
      }
      this.wire.delete(key);
      if (item.attempt !== attempt) return;
      item.present!.add(index);
      item.sent += blob.size;
      this.batch.bytesDone += blob.size;
      this.sentThisSession += blob.size;
      this.progressed(item);
      if (item.present!.size === item.totalChunks) void this.finishItem(item);
      return;
    }
  }

  /** Bytes moved: the file's share, the speed, and the tuner, which may want more lanes. */
  private progressed(item: UploadItem): void {
    let inItem = 0;
    for (const w of this.wire.values()) if (w.item === item) inItem += w.loaded;
    item.fraction = item.file.size ? (item.sent + inItem) / item.file.size : 1;
    const now = performance.now();
    const total = this.sentThisSession + this.inFlightBytes();
    this.meter.record(total, now);
    const lanes = this.tuner.lanes;
    this.tuner.sample(total, now, this.wire.size);
    if (this.tuner.lanes > lanes) this.pump();
    this.notify();
  }

  private inFlightBytes(): number {
    let n = 0;
    for (const w of this.wire.values()) n += w.loaded;
    return n;
  }

  private async finishItem(item: UploadItem): Promise<void> {
    const attempt = item.attempt;
    this.completing++;
    try {
      const image = await this.complete(item);
      if (item.attempt !== attempt) return;
      this.batch.uploaded++;
      this.settle(item, false);
      this.events.uploaded(image);
    } catch (err) {
      if (item.attempt === attempt) this.abandon(item, err);
    } finally {
      this.completing--;
      this.finishIfIdle();
    }
  }

  /** A chunk or the completion failed for good. The file's other chunks are
   * cancelled; it starts over if the server forgot the upload, otherwise it fails. */
  private abandon(item: UploadItem, err: unknown): void {
    item.attempt++;
    item.cancel?.abort();
    for (const [key, w] of this.wire) if (w.item === item) this.wire.delete(key);
    this.tuner.restart(performance.now());
    if (err instanceof ApiError && err.status === 404) {
      // The server forgot this upload (abandoned for a week): start over.
      this.batch.bytesDone -= item.sent;
      item.sent = 0;
      item.fraction = 0;
      item.phase = "hash";
      item.uploadId = undefined;
      item.queue = undefined;
      this.events.changed();
      if (!this.hashing) void this.hashLoop();
    } else {
      this.fail(item, errorMessage(err));
    }
  }

  private async complete(item: UploadItem): Promise<ImageInfo> {
    const url = `/api/uploads/${item.uploadId}/complete`;
    const imported = await this.desktopCells(item.name, desktopMatch(item.desktop, item.sha256));
    if (imported) {
      const body = await gzipJson({ imported_cells: imported });
      try {
        const r = await request<{ image: ImageInfo; imported: ImportSummary | null }>("POST", url, undefined, {
          raw: body, headers: { "Content-Type": "application/json", "Content-Encoding": "gzip" } });
        if (r.imported) this.logImport(item.name, r.imported);
        return r.image;
      } catch (err) {
        if (!(err instanceof ApiError) || !err.code.startsWith("invalid_import")) throw err;
        this.events.log(`${item.name}: the desktop review data couldn't be read (${err.message}); it will be segmented fresh.`);
      }
    }
    return (await post<{ image: ImageInfo }>(url)).image;
  }

  private async desktopCells(name: string, ref: DesktopRef | null): Promise<{ cells: unknown[]; params: unknown } | null> {
    if (!ref) return null;
    try {
      return await readDesktopCells(ref);
    } catch {
      this.events.log(`${name}: couldn't read its .cells.json; it will be segmented fresh.`);
      return null;
    }
  }

  private logImport(name: string, summary: ImportSummary): void {
    this.batch.desktop++;
    this.events.log(`${name}: brought in ${summary.cells} reviewed cell(s) from the desktop app`
      + (summary.skipped ? ` (${summary.skipped} couldn't be read and were left out).` : "."));
  }

  /** A file that's already on the server, with matching desktop review data
   * that the server doesn't have yet (uploads/services.import_into_existing). */
  private async importIntoExisting(name: string, imageId: string, sha256: string, ref: DesktopRef): Promise<void> {
    const imported = await this.desktopCells(name, ref);
    if (!imported) return;
    try {
      const body = await gzipJson({ sha256, imported_cells: imported });
      const r = await request<ImportResponse>("POST", `/api/images/${imageId}/import-cells`, undefined, {
        raw: body, headers: { "Content-Type": "application/json", "Content-Encoding": "gzip" } });
      if (r.result === "imported") {
        this.logImport(name, { cells: r.cells ?? 0, skipped: r.skipped ?? 0 });
        if (r.image) this.events.uploaded(r.image);
      }
    } catch (err) {
      // has_edits / busy / not_ready: the message says what was kept and why.
      this.events.log(`${name}: ${errorMessage(err)}`);
    }
  }

  // ------------------------------------------------------------------ //
  /** A file leaves the line. Unless it was uploaded, its unsent bytes come off
   * the batch total so the bar measures what's actually going up. */
  private settle(item: UploadItem, dropUnsent = true): void {
    if (dropUnsent) this.batch.bytesTotal -= item.file.size - item.sent;
    this.batch.filesDone++;
    this.items = this.items.filter((i) => i !== item);
    this.events.changed();
  }

  private fail(item: UploadItem, message: string): void {
    this.events.log(`ERROR uploading ${item.name}: ${message}`);
    this.batch.failed++;
    this.settle(item);
  }

  private stopAll(message: string): void {
    const dropped = this.items.filter((i) => i.phase !== "uploading");
    for (const item of dropped) {
      this.batch.failed++;
      this.settle(item);
    }
    this.events.log(`${message} ${dropped.length} file(s) were not uploaded.`);
    this.events.quotaExceeded(message);
  }

  private finishIfIdle(): void {
    if (this.hashing || this.lanesRunning || this.completing || this.importing || this.items.length) return;
    if (this.stage === "scanning" || this.stage === "checking") return;
    const b = this.batch;
    if (b.uploaded + b.failed + b.upToDate + b.kept + b.desktop === 0) {
      this.stage = "idle";
      this.events.changed();
      return;
    }
    const parts = [`${b.uploaded} uploaded`];
    if (b.uploaded) parts[0] += ` (${formatBytes(b.bytesDone)})`;
    parts.push(`${b.upToDate} already up to date`);
    if (b.desktop) parts.push(`desktop review work brought in for ${b.desktop}`);
    if (b.kept) parts.push(`${b.kept} changed file(s) left as they were`);
    if (b.failed) parts.push(`${b.failed} failed — see View › Show Log`);
    this.summary = `Done. ${parts.join(", ")}.`;
    this.stage = "done";
    this.events.log(`Finished: ${parts.join(", ")}.`);
    this.batch = newBatch();
    this.meter.reset();
    this.events.changed();
  }
}

/** Upload speed over a sliding window of recent samples of total bytes sent. */
export class RateMeter {
  private samples: { t: number; bytes: number }[] = [];
  private readonly windowMs: number;

  constructor(windowMs: number) {
    this.windowMs = windowMs;
  }

  record(totalBytes: number, nowMs: number): void {
    this.samples.push({ t: nowMs, bytes: totalBytes });
    while (this.samples.length > 2 && nowMs - this.samples[0].t > this.windowMs) this.samples.shift();
  }

  /** Bytes per second, or null until there are at least 2 s of samples. */
  rate(): number | null {
    const s = this.samples;
    if (s.length < 2) return null;
    const seconds = (s[s.length - 1].t - s[0].t) / 1000;
    if (seconds < 2) return null;
    return Math.max(0, s[s.length - 1].bytes - s[0].bytes) / seconds;
  }

  reset(): void {
    this.samples = [];
  }
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return "less than a minute";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `about ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `about ${hours} h ${rest} min` : `about ${hours} h`;
}

async function gzipJson(value: unknown): Promise<Blob> {
  const stream = new Blob([JSON.stringify(value)]).stream().pipeThrough(new CompressionStream("gzip"));
  return new Response(stream).blob();
}
