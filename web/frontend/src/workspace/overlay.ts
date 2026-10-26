// overlay.ts — cell outlines and masks.
//
// Kept cells in the channel color, filtered cells in a fixed
// neutral gray, masks at alpha 110/255, outline width clamp(round(1.5 * scale),
// 1, 4) screen px, and the level-of-detail cutoff — a cell under 8 screen px
// across is drawn as a dot sized to its footprint instead of its full ring.
//
// Different mechanics: instead of rasterizing one PIL layer, every cell of one
// style goes into a single Path2D that is filled/stroked once, with the context
// already transformed to image coordinates. That keeps a fit-to-window frame of
// the largest images (~25k cells) to a few thousand path commands and one draw
// call per style. Rings far larger than their on-screen size are thinned to
// roughly one vertex per screen pixel.

import {
  FILTERED_COLOR,
  IN_PROGRESS_COLOR,
  LOD_MIN_SCREEN_PX,
  MASK_FILL_ALPHA,
  OUTLINE_WIDTH_BASE,
  OUTLINE_WIDTH_MAX,
  OUTLINE_WIDTH_MIN,
} from "./constants";
import type { CellEntry } from "./cellStore";
import type { Point, RenderMode } from "./types";

export function hexToRgba(hex: string, alpha: number): string {
  const h = hex.replace("#", "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

export function outlineWidth(scale: number): number {
  return Math.max(OUTLINE_WIDTH_MIN, Math.min(OUTLINE_WIDTH_MAX, Math.round(OUTLINE_WIDTH_BASE * scale)));
}

export interface CellStyle {
  mode: RenderMode;
  color: string; // #rrggbb for kept cells
  /** Rescan review: every proposal draws as kept, whatever the model's filters said. */
  allKept?: boolean;
}

interface Paths {
  rings: Path2D;
  dots: Path2D;
  any: boolean;
}

const newPaths = (): Paths => ({ rings: new Path2D(), dots: new Path2D(), any: false });

function addCell(paths: Paths, entry: CellEntry, scale: number): void {
  const [x0, y0, x1, y1] = entry.bbox;
  const footprint = Math.max(x1 - x0, y1 - y0) * scale;
  paths.any = true;
  if (footprint < LOD_MIN_SCREEN_PX) {
    const r = Math.max(1.5, footprint / 2) / scale;
    const cx = (x0 + x1) / 2;
    const cy = (y0 + y1) / 2;
    paths.dots.moveTo(cx + r, cy);
    paths.dots.arc(cx, cy, r, 0, Math.PI * 2);
    return;
  }
  const target = Math.max(16, Math.ceil(footprint * 1.5));
  for (const ring of entry.cell.polygons) {
    if (ring.length < 2) continue;
    const stride = Math.max(1, Math.floor(ring.length / target));
    paths.rings.moveTo(ring[0][0], ring[0][1]);
    for (let i = stride; i < ring.length; i += stride) paths.rings.lineTo(ring[i][0], ring[i][1]);
    paths.rings.closePath();
  }
}

function paint(ctx: CanvasRenderingContext2D, paths: Paths, mode: RenderMode, color: string, scale: number): void {
  if (!paths.any) return;
  ctx.fillStyle = color;
  ctx.fill(paths.dots);
  if (mode === "mask") {
    ctx.fill(paths.rings);
  } else {
    ctx.strokeStyle = color;
    ctx.lineWidth = outlineWidth(scale) / scale;
    ctx.lineJoin = "round";
    ctx.stroke(paths.rings);
  }
}

/** Draw cells; `ctx` must be in image coordinates (Viewer.withImageTransform). */
export function drawCells(ctx: CanvasRenderingContext2D, entries: CellEntry[], scale: number, style: CellStyle): void {
  const kept = newPaths();
  const filtered = newPaths();
  for (const entry of entries) {
    addCell(style.allKept || entry.cell.status === "kept" ? kept : filtered, entry, scale);
  }
  const keptColor = hexToRgba(style.color, style.mode === "mask" ? MASK_FILL_ALPHA : 1);
  paint(ctx, filtered, style.mode, FILTERED_COLOR, scale);
  paint(ctx, kept, style.mode, keptColor, scale);
}

/** An unclosed draw-mode polygon: yellow line plus a dot per vertex
 * (rendering.render_in_progress_polygon). Screen coordinates. */
export function drawInProgress(ctx: CanvasRenderingContext2D, screenPoints: Point[]): void {
  if (!screenPoints.length) return;
  ctx.strokeStyle = IN_PROGRESS_COLOR;
  ctx.fillStyle = IN_PROGRESS_COLOR;
  ctx.lineWidth = 2;
  ctx.setLineDash([]);
  if (screenPoints.length >= 2) {
    ctx.beginPath();
    ctx.moveTo(screenPoints[0][0], screenPoints[0][1]);
    for (const [x, y] of screenPoints.slice(1)) ctx.lineTo(x, y);
    ctx.stroke();
  }
  for (const [x, y] of screenPoints) {
    ctx.beginPath();
    ctx.arc(x, y, 3, 0, Math.PI * 2);
    ctx.fill();
  }
}

/** Live drag feedback (selection rectangle, lasso, freehand stroke). Screen
 * coordinates. */
export function drawStroke(ctx: CanvasRenderingContext2D, screenPoints: Point[], color: string, dashed: boolean): void {
  if (screenPoints.length < 2) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.setLineDash(dashed ? [4, 2] : []);
  ctx.beginPath();
  ctx.moveTo(screenPoints[0][0], screenPoints[0][1]);
  for (const [x, y] of screenPoints.slice(1)) ctx.lineTo(x, y);
  ctx.stroke();
  ctx.setLineDash([]);
}

export function drawRect(ctx: CanvasRenderingContext2D, a: Point, b: Point): void {
  ctx.strokeStyle = "#ffffff";
  ctx.lineWidth = 2;
  ctx.setLineDash([4, 2]);
  ctx.strokeRect(Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.abs(b[0] - a[0]), Math.abs(b[1] - a[1]));
  ctx.setLineDash([]);
}
