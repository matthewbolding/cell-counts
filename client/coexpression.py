"""
coexpression.py — CCK/CHR overlap computation.

The scientifically interesting result this whole tool exists for: how many CCK and
CHR detections mark the same cell, reported as a rate against the total SNAP
population. SNAP is used only as that rate's denominator (total kept cells) here,
not as a third polygon to spatially overlap-test against — CCK/CHR overlap is the
whole computation.

A pair counts as coexpressing if their overlapping area is at least `threshold` of
the *smaller* cell's own (already-computed, shoelace/regionprops) area — not a
re-summed rasterized count, so this stays consistent with the area shown elsewhere
in the UI. Rasterizing the full image to test one pair would be wasteful (cells run
tens to low-hundreds of pixels across, images run into the thousands); every pair
is bbox-prefiltered first via `geometry.bbox_of` (already used for render-culling
and hit-testing) so only pairs that could possibly overlap ever get rasterized, and
what does get rasterized is cropped to just the pair's local union bbox.

`skimage.draw.polygon2mask`, not `shapely` — this project already uses numpy for
image handling, and scikit-image ships prebuilt wheels the same way numpy/tifffile
do (no compiler, no admin rights), so it's a lighter addition than a full
computational-geometry library for what's ultimately one primitive.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from skimage.draw import polygon2mask

from geometry import bbox_of

DEFAULT_THRESHOLD = 0.5


@dataclass
class CoexpressionResult:
    pairs: list[tuple[int, int, float]] = field(default_factory=list)  # (cck_id, chr_id, overlap_fraction)
    cck_ids: set[int] = field(default_factory=set)   # kept CCK cells involved in >=1 qualifying pair
    chr_ids: set[int] = field(default_factory=set)   # kept CHR cells involved in >=1 qualifying pair


def _bbox_intersects(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return ax0 <= bx1 and bx0 <= ax1 and ay0 <= by1 and by0 <= ay1


def _rasterize(polygons: list[list[list[float]]], origin_x: float, origin_y: float,
                shape: tuple[int, int]) -> np.ndarray:
    mask = np.zeros(shape, dtype=bool)
    for ring in polygons:
        if len(ring) < 3:
            continue
        # polygon2mask wants (row, col) = (y, x), local to this shared crop.
        rc = np.array([[y - origin_y, x - origin_x] for x, y in ring])
        mask |= polygon2mask(shape, rc)
    return mask


def _overlap_fraction(cell_a: dict, cell_b: dict) -> float:
    bbox_a = bbox_of(cell_a["polygons"])
    bbox_b = bbox_of(cell_b["polygons"])
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
    chr_bboxes = [bbox_of(c["polygons"]) for c in chr_kept]

    result = CoexpressionResult()
    for cck in cck_kept:
        cck_bbox = bbox_of(cck["polygons"])
        for chr_cell, chr_bbox in zip(chr_kept, chr_bboxes):
            if not _bbox_intersects(cck_bbox, chr_bbox):
                continue
            fraction = _overlap_fraction(cck, chr_cell)
            if fraction >= threshold:
                result.pairs.append((cck["id"], chr_cell["id"], round(fraction, 4)))
                result.cck_ids.add(cck["id"])
                result.chr_ids.add(chr_cell["id"])
    return result
