// spatialIndex.ts — a uniform grid over item bounding boxes. Rendering (cull to
// the viewport), hit-testing and drag-selection all ask "what's near this
// rectangle?"; with 25k cells on the largest images a linear scan per mouse
// move or per frame is too slow, and cells are small and evenly spread, which
// is exactly the case a fixed grid handles best (and supports cheap inserts
// and deletes as cells are drawn and removed).

import type { Rect } from "./types";

export interface Indexed {
  bbox: Rect;
  /** Scratch field: the query generation that last saw this item (dedupes
   * items that span several buckets without allocating a Set per query). */
  stamp?: number;
}

const DEFAULT_BUCKET = 64; // image px

export class GridIndex<T extends Indexed> {
  private readonly bucket: number;
  private readonly buckets = new Map<number, T[]>();
  private generation = 0;
  private count = 0;

  constructor(bucketSize = DEFAULT_BUCKET) {
    this.bucket = bucketSize;
  }

  get size(): number {
    return this.count;
  }

  private key(col: number, row: number): number {
    // One number per (col, row); the offset keeps slightly negative indices
    // (a hand-drawn cell poking past the image edge) distinct.
    return (row + 1024) * 2_097_152 + (col + 1024);
  }

  private range(rect: Rect): [number, number, number, number] {
    const b = this.bucket;
    return [Math.floor(rect[0] / b), Math.floor(rect[1] / b), Math.floor(rect[2] / b), Math.floor(rect[3] / b)];
  }

  insert(item: T): void {
    const [c0, r0, c1, r1] = this.range(item.bbox);
    for (let r = r0; r <= r1; r++) {
      for (let c = c0; c <= c1; c++) {
        const k = this.key(c, r);
        const list = this.buckets.get(k);
        if (list) list.push(item);
        else this.buckets.set(k, [item]);
      }
    }
    this.count++;
  }

  remove(item: T): void {
    const [c0, r0, c1, r1] = this.range(item.bbox);
    let found = false;
    for (let r = r0; r <= r1; r++) {
      for (let c = c0; c <= c1; c++) {
        const k = this.key(c, r);
        const list = this.buckets.get(k);
        if (!list) continue;
        const i = list.indexOf(item);
        if (i >= 0) {
          list.splice(i, 1);
          found = true;
          if (list.length === 0) this.buckets.delete(k);
        }
      }
    }
    if (found) this.count--;
  }

  /** Every item whose bbox intersects `rect`, each once. */
  query(rect: Rect): T[] {
    const gen = ++this.generation;
    const out: T[] = [];
    const [c0, r0, c1, r1] = this.range(rect);
    // A query wider than the occupied grid (fit-to-window on a big image) is
    // cheaper as a walk over the occupied buckets than over every grid cell.
    const span = (c1 - c0 + 1) * (r1 - r0 + 1);
    if (span > this.buckets.size) {
      for (const list of this.buckets.values()) this.collect(list, rect, gen, out);
    } else {
      for (let r = r0; r <= r1; r++) {
        for (let c = c0; c <= c1; c++) {
          const list = this.buckets.get(this.key(c, r));
          if (list) this.collect(list, rect, gen, out);
        }
      }
    }
    return out;
  }

  private collect(list: T[], rect: Rect, gen: number, out: T[]): void {
    for (const item of list) {
      if (item.stamp === gen) continue;
      item.stamp = gen;
      const b = item.bbox;
      if (b[0] <= rect[2] && rect[0] <= b[2] && b[1] <= rect[3] && rect[1] <= b[3]) out.push(item);
    }
  }
}
