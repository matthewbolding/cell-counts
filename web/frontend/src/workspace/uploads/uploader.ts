// uploader.ts — File › Add Images…, end to end (DESIGN.md, "Upload flow").
//
// Mirrors the desktop client's two concurrent halves: a hashing loop (plan → hash
// → init) and an upload loop (chunks → complete) run side by side, so the first
// file starts uploading while later ones are still being hashed. The plan step
// is the desktop's fast path: a file whose name, size and modification time
// match what's already on the server is trusted without hashing.
//
// Desktop review work rides along: if the folder has the desktop app's
// cellcounts.json and .cells.json files and a file's recorded sha256 matches,
// its cells go up with it and it isn't re-segmented. If the file is already on
// the server (uploaded earlier without them), the cells are imported into it,
// unless someone has already reviewed that image in the web app.
//
// Interruptions are expected — closing the laptop, flaky Wi-Fi, the tab
// closing. Every chunk is retried with backoff; anything not finished when the
// page goes away resumes the next time the folder is added, because `init`
// reports which chunks the server already has.

import { ApiError, errorMessage, post, request, sleep } from "../api";
import type { Channel } from "../constants";
import type { ImageInfo } from "../types";
import { moveSelection, type Move } from "../queueOrder";
import { hashFile } from "./hasher";
import { desktopMatch, readDesktopCells, type DesktopRef, type Recognized, type ScanResult } from "./scan";

const CHUNK_RETRY_MS = [1000, 2000, 5000, 10000, 20000, 30000];
const MAX_CHUNK_ATTEMPTS = 8;

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
  desktop: DesktopRef | null;
}

interface PlanFile {
  name: string;
  decision: "unchanged" | "needs_hash";
  image_id: string | null;
  sha256: string | null;
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

interface InitResponse {
  action: "upload" | "skip";
  upload_id?: string;
  chunk_size?: number;
  total_chunks?: number;
  chunks_present?: number[];
  image?: ImageInfo;
}

export interface UploadEvents {
  changed(): void;
  status(message: string, active: boolean): void;
  log(message: string): void;
  uploaded(image: ImageInfo): void;
  /** Ask once about every file whose content differs from what's on the server. */
  confirmReplace(names: string[]): Promise<boolean>;
  quotaExceeded(message: string): void;
}

interface Totals {
  upToDate: number;
  uploaded: number;
  failed: number;
  kept: number;
  hashed: number;
  toHash: number;
  desktop: number; // images whose desktop review work was brought in
}

const newTotals = (): Totals => ({ upToDate: 0, uploaded: 0, failed: 0, kept: 0, hashed: 0, toHash: 0, desktop: 0 });

export class UploadManager {
  items: UploadItem[] = [];
  paused = false;
  private readonly projectId: string;
  private readonly events: UploadEvents;
  private hashing = false;
  private uploading = false;
  private importing = 0;
  private totals = newTotals();
  private resumeWaiters: (() => void)[] = [];

  constructor(projectId: string, events: UploadEvents) {
    this.projectId = projectId;
    this.events = events;
  }

  get active(): boolean {
    return this.items.length > 0;
  }

  /** For the readiness dots: a file for this slot is waiting or uploading. */
  isPending(prefix: string, channel: Channel): boolean {
    return this.items.some((i) => i.prefix === prefix && i.channel === channel);
  }

  pause(): void {
    this.paused = true;
    this.events.changed();
  }

  resume(): void {
    this.paused = false;
    for (const wake of this.resumeWaiters.splice(0)) wake();
    this.events.changed();
    this.kick();
  }

  /** ▲/▼ in the Queue panel for local rows. The file uploading right now stays put. */
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
      this.events.status("No {PREFIX}_{CCK,CHR,SNAP}.tif files found in that folder.", false);
      return;
    }

