"""
tiles.py — build the zoomable tile pyramid the browser draws from.

Level 0 is full resolution; each level up halves both dimensions (2x2 box
average) until the whole image fits in one tile. Tiles are 512 px grayscale PNG
(lossless: people judge dim cells by eye, so no JPEG artifacts), stored as
`<level>/<col>_<row>.png` under `tiles/<image_id>/<rev>/`. `rev` comes from the
file's sha256, so replacing an image's content changes every tile URL and
browser caches can treat tiles as immutable.

The pyramid is written to a temporary sibling directory and renamed into place,
so a half-built pyramid is never visible.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import numpy as np
from PIL import Image as PILImage

from processing.images import contrast_stretch

TILE_SIZE = 512
PNG_COMPRESS_LEVEL = 4  # zlib level: most of the size win of 9 for far less CPU


def tiles_rev(sha256: str) -> str:
    return sha256[:12]


def level_count(width: int, height: int) -> int:
    """Levels needed until the image fits in one tile. Level L is
    ceil(width / 2**L) x ceil(height / 2**L) — the same rounding PIL's
    reduce(2) does at each step, which the browser relies on."""
    return 1 + max(0, math.ceil(math.log2(max(width, height) / TILE_SIZE)))


def _write_level(img: PILImage.Image, level_dir: Path) -> None:
    level_dir.mkdir(parents=True, exist_ok=True)
    w, h = img.size
    for row in range(math.ceil(h / TILE_SIZE)):
        for col in range(math.ceil(w / TILE_SIZE)):
            box = (col * TILE_SIZE, row * TILE_SIZE, min(w, (col + 1) * TILE_SIZE), min(h, (row + 1) * TILE_SIZE))
            img.crop(box).save(level_dir / f"{col}_{row}.png", compress_level=PNG_COMPRESS_LEVEL)


def build_pyramid(gray: np.ndarray, out_dir: Path) -> int:
    """Write every level for `gray` into `out_dir` (replacing it). Returns the
    number of levels."""
    tmp_dir = out_dir.with_name(out_dir.name + ".building")
    shutil.rmtree(tmp_dir, ignore_errors=True)

    img = PILImage.fromarray(contrast_stretch(gray), mode="L")
    levels = level_count(*img.size)
    for level in range(levels):
        _write_level(img, tmp_dir / str(level))
        if level + 1 < levels:
            img = img.reduce(2)

    shutil.rmtree(out_dir, ignore_errors=True)
    tmp_dir.rename(out_dir)
    return levels
