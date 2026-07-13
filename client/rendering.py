"""
rendering.py — mask/outline compositing.

Replaces the deleted `cell_review.py`'s approach (one native Tk Canvas polygon item
per cell, recreated on every redraw — fine for outline-only single-channel review,
not for filled multi-channel masks). Instead, one RGBA raster layer is built per
redraw via PIL and alpha-composited onto the background crop, then blitted as a
single `PhotoImage`.

Two measured-not-guessed performance decisions:
- Bbox-vs-viewport culling is a 20x win once zoomed in (thousands of cells -> a few
  hundred), but provides *zero* benefit at fit-to-window zoom, where every cell's
  bbox trivially intersects the whole-image viewport by definition — that's also
  the default view and the view real images are largest/densest in (measured 65ms
  for 6026 cells at fit-zoom on the real corpus).
- What actually cuts the fit-zoom cost is a level-of-detail cutoff: a cell whose
  on-screen footprint is sub-pixel (its 50-60-point ring resolves to ~1 screen
  pixel anyway) is drawn as a single small dot instead of the full ring.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from geometry import bbox_of

FILTERED_COLOR = (140, 140, 140, 200)  # fixed neutral gray, regardless of channel
IN_PROGRESS_COLOR = (255, 221, 0, 255)
MASK_FILL_ALPHA = 110
OUTLINE_WIDTH_BASE = 1.5
OUTLINE_WIDTH_MIN, OUTLINE_WIDTH_MAX = 1, 4
# Below this on-screen footprint, draw a dot instead of the ring. Measured against
# real detections (server/segment.py's find_contours output averages ~171 points
# per ring, not the handful you'd assume) — a naive 2px threshold barely triggers
# and saves ~0%; 8px triggers on the bulk of small cells at typical fit-zoom and
# measured a real ~2.4x redraw speedup on the densest real image.
LOD_MIN_SCREEN_PX = 8.0


def hex_to_rgba(hex_color: str, alpha: int) -> tuple[int, int, int, int]:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    return (r, g, b, alpha)


def render_overlay(cells: list[dict], viewport_rect: tuple[float, float, float, float],
                    scale: float, out_size: tuple[int, int], mode: str,
                    channel_color: str) -> Image.Image:
    """One RGBA layer: viewport-culled, screen-transformed cell shapes.

    `mode` is `"mask"` (filled) or `"outline"`. Kept cells render in
    `channel_color`; filtered cells always render in a fixed neutral gray so "this
    is a rejected detection" reads the same across CCK/CHR/SNAP.
    """
    x0, y0, x1, y1 = viewport_rect
    out_w, out_h = out_size
    layer = Image.new("RGBA", (max(1, out_w), max(1, out_h)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    kept_rgba = hex_to_rgba(channel_color, MASK_FILL_ALPHA if mode == "mask" else 255)
    line_w = max(OUTLINE_WIDTH_MIN, min(OUTLINE_WIDTH_MAX, round(OUTLINE_WIDTH_BASE * scale)))

    for cell in cells:
        polygons = cell["polygons"]
        bx0, by0, bx1, by1 = bbox_of(polygons)
        if bx1 < x0 or bx0 > x1 or by1 < y0 or by0 > y1:
            continue  # bbox doesn't intersect the viewport at all

        color = kept_rgba if cell["status"] == "kept" else FILTERED_COLOR

        screen_w = (bx1 - bx0) * scale
        screen_h = (by1 - by0) * scale
        if max(screen_w, screen_h) < LOD_MIN_SCREEN_PX:
            sx = ((bx0 + bx1) / 2 - x0) * scale
            sy = ((by0 + by1) / 2 - y0) * scale
            r = max(1.5, max(screen_w, screen_h) / 2)  # scale the dot with actual footprint, not a fixed size
            draw.ellipse([sx - r, sy - r, sx + r, sy + r], fill=color)
            continue

        for ring in polygons:
            if len(ring) < 2:
                continue
            pts = [((px - x0) * scale, (py - y0) * scale) for px, py in ring]
            if mode == "mask":
                draw.polygon(pts, fill=color)
            else:
                draw.polygon(pts, outline=color, width=line_w)

    return layer


def render_in_progress_polygon(points_image_space: list[tuple[float, float]],
                                viewport_rect: tuple[float, float, float, float],
                                scale: float, out_size: tuple[int, int]) -> Image.Image:
    """Small overlay for a draw-mode polygon that hasn't been closed yet — kept
    separate from render_overlay's raster pass since it's only ever a handful of
    points and re-rasterizing every committed cell just to show a live vertex
    preview would be wasteful."""
    x0, y0 = viewport_rect[0], viewport_rect[1]
    out_w, out_h = out_size
    layer = Image.new("RGBA", (max(1, out_w), max(1, out_h)), (0, 0, 0, 0))
    if not points_image_space:
        return layer

    draw = ImageDraw.Draw(layer)
    pts = [((px - x0) * scale, (py - y0) * scale) for px, py in points_image_space]
    if len(pts) >= 2:
        draw.line(pts, fill=IN_PROGRESS_COLOR, width=2)
    for sx, sy in pts:
        r = 3
        draw.ellipse([sx - r, sy - r, sx + r, sy + r], fill=IN_PROGRESS_COLOR)
    return layer


def composite(background_rgb: Image.Image, *overlays: Image.Image) -> Image.Image:
    result = background_rgb.convert("RGBA")
    for layer in overlays:
        result = Image.alpha_composite(result, layer)
    return result.convert("RGB")
