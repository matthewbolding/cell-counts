// geometry.ts — pure polygon math: a ray-casting point-in-polygon test and the
// shoelace area/centroid, rounded the same way the server's segmentation rounds
// them, so a hand-drawn cell gets exactly the area/centroid a detected one would.

import type { Point, Rect } from "./types";

/** Ray-casting point-in-polygon; `ring` is a list of [x, y]. */
export function pointInPolygon(x: number, y: number, ring: ArrayLike<number[] | Point>): boolean {
  let inside = false;
  const n = ring.length;
  if (n < 3) return false;
  let [xj, yj] = ring[n - 1];
  for (let i = 0; i < n; i++) {
    const [xi, yi] = ring[i];
    if (yi > y !== yj > y) {
      const xCross = ((xj - xi) * (y - yi)) / (yj - yi) + xi;
      if (x < xCross) inside = !inside;
    }
    xj = xi;
    yj = yi;
  }
  return inside;
}

/** True if (x, y) is inside any of a cell's rings (a detection can be several
 * disconnected clusters, hence several rings). */
export function pointInAnyRing(x: number, y: number, rings: number[][][]): boolean {
  return rings.some((ring) => pointInPolygon(x, y, ring));
}

export function bboxOf(polygons: number[][][]): Rect {
  let x0 = Infinity;
  let y0 = Infinity;
  let x1 = -Infinity;
  let y1 = -Infinity;
  for (const ring of polygons) {
    for (const [x, y] of ring) {
      if (x < x0) x0 = x;
      if (y < y0) y0 = y;
      if (x > x1) x1 = x;
      if (y > y1) y1 = y;
    }
  }
  return [x0, y0, x1, y1];
}

export const round2 = (v: number) => Math.round(v * 100) / 100;

/** Shoelace area + centroid of one ring: area rounded to an integer, centroid
 * to 2 decimals; degenerate rings fall back to the vertex average. */
export function shoelaceAreaCentroid(ring: number[][]): { area: number; centroid: Point } {
  const n = ring.length;
  if (n < 3) return { area: 0, centroid: [0, 0] };
  let area2 = 0;
  let cx = 0;
  let cy = 0;
  for (let i = 0; i < n; i++) {
    const [x0, y0] = ring[i];
    const [x1, y1] = ring[(i + 1) % n];
    const cross = x0 * y1 - x1 * y0;
    area2 += cross;
    cx += (x0 + x1) * cross;
    cy += (y0 + y1) * cross;
  }
  const area = area2 / 2;
  if (Math.abs(area) < 1e-6) {
    const sx = ring.reduce((s, p) => s + p[0], 0);
    const sy = ring.reduce((s, p) => s + p[1], 0);
    return { area: 0, centroid: [round2(sx / n), round2(sy / n)] };
  }
  cx /= 6 * area;
  cy /= 6 * area;
  return { area: Math.round(Math.abs(area)), centroid: [round2(cx), round2(cy)] };
}

export function rectsIntersect(a: Rect, b: Rect): boolean {
  return a[0] <= b[2] && b[0] <= a[2] && a[1] <= b[3] && b[1] <= a[3];
}

export function normalizeRect(x0: number, y0: number, x1: number, y1: number): Rect {
  return [Math.min(x0, x1), Math.min(y0, y1), Math.max(x0, x1), Math.max(y0, y1)];
}

export function polygonBbox(points: Point[]): Rect {
  return bboxOf([points]);
}
