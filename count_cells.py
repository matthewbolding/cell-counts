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
from skimage.measure import regionprops
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


def build_overlay(gray: np.ndarray, kept_masks: np.ndarray, removed_masks: np.ndarray | None,
                  n_kept: int, n_filtered: int,
                  keep_color=(255, 60, 60), drop_color=(0, 170, 255),
                  thickness: int = 1, dots: bool = False) -> Image.Image:
    """Stretched original with kept cells outlined red, filtered ones blue."""
    rgb = to_display_rgb(gray)

    _draw_boundaries(rgb, removed_masks, drop_color, thickness)  # under kept
    _draw_boundaries(rgb, kept_masks, keep_color, thickness)

    if dots:
        for prop in regionprops(kept_masks):
            y, x = (int(round(v)) for v in prop.centroid)
            rgb[max(0, y - 1):y + 2, max(0, x - 1):x + 2] = (80, 255, 120)

    im = Image.fromarray(rgb, mode="RGB")
    draw = ImageDraw.Draw(im)
    label = f"{n_kept} cells" + (f"   {n_filtered} filtered" if n_filtered else "")
    draw.rectangle([4, 4, 12 + 7 * len(label), 22], fill=(0, 0, 0))
    draw.text((8, 7), label, fill=(255, 255, 0))
    return im


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

    removed_masks = None
    n_filtered = 0
    if args.locality:
        masks, removed_masks, linfo = filter_isolated(
            masks, radius=args.locality_radius, factor=args.locality_factor,
            min_cluster=args.locality_min_cluster)
        n_filtered = linfo["n_removed"]
        if linfo["skipped"]:
            log.info("%s: fewer detections than min-cluster; locality filter skipped", path.name)
        else:
            log.info("%s: locality removed %d isolated (radius %.1f px, median NN %.1f px)",
                     path.name, n_filtered, linfo["radius"], linfo["median_nn"])

    labels = np.unique(masks)
    n_cells = int((labels != 0).sum())

    stem = path.stem
    overlay = build_overlay(gray, masks, removed_masks, n_cells, n_filtered,
                            thickness=args.outline_thickness, dots=args.dots)
    overlay_path = dirs["overlays"] / f"{stem}.png"
    overlay.save(overlay_path)

    dtype = np.uint16 if n_cells < 65535 else np.uint32
    mask_path = dirs["masks"] / f"{stem}.tif"
    tifffile.imwrite(str(mask_path), masks.astype(dtype))

    log.info("%-40s -> %5d cells  (shape %s)", path.name, n_cells, gray.shape)
    return {
        "filename": path.name,
        "n_cells": n_cells,
        "n_filtered": n_filtered,
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
        w.writerow(["filename", "n_cells", "n_filtered", "height", "width",
                    "cellprob_threshold", "flow_threshold", "diameter", "min_size",
                    "locality", "locality_min_cluster", "locality_factor", "locality_radius"])
        for r in rows:
            w.writerow([r["filename"], r["n_cells"], r["n_filtered"], r["height"], r["width"],
                        args.cellprob_threshold, args.flow_threshold, args.diameter, args.min_size,
                        args.locality, args.locality_min_cluster, args.locality_factor,
                        args.locality_radius])


def write_html(rows: list[dict], out: Path) -> None:
    total = sum(r["n_cells"] for r in rows)
    total_filt = sum(r["n_filtered"] for r in rows)
    cards = []
    for r in rows:
        rel = Path("overlays") / Path(r["overlay"]).name
        filt = f' &middot; <span>{r["n_filtered"]} filtered</span>' if r["n_filtered"] else ""
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
<div class="meta">{len(rows)} images &middot; {total} cells total &middot; {total_filt} filtered as isolated</div>
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

    dirs = {"overlays": args.out / "overlays", "masks": args.out / "masks"}
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