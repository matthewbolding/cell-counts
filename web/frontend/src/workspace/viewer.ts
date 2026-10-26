// viewer.ts — the dark canvas in the middle of the review screen.
//
// Owns the <canvas>, the viewport (viewport.ts) and the tile layer (tiles.ts),
// and renders on demand (one requestAnimationFrame per burst of changes, not a
// permanent loop). What gets drawn on top of the tiles is the current Scene's
// business — a channel's cells, the composite overlay, rescan proposals — and
// left-button gestures go to a GestureHandler (interactions.ts). The viewer
// itself only handles navigation, the same bindings as review.py adapted to
// trackpads: wheel/two-finger scroll pans, Shift+wheel pans sideways,
// Ctrl/⌘+wheel and pinch zoom toward the pointer, middle-drag pans, and holding
// the right button hides every overlay to show the plain image.

import { CANVAS_BG, ZOOM_STEP } from "./constants";
import { TileLayer, type TiledImage } from "./tiles";
import type { Point } from "./types";
import { Viewport } from "./viewport";

export interface Scene {
  /** Background image; null shows `placeholder` instead. */
  image: TiledImage | null;
  placeholder?: string;
  /** Overlays drawn with the context in image coordinates. */
  drawImageSpace?(ctx: CanvasRenderingContext2D, view: Viewport): void;
}

export interface PointerInfo {
  sx: number; // screen (CSS px, relative to the canvas)
  sy: number;
  x: number; // image px
  y: number;
  shift: boolean;
  ctrl: boolean; // Ctrl, or ⌘ on macOS
}

export interface GestureHandler {
  press(p: PointerInfo): void;
  drag(p: PointerInfo): void;
  release(p: PointerInfo): void;
  cancel(): void;
  /** Live feedback (drag rectangle, lasso, in-progress polygon) in screen px. */
  drawFeedback(ctx: CanvasRenderingContext2D, view: Viewport): void;
}

const LINE_DELTA_PX = 33; // a "line" of wheel scrolling (deltaMode 1) in px
const WHEEL_ZOOM_PER_100PX = ZOOM_STEP; // one classic wheel notch (~100 px) = one − / + press

export class Viewer {
  readonly root: HTMLElement;
  readonly canvas: HTMLCanvasElement;
  readonly view = new Viewport();
  scene: Scene = { image: null, placeholder: "" };
  gestures: GestureHandler | null = null;
  /** Called after any pan/zoom the viewer did itself (wheel, middle-drag). */
  onViewChanged: () => void = () => {};
  /** Called once the canvas has its real size for the first time. */
  onFirstLayout: () => void = () => {};
  /** False until the canvas has been laid out; until then the viewport's size
   * is a placeholder and fitting an image to it would be wrong. */
  sized = false;

  private readonly ctx: CanvasRenderingContext2D;
  private readonly tiles: TileLayer;
  private frame = 0;
  private overlaysHidden = false;
  private panDrag: { sx: number; sy: number; pointerId: number } | null = null;
  private leftPointer: number | null = null;

  constructor() {
    this.canvas = document.createElement("canvas");
    this.canvas.className = "ws-canvas";
    this.canvas.tabIndex = 0;
    this.canvas.setAttribute("aria-label", "Image viewer");
    this.root = document.createElement("div");
    this.root.className = "ws-canvas-frame";
    this.root.append(this.canvas);
    this.ctx = this.canvas.getContext("2d", { alpha: false })!;
    this.tiles = new TileLayer(() => this.requestRender());

    new ResizeObserver(() => this.resize()).observe(this.root);
    this.canvas.addEventListener("wheel", (e) => this.onWheel(e), { passive: false });
    this.canvas.addEventListener("pointerdown", (e) => this.onPointerDown(e));
    this.canvas.addEventListener("pointermove", (e) => this.onPointerMove(e));
    this.canvas.addEventListener("pointerup", (e) => this.onPointerUp(e));
    this.canvas.addEventListener("pointercancel", (e) => this.onPointerCancel(e));
    this.canvas.addEventListener("contextmenu", (e) => e.preventDefault());
    this.canvas.addEventListener("auxclick", (e) => e.preventDefault());
  }

  setCursor(cursor: string): void {
    this.canvas.style.cursor = cursor;
  }

  setScene(scene: Scene): void {
    this.scene = scene;
    this.requestRender();
  }

