// tiles.ts — draws an image from the server's tile pyramid (processing/tiles.py):
// 512 px PNG tiles, level 0 = full resolution, level L = ceil(dim / 2^L).
//
// Each frame picks the coarsest level that still has at least one tile pixel
// per device pixel, draws whatever coarser levels are already cached
// underneath it (so panning into new territory shows a blurry image at once
// instead of black), and asks for the missing tiles at the chosen level. Decoded
// tiles live in a small LRU (each is ~1 MB of RGBA once decoded), and requests
// for tiles that scrolled out of view before their turn are dropped.

import type { Viewport } from "./viewport";
import type { Rect, TilesInfo } from "./types";

const CACHE_TILES = 160;
const MAX_IN_FLIGHT = 6;
const RETRY_FAILED_MS = 10000;

export function levelForScale(scale: number, dpr: number, levels: number): number {
  const devicePerImagePx = scale * dpr;
  if (devicePerImagePx >= 1) return 0;
  const level = Math.floor(Math.log2(1 / devicePerImagePx));
  return Math.max(0, Math.min(levels - 1, level));
}

export function levelSize(width: number, height: number, level: number): [number, number] {
  const f = 2 ** level;
  return [Math.ceil(width / f), Math.ceil(height / f)];
}

export interface TileRange {
  c0: number;
  r0: number;
  c1: number;
  r1: number;
}

/** Tiles at `level` that intersect `rect` (image px), clamped to the grid;
 * null when the rect misses the image entirely. */
export function tileRange(rect: Rect, width: number, height: number, level: number, tileSize: number): TileRange | null {
  const [lw, lh] = levelSize(width, height, level);
  const cols = Math.ceil(lw / tileSize);
  const rows = Math.ceil(lh / tileSize);
  const span = tileSize * 2 ** level; // image px per tile
  const c0 = Math.max(0, Math.floor(rect[0] / span));
  const r0 = Math.max(0, Math.floor(rect[1] / span));
  const c1 = Math.min(cols - 1, Math.floor(rect[2] / span));
  const r1 = Math.min(rows - 1, Math.floor(rect[3] / span));
  if (c1 < c0 || r1 < r0) return null;
  return { c0, r0, c1, r1 };
}

export function tileUrl(imageId: string, tiles: TilesInfo, level: number, col: number, row: number): string {
  return `/tiles/${imageId}/${tiles.rev}/${level}/${col}_${row}.png`;
}

export interface TiledImage {
  id: string;
  width: number;
  height: number;
  tiles: TilesInfo;
}

class LruCache {
  private readonly map = new Map<string, ImageBitmap>();

  get(key: string): ImageBitmap | undefined {
    const v = this.map.get(key);
    if (v) {
      this.map.delete(key);
      this.map.set(key, v);
    }
    return v;
  }

  peek(key: string): ImageBitmap | undefined {
    return this.map.get(key);
  }

  set(key: string, value: ImageBitmap): void {
    this.map.set(key, value);
    while (this.map.size > CACHE_TILES) {
      const oldest = this.map.keys().next().value as string;
      this.map.get(oldest)?.close();
      this.map.delete(oldest);
    }
  }
}

export class TileLayer {
  private readonly cache = new LruCache();
  private readonly failed = new Map<string, number>();
  private readonly inFlight = new Set<string>();
  private queue: { key: string; url: string }[] = [];
  private wanted = new Set<string>();
  private readonly onLoad: () => void;

  constructor(onLoad: () => void) {
    this.onLoad = onLoad;
  }

  /** Draw `image` under the viewport. The context must already be transformed
   * to image coordinates (see Viewer). Returns true while tiles are missing. */
  draw(ctx: CanvasRenderingContext2D, view: Viewport, dpr: number, image: TiledImage): boolean {
    const { tiles } = image;
    const rect = view.visibleRect();
    const target = levelForScale(view.scale, dpr, tiles.levels);
    const coarsest = tiles.levels - 1;
    // Half a device pixel of overlap hides hairline seams between tiles drawn
    // at fractional positions.
    const seam = 0.5 / (view.scale * dpr);
    const wanted = new Set<string>();
    let missing = false;

    for (let level = coarsest; level >= target; level--) {
      const range = tileRange(rect, image.width, image.height, level, tiles.tile_size);
      if (!range) continue;
      const span = tiles.tile_size * 2 ** level;
      const f = 2 ** level;
      for (let row = range.r0; row <= range.r1; row++) {
        for (let col = range.c0; col <= range.c1; col++) {
          const key = `${image.id}/${tiles.rev}/${level}/${col}_${row}`;
          const bitmap = level === target ? this.cache.get(key) : this.cache.peek(key);
          if (bitmap) {
            ctx.drawImage(bitmap, col * span, row * span, bitmap.width * f + seam, bitmap.height * f + seam);
          } else if (level === target || level === coarsest) {
            wanted.add(key);
            if (level === target) missing = true;
            this.enqueue(key, tileUrl(image.id, tiles, level, col, row));
          }
        }
      }
    }
    this.wanted = wanted;
    this.pump();
    return missing;
  }

  private enqueue(key: string, url: string): void {
    if (this.inFlight.has(key) || this.queue.some((q) => q.key === key)) return;
    const failedAt = this.failed.get(key);
    if (failedAt !== undefined && performance.now() - failedAt < RETRY_FAILED_MS) return;
    this.queue.push({ key, url });
  }

  private pump(): void {
    // Drop requests for tiles that are no longer on screen.
    this.queue = this.queue.filter((q) => this.wanted.has(q.key));
    while (this.inFlight.size < MAX_IN_FLIGHT && this.queue.length) {
      const next = this.queue.shift()!;
      this.inFlight.add(next.key);
      void this.load(next.key, next.url);
    }
  }

  private async load(key: string, url: string): Promise<void> {
    try {
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) throw new Error(`tile ${response.status}`);
      const bitmap = await createImageBitmap(await response.blob());
      this.cache.set(key, bitmap);
      this.failed.delete(key);
    } catch {
      this.failed.set(key, performance.now());
    } finally {
      this.inFlight.delete(key);
      this.pump();
      this.onLoad();
    }
  }
}
