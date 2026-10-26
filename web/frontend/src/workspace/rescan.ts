// rescan.ts — Rescan mode, after a region is dragged: the settings dialog, the
// sweep, and reviewing its proposals.
//
// The sweep itself now runs on the server against the original TIFF (no crop
// upload from the browser): one GPU job per (diameter, cellprob) combination,
// ahead of bulk segmentation in the queue. This module asks for the
// parameters, polls the sweep, and holds the review state — which combination
// is showing and which proposed cells have been accepted.

import { del, get, post } from "./api";
import { type CellEntry, entriesFor, hitTest } from "./cellStore";
import { DEFAULT_RESCAN_CELLPROBS, DEFAULT_RESCAN_DIAMETERS, MAX_RESCAN_COMBOS } from "./constants";
import { showDialog } from "./dialogs";
import { el } from "./dom";
import type { Cell, Rect } from "./types";

const POLL_MS = 1500;

export interface RescanParams {
  diameters: number[];
  cellprobs: number[];
}

export interface Combo {
  job_id: string;
  diameter: number;
  cellprob_threshold: number;
  status: string;
  cells: Cell[] | null;
  error: string | null;
}

interface SweepDetail {
  sweep_id: string;
  image_id: string;
  rect: Rect;
  status: "running" | "done";
  combos: Combo[];
}

export function parseValues(text: string): number[] | null {
  const parts = text.split(",").map((v) => v.trim()).filter(Boolean);
  if (!parts.length) return null;
  const values = parts.map(Number);
  return values.every(Number.isFinite) ? values : null;
}

export async function askRescanParams(width: number, height: number): Promise<RescanParams | null> {
  const diameter = el("input", { attrs: { type: "text", value: DEFAULT_RESCAN_DIAMETERS, "aria-label": "Diameters" } });
  const cellprob = el("input", { attrs: { type: "text", value: DEFAULT_RESCAN_CELLPROBS, "aria-label": "Cell probability thresholds" } });
  const count = el("p", { class: "muted" });
  const update = () => {
    const d = parseValues(diameter.value);
    const c = parseValues(cellprob.value);
    if (!d || !c) count.textContent = "Enter valid comma-separated numbers.";
    else {
      const n = d.length * c.length;
      count.textContent = n > MAX_RESCAN_COMBOS
        ? `${n} combinations — the limit is ${MAX_RESCAN_COMBOS}.`
        : `${n} combination${n === 1 ? "" : "s"} will be tried.`;
    }
  };
  diameter.addEventListener("input", update);
  cellprob.addEventListener("input", update);
  update();

  const body = el("div", { class: "ws-form" },
    el("p", {}, el("strong", { text: `Region: ${Math.round(width)} x ${Math.round(height)} px` })),
    el("label", { text: "Diameter (px), comma-separated (0 = auto):" }), diameter,
    el("label", { text: "Cell probability threshold, comma-separated:" }), cellprob,
    count);

  let result: RescanParams | null = null;
  const ok = await showDialog<boolean>({
    title: "Rescan Region",
    body,
    buttons: [{ label: "Cancel", value: false }, { label: "Start", value: true, default: true }],
    cancelValue: false,
    beforeClose: (start) => {
      if (!start) return true;
      const d = parseValues(diameter.value);
      const c = parseValues(cellprob.value);
      if (!d || !c || d.length * c.length > MAX_RESCAN_COMBOS) {
        count.textContent = !d || !c
          ? "Enter at least one valid number in each field."
          : `Too many combinations — the limit is ${MAX_RESCAN_COMBOS}.`;
        count.classList.add("error-text");
        return false;
      }
      result = { diameters: d, cellprobs: c };
      return true;
    },
  });
  return ok ? result : null;
}

/** Start a sweep and poll it to completion. `onProgress(done, total)` is called
 * on every poll. Rejects if cancelled. */
export class SweepRunner {
  private cancelled = false;
  sweepId: string | null = null;

  async run(imageId: string, rect: Rect, params: RescanParams,
            onProgress: (done: number, total: number) => void): Promise<SweepDetail> {
    const created = await post<{ sweep_id: string; combos: unknown[] }>(`/api/images/${imageId}/rescans`, {
      rect, diameters: params.diameters, cellprobs: params.cellprobs });
    this.sweepId = created.sweep_id;
    onProgress(0, created.combos.length);
    for (;;) {
      await new Promise((r) => setTimeout(r, POLL_MS));
      if (this.cancelled) throw new Error("cancelled");
      const detail = await get<SweepDetail>(`/api/rescans/${this.sweepId}`);
      const done = detail.combos.filter((c) => c.status !== "queued" && c.status !== "running").length;
      onProgress(done, detail.combos.length);
      if (detail.status === "done") return detail;
    }
  }

  cancel(): void {
    this.cancelled = true;
    if (this.sweepId) void del(`/api/rescans/${this.sweepId}`).catch(() => {});
  }
}

/** The rescan review sub-view: one combination on screen at a time, accepted
 * proposals collected across all of them until Merge or Discard. */
export class RescanReview {
  readonly imageId: string;
  readonly rect: Rect;
  readonly combos: Combo[];
  index: number;
  private readonly entries: CellEntry[][];
  /** "<combo index>:<cell id>" -> accepted cell and the job that proposed it. */
  readonly accepted = new Map<string, { cell: Cell; jobId: string }>();

  constructor(imageId: string, rect: Rect, combos: Combo[]) {
    this.imageId = imageId;
    this.rect = rect;
    this.combos = combos;
    this.entries = combos.map((c) => entriesFor(c.cells ?? []));
    // Land on the first combination that found anything,
    // so an empty first combo doesn't look like the whole sweep found nothing.
    const firstHit = combos.findIndex((c) => (c.cells ?? []).length > 0);
    this.index = firstHit >= 0 ? firstHit : 0;
  }

  get current(): Combo {
    return this.combos[this.index];
  }

  step(delta: number): void {
    this.index = (this.index + delta + this.combos.length) % this.combos.length;
  }

  private key(cellId: number, index = this.index): string {
    return `${index}:${cellId}`;
  }

  toggleAt(x: number, y: number, scale: number): boolean {
    const cell = hitTest(this.entries[this.index], x, y, scale);
    if (!cell) return false;
    const k = this.key(cell.id);
    if (this.accepted.has(k)) this.accepted.delete(k);
    else this.accepted.set(k, { cell, jobId: this.current.job_id });
    return true;
  }

  /** The current combination's proposals, split for drawing. */
  split(): { pending: CellEntry[]; accepted: CellEntry[] } {
    const pending: CellEntry[] = [];
    const accepted: CellEntry[] = [];
    for (const e of this.entries[this.index]) {
      (this.accepted.has(this.key(e.cell.id)) ? accepted : pending).push(e);
    }
    return { pending, accepted };
  }

  acceptedHere(): number {
    let n = 0;
    for (const k of this.accepted.keys()) if (k.startsWith(`${this.index}:`)) n++;
    return n;
  }
}
