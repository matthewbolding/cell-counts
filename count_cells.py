#!/usr/bin/env python3
"""
count_cells.py — first-iteration nuclei/cell counter for fluorescence microscopy.

Strategy
--------
Uses Cellpose-SAM (the `cpsam` generalist model, Cellpose >= 4.0) to produce an
*instance* segmentation. Counting is then just "how many distinct labels did we
get", and the same label image gives us a verification overlay for free. This is
deliberately preferred over raw SAM: raw Segment-Anything has no concept of a
"cell", over-segments biological nuclei, and is slow when there are hundreds of
objects. Cellpose-SAM keeps the SAM backbone but is trained on cellular data, so
it both knows what a cell is and runs comfortably on an RTX 3080 Ti.

Locality filter (--locality)
----------------------------
Real cells cluster; they are rarely alone in a dark void. So instead of raising
--cellprob-threshold (which loses faint *real* cells too), keep the model
permissive and drop detections that are spatially isolated afterward. Detections
whose centroids are within a radius are joined into groups, and only groups with
at least --locality-min-cluster members survive: the tissue is one huge group of
thousands, while scattered background noise forms tiny islands that get removed.
The radius is adaptive by default (a multiple of the median nearest-neighbour
distance), so it self-scales to the image. Filtered detections are drawn in a
distinct colour on the overlay so a human can confirm the filter isn't eating
real cells.

Outputs (under --out, default ./results):
  report.csv            one row per image: filename, n_cells, n_filtered, params
  report.html           thumbnail gallery (overlay + count) for fast eyeballing
  overlays/<stem>.png   sister image: contrast-stretched original; kept cells
                        outlined in red, locality-filtered ones in blue
  masks/<stem>.tif      raw uint16/uint32 label image of the KEPT cells
  review/<stem>.json    every detection (kept + filtered) as a vector polygon
                        with a status, for the standalone review GUI
                        (see cell_review.py) to render and let a human edit
  review/backgrounds/<stem>.png
                        plain contrast-stretched image, no overlay baked in —
                        the review GUI draws polygons on top of this itself

Only files matching --pattern (default *_SNAP.tif, case-insensitive) are processed.

Quick start
-----------
    # CUDA torch first (WSL2 + RTX 3080 Ti), then the rest:
    pip install torch --index-url https://download.pytorch.org/whl/cu124
    pip install cellpose tifffile scikit-image pillow numpy scipy

    python count_cells.py --input photos --out results --locality
    python count_cells.py --input photos --limit 5 --cellprob-threshold -2 --locality

Tuning knobs that matter for low-signal images like these:
  --cellprob-threshold  lower it (e.g. -1, -2, -3) to recover faint nuclei
  --flow-threshold      raise it (e.g. 0.6) to keep more masks
  --brightness          drop detections with no real signal inside them
  --brightness-factor   threshold = background + factor x noise; lower = fainter OK
  --locality            enable the isolated-detection filter (recommended here)
  --locality-min-cluster smallest group size to keep; raise to kill more noise
  --locality-factor     scales the adaptive grouping radius (factor x median NN)
  --locality-radius     fixed px grouping radius instead of adaptive; 0 = adaptive
  --min-size            raise to drop speckle, lower to keep tiny nuclei
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import os
import sys
import warnings
from pathlib import Path

# On Apple Silicon, let unsupported MPS ops fall back to CPU instead of crashing.
# Harmless on CUDA/CPU. Must be set before torch is imported.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import numpy as np
import tifffile
from PIL import Image, ImageDraw
from scipy.spatial import cKDTree
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components
from skimage.exposure import rescale_intensity
from skimage.measure import find_contours, regionprops
from skimage.morphology import dilation, disk
from skimage.segmentation import clear_border, find_boundaries

log = logging.getLogger("count_cells")


# --------------------------------------------------------------------------- #
# Image loading
# --------------------------------------------------------------------------- #
def load_grayscale(path: Path, channel: int | None) -> np.ndarray:
    """Load a TIFF and reduce it to a single 2-D plane suitable for nuclei.

    `channel` semantics:
        None  -> auto: if multi-channel, pick the channel with the most total
                 signal (a decent guess for "which channel holds the stain").
        >= 0  -> select that channel index explicitly.
    Singleton dimensions are squeezed; the channel axis is taken to be the
    smallest non-spatial axis (channels are assumed << image height/width).
    """
    img = np.squeeze(tifffile.imread(str(path)))

    if img.ndim == 2:
        return img

    if img.ndim == 3:
        # Heuristic: the channel axis is the smallest dimension (<= 8).
        caxis = int(np.argmin(img.shape))
        if img.shape[caxis] > 8:
            # No obviously-small axis: treat as a stack, max-project it.
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
    """Split `masks` into (kept, removed) label images by neighbourhood grouping.

    Detections are joined by an edge when their centroids are within `radius`, and
    only detections belonging to a connected group of at least `min_cluster`
    members are kept. Real tissue forms one huge group; scattered background
    detections form tiny islands (size 1-few) and are removed. This is far more
    decisive than a global-median distance test, which lets small noise clumps
    survive.

    `radius` is used if given (>0); otherwise it is `factor * median(nearest-
    neighbour distance)`, which self-scales to the image's cell spacing.

    Returns (kept_masks, removed_masks, info).
    """
    props = regionprops(masks)
    n = len(props)
    if n < min_cluster:
        # Fewer detections than a single valid group needs; can't judge — leave as-is.
        return masks, np.zeros_like(masks), {"n_removed": 0, "radius": float("nan"),
                                             "median_nn": float("nan"), "skipped": True}

    labels = np.array([p.label for p in props])
    centroids = np.array([p.centroid for p in props])  # (N, 2) as (y, x)

    tree = cKDTree(centroids)
    nn = tree.query(centroids, k=2)[0][:, 1]  # distance to nearest other detection
    r = radius if radius and radius > 0 else factor * float(np.median(nn))

    pairs = tree.query_pairs(r, output_type="ndarray")
    if len(pairs):
        graph = csr_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
        _, comp = connected_components(graph, directed=False)
    else:
        comp = np.arange(n)  # no edges: every detection is its own group of 1

    sizes = np.bincount(comp)
    keep = sizes[comp] >= min_cluster
    removed_labels = labels[~keep]
    removed_mask = np.isin(masks, removed_labels)

    kept = masks.copy()
    kept[removed_mask] = 0
    removed = masks.copy()
    removed[~removed_mask] = 0

    return kept, removed, {"n_removed": int(removed_labels.size), "radius": r,
                           "median_nn": float(np.median(nn)), "skipped": False}


# --------------------------------------------------------------------------- #
# Brightness filter: drop detections with no real signal inside them
# --------------------------------------------------------------------------- #
def filter_dim(masks: np.ndarray, gray: np.ndarray, min_brightness: float, factor: float):
    """Split `masks` into (kept, removed) by interior brightness on the RAW image.

    The model sometimes rings a dark patch that contains no signal. Background is
    estimated from all non-cell pixels (robust median + MAD), and any detection
    whose mean interior intensity fails to clear `background + factor * noise` is
    removed. `min_brightness` (>0) overrides the adaptive threshold with an
    absolute one in raw-image units.

    Returns (kept_masks, removed_masks, info).
    """
    props = regionprops(masks, intensity_image=gray.astype(np.float32))
    if not props:
        return masks, np.zeros_like(masks), {"n_removed": 0, "threshold": float("nan"),
                                             "background": float("nan"), "skipped": True}

    bg_pixels = gray[masks == 0]
    if bg_pixels.size:
        bg = float(np.median(bg_pixels))
        noise = float(np.median(np.abs(bg_pixels - bg))) * 1.4826  # robust std via MAD
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

    return kept, removed, {"n_removed": int(removed_labels.size), "threshold": thr,
                           "background": bg, "skipped": False}


# --------------------------------------------------------------------------- #
# Verification overlay
# --------------------------------------------------------------------------- #
def to_display_rgb(gray: np.ndarray, low: float = 1.0, high: float = 99.8) -> np.ndarray:
    """Percentile contrast-stretch to 8-bit RGB so faint nuclei are visible."""
    g = gray.astype(np.float32)
    lo, hi = np.percentile(g, [low, high])
    if hi <= lo:
        hi = lo + 1.0
    stretched = rescale_intensity(g, in_range=(lo, hi), out_range=(0, 255)).astype(np.uint8)
    return np.dstack([stretched] * 3)


def _draw_boundaries(rgb, masks, color, thickness):
    if masks is None or not masks.any():
        return
    b = find_boundaries(masks, mode="outer")
    if thickness > 1:
        b = dilation(b, disk(thickness - 1))
    rgb[b] = color


def build_overlay(gray: np.ndarray, kept_masks: np.ndarray,
                  iso_masks: np.ndarray | None, dim_masks: np.ndarray | None,
                  n_kept: int, n_iso: int, n_dim: int,
                  keep_color=(255, 60, 60), iso_color=(0, 170, 255), dim_color=(255, 170, 0),
                  thickness: int = 1, dots: bool = False) -> Image.Image:
    """Stretched original: kept cells red, isolated-filtered blue, dim-filtered amber."""
    rgb = to_display_rgb(gray)

    _draw_boundaries(rgb, dim_masks, dim_color, thickness)   # rejects underneath
    _draw_boundaries(rgb, iso_masks, iso_color, thickness)
    _draw_boundaries(rgb, kept_masks, keep_color, thickness)  # kept on top

    if dots:
        for prop in regionprops(kept_masks):
            y, x = (int(round(v)) for v in prop.centroid)
            rgb[max(0, y - 1):y + 2, max(0, x - 1):x + 2] = (80, 255, 120)

    im = Image.fromarray(rgb, mode="RGB")
    draw = ImageDraw.Draw(im)
    extra = []
    if n_dim:
        extra.append(f"{n_dim} dim")
    if n_iso:
        extra.append(f"{n_iso} isolated")
    label = f"{n_kept} cells" + (f"   {'  '.join(extra)}" if extra else "")
    draw.rectangle([4, 4, 12 + 7 * len(label), 22], fill=(0, 0, 0))
    draw.text((8, 7), label, fill=(255, 255, 0))
    return im


# --------------------------------------------------------------------------- #
# Review data: vector polygons for the standalone review GUI
# --------------------------------------------------------------------------- #
def _polygons_for_region(prop) -> list[list[list[float]]]:
    """Boundary ring(s) of one regionprops region, each as a list of [x, y] points.

    `prop.image` is a tight local binary crop of just this one label. Padding it
    by one pixel of False first guarantees find_contours closes every loop even
    when the region touches its own bounding box (which it always does). Cellpose
    occasionally assigns one label to a couple of disconnected pixel clusters, in
    which case find_contours returns more than one closed loop; every loop is
    kept (not just the longest) so the polygon never silently omits part of the
    labelled region.
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


