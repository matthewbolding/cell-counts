"""
coexpression.py — CCK/CHR overlap:

- only kept cells take part;
- a CCK/CHR pair coexpresses when their rasterized overlap is at least
  `threshold` (default 50%) of the *smaller* cell's stored area;
- the rate is pairs / kept SNAP cells.

Bbox-testing every CCK cell against every CHR cell in pure Python is fine for a
few hundred kept cells but quadratic, so candidate pairs come from one
vectorized numpy bbox-intersection pass per CCK cell; the rasterized overlap
test that decides each pair (`skimage.draw.polygon2mask` on the pair's shared
crop) is exact. projects/tests pins the results against reference output, cell
for cell.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from skimage.draw import polygon2mask

DEFAULT_THRESHOLD = 0.5


@dataclass
class CoexpressionResult:
    pairs: list[tuple[int, int, float]] = field(default_factory=list)  # (cck_id, chr_id, overlap_fraction)
    cck_ids: set[int] = field(default_factory=set)
    chr_ids: set[int] = field(default_factory=set)


def bbox_of(polygons: list[list[list[float]]]) -> tuple[float, float, float, float]:
    xs = [pt[0] for ring in polygons for pt in ring]
    ys = [pt[1] for ring in polygons for pt in ring]
    return (min(xs), min(ys), max(xs), max(ys))


def _rasterize(polygons, origin_x: float, origin_y: float, shape: tuple[int, int]) -> np.ndarray:
    mask = np.zeros(shape, dtype=bool)
    for ring in polygons:
        if len(ring) < 3:
            continue
        rc = np.array([[y - origin_y, x - origin_x] for x, y in ring])
        mask |= polygon2mask(shape, rc)
    return mask


def _overlap_fraction(cell_a: dict, cell_b: dict, bbox_a, bbox_b) -> float:
    x0 = min(bbox_a[0], bbox_b[0])
    y0 = min(bbox_a[1], bbox_b[1])
    x1 = max(bbox_a[2], bbox_b[2])
    y1 = max(bbox_a[3], bbox_b[3])
    shape = (max(1, int(np.ceil(y1 - y0)) + 1), max(1, int(np.ceil(x1 - x0)) + 1))

    mask_a = _rasterize(cell_a["polygons"], x0, y0, shape)
    mask_b = _rasterize(cell_b["polygons"], x0, y0, shape)
    intersection = np.count_nonzero(mask_a & mask_b)
    if intersection == 0:
        return 0.0
    smaller_area = min(cell_a["area"], cell_b["area"])
    if smaller_area <= 0:
        return 0.0
    return intersection / smaller_area


def compute_coexpression(cck_cells: list[dict], chr_cells: list[dict],
                         threshold: float = DEFAULT_THRESHOLD) -> CoexpressionResult:
    cck_kept = [c for c in cck_cells if c["status"] == "kept"]
    chr_kept = [c for c in chr_cells if c["status"] == "kept"]
    result = CoexpressionResult()
    if not cck_kept or not chr_kept:
        return result

    chr_bboxes = [bbox_of(c["polygons"]) for c in chr_kept]
    chr_arr = np.array(chr_bboxes)  # (n, 4): x0, y0, x1, y1

    for cck in cck_kept:
        bx0, by0, bx1, by1 = cck_bbox = bbox_of(cck["polygons"])
        # Inclusive bbox intersection (touching boxes count), vectorized.
        hits = np.nonzero((bx0 <= chr_arr[:, 2]) & (chr_arr[:, 0] <= bx1)
                          & (by0 <= chr_arr[:, 3]) & (chr_arr[:, 1] <= by1))[0]
        for j in hits:
            chr_cell = chr_kept[j]
            fraction = _overlap_fraction(cck, chr_cell, cck_bbox, chr_bboxes[j])
            if fraction >= threshold:
                result.pairs.append((cck["id"], chr_cell["id"], round(fraction, 4)))
                result.cck_ids.add(cck["id"])
                result.chr_ids.add(chr_cell["id"])
    return result
