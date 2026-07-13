"""
geometry.py — pure polygon math, independent of any rendering.

`point_in_polygon`/`point_in_any_ring` are ported as-is from the deleted
`cell_review.py` (they operated on image-space points already independent of the
Tk Canvas items used to render them, so the rewrite to a rasterized renderer in
rendering.py changes nothing about hit-testing).

`shoelace_area_centroid` is new: model-detected cells get `area`/`centroid` for
free from `regionprops` on a raster mask (server/segment.py:_build_cells), but a
hand-drawn cell (Phase 2's draw tool) has no mask — only the vertex ring the
reviewer clicked out.
"""

from __future__ import annotations


def point_in_polygon(x: float, y: float, poly: list[list[float]]) -> bool:
    """Ray-casting point-in-polygon test; poly is a list of [x, y] pairs."""
    inside = False
    xj, yj = poly[-1]
    for xi, yi in poly:
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        xj, yj = xi, yi
    return inside


def point_in_any_ring(x: float, y: float, rings: list[list[list[float]]]) -> bool:
    """True if (x, y) falls inside any of a cell's boundary rings.

    A detection can be made of more than one disconnected pixel cluster (rare, but
    Cellpose occasionally gives one label to two separate blobs), so a cell's shape
    is a list of independent rings rather than one polygon.
    """
    return any(point_in_polygon(x, y, ring) for ring in rings)


def bbox_of(polygons: list[list[list[float]]]) -> tuple[float, float, float, float]:
    xs = [pt[0] for ring in polygons for pt in ring]
    ys = [pt[1] for ring in polygons for pt in ring]
    return (min(xs), min(ys), max(xs), max(ys))


def shoelace_area_centroid(ring: list[list[float]]) -> tuple[int, list[float]]:
    """Signed-area (shoelace) formula for a single ring's area + centroid.

    Matches the server's rounding convention (`round(x, 2)` for coordinates).
    Degenerate rings (near-zero area, e.g. collinear points) fall back to a plain
    vertex average for the centroid rather than dividing by ~zero.
    """
    n = len(ring)
    if n < 3:
        return 0, [0.0, 0.0]

    area2 = 0.0
    cx = cy = 0.0
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        area2 += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross

    area = area2 / 2.0
    if abs(area) < 1e-6:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return 0, [round(sum(xs) / n, 2), round(sum(ys) / n, 2)]

    cx /= 6.0 * area
    cy /= 6.0 * area
    return round(abs(area)), [round(cx, 2), round(cy, 2)]
