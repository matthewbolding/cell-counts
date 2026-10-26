// interactions.ts — what a left-button press, drag and release mean in each mode,
// ported from review.py's _on_canvas_press/_drag/_release:
//
//   Review   click toggles a cell; drag selects (lasso or rectangle) to mark many
//   Draw     polygon: click vertices, double-click or Enter closes; freehand: drag
//   Delete   click removes a cell; drag removes a lasso/rectangle selection
//   Rescan   drag a rectangle to rescan that region
//
// plus the two modes that override the mode selector: the Composite tab is view
// only, and rescan review turns clicks into accept/un-accept. This module only
// tracks the gesture and draws its live feedback; what an outcome *does* is the
// host's job (workspace.ts).

import { DOUBLE_CLICK_MS, DOUBLE_CLICK_SCREEN_PX, DRAG_THRESHOLD, IN_PROGRESS_COLOR, MIN_DRAG_POINT_PX } from "./constants";
import { normalizeRect } from "./geometry";
import { drawInProgress, drawRect, drawStroke } from "./overlay";
import type { DrawStyle, Point, Rect, SelectionStyle } from "./types";
import type { GestureHandler, PointerInfo } from "./viewer";
import type { Viewport } from "./viewport";

export type Mode = "review" | "draw" | "delete" | "rescan";

/** What clicking on the canvas means right now, independent of the mode. */
export type Context = "edit" | "readonly" | "composite" | "rescan-review";

export type Region = { kind: "rect"; rect: Rect } | { kind: "lasso"; points: Point[] };

export interface InteractionHost {
  readonly mode: Mode;
  readonly selectionStyle: SelectionStyle;
  readonly drawStyle: DrawStyle;
  context(): Context;
  requestRender(): void;
  reviewClick(x: number, y: number): void;
  reviewSelect(region: Region): void;
  deleteClick(x: number, y: number): void;
  deleteSelect(region: Region): void;
  /** Close a drawn outline (the host rejects one with fewer than 3 points). */
  commitDraw(points: Point[]): void;
  rescanRegion(rect: Rect): void;
  rescanReviewClick(x: number, y: number): void;
}

export class Interactions implements GestureHandler {
  private readonly host: InteractionHost;
  private press0: PointerInfo | null = null;
  private dragging = false;
  private rectEnd: Point | null = null;
  private strokeScreen: Point[] = []; // lasso or freehand stroke, for feedback
  private lasso: Point[] = []; // image px
  /** Vertices of the outline being drawn (image px), polygon or freehand. */
  drawPoints: Point[] = [];
  private lastClick: { t: number; sx: number; sy: number } | null = null;

  constructor(host: InteractionHost) {
    this.host = host;
  }

  /** Forget any half-finished gesture (mode change, image change, Esc). */
  reset(): void {
    this.press0 = null;
    this.dragging = false;
    this.rectEnd = null;
    this.strokeScreen = [];
    this.lasso = [];
    this.drawPoints = [];
    this.lastClick = null;
    this.host.requestRender();
  }

  get hasDrawing(): boolean {
    return this.drawPoints.length > 0;
  }

  press(p: PointerInfo): void {
    this.press0 = p;
    this.dragging = false;
    this.rectEnd = null;
    this.strokeScreen = [];
    this.lasso = [];
  }

  private freehandDraw(): boolean {
    return this.host.mode === "draw" && this.host.drawStyle === "freehand";
  }

  private lassoSelect(): boolean {
    return (this.host.mode === "review" || this.host.mode === "delete") && this.host.selectionStyle === "lasso";
  }

