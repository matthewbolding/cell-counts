"""
segment.py — Cellpose-SAM segmentation, ported from the original count_cells.py.

Unlike the original CLI, this module has no side-output responsibility: no overlay
PNG, no mask TIFF, no CSV/HTML report, no background PNG. The client already has the
source TIFF and renders everything itself, so a segmentation run's only product is
the `cells` JSON array described in the manifest schema (see client/manifest.py).

`load_model()` is called once at server startup; `run()` is called once per job by
the worker thread in jobs.py (never on the asyncio event loop directly — model.eval
is a long blocking call).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import numpy as np
import tifffile
from scipy.spatial import cKDTree
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components
from skimage.measure import find_contours, regionprops
from skimage.segmentation import clear_border

log = logging.getLogger("segment")

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
# Image loading
# --------------------------------------------------------------------------- #
def load_grayscale(path: Path, channel: int | None) -> np.ndarray:
    """Load a TIFF and reduce it to a single 2-D plane suitable for nuclei.

    `channel` semantics:
        None  -> auto: if multi-channel, pick the channel with the most total
                 signal (a decent guess for "which channel holds the stain").
        >= 0  -> select that channel index explicitly.
    """
    img = np.squeeze(tifffile.imread(str(path)))

    if img.ndim == 2:
        return img

    if img.ndim == 3:
        caxis = int(np.argmin(img.shape))
        if img.shape[caxis] > 8:
            log.warning("%s: shape %s has no clear channel axis; max-projecting axis 0",
                        path.name, img.shape)
            return img.max(axis=0)
        img = np.moveaxis(img, caxis, 0)  # -> (C, Y, X)
        if channel is None:
            sums = img.reshape(img.shape[0], -1).sum(axis=1)
            sel = int(np.argmax(sums))
            log.info("%s: %d channels, auto-selected channel %d", path.name, img.shape[0], sel)
        else:
            sel = min(channel, img.shape[0] - 1)
        return img[sel]

    raise ValueError(f"{path.name}: unsupported image with {img.ndim} dimensions {img.shape}")


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
    """Boundary ring(s) of one regionprops region, each as a list of [x, y] points.

    Padding the tight local crop by one pixel of False guarantees find_contours
    closes every loop even when the region touches its own bounding box (which it
    always does). Every closed loop is kept — Cellpose occasionally assigns one
    label to a couple of disconnected pixel clusters.
    """
    minr, minc, _maxr, _maxc = prop.bbox
    local = np.pad(prop.image, 1, mode="constant", constant_values=False)
    contours = find_contours(local.astype(np.uint8), level=0.5)
    rings = []
    for contour in contours:
        rows = contour[:, 0] - 1 + minr
        cols = contour[:, 1] - 1 + minc
        rings.append([[round(float(c), 2), round(float(r), 2)] for r, c in zip(rows, cols)])
    return rings


def _build_cells(kept_masks: np.ndarray, dim_masks: np.ndarray | None,
                  iso_masks: np.ndarray | None) -> list[dict]:
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
# Model
# --------------------------------------------------------------------------- #
def load_model(cpu: bool = False, fp32: bool = False):
    """Load the Cellpose-SAM model once. Picks CUDA > MPS > CPU automatically."""
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

    kwargs = {"gpu": use_gpu}
    if fp32:
        kwargs["use_bfloat16"] = False
    return models.CellposeModel(**kwargs)


# --------------------------------------------------------------------------- #
# Run one image
# --------------------------------------------------------------------------- #
def run(model, path: Path, params: dict[str, Any] | None = None) -> dict:
    """Segment one TIFF and return {width, height, cells: [...]}."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    gray = load_grayscale(path, p["channel"])

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
        log.info("%s: brightness removed %d dim", path.name, binfo["n_removed"])

    iso_masks = None
    if p["locality"]:
        masks, iso_masks, linfo = filter_isolated(
            masks, radius=p["locality_radius"], factor=p["locality_factor"],
            min_cluster=p["locality_min_cluster"])
        log.info("%s: locality removed %d isolated", path.name, linfo["n_removed"])

    cells = _build_cells(masks, dim_masks, iso_masks)
    n_kept = sum(1 for c in cells if c["status"] == "kept")
    log.info("%-40s -> %5d cells kept (%d filtered)", path.name, n_kept, len(cells) - n_kept)

    return {
        "width": int(gray.shape[1]),
        "height": int(gray.shape[0]),
        "params": p,
        "cells": cells,
    }
