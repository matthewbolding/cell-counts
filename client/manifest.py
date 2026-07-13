"""
manifest.py — folder scanning, hashing, and the consolidated `cellcounts.json`.

One JSON per photo folder replaces the old per-image `review/<stem>.json` files.
`cellcounts.json` itself holds only the light `images` section (filename -> hash /
processing status / params) — touched on every folder-open hash check. Each image's
heavier cell data (polygons, potentially thousands per image) lives in its own
sidecar file, `<filename>.cells.json`, so a hash-check scan never has to touch — or
even parse — the bulk of the polygon data, and a debounced save during live editing
only ever rewrites the one image actually being edited instead of the whole folder.

(Phase 1 originally embedded all images' cells in one `cellcounts.json`. Measured
against real usage, that format doesn't scale: a fresh folder-wide save re-serializes
every previously-processed image's cells on every new image's completion, and a
close-to-real corpus of ~186 images would land the single JSON file around 1GB with
multi-second write stalls. `_load()` transparently migrates any folder still in that
old format the first time it's opened.)

Writes are atomic (`.tmp` + `.bak` + `os.replace`) since a corrupted manifest or
sidecar risks real review work.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2
MANIFEST_NAME = "cellcounts.json"
CELLS_SUFFIX = ".cells.json"
HASH_CHUNK_SIZE = 8 * 1024 * 1024

# {PREFIX}_{CCK,CHR,SNAP}.tif — PREFIX is a letter (animal) + digits (sample number).
FILENAME_RE = re.compile(r"^([A-Za-z]\d+)_(CCK|CHR|SNAP)\.tif$", re.IGNORECASE)

_SKIP_NAMES = {MANIFEST_NAME}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK_SIZE), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _atomic_write_json(path: Path, obj) -> None:
    tmp_path = Path(str(path) + ".tmp")
    bak_path = Path(str(path) + ".bak")
    tmp_path.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    if path.exists():
        try:
            shutil.copyfile(path, bak_path)
        except OSError:
            pass  # best-effort backup; never let it block the real write
    os.replace(tmp_path, path)


@dataclass
class ScannedFile:
    path: Path
    prefix: str
    channel: str  # "CCK" | "CHR" | "SNAP"


def scan_folder(folder: Path) -> tuple[list[ScannedFile], list[Path]]:
    """Return (recognized {PREFIX}_{CCK,CHR,SNAP}.tif files, skipped/unrecognized files).

    Unrecognized files (wrong suffix, e.g. a stray `_CCL.tif`) are reported, not
    silently dropped — the real corpus has at least one such file, so the caller
    must warn about it rather than assume every prefix has a clean CCK/CHR/SNAP
    triple. `.cells.json` sidecars are naturally excluded (they don't end in .tif).
    """
    recognized: list[ScannedFile] = []
    skipped: list[Path] = []
    for p in sorted(folder.rglob("*")):
        if not p.is_file() or p.name in _SKIP_NAMES or p.name.endswith((".tmp", ".bak")):
            continue
        if p.suffix.lower() != ".tif":
            continue
        m = FILENAME_RE.match(p.name)
        if m:
            recognized.append(ScannedFile(path=p, prefix=m.group(1).upper(), channel=m.group(2).upper()))
        else:
            skipped.append(p)
    return recognized, skipped


class Manifest:
    def __init__(self, folder: Path):
        self.folder = folder
        self.path = folder / MANIFEST_NAME
        self.data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"schema_version": SCHEMA_VERSION, "images": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schema_version": SCHEMA_VERSION, "images": {}}  # corrupt: start fresh rather than crash

        images = raw.get("images", {})
        needs_migration = "cells" in raw or raw.get("schema_version", 1) < SCHEMA_VERSION
        if needs_migration:
            for filename, cells in raw.get("cells", {}).items():
                self.save_cells(filename, cells)
            data = {"schema_version": SCHEMA_VERSION, "images": images}
            self.data = data
            self.save()
            return data

        return {"schema_version": SCHEMA_VERSION, "images": images}

    def needs_processing(self, filename: str, current_hash: str) -> bool:
        entry = self.data["images"].get(filename)
        if entry is None:
            return True
        if entry.get("hash") != current_hash:
            return True  # content changed since it was submitted/completed -> must resubmit
        # "processing" means a job is already outstanding for this exact content
        # (see record_submitted) -- resume polling it, don't submit a second one.
        return entry.get("status") not in ("done", "processing")

    def pending_job(self, filename: str, current_hash: str) -> str | None:
        """job_id of an already-outstanding submission for this exact content, if
        any -- lets a resumed session poll it instead of re-uploading. Returns
        None once record_result/record_error overwrites the entry (status is no
        longer "processing") or if the file changed since it was submitted."""
        entry = self.data["images"].get(filename)
        if entry is None or entry.get("hash") != current_hash or entry.get("status") != "processing":
            return None
        return entry.get("job_id")

    def record_submitted(self, filename: str, prefix: str, channel: str, file_hash: str, job_id: str) -> None:
        """Written immediately after a successful upload, before waiting for the
        job to finish — so a client that closes (or crashes) between now and the
        job actually completing resumes polling `job_id` next launch instead of
        re-uploading and creating a duplicate job."""
        self.data["images"][filename] = {
            "prefix": prefix, "channel": channel, "hash": file_hash,
            "width": None, "height": None,
            "status": "processing", "processed_at": None,
            "params": None, "error": None, "job_id": job_id,
        }
        self.save()

    def record_result(self, filename: str, prefix: str, channel: str, file_hash: str,
                       width: int, height: int, params: dict, cells: list[dict]) -> None:
        # Heavy data first: only mark `status: "done"` in the light manifest once the
        # cells are actually on disk, so a crash between the two writes can't leave a
        # "done" entry pointing at a stale/missing sidecar.
        self.save_cells(filename, cells)
        self.data["images"][filename] = {
            "prefix": prefix, "channel": channel, "hash": file_hash,
            "width": width, "height": height,
            "status": "done", "processed_at": now_iso(),
            "params": params, "error": None,
        }
        self.save()

    def record_error(self, filename: str, prefix: str, channel: str, file_hash: str, error: str) -> None:
        self.data["images"][filename] = {
            "prefix": prefix, "channel": channel, "hash": file_hash,
            "width": None, "height": None,
            "status": "error", "processed_at": now_iso(),
            "params": None, "error": error,
        }
        self.save()

    def save(self) -> None:
        _atomic_write_json(self.path, self.data)

    def _cells_path(self, filename: str) -> Path:
        return self.folder / f"{filename}{CELLS_SUFFIX}"

    def load_cells(self, filename: str) -> list[dict]:
        """Lazily load one image's cells — only called when that image is actually
        opened for review, never during a folder-wide hash scan."""
        path = self._cells_path(filename)
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("cells", [])
        except (OSError, ValueError):
            return []

    def save_cells(self, filename: str, cells: list[dict]) -> None:
        """Atomic; plain file I/O with no shared mutable state, so safe to call from
        a background thread (the review UI debounces edits onto one)."""
        _atomic_write_json(self._cells_path(filename), {"cells": cells})
