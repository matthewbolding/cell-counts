// cellStore.ts — one image's cells in memory: the cells themselves (the
// `.cells.json` shape), their bounding boxes, and a spatial index over them, plus
// the queries the review screen needs (hit test, rectangle/lasso selection,
// counts). Hit-testing: the smallest cell whose outline contains the point wins;
// failing that, the nearest centroid within 10 screen px.

import { bboxOf, pointInAnyRing, pointInPolygon } from "./geometry";
import { GridIndex } from "./spatialIndex";
import type { Cell, CellStatus, Point, Rect } from "./types";

export interface CellEntry {
  cell: Cell;
  bbox: Rect;
  stamp?: number;
}

export class CellStore {
  readonly imageId: string;
  version: number;
  private readonly entries = new Map<number, CellEntry>();
  private readonly index = new GridIndex<CellEntry>();

  constructor(imageId: string, version: number, cells: Cell[]) {
    this.imageId = imageId;
    this.version = version;
    for (const cell of cells) this.insert(cell);
  }

  get size(): number {
    return this.entries.size;
  }

  private insert(cell: Cell): void {
    if (!cell.polygons.length || !cell.polygons[0].length) return;
    const entry: CellEntry = { cell, bbox: bboxOf(cell.polygons) };
    this.entries.set(cell.id, entry);
    this.index.insert(entry);
  }

  has(id: number): boolean {
    return this.entries.has(id);
  }

  get(id: number): Cell | undefined {
    return this.entries.get(id)?.cell;
  }

  add(cell: Cell): void {
    if (this.entries.has(cell.id)) this.remove(cell.id);
    this.insert(cell);
  }

  remove(id: number): Cell | undefined {
    const entry = this.entries.get(id);
    if (!entry) return undefined;
    this.index.remove(entry);
    this.entries.delete(id);
    return entry.cell;
  }

  setStatus(id: number, status: CellStatus, edited: boolean): void {
    const cell = this.entries.get(id)?.cell;
    if (cell) {
      cell.status = status;
      cell.edited = edited;
    }
  }

  all(): CellEntry[] {
    return Array.from(this.entries.values());
  }

  query(rect: Rect): CellEntry[] {
    return this.index.query(rect);
  }

  maxId(): number {
    let max = 0;
    for (const id of this.entries.keys()) if (id > max) max = id;
    return max;
  }

  counts(): { kept: number; filtered: number; total: number; edited: number } {
    let kept = 0;
    let edited = 0;
    for (const { cell } of this.entries.values()) {
      if (cell.status === "kept") kept++;
      if (cell.edited) edited++;
    }
    return { kept, filtered: this.entries.size - kept, total: this.entries.size, edited };
  }

  hitTest(x: number, y: number, scale: number): Cell | null {
    return hitTest(this.query([x - 10 / scale, y - 10 / scale, x + 10 / scale, y + 10 / scale]), x, y, scale);
  }

  /** Cells whose centroid falls inside the rectangle. */
  inRect(rect: Rect): Cell[] {
    const [x0, y0, x1, y1] = rect;
    return this.query(rect)
      .map((e) => e.cell)
      .filter((c) => c.centroid[0] >= x0 && c.centroid[0] <= x1 && c.centroid[1] >= y0 && c.centroid[1] <= y1);
  }

  /** Cells whose centroid falls inside the lasso. */
  inLasso(points: Point[]): Cell[] {
    if (points.length < 3) return [];
    const bbox = bboxOf([points]);
    return this.query(bbox)
      .map((e) => e.cell)
      .filter((c) => pointInPolygon(c.centroid[0], c.centroid[1], points));
  }
}

/** The hit test over an arbitrary set of entries (also used for rescan
 * proposals, which aren't in a store). */
export function hitTest(entries: CellEntry[], x: number, y: number, scale: number): Cell | null {
  const pad = 2 / scale;
  let best: Cell | null = null;
  for (const { cell, bbox } of entries) {
    if (x >= bbox[0] - pad && x <= bbox[2] + pad && y >= bbox[1] - pad && y <= bbox[3] + pad) {
      if (pointInAnyRing(x, y, cell.polygons) && (best === null || cell.area < best.area)) best = cell;
    }
  }
  if (best) return best;

  let bestD = 10 / scale;
  for (const { cell } of entries) {
    const d = Math.hypot(cell.centroid[0] - x, cell.centroid[1] - y);
    if (d < bestD) {
      best = cell;
      bestD = d;
    }
  }
  return best;
}

export function entriesFor(cells: Cell[]): CellEntry[] {
  return cells.filter((c) => c.polygons.length && c.polygons[0].length).map((cell) => ({ cell, bbox: bboxOf(cell.polygons) }));
}
