// panels.ts — the left column: Samples (with SNAP/CCK/CHR readiness dots) above
// the Queue (everything still outstanding, in processing order). Both are
// hand-rolled row lists: plain rows, selection as a solid #cce0ff fill, status
// in color only. Queue rows "breathe" (a slow eased fade) while something is
// happening to them right now.

import {
  CHANNELS,
  QUEUE_BREATHE_PERIOD_MS,
  QUEUE_SEGMENTING_ACTIVE_BG,
  QUEUE_SERVER_QUEUED_BG,
  QUEUE_UPLOADING_ACTIVE_BG,
  ROW_BG,
  ROW_BG_SELECTED,
  type Channel,
} from "./constants";
import { clear, el } from "./dom";
import { clickSelection, type Move } from "./queueOrder";

export type Readiness = "not_ready" | "processing" | "ready";

// --------------------------------------------------------------------------- //
// Samples
// --------------------------------------------------------------------------- //
interface SampleRow {
  row: HTMLElement;
  dots: Record<Channel, HTMLElement>;
}

export class SamplesPanel {
  readonly root: HTMLElement;
  private readonly list: HTMLElement;
  private rows = new Map<string, SampleRow>();
  onSelect: (prefix: string, channel: Channel | null) => void = () => {};

  constructor() {
    this.list = el("div", { class: "ws-rows", attrs: { role: "listbox", "aria-label": "Samples" } });
    this.root = el("section", { class: "ws-pane" }, el("h2", { text: "Samples" }), this.list);
  }

  setSamples(prefixes: string[]): void {
    clear(this.list);
    this.rows.clear();
    if (!prefixes.length) {
      this.list.append(el("div", { class: "ws-empty", text: "No images yet. Use File › Add Images…" }));
      return;
    }
    for (const prefix of prefixes) {
      const dots = {} as Record<Channel, HTMLElement>;
      const dotWrap = el("span", { class: "dots" });
      for (const ch of CHANNELS) {
        const dot = el("span", { class: "dot", title: ch, on: {
          click: (e) => {
            e.stopPropagation();
            this.onSelect(prefix, ch);
          },
        } });
        dots[ch] = dot;
        dotWrap.append(dot);
      }
      const row = el("div", { class: "ws-row", attrs: { role: "option" }, on: { click: () => this.onSelect(prefix, null) } },
        el("span", { class: "ws-row-label", text: prefix }), dotWrap);
      this.rows.set(prefix, { row, dots });
      this.list.append(row);
    }
  }

  update(readiness: (prefix: string, channel: Channel) => Readiness, selected: string | null): void {
    for (const [prefix, { row, dots }] of this.rows) {
      const isSelected = prefix === selected;
      row.classList.toggle("selected", isSelected);
      row.setAttribute("aria-selected", String(isSelected));
      for (const ch of CHANNELS) {
        const state = readiness(prefix, ch);
        dots[ch].className = `dot${state === "ready" ? " ready" : state === "processing" ? " processing" : ""}`;
        dots[ch].title = `${ch}: ${state === "ready" ? "done" : state === "processing" ? "in progress" : "not ready"}`;
      }
    }
  }

  scrollTo(prefix: string): void {
    this.rows.get(prefix)?.row.scrollIntoView({ block: "nearest" });
  }
}

// --------------------------------------------------------------------------- //
// Queue
// --------------------------------------------------------------------------- //
export type QueueRowState =
  | "waiting" // local, not uploaded yet
  | "uploading" // local, uploading now
  | "server-queued" // on the server, waiting (prepare or GPU)
  | "segmenting"; // on the GPU right now

export interface QueueItem {
  key: string;
  label: string;
  state: QueueRowState;
  title?: string;
  /** Short right-aligned note, e.g. "62%" or "checking 40%". */
  detail?: string;
  /** 0..1: drawn as a thin bar along the bottom of the row. */
  progress?: number;
}

const BREATHE_TICK_MS = 60;

const isActive = (s: QueueRowState) => s === "uploading" || s === "segmenting";
const baseBg = (s: QueueRowState) => (s === "server-queued" || s === "segmenting" ? QUEUE_SERVER_QUEUED_BG : ROW_BG);
const targetBg = (s: QueueRowState) => (s === "segmenting" ? QUEUE_SEGMENTING_ACTIVE_BG : QUEUE_UPLOADING_ACTIVE_BG);

export function breathePhase(elapsedMs: number): number {
  return (1 - Math.cos((2 * Math.PI * elapsedMs) / QUEUE_BREATHE_PERIOD_MS)) / 2;
}

