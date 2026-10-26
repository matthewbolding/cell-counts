import { describe, expect, it } from "vitest";

import { CellStore, hitTest, entriesFor } from "../cellStore";
import { bboxOf, pointInPolygon, shoelaceAreaCentroid } from "../geometry";
import { GridIndex } from "../spatialIndex";
import { levelForScale, levelSize, tileRange } from "../tiles";
import type { Cell } from "../types";
import { AddCells, ModifyCells, RemoveCells, UndoHistory } from "../undo";
import { Viewport } from "../viewport";

const square = (id: number, cx: number, cy: number, r = 5, status: Cell["status"] = "kept"): Cell => ({
  id,
  polygons: [[[cx - r, cy - r], [cx + r, cy - r], [cx + r, cy + r], [cx - r, cy + r]]],
  centroid: [cx, cy],
  area: (2 * r) ** 2,
  status,
  source: "model",
  edited: false,
});

describe("geometry", () => {
  it("ray-casts points in and out of a polygon", () => {
    const tri = [[0, 0], [10, 0], [0, 10]];
    expect(pointInPolygon(2, 2, tri)).toBe(true);
    expect(pointInPolygon(8, 8, tri)).toBe(false);
  });

  it("rounds the shoelace area and centroid like the server", () => {
    expect(shoelaceAreaCentroid([[0, 0], [4, 0], [4, 3], [0, 3]])).toEqual({ area: 12, centroid: [2, 1.5] });
    // Degenerate (collinear) rings fall back to the vertex average.
    expect(shoelaceAreaCentroid([[0, 0], [1, 1], [2, 2]])).toEqual({ area: 0, centroid: [1, 1] });
  });

  it("computes bounding boxes across rings", () => {
    expect(bboxOf([[[1, 5], [3, 2]], [[-1, 0], [0, 9]]])).toEqual([-1, 0, 3, 9]);
  });
});

describe("GridIndex", () => {
  it("returns each intersecting item once, and forgets removed items", () => {
    const index = new GridIndex<{ bbox: [number, number, number, number]; id: number }>(10);
    const big = { bbox: [0, 0, 95, 95] as [number, number, number, number], id: 1 };
    const small = { bbox: [200, 200, 205, 205] as [number, number, number, number], id: 2 };
    index.insert(big);
    index.insert(small);
    expect(index.query([50, 50, 60, 60]).map((i) => i.id)).toEqual([1]);
    expect(index.query([-1000, -1000, 1000, 1000]).map((i) => i.id).sort()).toEqual([1, 2]);
    index.remove(big);
    expect(index.query([50, 50, 60, 60])).toEqual([]);
    expect(index.size).toBe(1);
  });
});

describe("Viewport (origin/scale model)", () => {
  it("fits by the limiting axis and zooms about a fixed point", () => {
    const v = new Viewport();
    v.resize(800, 600);
    v.fit(2000, 1000);
    expect(v.scale).toBeCloseTo(0.4);
    const before = v.screenToImage(100, 100);
    v.zoomAt(2, 100, 100);
    const after = v.screenToImage(100, 100);
    expect(after[0]).toBeCloseTo(before[0]);
    expect(after[1]).toBeCloseTo(before[1]);
  });
});

describe("tile pyramid math (matches processing/tiles.py)", () => {
  it("picks the coarsest level that still has a tile pixel per device pixel", () => {
    expect(levelForScale(1, 1, 6)).toBe(0);
    expect(levelForScale(0.5, 1, 6)).toBe(1);
    expect(levelForScale(0.5, 2, 6)).toBe(0); // retina: 1 device px per image px
    expect(levelForScale(0.01, 1, 6)).toBe(5); // clamped to the last level
  });

  it("uses PIL's ceil rounding per level", () => {
    expect(levelSize(13246, 10961, 5)).toEqual([414, 343]);
    expect(tileRange([0, 0, 13246, 10961], 13246, 10961, 0, 512)).toEqual({ c0: 0, r0: 0, c1: 25, r1: 21 });
    expect(tileRange([-50, -50, -1, -1], 1000, 1000, 0, 512)).toBeNull();
  });
});

describe("CellStore hit testing", () => {
  it("prefers the smallest containing cell, then the nearest centroid within 10 screen px", () => {
    const store = new CellStore("img", 1, [square(1, 50, 50, 20), square(2, 50, 50, 5), square(3, 200, 200, 2)]);
    expect(store.hitTest(51, 51, 1)?.id).toBe(2);
    expect(store.hitTest(206, 200, 1)?.id).toBe(3); // outside the ring, centroid 6 px away
    expect(store.hitTest(215, 200, 1)).toBeNull();
  });

  it("selects by centroid for rectangles and lassos", () => {
    const store = new CellStore("img", 1, [square(1, 10, 10), square(2, 50, 50), square(3, 90, 10)]);
    expect(store.inRect([0, 0, 60, 60]).map((c) => c.id).sort()).toEqual([1, 2]);
    expect(store.inLasso([[0, 0], [100, 0], [100, 20], [0, 20]]).map((c) => c.id).sort()).toEqual([1, 3]);
  });

  it("hit-tests a free list of entries (rescan proposals)", () => {
    expect(hitTest(entriesFor([square(7, 5, 5)]), 5, 5, 1)?.id).toBe(7);
  });
});

describe("undo history -> server ops", () => {
  it("records one step per gesture and turns undo/redo into ops", () => {
    const store = new CellStore("img", 1, [square(1, 10, 10), square(2, 30, 30)]);
    const history = new UndoHistory();
    const marked = history.perform(store, ModifyCells.setStatus([store.get(1)!, store.get(2)!], "filtered"));
    expect(marked).toEqual([{ op: "update", changes: [
      { id: 1, status: "filtered", edited: true }, { id: 2, status: "filtered", edited: true }] }]);
    expect(store.counts()).toMatchObject({ kept: 0, edited: 2 });

    expect(history.undo(store)).toEqual([{ op: "update", changes: [
      { id: 1, status: "kept", edited: false }, { id: 2, status: "kept", edited: false }] }]);
    expect(store.counts()).toMatchObject({ kept: 2, edited: 0 });

    history.perform(store, new RemoveCells([store.get(1)!]));
    expect(history.canRedo("img")).toBe(false); // a fresh edit clears redo
    expect(history.undo(store)?.[0].op).toBe("add");
    expect(store.has(1)).toBe(true);

    const drawn = { ...square(3, 60, 60), source: "human", edited: true };
    expect(history.perform(store, new AddCells([drawn]))[0]).toMatchObject({ op: "add" });
    expect(history.undo(store)).toEqual([{ op: "remove", ids: [3] }]);
    expect(history.redo(store)?.[0].op).toBe("add");
    expect(store.maxId()).toBe(3);
  });

  it("keeps history per image", () => {
    const a = new CellStore("a", 1, [square(1, 1, 1)]);
    const b = new CellStore("b", 1, [square(1, 1, 1)]);
    const history = new UndoHistory();
    history.perform(a, ModifyCells.setStatus([a.get(1)!], "filtered"));
    expect(history.undo(b)).toBeNull();
    expect(history.canUndo("a")).toBe(true);
  });
});
