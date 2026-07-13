"""
rescan.py — crop-based parameter sweep: orchestration and coordinate math.

No server-side changes were needed for this feature — `/uploads/{id}/complete`
already accepts an arbitrary `params` dict that flows straight into
`segment.run(model, path, params)`. A "sweep" is just several independent
upload+job round trips against the same small crop, each with different params,
using the exact chunked-upload/job-poll machinery `api_client.py` already has.

One real constraint: `server/uploads.py:complete_upload()` deletes its staging
directory once it runs, so the same `upload_id` can't be completed twice. Since
`upload_id` is derived from `(filename, sha256)`, each combination gets its own
distinct filename (identical crop bytes, different name) rather than trying to
reuse one upload across parameter variants.

Pure logic, no Tk — testable with a fake `ApiClient` that just returns canned
results, same as any other module in this project that talks to the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import tifffile

from api_client import ApiClient, ApiError
from manifest import hash_file


@dataclass
class RescanCombo:
    diameter: float
    cellprob_threshold: float
    job_id: str
    cells: list[dict] = field(default_factory=list)
    error: str | None = None  # set instead of cells if this combination failed


def crop_raw_region(raw: np.ndarray, rect: tuple[float, float, float, float]) -> np.ndarray:
    """Clamp `rect` (image-space x0,y0,x1,y1) to `raw`'s bounds and return the
    sliced region (a view, not a copy). A rect that overhangs the array's edges
    is clamped, not wrapped or errored — same class of bug `imaging.crop_and_scale`
    already guards against for on-screen rendering."""
    h, w = raw.shape
    x0, y0, x1, y1 = rect
    cx0 = max(0, min(int(round(x0)), w))
    cy0 = max(0, min(int(round(y0)), h))
    cx1 = max(0, min(int(round(x1)), w))
    cy1 = max(0, min(int(round(y1)), h))
    return raw[cy0:cy1, cx0:cx1]


def translate_cells(cells: list[dict], origin_x: float, origin_y: float) -> list[dict]:
    """The server has no idea a cropped image came from a larger one — everything
    it returns is in the crop's own local (0,0)-origin coordinate space. Add the
    crop's origin back onto every polygon point and centroid so the result lines
    up with the real image. Returns new dicts; never mutates the input."""
    translated = []
    for c in cells:
        new_polygons = [[[round(px + origin_x, 2), round(py + origin_y, 2)] for px, py in ring]
                         for ring in c["polygons"]]
        new_centroid = [round(c["centroid"][0] + origin_x, 2), round(c["centroid"][1] + origin_y, 2)]
        translated.append({**c, "polygons": new_polygons, "centroid": new_centroid})
    return translated


def _fmt(value: float) -> str:
    """Filesystem-friendly number formatting for temp filenames: 0.0 -> "0",
    -2.5 -> "neg2p5"."""
    return f"{value:g}".replace(".", "p").replace("-", "neg")


def run_sweep(client: ApiClient, crop: np.ndarray, origin: tuple[float, float],
              diameters: list[float], cellprobs: list[float], tmp_dir: Path,
              on_progress: Callable[[int, int], None] | None = None) -> list[RescanCombo]:
    """Run every (diameter, cellprob_threshold) combination sequentially against
    `crop`, translating each successful result back into full-image coordinates
    via `origin`. A combination that fails (network error, job error) is recorded
    on its own `RescanCombo.error` rather than aborting the rest — same
    one-failure-doesn't-stop-the-batch precedent `app.py`'s main processing loop
    already follows. Caller owns `tmp_dir` (created/cleaned up by them)."""
    origin_x, origin_y = origin
    combos = [(d, cp) for d in diameters for cp in cellprobs]
    total = len(combos)
    results: list[RescanCombo] = []

    for i, (diameter, cellprob) in enumerate(combos, 1):
        temp_path = tmp_dir / f"rescan_d{_fmt(diameter)}_cp{_fmt(cellprob)}_{i}.tif"
        tifffile.imwrite(temp_path, crop)
        file_hash = hash_file(temp_path)
        try:
            job_id = client.upload_file(
                temp_path, file_hash,
                params={"diameter": diameter, "cellprob_threshold": cellprob})
            result = client.poll_job(job_id)
            cells = translate_cells(result["cells"], origin_x, origin_y)
            results.append(RescanCombo(diameter=diameter, cellprob_threshold=cellprob,
                                        job_id=job_id, cells=cells))
        except ApiError as exc:
            results.append(RescanCombo(diameter=diameter, cellprob_threshold=cellprob,
                                        job_id="", error=str(exc)))
        if on_progress:
            on_progress(i, total)

    return results