def _build_review_cells(kept_masks: np.ndarray, dim_masks: np.ndarray | None,
                        iso_masks: np.ndarray | None) -> list[dict]:
    """Flatten kept + filtered label images into one list of polygon records.

    Label ids are unique across the three arrays (they all partition the same
    original detection set), so no id collisions are possible between them.
    """
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
                "original_status": status,
                "status": status,
                "filter_reason": reason,
                "centroid": [round(float(x), 2), round(float(y), 2)],
                "area": int(prop.area),
                "polygons": polygons,
            })
    return cells


def write_review_data(path: Path, gray: np.ndarray, kept_masks: np.ndarray,
                      dim_masks: np.ndarray | None, iso_masks: np.ndarray | None,
                      dirs: dict) -> Path:
    """Write the plain (unannotated) background PNG and the per-cell polygon JSON
    that cell_review.py reads and edits."""
    stem = path.stem
    bg_path = dirs["review_backgrounds"] / f"{stem}.png"
    Image.fromarray(to_display_rgb(gray), mode="RGB").save(bg_path)

    data = {
        "schema_version": 2,
        "image": path.name,
        "width": int(gray.shape[1]),
        "height": int(gray.shape[0]),
        "background": f"backgrounds/{bg_path.name}",
        "cells": _build_review_cells(kept_masks, dim_masks, iso_masks),
    }
    review_path = dirs["review"] / f"{stem}.json"
    review_path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    return review_path


