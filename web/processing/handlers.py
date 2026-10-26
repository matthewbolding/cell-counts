"""
handlers.py — what each kind of job actually does. Called by processing/worker.py,
one job at a time per worker.

Two rules every handler follows:

- **The image may change underneath a running job.** A new upload can replace
  an image's content (or delete its project) while that image's old job is still
  running; replacing cancels queued jobs but never interrupts a running one.
  Handlers note the image's sha256 when they start and, before writing results,
  re-read the row under a lock: if the content changed or the row is gone, the
  results are dropped (JobGone) instead of being attached to the wrong file.
- **Failures leave a plain-language message on the image** (Image.error, which
  the review screen shows), while the full exception goes to the job row and
  the log for an admin.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from django.db import transaction
from django.utils import timezone

from core import storage
from processing import images, queue, segmentation, tiles
from processing.export import build_export_rows, download_filename, export_xlsx
from processing.models import Export, Job, JobKind, RescanSweep
from projects.models import Cell, Image, ImageStatus

log = logging.getLogger(__name__)

HASH_BLOCK = 8 * 1024 * 1024


class JobGone(Exception):
    """The job's image/project/export was deleted or replaced mid-run; there's
    nothing to record and nothing went wrong."""


class JobFailed(Exception):
    """An expected failure whose message is already suitable for the user."""


@dataclass
class WorkerContext:
    """State a worker keeps between jobs: the loaded model (GPU only) and the
    last image it read, since a rescan sweep runs up to 25 jobs against the same
    file and re-reading a 300 MB TIFF for each would dominate the sweep."""

    model: Any = None
    _gray_key: tuple | None = None
    _gray: np.ndarray | None = field(default=None, repr=False)

    def load_gray(self, image: Image, channel: int | None = None) -> np.ndarray:
        key = (image.id, image.sha256, channel)
        if self._gray_key != key:
            self._gray = None  # let the previous array go before reading the next
            self._gray = images.load_grayscale(original_file(image), channel)
            self._gray_key = key
        return self._gray

    def forget_image(self) -> None:
        self._gray_key = None
        self._gray = None


def original_file(image: Image) -> Path:
    path = storage.original_path(image.project_id, image.id)
    if not path.is_file():
        raise JobFailed("The uploaded file is missing on the server — add it again.")
    return path


def _fresh_image(job: Job) -> Image:
    try:
        return Image.objects.select_related("project").get(pk=job.image_id)
    except Image.DoesNotExist as exc:
        raise JobGone from exc


@transaction.atomic
def _lock_image(image_id, sha256: str) -> Image:
    """Re-read the image under a row lock, or raise JobGone if it was deleted or
    its content replaced since the job started."""
    image = Image.objects.select_for_update().filter(pk=image_id).first()
    if image is None or image.sha256 != sha256:
        raise JobGone
    return image


def _set_image_error(image_id, sha256: str, message: str) -> None:
    Image.objects.filter(pk=image_id, sha256=sha256).update(
        status=ImageStatus.ERROR, error=message[:500], updated_at=timezone.now())


def _read_failure_message(exc: Exception) -> str:
    if isinstance(exc, JobFailed):
        return str(exc)
    if isinstance(exc, MemoryError):
        return "The server ran out of memory on this image."
    if "out of memory" in str(exc).lower():
        return "The GPU ran out of memory on this image."
    return "Processing failed. Try Retry; if it keeps failing, tell your administrator."


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(HASH_BLOCK):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# prepare (CPU): verify, measure, tile
# --------------------------------------------------------------------------- #
def prepare(job: Job, ctx: WorkerContext) -> dict:
    image = _fresh_image(job)
    sha = image.sha256
    try:
        path = original_file(image)
        if sha256_of(path) != sha:
            path.unlink(missing_ok=True)
            raise JobFailed("The upload was corrupted in transit — add the file again.")
        try:
            gray = images.load_grayscale(path)
        except Exception as exc:
            raise JobFailed("This file couldn't be read as a TIFF image.") from exc
        height, width = gray.shape
        rev = tiles.tiles_rev(sha)
        levels = tiles.build_pyramid(gray, storage.tiles_dir(image.id, rev))
        del gray
    except JobGone:
        raise
    except Exception as exc:
        _set_image_error(image.id, sha, _read_failure_message(exc))
        raise

    with transaction.atomic():
        image = _lock_image(image.id, sha)
        _remove_other_tile_revs(image.id, keep=rev)
        image.width, image.height = width, height
        image.tiles_rev, image.tile_levels = rev, levels
        image.error = ""
        if job.params.get("imported"):
            image.status = ImageStatus.DONE
            image.processed_at = image.processed_at or timezone.now()
            image.save()
        else:
            image.save()
            queue.enqueue_segmentation(image, job.user)
    return {"width": width, "height": height, "levels": levels}


def _remove_other_tile_revs(image_id, keep: str) -> None:
    root = storage.tiles_root(image_id)
    if not root.is_dir():
        return
    for child in root.iterdir():
        if child.name != keep:
            shutil.rmtree(child, ignore_errors=True)


# --------------------------------------------------------------------------- #
# segment (GPU)
# --------------------------------------------------------------------------- #
def segment(job: Job, ctx: WorkerContext) -> dict:
    image = _fresh_image(job)
    sha = image.sha256
    Image.objects.filter(pk=image.id, sha256=sha).update(
        status=ImageStatus.SEGMENTING, updated_at=timezone.now())
    try:
        params = {**(job.params or {})}
        gray = ctx.load_gray(image, params.get("channel"))
        result = segmentation.run(ctx.model, gray, params)
    except Exception as exc:
        _set_image_error(image.id, sha, _read_failure_message(exc))
        raise
    finally:
        # A full-image segmentation is rarely followed by a rescan of the same
        # image; don't hold hundreds of MB between jobs.
        ctx.forget_image()

    cells = result["cells"]
    with transaction.atomic():
        image = _lock_image(image.id, sha)
        image.cells.all().delete()
        Cell.objects.bulk_create([
            Cell(image=image, cell_id=c["id"], polygons=c["polygons"], centroid_x=c["centroid"][0],
                 centroid_y=c["centroid"][1], area=c["area"], status=c["status"],
                 filter_reason=c["filter_reason"] or "", source=c["source"], edited=False)
            for c in cells
        ], batch_size=2000)
        image.status = ImageStatus.DONE
        image.error = ""
        image.cells_version += 1
        image.segmentation_params = result["params"]
        image.processed_at = timezone.now()
        image.save()
    n_kept = sum(1 for c in cells if c["status"] == "kept")
    return {"n_cells": len(cells), "n_kept": n_kept}


# --------------------------------------------------------------------------- #
# rescan (GPU): one parameter combination on one crop
# --------------------------------------------------------------------------- #
def rescan(job: Job, ctx: WorkerContext) -> dict:
    try:
        sweep = RescanSweep.objects.select_related("image").get(pk=job.sweep_id)
    except RescanSweep.DoesNotExist as exc:
        raise JobGone from exc
    image = sweep.image
    gray = ctx.load_gray(image)
    crop, (origin_x, origin_y) = images.crop(gray, tuple(sweep.rect))
    if crop.size == 0:
        raise JobFailed("That region is outside the image.")
    params = {"diameter": job.params["diameter"], "cellprob_threshold": job.params["cellprob_threshold"]}
    result = segmentation.run(ctx.model, crop, params)
    return {
        "cells": segmentation.translate_cells(result["cells"], origin_x, origin_y),
        "diameter": params["diameter"],
        "cellprob_threshold": params["cellprob_threshold"],
    }


# --------------------------------------------------------------------------- #
# export (CPU)
# --------------------------------------------------------------------------- #
def export(job: Job, ctx: WorkerContext) -> dict:
    try:
        exp = Export.objects.select_related("project").get(pk=job.export_id)
    except Export.DoesNotExist as exc:
        raise JobGone from exc
    summary_rows, cell_rows = build_export_rows(exp.project, list(exp.prefixes))
    path = export_xlsx(storage.export_path(exp.id),
                       summary_rows if exp.include_summary else None,
                       cell_rows if exp.include_cells else None)
    return {"filename": download_filename(exp.project.name), "size": path.stat().st_size}


# --------------------------------------------------------------------------- #
# purge (CPU): files left behind by deleted projects / replaced images
# --------------------------------------------------------------------------- #
def _uuid(value) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def purge(job: Job, ctx: WorkerContext) -> dict:
    root = storage.check_volume()
    removed = 0
    project_id = _uuid(job.params.get("project_id"))
    if project_id:
        shutil.rmtree(root / "originals" / project_id, ignore_errors=True)
        removed += 1
    for image_id in job.params.get("image_ids", []):
        if (iid := _uuid(image_id)) is not None:
            shutil.rmtree(storage.tiles_root(iid), ignore_errors=True)
            removed += 1
    for image_id, rev in job.params.get("tile_dirs", []):
        iid = _uuid(image_id)
        if iid is not None and isinstance(rev, str) and rev.isalnum():
            shutil.rmtree(storage.tiles_dir(iid, rev), ignore_errors=True)
            removed += 1
    return {"removed": removed}


HANDLERS: dict[str, Callable[[Job, WorkerContext], dict]] = {
    JobKind.PREPARE: prepare,
    JobKind.SEGMENT: segment,
    JobKind.RESCAN: rescan,
    JobKind.EXPORT: export,
    JobKind.PURGE: purge,
}
