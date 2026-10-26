// edits.ts — getting review edits to the server, and the edit lease that keeps
// two collaborators from editing one image at once.
//
// CellSaver is the web version of review.py's debounced flush: edits apply
// locally at once, their ops (undo.ts) queue per image, and the batch is sent a
// second after the last edit — or right away when leaving the image or the
// page. Each image has at most one request in flight, and every batch carries
// the version it was built on, so ops reach the server in order and a stale
// copy is refused (409) instead of overwriting someone else's work.
//
// EditLease takes the image's edit lock (projects.models.ImageLock) when an
// editor opens it, renews it every 20 s while it stays open, and releases it on
// the way out. If someone else holds it, the image is shown read-only.

import { ApiError, del, post, type RequestOptions } from "./api";
import type { CellStore } from "./cellStore";
import { LOCK_RENEW_MS, SAVE_IDLE_MS } from "./constants";
import type { CellOp } from "./undo";

const RETRY_MS = [2000, 5000, 10000, 20000, 30000];

interface Pending {
  store: CellStore;
  ops: CellOp[];
  timer: number | null;
  inFlight: Promise<void> | null;
  failures: number;
}

export interface SaverEvents {
  /** The server's copy changed (version conflict): reload it, drop history. */
  onConflict(imageId: string): void;
  /** Someone else holds the edit lock now. */
  onLocked(imageId: string, holder: string): void;
  /** A save failed and will be retried (network) or was rejected outright. */
  onError(imageId: string, message: string, willRetry: boolean): void;
  onSaved(imageId: string): void;
}

export class CellSaver {
  private readonly pending = new Map<string, Pending>();
  private readonly events: SaverEvents;

  constructor(events: SaverEvents) {
    this.events = events;
  }

  queue(store: CellStore, ops: CellOp[]): void {
    if (!ops.length) return;
    let p = this.pending.get(store.imageId);
    if (!p) {
      p = { store, ops: [], timer: null, inFlight: null, failures: 0 };
      this.pending.set(store.imageId, p);
    }
    p.store = store;
    p.ops.push(...ops);
    this.schedule(p, SAVE_IDLE_MS);
  }

  /** True while an image has edits the server hasn't confirmed. */
  isDirty(imageId: string): boolean {
    const p = this.pending.get(imageId);
    return !!p && (p.ops.length > 0 || p.inFlight !== null);
  }

  get anyDirty(): boolean {
    for (const id of this.pending.keys()) if (this.isDirty(id)) return true;
    return false;
  }

  /** Throw away unsent edits (the image is being reloaded after a conflict). */
  discard(imageId: string): void {
    const p = this.pending.get(imageId);
    if (p?.timer) clearTimeout(p.timer);
    this.pending.delete(imageId);
  }

  private schedule(p: Pending, ms: number): void {
    if (p.timer) clearTimeout(p.timer);
    p.timer = window.setTimeout(() => {
      p.timer = null;
      void this.flush(p.store.imageId);
    }, ms);
  }

  /** Send everything queued for one image now; resolves once the server has
   * it (or it failed). `keepalive` lets the request outlive the page. */
  async flush(imageId: string, keepalive = false): Promise<void> {
    const p = this.pending.get(imageId);
    if (!p) return;
    if (p.timer) {
      clearTimeout(p.timer);
      p.timer = null;
    }
    if (p.inFlight) {
      await p.inFlight;
      if (p.ops.length) return this.flush(imageId, keepalive);
      return;
    }
    if (!p.ops.length) return;

    const ops = p.ops;
    p.ops = [];
    const options: RequestOptions = keepalive ? { keepalive: true } : {};
    p.inFlight = (async () => {
      try {
        const result = await post<{ version: number }>(`/api/images/${imageId}/cells/ops`,
          { base_version: p.store.version, ops }, options);
        p.store.version = result.version;
        p.failures = 0;
        this.events.onSaved(imageId);
      } catch (err) {
        this.handleError(p, ops, err);
      } finally {
        p.inFlight = null;
      }
    })();
    await p.inFlight;
    if (p.ops.length && !p.timer) await this.flush(imageId, keepalive);
  }

  private handleError(p: Pending, ops: CellOp[], err: unknown): void {
    const id = p.store.imageId;
    if (err instanceof ApiError && err.isRetryable) {
      // Put the batch back in front of anything queued since, and try again.
      p.ops = [...ops, ...p.ops];
      const delay = RETRY_MS[Math.min(p.failures, RETRY_MS.length - 1)];
      p.failures++;
      this.events.onError(id, "Couldn't save your last edit — retrying.", true);
      this.schedule(p, delay);
      return;
    }
    this.discard(id);
    if (err instanceof ApiError && err.code === "version_conflict") {
      this.events.onConflict(id);
    } else if (err instanceof ApiError && err.status === 423) {
      this.events.onLocked(id, String(err.body.holder ?? "Someone else"));
    } else {
      this.events.onError(id, err instanceof Error ? err.message : String(err), false);
    }
  }

  /** Leaving the page: fire every pending batch with keepalive. */
  flushAllOnUnload(): void {
    for (const id of this.pending.keys()) void this.flush(id, true);
  }
}

export interface LeaseState {
  imageId: string;
  heldByMe: boolean;
  holder: string | null;
  readOnly: boolean; // viewer role
}

/** One image's edit lease at a time — the image on screen. */
export class EditLease {
  private imageId: string | null = null;
  private timer: number | null = null;
  private readonly onChange: (state: LeaseState) => void;

  constructor(onChange: (state: LeaseState) => void) {
    this.onChange = onChange;
  }

  async acquire(imageId: string): Promise<LeaseState> {
    if (this.imageId && this.imageId !== imageId) this.release();
    this.imageId = imageId;
    const state = await this.request(imageId);
    this.startTimer();
    return state;
  }

  private async request(imageId: string): Promise<LeaseState> {
    try {
      const r = await post<{ held_by_me: boolean; holder: string | null; read_only?: boolean }>(
        `/api/images/${imageId}/lock`);
      return { imageId, heldByMe: r.held_by_me, holder: r.holder, readOnly: !!r.read_only };
    } catch {
      // Can't tell: stay editable; a save will report the lock if there is one.
      return { imageId, heldByMe: true, holder: null, readOnly: false };
    }
  }

  private startTimer(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = window.setInterval(async () => {
      const id = this.imageId;
      if (!id) return;
      const state = await this.request(id);
      if (this.imageId === id) this.onChange(state);
    }, LOCK_RENEW_MS);
  }

  release(keepalive = false): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    const id = this.imageId;
    this.imageId = null;
    if (id) void del(`/api/images/${id}/lock`, keepalive ? { keepalive: true } : {}).catch(() => {});
  }
}
