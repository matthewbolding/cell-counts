"""
manifest.py — folder scanning, hashing, and the consolidated `cellcounts.json`.

One JSON per photo folder replaces the old per-image `review/<stem>.json` files.
It's split into a light `images` section (touched on every folder-open hash check)
and a heavier `cells` section (touched only when a file is actually (re)processed),
so an incremental rescan of an already-processed folder never has to touch — or even
parse — the bulk of the polygon data.

Writes are atomic (`.tmp` + `.bak` + `os.replace`) since a corrupted manifest now
risks an entire folder's review work, not just one image's.
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

SCHEMA_VERSION = 1
MANIFEST_NAME = "cellcounts.json"
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
    triple.
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
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                data.setdefault("images", {})
                data.setdefault("cells", {})
                return data
            except (OSError, ValueError):
                pass  # fall through to a fresh manifest rather than crash on corruption
        return {"schema_version": SCHEMA_VERSION, "images": {}, "cells": {}}

    def needs_processing(self, filename: str, current_hash: str) -> bool:
        entry = self.data["images"].get(filename)
        if entry is None:
            return True
        return entry.get("hash") != current_hash or entry.get("status") != "done"

    def record_result(self, filename: str, prefix: str, channel: str, file_hash: str,
                       width: int, height: int, params: dict, cells: list[dict]) -> None:
        self.data["images"][filename] = {
            "prefix": prefix, "channel": channel, "hash": file_hash,
            "width": width, "height": height,
            "status": "done", "processed_at": now_iso(),
            "params": params, "error": None,
        }
        self.data["cells"][filename] = cells
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
        tmp_path = Path(str(self.path) + ".tmp")
        bak_path = Path(str(self.path) + ".bak")
        tmp_path.write_text(json.dumps(self.data, separators=(",", ":")), encoding="utf-8")
        if self.path.exists():
            try:
                shutil.copyfile(self.path, bak_path)
            except OSError:
                pass  # best-effort backup; never let it block the real write
        os.replace(tmp_path, self.path)