  requestRender(): void {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      this.render();
    });
  }

  // ------------------------------------------------------------------ //
  // Rendering
  // ------------------------------------------------------------------ //
  private resize(): void {
    const rect = this.root.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.view.resize(rect.width, rect.height);
    this.canvas.width = Math.max(1, Math.round(rect.width * dpr));
    this.canvas.height = Math.max(1, Math.round(rect.height * dpr));
    this.canvas.style.width = `${rect.width}px`;
    this.canvas.style.height = `${rect.height}px`;
    if (!this.sized && rect.width > 0 && rect.height > 0) {
      this.sized = true;
      this.onFirstLayout();
    }
    this.render();
  }

  private render(): void {
    const ctx = this.ctx;
    const dpr = window.devicePixelRatio || 1;
    const { view, scene } = this;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = CANVAS_BG;
    ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);

    if (!scene.image) {
      if (scene.placeholder) {
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.fillStyle = "#9a9a9a";
        ctx.font = "13px -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(scene.placeholder, view.width / 2, view.height / 2);
      }
      return;
    }

    const s = view.scale * dpr;
    ctx.setTransform(s, 0, 0, s, -view.originX * s, -view.originY * s);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    this.tiles.draw(ctx, view, dpr, scene.image);
    if (!this.overlaysHidden) scene.drawImageSpace?.(ctx, view);

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (!this.overlaysHidden) this.gestures?.drawFeedback(ctx, view);
  }

  // ------------------------------------------------------------------ //
  // Navigation
  // ------------------------------------------------------------------ //
  private onWheel(e: WheelEvent): void {
    e.preventDefault();
    if (!this.scene.image) return;
    const unit = e.deltaMode === 1 ? LINE_DELTA_PX : e.deltaMode === 2 ? this.view.height : 1;
    let dx = e.deltaX * unit;
    let dy = e.deltaY * unit;
    const [sx, sy] = this.local(e);
    if (e.ctrlKey || e.metaKey) {
      // Ctrl/⌘+wheel, and trackpad pinch (which browsers report as ctrl+wheel).
      this.view.zoomAt(WHEEL_ZOOM_PER_100PX ** (-dy / 100), sx, sy);
    } else {
      if (e.shiftKey && dx === 0) [dx, dy] = [dy, 0]; // a plain mouse wheel with Shift pans sideways
      this.view.pan(dx, dy);
    }
    this.onViewChanged();
    this.requestRender();
  }

  zoomBy(factor: number): void {
    if (this.view.zoomAt(factor)) {
      this.onViewChanged();
      this.requestRender();
    }
  }

  private local(e: MouseEvent): Point {
    const r = this.canvas.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  }

  private info(e: PointerEvent): PointerInfo {
    const [sx, sy] = this.local(e);
    const [x, y] = this.view.screenToImage(sx, sy);
    return { sx, sy, x, y, shift: e.shiftKey, ctrl: e.ctrlKey || e.metaKey };
  }

  private onPointerDown(e: PointerEvent): void {
    this.canvas.focus({ preventScroll: true });
    if (e.button === 1) {
      e.preventDefault();
      const [sx, sy] = this.local(e);
      this.panDrag = { sx, sy, pointerId: e.pointerId };
      this.canvas.setPointerCapture(e.pointerId);
      this.canvas.classList.add("panning");
      return;
    }
    if (e.button === 2) {
      this.overlaysHidden = true;
      this.canvas.setPointerCapture(e.pointerId);
      this.requestRender();
      return;
    }
    if (e.button !== 0 || !this.scene.image) return;
    this.leftPointer = e.pointerId;
    this.canvas.setPointerCapture(e.pointerId);
    this.gestures?.press(this.info(e));
  }

  private onPointerMove(e: PointerEvent): void {
    if (this.panDrag && e.pointerId === this.panDrag.pointerId) {
      const [sx, sy] = this.local(e);
      this.view.pan(this.panDrag.sx - sx, this.panDrag.sy - sy);
      this.panDrag.sx = sx;
      this.panDrag.sy = sy;
      this.onViewChanged();
      this.requestRender();
      return;
    }
    if (this.leftPointer === e.pointerId) this.gestures?.drag(this.info(e));
  }

  private onPointerUp(e: PointerEvent): void {
    if (this.panDrag && e.pointerId === this.panDrag.pointerId) {
      this.panDrag = null;
      this.canvas.classList.remove("panning");
      return;
    }
    if (e.button === 2 && this.overlaysHidden) {
      this.overlaysHidden = false;
      this.requestRender();
      return;
    }
    if (this.leftPointer === e.pointerId) {
      this.leftPointer = null;
      this.gestures?.release(this.info(e));
    }
  }

  private onPointerCancel(e: PointerEvent): void {
    if (this.leftPointer === e.pointerId) {
      this.leftPointer = null;
      this.gestures?.cancel();
    }
    this.panDrag = null;
    this.overlaysHidden = false;
    this.requestRender();
  }
}
