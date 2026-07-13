"""
imaging.py — TIFF loading and display preparation.

Phase 1 deliberately dropped server-side background-image generation ("the client
already has the source TIFF and can render everything itself"), so this is new:
the client now loads and contrast-stretches full-resolution source TIFFs itself.
The real corpus ranges from 2090x1690 up to 13246x10961 (uint16), and the naive
version of this — `np.percentile` and a rescale over the full float32-cast array —
measured a ~4GB peak RSS on the largest file, ~27x the size of the resulting
display array. Both fixes below are load-bearing, not stylistic:

- Percentiles are computed on a strided subsample (measured within 0.1% of the
  full-array result, 57x faster, and shrinks percentile's internal working copy
  from ~290MB to ~3MB on the largest file).
- The uint16 -> uint8 rescale runs in fixed-size row bands into a preallocated
  output array, bounding the transient float32 buffer to a constant size instead
  of scaling with image size (this is what actually kills the 4GB peak).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import tifffile
from PIL import Image

try:
    RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # older Pillow
    RESAMPLE = Image.LANCZOS

PERCENTILE_STRIDE = 10
PERCENTILE_RANGE = (1.0, 99.8)
RESCALE_BAND_ROWS = 2000
LETTERBOX_COLOR = (26, 26, 26)  # matches the review canvas background


def _load_grayscale_raw(path: Path, channel: int | None = None) -> np.ndarray:
    """Reduce a TIFF to one 2-D plane. Mirrors server/segment.py's load_grayscale
    (duplicated rather than imported — client and server are separately deployed)."""
    img = np.squeeze(tifffile.imread(str(path)))

    if img.ndim == 2:
        return img

    if img.ndim == 3:
        caxis = int(np.argmin(img.shape))
        if img.shape[caxis] > 8:
            return img.max(axis=0)
        img = np.moveaxis(img, caxis, 0)  # -> (C, Y, X)
        if channel is None:
            sums = img.reshape(img.shape[0], -1).sum(axis=1)
            sel = int(np.argmax(sums))
        else:
            sel = min(channel, img.shape[0] - 1)
        return img[sel]

    raise ValueError(f"{path.name}: unsupported image with {img.ndim} dimensions {img.shape}")


def _contrast_stretch_uint8(img: np.ndarray, lo: float, hi: float,
                             band_rows: int = RESCALE_BAND_ROWS) -> np.ndarray:
    if hi <= lo:
        hi = lo + 1.0
    scale = 255.0 / (hi - lo)
    out = np.empty(img.shape, dtype=np.uint8)
    for start in range(0, img.shape[0], band_rows):
        end = min(start + band_rows, img.shape[0])
        band = img[start:end].astype(np.float32)
        band -= lo
        band *= scale
        np.clip(band, 0, 255, out=band)
        out[start:end] = band.astype(np.uint8)
    return out


def load_display_array(path: Path, channel: int | None = None) -> np.ndarray:
    """Full-resolution contrast-stretched uint8 grayscale array, ready to crop."""
    gray = _load_grayscale_raw(path, channel)
    sample = gray[::PERCENTILE_STRIDE, ::PERCENTILE_STRIDE]
    lo, hi = np.percentile(sample, PERCENTILE_RANGE)
    return _contrast_stretch_uint8(gray, float(lo), float(hi))


def crop_and_scale(display_array: np.ndarray, viewport_rect: tuple[float, float, float, float],
                    out_size: tuple[int, int]) -> Image.Image:
    """Crop `viewport_rect` (image-space x0,y0,x1,y1) out of `display_array` and
    scale it to `out_size` screen pixels.

    The requested rect is clamped to the array's bounds first — slicing with an
    unclamped negative/out-of-range rect wouldn't error, it would silently wrap
    around and read from the wrong edge of the array. Whatever part of the
    requested rect falls outside the image (e.g. panned past an edge) is
    letterboxed rather than stretched to fill.
    """
    h, w = display_array.shape
    x0, y0, x1, y1 = viewport_rect
    out_w, out_h = out_size

    cx0 = max(0, min(int(np.floor(x0)), w))
    cy0 = max(0, min(int(np.floor(y0)), h))
    cx1 = max(0, min(int(np.ceil(x1)), w))
    cy1 = max(0, min(int(np.ceil(y1)), h))

    canvas = Image.new("RGB", (max(1, out_w), max(1, out_h)), LETTERBOX_COLOR)
    if cx1 <= cx0 or cy1 <= cy0:
        return canvas  # viewport entirely outside the image

    crop = display_array[cy0:cy1, cx0:cx1]  # a view, not a copy
    crop_img = Image.fromarray(np.dstack([crop] * 3), mode="RGB")

    req_w, req_h = (x1 - x0) or 1.0, (y1 - y0) or 1.0
    scale_x, scale_y = out_w / req_w, out_h / req_h
    paste_x = round((cx0 - x0) * scale_x)
    paste_y = round((cy0 - y0) * scale_y)
    resized_w = max(1, round((cx1 - cx0) * scale_x))
    resized_h = max(1, round((cy1 - cy0) * scale_y))
    canvas.paste(crop_img.resize((resized_w, resized_h), RESAMPLE), (paste_x, paste_y))
    return canvas


class DisplayImageCache:
    """Holds exactly one image's display array at a time.

    Corpus file sizes vary >40x (2090x1690 up to 13246x10961), so an LRU of "the
    last N images" doesn't bound memory the way "keep exactly 1, discard on
    navigate-away" does — N could land on several of the largest files at once.
    """

    def __init__(self):
        self._path: Path | None = None
        self._array: np.ndarray | None = None

    def get(self, path: Path) -> np.ndarray:
        if self._path != path:
            self._array = load_display_array(path)
            self._path = path
        return self._array

    def invalidate(self) -> None:
        self._path = None
        self._array = None