# --------------------------------------------------------------------------- #
# Counting one image
# --------------------------------------------------------------------------- #
def count_image(model, path: Path, args, dirs) -> dict:
    gray = load_grayscale(path, args.channel)

    masks, _flows, _styles = model.eval(
        gray,
        diameter=(args.diameter or None),
        flow_threshold=args.flow_threshold,
        cellprob_threshold=args.cellprob_threshold,
        min_size=args.min_size,
        batch_size=args.batch_size,
        resample=not args.no_resample,
        normalize=True,
    )

    if args.exclude_border:
        masks = clear_border(masks)

    # Brightness filter first: strip rings drawn over empty background, so the
    # locality step then judges grouping on real detections only.
    dim_masks = None
    n_dim = 0
    if args.brightness:
        masks, dim_masks, binfo = filter_dim(
            masks, gray, min_brightness=args.min_brightness, factor=args.brightness_factor)
        n_dim = binfo["n_removed"]
        log.info("%s: brightness removed %d dim (threshold %.1f, background %.1f)",
                 path.name, n_dim, binfo["threshold"], binfo["background"])

    iso_masks = None
    n_iso = 0
    if args.locality:
        masks, iso_masks, linfo = filter_isolated(
            masks, radius=args.locality_radius, factor=args.locality_factor,
            min_cluster=args.locality_min_cluster)
        n_iso = linfo["n_removed"]
        if linfo["skipped"]:
            log.info("%s: fewer detections than min-cluster; locality filter skipped", path.name)
        else:
            log.info("%s: locality removed %d isolated (radius %.1f px, median NN %.1f px)",
                     path.name, n_iso, linfo["radius"], linfo["median_nn"])

    labels = np.unique(masks)
    n_cells = int((labels != 0).sum())

    stem = path.stem
    overlay = build_overlay(gray, masks, iso_masks, dim_masks, n_cells, n_iso, n_dim,
                            thickness=args.outline_thickness, dots=args.dots)
    overlay_path = dirs["overlays"] / f"{stem}.png"
    overlay.save(overlay_path)

    dtype = np.uint16 if n_cells < 65535 else np.uint32
    mask_path = dirs["masks"] / f"{stem}.tif"
    tifffile.imwrite(str(mask_path), masks.astype(dtype))

    write_review_data(path, gray, masks, dim_masks, iso_masks, dirs)

    log.info("%-40s -> %5d cells  (shape %s)", path.name, n_cells, gray.shape)
    return {
        "filename": path.name,
        "n_cells": n_cells,
        "n_dim": n_dim,
        "n_isolated": n_iso,
        "n_filtered": n_dim + n_iso,
        "height": gray.shape[0],
        "width": gray.shape[1],
        "overlay": overlay_path,
    }


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #
def write_csv(rows: list[dict], args, out: Path) -> None:
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["filename", "n_cells", "n_dim", "n_isolated", "height", "width",
                    "cellprob_threshold", "flow_threshold", "diameter", "min_size",
                    "brightness", "brightness_factor", "min_brightness",
                    "locality", "locality_min_cluster", "locality_factor", "locality_radius"])
        for r in rows:
            w.writerow([r["filename"], r["n_cells"], r["n_dim"], r["n_isolated"],
                        r["height"], r["width"],
                        args.cellprob_threshold, args.flow_threshold, args.diameter, args.min_size,
                        args.brightness, args.brightness_factor, args.min_brightness,
                        args.locality, args.locality_min_cluster, args.locality_factor,
                        args.locality_radius])


