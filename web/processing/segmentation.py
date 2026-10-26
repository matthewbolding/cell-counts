"""
segmentation.py — Cellpose-SAM segmentation, ported from server/segment.py.

The algorithm is unchanged: Cellpose, then the brightness filter (drop
detections with no real signal), then the locality filter (drop spatially
isolated detections), then vector outlines. Filtered detections are kept as
`status: "filtered"` with a reason, exactly as the desktop app expects.

What changed from the server version: `run()` takes an array instead of a path
(rescan passes a crop), and there's a `FakeModel` so the whole pipeline runs on
a laptop or in tests without torch or a GPU (CELLCOUNTS_FAKE_MODEL=1).
"""

from __future__ import annotations

import logging
import os
from typing import Any

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree
from skimage.measure import find_contours, regionprops
from skimage.segmentation import clear_border

log = logging.getLogger(__name__)

DEFAULT_PARAMS: dict[str, Any] = {
    "channel": None,
    "diameter": 0.0,
    "cellprob_threshold": 0.0,
    "flow_threshold": 0.4,
    "min_size": 15,
    "batch_size": 8,
    "resample": True,
    "exclude_border": False,
    "brightness": True,
    "brightness_factor": 3.0,
    "min_brightness": 0.0,
    "locality": True,
    "locality_min_cluster": 10,
    "locality_factor": 2.0,
    "locality_radius": 0.0,
}


# --------------------------------------------------------------------------- #
# Locality filter: drop spatially isolated detections
# --------------------------------------------------------------------------- #
def filter_isolated(masks: np.ndarray, radius: float, factor: float, min_cluster: int):
    props = regionprops(masks)
    n = len(props)
    if n < min_cluster:
        return masks, np.zeros_like(masks), {"n_removed": 0, "skipped": True}

    labels = np.array([p.label for p in props])
    centroids = np.array([p.centroid for p in props])

    tree = cKDTree(centroids)
    nn = tree.query(centroids, k=2)[0][:, 1]
    r = radius if radius and radius > 0 else factor * float(np.median(nn))

    pairs = tree.query_pairs(r, output_type="ndarray")
    if len(pairs):
        graph = csr_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
        _, comp = connected_components(graph, directed=False)
    else:
        comp = np.arange(n)

    sizes = np.bincount(comp)
    keep = sizes[comp] >= min_cluster
    removed_labels = labels[~keep]
    removed_mask = np.isin(masks, removed_labels)

    kept = masks.copy()
    kept[removed_mask] = 0
    removed = masks.copy()
    removed[~removed_mask] = 0
    return kept, removed, {"n_removed": int(removed_labels.size), "skipped": False}


# --------------------------------------------------------------------------- #
# Brightness filter: drop detections with no real signal inside them
# --------------------------------------------------------------------------- #
def filter_dim(masks: np.ndarray, gray: np.ndarray, min_brightness: float, factor: float):
    props = regionprops(masks, intensity_image=gray.astype(np.float32))
    if not props:
        return masks, np.zeros_like(masks), {"n_removed": 0, "skipped": True}

    bg_pixels = gray[masks == 0]
    if bg_pixels.size:
        bg = float(np.median(bg_pixels))
        noise = float(np.median(np.abs(bg_pixels - bg))) * 1.4826
    else:
        bg, noise = 0.0, 1.0

    thr = min_brightness if min_brightness and min_brightness > 0 else bg + factor * max(noise, 1e-6)

    labels = np.array([p.label for p in props])
    means = np.array([p.intensity_mean for p in props])
    removed_labels = labels[means < thr]
    removed_mask = np.isin(masks, removed_labels)

    kept = masks.copy()
    kept[removed_mask] = 0
    removed = masks.copy()
    removed[~removed_mask] = 0
    return kept, removed, {"n_removed": int(removed_labels.size), "skipped": False}


# --------------------------------------------------------------------------- #
# Vector polygons
# --------------------------------------------------------------------------- #
def _polygons_for_region(prop) -> list[list[list[float]]]:
    """Boundary ring(s) of one region as [x, y] lists. Padding the local crop by a
    pixel of False guarantees find_contours closes every loop; every loop is
    kept because Cellpose occasionally labels two disconnected clusters as one."""
    minr, minc, _maxr, _maxc = prop.bbox
    local = np.pad(prop.image, 1, mode="constant", constant_values=False)
    rings = []
    for contour in find_contours(local.astype(np.uint8), level=0.5):
        rows = contour[:, 0] - 1 + minr
        cols = contour[:, 1] - 1 + minc
        rings.append([[round(float(c), 2), round(float(r), 2)] for r, c in zip(rows, cols, strict=True)])
    return rings