    const already = new Set(this.items.map((i) => i.name));
    const fresh = recognized.filter((r) => !already.has(r.name));
    this.events.status(`Checking ${fresh.length} image(s) against the server…`, true);
    let plan: { files: PlanFile[] };
    try {
      plan = await post(`/api/projects/${this.projectId}/uploads/plan`, {
        files: fresh.map((r) => ({ name: r.name, size: r.file.size, last_modified: r.file.lastModified })),
      });
    } catch (err) {
      this.events.status(`Couldn't start the upload: ${errorMessage(err)}`, false);
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
    }
    this.totals.upToDate += unchanged;
    this.totals.toHash += fresh.length - unchanged;
    if (scan.desktopFolders) {
      this.events.log(`Found desktop review data (${scan.desktopFolders} cellcounts.json file(s)); it will be `
        + "brought in for every image whose file hasn't changed since.");
    }
    this.events.log(`Found ${recognized.length} recognized image(s); ${unchanged} already up to date, `
      + `${fresh.length - unchanged} to check.`);
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
      phase: "hash", replace: false, desktop: r.desktop };
  }

  private kick(): void {
    if (!this.hashing) void this.hashLoop();
    if (!this.uploading) void this.uploadLoop();
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
            this.remove(c);
            this.totals.kept++;
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
      this.totals.hashed++;
      const label = `Hashing ${item.name} (${this.totals.hashed}/${this.totals.toHash})`;
      this.events.status(label, true);
      try {
        item.sha256 = await hashFile(item.file, (f) => {
          if (f < 1) this.events.status(`${label} — ${Math.round(f * 100)}%`, true);
        });
      } catch (err) {
        this.fail(item, `couldn't read the file (${errorMessage(err)})`);
        return;
      }
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
        this.remove(item);
        this.totals.upToDate++;
        const ref = desktopMatch(item.desktop, item.sha256);
        if (ref && r.image) await this.importIntoExisting(item.name, r.image.id, item.sha256!, ref);
        return;
      }
      item.uploadId = r.upload_id;
      item.chunkSize = r.chunk_size;
      item.totalChunks = r.total_chunks;
      item.present = new Set(r.chunks_present ?? []);
      item.phase = "ready";
      this.events.changed();
      if (!this.uploading) void this.uploadLoop();
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
  // Upload half: chunks → complete
  // ------------------------------------------------------------------ //
  private async uploadLoop(): Promise<void> {
    this.uploading = true;
    try {
      for (;;) {
        const item = this.items.find((i) => i.phase === "ready");
        if (!item) break;
        if (this.paused) await this.waitForResume();
        item.phase = "uploading";
        this.events.changed();
        try {
          await this.sendChunks(item);
          const image = await this.complete(item);
          this.remove(item);
          this.totals.uploaded++;
          this.events.uploaded(image);
        } catch (err) {
          if (err instanceof ApiError && err.status === 404) {
            // The server forgot this upload (abandoned for a week): start over.
            item.phase = "hash";
            item.uploadId = undefined;
            if (!this.hashing) void this.hashLoop();
          } else {
            this.fail(item, errorMessage(err));
          }
        }
      }
    } finally {
      this.uploading = false;
      this.finishIfIdle();
    }
  }

  private waitForResume(): Promise<void> {
    this.events.status("Uploads stopped. Process › Start Uploads to continue.", false);
    return new Promise((resolve) => this.resumeWaiters.push(resolve));
  }

  private async sendChunks(item: UploadItem): Promise<void> {
    const total = item.totalChunks!;
    const size = item.chunkSize!;
    for (let index = 0; index < total; index++) {
      if (item.present!.has(index)) continue;
      if (this.paused) await this.waitForResume();
      this.events.status(`Uploading ${item.name}: chunk ${index + 1}/${total}`, true);
      const blob = item.file.slice(index * size, Math.min(item.file.size, (index + 1) * size));
      for (let attempt = 0; ; attempt++) {
        try {
          await request("PUT", `/api/uploads/${item.uploadId}/chunks/${index}`, undefined,
            { raw: blob, headers: { "Content-Type": "application/octet-stream" } });
          item.present!.add(index);
          break;
        } catch (err) {
          if (!(err instanceof ApiError) || !err.isRetryable || attempt + 1 >= MAX_CHUNK_ATTEMPTS) throw err;
          const wait = CHUNK_RETRY_MS[Math.min(attempt, CHUNK_RETRY_MS.length - 1)];
          this.events.status(`Upload of ${item.name} interrupted; retrying in ${Math.round(wait / 1000)} s…`, true);
          await sleep(wait);
        }
      }
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
    this.totals.desktop++;
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
  private remove(item: UploadItem): void {
    this.items = this.items.filter((i) => i !== item);
    this.events.changed();
  }

  private fail(item: UploadItem, message: string): void {
    this.events.log(`ERROR uploading ${item.name}: ${message}`);
    this.totals.failed++;
    this.remove(item);
  }

  private stopAll(message: string): void {
    const dropped = this.items.filter((i) => i.phase !== "uploading");
    this.items = this.items.filter((i) => i.phase === "uploading");
    this.totals.failed += dropped.length;
    this.events.log(`${message} ${dropped.length} file(s) were not uploaded.`);
    this.events.changed();
    this.events.quotaExceeded(message);
  }

  private finishIfIdle(): void {
    if (this.hashing || this.uploading || this.importing || this.items.length) return;
    const t = this.totals;
    if (t.uploaded + t.failed + t.upToDate + t.kept + t.desktop === 0) return;
    const parts = [`${t.uploaded} uploaded`, `${t.upToDate} already up to date`];
    if (t.desktop) parts.push(`desktop review work brought in for ${t.desktop}`);
    if (t.kept) parts.push(`${t.kept} changed file(s) left as they were`);
    if (t.failed) parts.push(`${t.failed} failed — see View › Show Log`);
    this.events.status(`Done. ${parts.join(", ")}.`, false);
    this.events.log(`Finished: ${parts.join(", ")}.`);
    this.totals = newTotals();
  }
}

async function gzipJson(value: unknown): Promise<Blob> {
  const stream = new Blob([JSON.stringify(value)]).stream().pipeThrough(new CompressionStream("gzip"));
  return new Response(stream).blob();
}