export function lerpColor(c1: string, c2: string, t: number): string {
  const p = (c: string, i: number) => parseInt(c.slice(i, i + 2), 16);
  const mix = (i: number) => Math.round(p(c1, i) + (p(c2, i) - p(c1, i)) * t);
  return `#${[1, 3, 5].map((i) => mix(i).toString(16).padStart(2, "0")).join("")}`;
}

export class QueuePanel {
  readonly root: HTMLElement;
  private readonly list: HTMLElement;
  private items: QueueItem[] = [];
  private signature: string | null = null; // null: nothing rendered yet, not even the empty state
  private rowEls = new Map<string, { row: HTMLElement; detail: HTMLElement }>();
  private selected = new Set<string>();
  private anchor: string | null = null;
  private animation = 0;
  private readonly started = performance.now();
  onMove: (keys: Set<string>, how: Move) => void = () => {};

  constructor() {
    this.list = el("div", { class: "ws-rows", attrs: { role: "listbox", "aria-label": "Queue", "aria-multiselectable": "true" } });
    const button = (text: string, how: Move, label: string) =>
      el("button", { text, title: label, attrs: { type: "button", "aria-label": label },
        on: { click: () => this.selected.size && this.onMove(new Set(this.selected), how) } });
    const buttons = el("div", { class: "ws-queue-buttons" },
      button("▲", "up", "Move up"), button("▲▲", "top", "Send to top"),
      button("▼", "down", "Move down"), button("▼▼", "bottom", "Send to bottom"));
    this.root = el("section", { class: "ws-pane" }, el("h2", { text: "Queue" }), this.list, buttons);
  }


  setItems(items: QueueItem[]): void {
    const signature = items.map((i) => `${i.key}:${i.state}:${i.label}`).join("|");
    this.items = items;
    if (signature === this.signature) {
      // Same rows: just move their progress along, without rebuilding (which
      // would reset scrolling and interrupt a click in progress).
      this.paintProgress();
      return;
    }
    this.signature = signature;
    const keys = new Set(items.map((i) => i.key));
    for (const k of [...this.selected]) if (!keys.has(k)) this.selected.delete(k);

    const scrollTop = this.list.scrollTop;
    clear(this.list);
    this.rowEls.clear();
    if (!items.length) this.list.append(el("div", { class: "ws-empty", text: "Nothing waiting." }));
    for (const item of items) {
      const detail = el("span", { class: "ws-row-detail" });
      const row = el("div", { class: "ws-row", title: item.title ?? "", attrs: { role: "option" },
        on: { click: (e) => this.click(item.key, e) } }, el("span", { class: "ws-row-label", text: item.label }), detail);
      this.rowEls.set(item.key, { row, detail });
      this.list.append(row);
    }
    this.list.scrollTop = scrollTop;
    this.paint();
    this.paintProgress();
    this.syncAnimation();
  }

  private paintProgress(): void {
    for (const item of this.items) {
      const els = this.rowEls.get(item.key);
      if (!els) continue;
      els.detail.textContent = item.detail ?? "";
      els.row.classList.toggle("has-progress", item.progress !== undefined);
      if (item.progress !== undefined) els.row.style.setProperty("--progress", `${(item.progress * 100).toFixed(1)}%`);
    }
  }

  private click(key: string, e: MouseEvent): void {
    const result = clickSelection(this.items.map((i) => i.key), this.selected, this.anchor, key,
      { shift: e.shiftKey, ctrl: e.ctrlKey || e.metaKey });
    this.selected = result.selected;
    this.anchor = result.anchor;
    this.paint();
  }

  private paint(): void {
    const t = breathePhase(performance.now() - this.started);
    for (const item of this.items) {
      const row = this.rowEls.get(item.key)?.row;
      if (!row) continue;
      const selected = this.selected.has(item.key);
      const base = selected ? ROW_BG_SELECTED : baseBg(item.state);
      row.style.background = isActive(item.state) ? lerpColor(base, targetBg(item.state), t) : base;
      row.setAttribute("aria-selected", String(selected));
    }
  }

  /** The breathing runs only while some row is active, on a 60 ms
   * tick (a full-rate animation frame loop would be wasted on a slow fade). */
  private syncAnimation(): void {
    const any = this.items.some((i) => isActive(i.state));
    if (any && !this.animation) {
      this.animation = window.setInterval(() => this.paint(), BREATHE_TICK_MS);
    } else if (!any && this.animation) {
      clearInterval(this.animation);
      this.animation = 0;
    }
  }
}
