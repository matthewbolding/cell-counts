"""
storage.py — every path under the NAS mount, in one place.

Layout (see DESIGN.md): staging/, originals/, tiles/, exports/, backups/ under
`settings.CELLCOUNTS_DATA_ROOT`. The root must contain a `.cellcounts-volume`
marker file before anything is written. That's the guard against the classic NFS
failure: if the mount didn't come up after a reboot, `/data` is just an empty
directory on the local disk, and without this check every upload would quietly
fill the (small) system disk instead of the NAS.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

from django.conf import settings

VOLUME_MARKER = ".cellcounts-volume"


class StorageUnavailable(RuntimeError):
    """The data volume isn't mounted (marker missing). Callers turn this into a
    503 rather than writing anywhere."""


def data_root() -> Path:
    return Path(settings.CELLCOUNTS_DATA_ROOT)


def check_volume() -> Path:
    root = data_root()
    if not (root / VOLUME_MARKER).is_file():
        raise StorageUnavailable(
            f"{root} is not the Cell Counts data volume ({VOLUME_MARKER} missing) — is the NAS mounted?")
    return root


def volume_available() -> bool:
    try:
        check_volume()
    except StorageUnavailable:
        return False
    return True


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def staging_path(upload_id: UUID) -> Path:
    return _ensure_dir(check_volume() / "staging") / f"{upload_id}.part"


def original_path(project_id: UUID, image_id: UUID) -> Path:
    return _ensure_dir(check_volume() / "originals" / str(project_id)) / f"{image_id}.tif"


def tiles_dir(image_id: UUID, rev: str) -> Path:
    return check_volume() / "tiles" / str(image_id) / rev


def tiles_root(image_id: UUID) -> Path:
    return check_volume() / "tiles" / str(image_id)


def export_path(export_id: UUID) -> Path:
    return _ensure_dir(check_volume() / "exports") / f"{export_id}.xlsx"


def write_at(path: Path, offset: int, data_iter) -> int:
    """Write an iterable of byte chunks into `path` starting at `offset`, without
    truncating. Several chunks of one upload can arrive concurrently; each
    lands at its own offset in the same preallocated file, so there's no
    reassembly copy at the end. Returns bytes written."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o640)
    written = 0
    try:
        for block in data_iter:
            os.pwrite(fd, block, offset + written)
            written += len(block)
        os.fsync(fd)
    finally:
        os.close(fd)
    return written
