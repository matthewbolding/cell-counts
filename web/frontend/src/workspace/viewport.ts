// viewport.ts — the screen <-> image mapping, the same model as review.py:
// `origin` is the image-space point at the canvas's top-left and `scale` is CSS
// pixels per image pixel. Everything else (fit, zoom-about-a-point, pan) is
// arithmetic on those two, which keeps hit-testing exact at any zoom.

import { MAX_SCALE, MIN_SCALE } from "./constants";
import type { Point, Rect } from "./types";

export interface ViewSnapshot {
  originX: number;
  originY: number;
  scale: number;
}

export class Viewport {
  originX = 0;
  originY = 0;
  scale = 1;
  width = 100; // CSS px
  height = 100;

  resize(width: number, height: number): void {
    this.width = Math.max(1, width);
    this.height = Math.max(1, height);
  }

  screenToImage(sx: number, sy: number): Point {
    return [this.originX + sx / this.scale, this.originY + sy / this.scale];
  }

  imageToScreen(x: number, y: number): Point {
    return [(x - this.originX) * this.scale, (y - this.originY) * this.scale];
  }

  visibleRect(): Rect {
    return [this.originX, this.originY, this.originX + this.width / this.scale, this.originY + this.height / this.scale];
  }

  /** Scale that fits the whole image in the viewport, nothing cut off: the
   * smaller of the two axis ratios is the limiting one. */
  fitScale(imageWidth: number, imageHeight: number): number {
    return Math.min(this.width / imageWidth, this.height / imageHeight);
  }

  centerOn(imageWidth: number, imageHeight: number): void {
    this.originX = imageWidth / 2 - this.width / this.scale / 2;
    this.originY = imageHeight / 2 - this.height / this.scale / 2;
  }

  /** Fit × multiplier, centered — what landing on a new sample does. */
  fit(imageWidth: number, imageHeight: number, multiplier = 1): void {
    this.scale = clampScale(this.fitScale(imageWidth, imageHeight) * multiplier);
    this.centerOn(imageWidth, imageHeight);
  }

  fitToRect(rect: Rect): void {
    const [x0, y0, x1, y1] = rect;
    const w = Math.max(1, x1 - x0);
    const h = Math.max(1, y1 - y0);
    this.scale = clampScale(Math.min(this.width / w, this.height / h));
    const cx = (x0 + x1) / 2;
    const cy = (y0 + y1) / 2;
    this.originX = cx - this.width / this.scale / 2;
    this.originY = cy - this.height / this.scale / 2;
  }

  /** Zoom by `factor`, keeping the image point under (ax, ay) — screen px —
   * fixed. Returns false if the scale was already at its limit. */
  zoomAt(factor: number, ax = this.width / 2, ay = this.height / 2): boolean {
    const newScale = clampScale(this.scale * factor);
    if (Math.abs(newScale - this.scale) < 1e-12) return false;
    const [ix, iy] = this.screenToImage(ax, ay);
    this.scale = newScale;
    this.originX = ix - ax / newScale;
    this.originY = iy - ay / newScale;
    return true;
  }

  /** Move the view by a screen-pixel delta (positive = content moves up/left,
   * like scrolling down/right). */
  pan(dxScreen: number, dyScreen: number): void {
    this.originX += dxScreen / this.scale;
    this.originY += dyScreen / this.scale;
  }

  snapshot(): ViewSnapshot {
    return { originX: this.originX, originY: this.originY, scale: this.scale };
  }

  restore(s: ViewSnapshot): void {
    this.originX = s.originX;
    this.originY = s.originY;
    this.scale = s.scale;
  }
}

export function clampScale(scale: number): number {
  return Math.max(MIN_SCALE, Math.min(MAX_SCALE, scale));
}
