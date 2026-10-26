"""
images.py — TIFF loading and contrast stretching, shared by tiling (CPU worker)
and segmentation/rescan (GPU worker).

Memory rules (the corpus runs up to 13246x10961 uint16): percentiles come
from a strided subsample, and the
uint16 -> uint8 rescale runs in fixed-size row bands so the float32 working
buffer never scales with image size.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import tifffile

log = logging.getLogger(__name__)

PERCENTILE_STRIDE = 10
PERCENTILE_RANGE = (1.0, 99.8)
RESCALE_BAND_ROWS = 2000


def load_grayscale(path: Path, channel: int | None = None) -> np.ndarray:
    """Reduce a TIFF to one 2-D plane. `channel=None` auto-picks the channel with
    the most total signal when the file has several."""
    img = np.squeeze(tifffile.imread(str(path)))

    if img.ndim == 2:
        return img

    if img.ndim == 3:
        caxis = int(np.argmin(img.shape))
        if img.shape[caxis] > 8:
            log.warning("%s: shape %s has no clear channel axis; max-projecting axis 0", path.name, img.shape)
            return img.max(axis=0)
        img = np.moveaxis(img, caxis, 0)  # -> (C, Y, X)
        if channel is None:
            sums = img.reshape(img.shape[0], -1).sum(axis=1)
            sel = int(np.argmax(sums))
        else:
            sel = min(channel, img.shape[0] - 1)
        return img[sel]

    raise ValueError(f"{path.name}: unsupported image with {img.ndim} dimensions {img.shape}")


def contrast_stretch(gray: np.ndarray) -> np.ndarray:
    """Full-resolution uint8 display array, percentile-stretched for review."""
    sample = gray[::PERCENTILE_STRIDE, ::PERCENTILE_STRIDE]
    lo, hi = (float(v) for v in np.percentile(sample, PERCENTILE_RANGE))
    if hi <= lo:
        hi = lo + 1.0
    scale = 255.0 / (hi - lo)
    out = np.empty(gray.shape, dtype=np.uint8)
    for start in range(0, gray.shape[0], RESCALE_BAND_ROWS):
        end = min(start + RESCALE_BAND_ROWS, gray.shape[0])
        band = gray[start:end].astype(np.float32)
        band -= lo
        band *= scale
        np.clip(band, 0, 255, out=band)
        out[start:end] = band.astype(np.uint8)
    return out


def crop(gray: np.ndarray, rect: tuple[float, float, float, float]) -> tuple[np.ndarray, tuple[int, int]]:
    """Clamp `rect` (x0, y0, x1, y1) to the array and slice it. Returns the crop
    and its integer origin, which is what results get translated back by."""
    h, w = gray.shape
    x0, y0, x1, y1 = rect
    cx0 = max(0, min(int(round(x0)), w))
    cy0 = max(0, min(int(round(y0)), h))
    cx1 = max(0, min(int(round(x1)), w))
    cy1 = max(0, min(int(round(y1)), h))
    return gray[cy0:cy1, cx0:cx1], (cx0, cy0)