def write_html(rows: list[dict], out: Path) -> None:
    total = sum(r["n_cells"] for r in rows)
    total_dim = sum(r["n_dim"] for r in rows)
    total_iso = sum(r["n_isolated"] for r in rows)
    cards = []
    for r in rows:
        rel = Path("overlays") / Path(r["overlay"]).name
        bits = []
        if r["n_dim"]:
            bits.append(f'{r["n_dim"]} dim')
        if r["n_isolated"]:
            bits.append(f'{r["n_isolated"]} isolated')
        filt = f' &middot; <span>{", ".join(bits)}</span>' if bits else ""
        cards.append(
            f'<figure><img src="{html.escape(str(rel))}" loading="lazy">'
            f'<figcaption><b>{r["n_cells"]}</b> cells{filt}<br>'
            f'<span>{html.escape(r["filename"])}</span></figcaption></figure>'
        )
    out.write_text(f"""<!doctype html><meta charset="utf-8">
<title>Cell count report</title>
<style>
 body{{font-family:system-ui,sans-serif;margin:24px;background:#111;color:#eee}}
 h1{{font-weight:600}} .meta{{color:#9aa;margin-bottom:20px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px}}
 figure{{margin:0;background:#1c1c1c;border:1px solid #2a2a2a;border-radius:8px;overflow:hidden}}
 img{{width:100%;display:block;background:#000}}
 figcaption{{padding:8px 10px;font-size:13px}} figcaption span{{color:#9aa;word-break:break-all}}
 b{{color:#ffd23f}}
</style>
<h1>Cell count report</h1>
<div class="meta">{len(rows)} images &middot; {total} cells total &middot; \
{total_dim} dim + {total_iso} isolated filtered</div>
<div class="grid">{''.join(cards)}</div>
""", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def find_images(root: Path, pattern: str) -> list[Path]:
    pat = pattern.lower().replace("*", "")
    return sorted(p for p in root.rglob("*") if p.is_file() and p.name.lower().endswith(pat))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Count cells/nuclei in *_SNAP.tif microscopy images.")
    ap.add_argument("--input", type=Path, default=Path("photos"), help="folder to scan (recursive)")
    ap.add_argument("--out", type=Path, default=Path("results"), help="output folder")
    ap.add_argument("--pattern", default="*_SNAP.tif", help="filename suffix to match (case-insensitive)")
    ap.add_argument("--channel", type=int, default=None, help="channel index; default auto-select brightest")
    ap.add_argument("--diameter", type=float, default=0.0, help="expected cell diameter px; 0=auto")
    ap.add_argument("--cellprob-threshold", type=float, default=0.0, help="lower => more/fainter cells")
    ap.add_argument("--flow-threshold", type=float, default=0.4, help="raise => keep more masks")
    ap.add_argument("--min-size", type=int, default=15, help="drop masks smaller than this (px)")
    ap.add_argument("--batch-size", type=int, default=8, help="tiles per GPU batch; raise (16/32) to use more VRAM")
    ap.add_argument("--no-resample", action="store_true", help="skip flow resampling: faster, blockier masks")
    ap.add_argument("--exclude-border", action="store_true", help="don't count cells touching the edge")
    # brightness filter
    ap.add_argument("--brightness", action="store_true",
                    help="drop detections whose interior has no signal above background")
    ap.add_argument("--brightness-factor", type=float, default=3.0,
                    help="threshold = background + factor * robust-noise; lower keeps fainter cells")
    ap.add_argument("--min-brightness", type=float, default=0.0,
                    help="absolute interior-intensity threshold in raw units; 0 = adaptive")
    # locality filter
    ap.add_argument("--locality", action="store_true", help="drop spatially isolated detections")
    ap.add_argument("--locality-min-cluster", type=int, default=10,
                    help="min detections in a neighbourhood group to keep it (kills background specks)")
    ap.add_argument("--locality-factor", type=float, default=2.0,
                    help="adaptive radius = factor * median nearest-neighbour distance")
    ap.add_argument("--locality-radius", type=float, default=0.0,
                    help="fixed px grouping radius instead of adaptive; 0 = adaptive")
    # overlay / run control
    ap.add_argument("--outline-thickness", type=int, default=1, help="overlay outline thickness")
    ap.add_argument("--dots", action="store_true", help="also mark centroids on the overlay")
    ap.add_argument("--limit", type=int, default=0, help="process at most N images (0=all)")
    ap.add_argument("--cpu", action="store_true", help="force CPU even if a GPU is present")
    ap.add_argument("--fp32", action="store_true",
                    help="disable bfloat16 weights (use if MPS produces bad/empty masks)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    warnings.filterwarnings("ignore", message="Sparse invariant checks")

    images = find_images(args.input, args.pattern)
    if args.limit:
        images = images[:args.limit]
    if not images:
        log.error("No files matching %s under %s", args.pattern, args.input)
        return 1
    log.info("Found %d image(s) matching %s", len(images), args.pattern)

    # Import torch/cellpose lazily so --help works without them installed.
    import torch
    from cellpose import models

    # Pick the best available accelerator: CUDA (NVIDIA) or MPS (Apple Silicon).
    use_gpu = False
    if not args.cpu:
        if torch.cuda.is_available():
            use_gpu = True
            log.info("Accelerator: CUDA (%s)", torch.cuda.get_device_name(0))
        elif torch.backends.mps.is_available():
            use_gpu = True
            log.info("Accelerator: Apple MPS (Metal)")
    if not use_gpu:
        reason = "" if args.cpu else " (no CUDA or MPS device found)"
        log.warning("Running on CPU%s — this will be slow.", reason)

    # Cellpose(gpu=True) routes to CUDA or MPS automatically via its device logic.
    model_kwargs = {"gpu": use_gpu}
    if args.fp32:
        model_kwargs["use_bfloat16"] = False  # bf16 can misbehave on some MPS builds
    model = models.CellposeModel(**model_kwargs)

    dirs = {
        "overlays": args.out / "overlays",
        "masks": args.out / "masks",
        "review": args.out / "review",
        "review_backgrounds": args.out / "review" / "backgrounds",
    }
    for d in (args.out, *dirs.values()):
        d.mkdir(parents=True, exist_ok=True)

    rows = []
    for path in images:
        try:
            rows.append(count_image(model, path, args, dirs))
        except Exception as exc:  # noqa: BLE001 — keep batch going, log the casualty
            log.exception("FAILED on %s: %s", path.name, exc)

    if not rows:
        log.error("Every image failed; see errors above.")
        return 1

    write_csv(rows, args, args.out / "report.csv")
    write_html(rows, args.out / "report.html")
    log.info("Done. %d images, %d cells total, %d filtered. Report: %s",
             len(rows), sum(r["n_cells"] for r in rows),
             sum(r["n_filtered"] for r in rows), args.out / "report.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())