def build_cells(kept_masks: np.ndarray, dim_masks: np.ndarray | None, iso_masks: np.ndarray | None) -> list[dict]:
    cells = []
    groups = [("kept", None, kept_masks)]
    if dim_masks is not None:
        groups.append(("filtered", "dim", dim_masks))
    if iso_masks is not None:
        groups.append(("filtered", "isolated", iso_masks))

    for status, reason, masks in groups:
        if masks is None or not masks.any():
            continue
        for prop in regionprops(masks):
            polygons = _polygons_for_region(prop)
            if not polygons:
                continue
            y, x = prop.centroid
            cells.append({
                "id": int(prop.label),
                "polygons": polygons,
                "centroid": [round(float(x), 2), round(float(y), 2)],
                "area": int(prop.area),
                "status": status,
                "filter_reason": reason,
                "color": None,
                "source": "model",
                "edited": False,
            })
    return cells


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class FakeModel:
    """Stand-in for CellposeModel with the same eval() signature: Otsu threshold,
    connected components, small objects dropped. Produces plausible blobs for
    development and tests; never used in production."""

    def eval(self, gray, diameter=None, cellprob_threshold=0.0, min_size=15, **_kwargs):
        from scipy import ndimage
        from skimage.filters import gaussian, threshold_otsu
        from skimage.segmentation import relabel_sequential

        smooth = gaussian(gray.astype(np.float32), sigma=1.5, preserve_range=True)
        if float(smooth.max()) == float(smooth.min()):
            return np.zeros(gray.shape, dtype=np.int32), None, None
        # cellprob_threshold nudges the cutoff the way it nudges Cellpose: lower
        # finds more, higher finds fewer.
        thr = threshold_otsu(smooth) * (1.0 + 0.05 * float(cellprob_threshold))
        labels, _ = ndimage.label(smooth > thr)
        sizes = np.bincount(labels.ravel())
        small = sizes < max(1, int(min_size))
        small[0] = False
        labels[small[labels]] = 0
        relabeled, _, _ = relabel_sequential(labels)
        return relabeled.astype(np.int32), None, None


def load_model(cpu: bool = False, fp32: bool = False):
    """Cellpose-SAM, loaded once per GPU worker. Picks CUDA > MPS > CPU, or the
    FakeModel when CELLCOUNTS_FAKE_MODEL is set."""
    if os.environ.get("CELLCOUNTS_FAKE_MODEL", "").lower() in ("1", "true", "yes"):
        log.warning("Using FakeModel (CELLCOUNTS_FAKE_MODEL is set) — not for real data.")
        return FakeModel()

    import torch
    from cellpose import models

    use_gpu = False
    if not cpu:
        if torch.cuda.is_available():
            use_gpu = True
            log.info("Accelerator: CUDA (%s)", torch.cuda.get_device_name(0))
        elif torch.backends.mps.is_available():
            use_gpu = True
            log.info("Accelerator: Apple MPS (Metal)")
    if not use_gpu:
        log.warning("Running on CPU%s — this will be slow.", "" if cpu else " (no CUDA or MPS device found)")

    kwargs: dict[str, Any] = {"gpu": use_gpu}
    if fp32:
        kwargs["use_bfloat16"] = False
    return models.CellposeModel(**kwargs)


def release_gpu_memory() -> None:
    """Hand PyTorch's cached-but-unused memory back to the driver after each job
    (see server/jobs.py for why: image sizes vary ~40x, so the cache otherwise
    holds the session's peak forever)."""
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
def run(model, gray: np.ndarray, params: dict[str, Any] | None = None) -> dict:
    """Segment one 2-D array. Returns {width, height, params, cells}."""
    p = {**DEFAULT_PARAMS, **(params or {})}

    masks, _flows, _styles = model.eval(
        gray,
        diameter=(p["diameter"] or None),
        flow_threshold=p["flow_threshold"],
        cellprob_threshold=p["cellprob_threshold"],
        min_size=p["min_size"],
        batch_size=p["batch_size"],
        resample=p["resample"],
        normalize=True,
    )

    if p["exclude_border"]:
        masks = clear_border(masks)

    dim_masks = None
    if p["brightness"]:
        masks, dim_masks, binfo = filter_dim(
            masks, gray, min_brightness=p["min_brightness"], factor=p["brightness_factor"])
        log.info("brightness filter removed %d dim", binfo["n_removed"])

    iso_masks = None
    if p["locality"]:
        masks, iso_masks, linfo = filter_isolated(
            masks, radius=p["locality_radius"], factor=p["locality_factor"],
            min_cluster=p["locality_min_cluster"])
        log.info("locality filter removed %d isolated", linfo["n_removed"])

    cells = build_cells(masks, dim_masks, iso_masks)
    return {"width": int(gray.shape[1]), "height": int(gray.shape[0]), "params": p, "cells": cells}


def translate_cells(cells: list[dict], origin_x: float, origin_y: float) -> list[dict]:
    """Shift crop-local results back into full-image coordinates (rescan)."""
    out = []
    for c in cells:
        polygons = [[[round(px + origin_x, 2), round(py + origin_y, 2)] for px, py in ring]
                    for ring in c["polygons"]]
        centroid = [round(c["centroid"][0] + origin_x, 2), round(c["centroid"][1] + origin_y, 2)]
        out.append({**c, "polygons": polygons, "centroid": centroid})
    return out