  drag(p: PointerInfo): void {
    const start = this.press0;
    const context = this.host.context();
    if (!start || context !== "edit") return;
    if (!this.dragging) {
      if (Math.abs(p.sx - start.sx) < DRAG_THRESHOLD && Math.abs(p.sy - start.sy) < DRAG_THRESHOLD) return;
      const mode = this.host.mode;
      if (mode !== "review" && mode !== "delete" && mode !== "rescan" && !this.freehandDraw()) return;
      this.dragging = true;
      this.strokeScreen = [[start.sx, start.sy]];
      if (this.freehandDraw()) this.drawPoints = [[start.x, start.y]];
      else if (this.lassoSelect()) this.lasso = [[start.x, start.y]];
    }

    if (this.freehandDraw() || this.lassoSelect()) {
      const last = this.strokeScreen[this.strokeScreen.length - 1];
      if (Math.abs(p.sx - last[0]) < MIN_DRAG_POINT_PX && Math.abs(p.sy - last[1]) < MIN_DRAG_POINT_PX) return;
      this.strokeScreen.push([p.sx, p.sy]);
      (this.freehandDraw() ? this.drawPoints : this.lasso).push([p.x, p.y]);
    } else {
      this.rectEnd = [p.sx, p.sy];
    }
    this.host.requestRender();
  }

  release(p: PointerInfo): void {
    const start = this.press0;
    if (!start) return;
    const wasDragging = this.dragging;
    const lasso = this.lasso;
    this.press0 = null;
    this.dragging = false;
    this.rectEnd = null;
    this.strokeScreen = [];
    this.lasso = [];
    this.host.requestRender();

    const context = this.host.context();
    if (context === "composite" || context === "readonly") return;
    if (context === "rescan-review") {
      if (!wasDragging) this.host.rescanReviewClick(p.x, p.y);
      return;
    }

    const rect = normalizeRect(start.x, start.y, p.x, p.y);
    const region: Region = this.host.selectionStyle === "lasso"
      ? { kind: "lasso", points: lasso }
      : { kind: "rect", rect };

    switch (this.host.mode) {
      case "review":
        if (wasDragging) this.host.reviewSelect(region);
        else this.host.reviewClick(p.x, p.y);
        break;
      case "delete":
        if (wasDragging) this.host.deleteSelect(region);
        else this.host.deleteClick(p.x, p.y);
        break;
      case "draw":
        if (wasDragging && this.host.drawStyle === "freehand") {
          const points = this.drawPoints;
          this.drawPoints = [];
          this.host.commitDraw(points);
        } else if (!wasDragging && this.host.drawStyle === "polygon") {
          this.polygonClick(p);
        }
        break;
      case "rescan":
        if (wasDragging) this.host.rescanRegion(rect);
        break;
    }
  }

  cancel(): void {
    this.press0 = null;
    this.dragging = false;
    this.rectEnd = null;
    this.strokeScreen = [];
    this.lasso = [];
    if (this.freehandDraw()) this.drawPoints = [];
    this.host.requestRender();
  }

  /** review.py's own double-click detection: the second click of a pair must
   * close the outline, not add a near-duplicate vertex first. */
  private polygonClick(p: PointerInfo): void {
    const now = performance.now();
    const last = this.lastClick;
    const isDouble = last !== null && now - last.t < DOUBLE_CLICK_MS
      && Math.abs(p.sx - last.sx) < DOUBLE_CLICK_SCREEN_PX && Math.abs(p.sy - last.sy) < DOUBLE_CLICK_SCREEN_PX;
    if (isDouble) {
      this.lastClick = null;
      this.commitPolygon();
      return;
    }
    this.lastClick = { t: now, sx: p.sx, sy: p.sy };
    this.drawPoints.push([p.x, p.y]);
    this.host.requestRender();
  }

  /** Enter, or a double-click: close the polygon being drawn. */
  commitPolygon(): void {
    if (this.host.mode !== "draw" || !this.drawPoints.length) return;
    const points = this.drawPoints;
    this.drawPoints = [];
    this.host.requestRender();
    this.host.commitDraw(points);
  }

  drawFeedback(ctx: CanvasRenderingContext2D, view: Viewport): void {
    if (this.drawPoints.length && this.host.mode === "draw") {
      const screen = this.drawPoints.map(([x, y]) => view.imageToScreen(x, y));
      if (this.dragging) drawStroke(ctx, screen, IN_PROGRESS_COLOR, false);
      else drawInProgress(ctx, screen);
    }
    if (!this.dragging || !this.press0) return;
    if (this.lassoSelect()) drawStroke(ctx, this.strokeScreen, "#ffffff", true);
    else if (this.rectEnd && !this.freehandDraw()) drawRect(ctx, [this.press0.sx, this.press0.sy], this.rectEnd);
  }
}
