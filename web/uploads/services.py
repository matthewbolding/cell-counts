"""
services.py — the upload protocol's logic, kept out of the HTTP layer.

`plan` is the cheap stat check (filename + size + lastModified: a file that
matches what's on the server isn't hashed again), `init` happens once the
browser has hashed a file, chunks are written in place
at their offsets, and `complete` turns a fully received file into an Image and
hands it to the CPU worker.

Quota is always charged to the *project owner*, whoever uploads: a collaborator
adding images to a shared project fills the owner's space. Open upload sessions
count as
reserved space (User.storage_used_bytes), and the check runs with the owner's
row locked so two uploads racing can't both squeeze under the limit.
"""

from __future__ import annotations

import json
import logging
import os
import re
import zlib
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from accounts.models import User
from core import storage
from core.api import ApiError
from processing import queue
from processing.models import Job, JobKind
from processing.tiles import tiles_rev
from projects.api import validate_new_cell
from projects.models import Cell, Image, ImageStatus, Project, Sample
from uploads.models import UploadSession

log = logging.getLogger(__name__)

# {PREFIX}_{CCK,CHR,SNAP}.tif, PREFIX = letters (animal) + digits (sample).
FILENAME_RE = re.compile(r"^([A-Za-z]+\d+)_(CCK|CHR|SNAP)\.tif$", re.IGNORECASE)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

MAX_FILE_BYTES = 8 * 1024**3          # the real corpus tops out under 300 MB per file
MAX_PLAN_FILES = 20_000
READ_BLOCK = 1024 * 1024
MAX_IMPORT_COMPRESSED = 64 * 1024**2  # a .cells.json for the densest image is ~17 MB uncompressed
MAX_IMPORT_BYTES = 512 * 1024**2
STALE_UPLOAD_AGE = timedelta(days=7)


@dataclass(frozen=True)
class ParsedName:
    filename: str
    prefix: str
    channel: str


def parse_filename(name: str) -> ParsedName | None:
    m = FILENAME_RE.match(name or "")
    if not m:
        return None
    return ParsedName(filename=name, prefix=m.group(1).upper(), channel=m.group(2).upper())


def _existing_image(project: Project, parsed: ParsedName) -> Image | None:
    """The image occupying this sample/channel slot, if any. Looked up by slot
    rather than exact filename so `a1_cck.tif` and `A1_CCK.tif` are recognized as
    the same image instead of colliding on the (sample, channel) constraint."""
    return (Image.objects.filter(project=project, sample__prefix=parsed.prefix, channel=parsed.channel)
            .select_related("sample").first())


def _original_exists(image: Image) -> bool:
    return storage.original_path(image.project_id, image.id).is_file()


def quota_json(owner: User) -> dict:
    return {"used": owner.storage_used_bytes(), "limit": owner.storage_quota_bytes}


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def plan(project: Project, files: list) -> dict:
    if not isinstance(files, list) or len(files) > MAX_PLAN_FILES:
        raise ApiError(400, "invalid_request", f"expected up to {MAX_PLAN_FILES} files")

    parsed_files: list[tuple[dict, ParsedName]] = []
    unrecognized: list[str] = []
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("name"), str):
            raise ApiError(400, "invalid_request", "each file needs a name")
        parsed = parse_filename(f["name"])
        if parsed is None:
            unrecognized.append(f["name"])
        else:
            parsed_files.append((f, parsed))

    existing = {(img.sample.prefix, img.channel): img
                for img in project.images.select_related("sample")}
    decisions = []
    for f, parsed in parsed_files:
        img = existing.get((parsed.prefix, parsed.channel))
        unchanged = (
            img is not None
            and img.status != ImageStatus.ERROR
            and img.size_bytes == f.get("size")
            and img.client_mtime is not None
            and img.client_mtime == f.get("last_modified")
        )
        decisions.append({
            "name": f["name"],
            "prefix": parsed.prefix,
            "channel": parsed.channel,
            "decision": "unchanged" if unchanged else "needs_hash",
            "image_id": str(img.id) if img else None,
            # Lets the browser match a folder's review data to a file it skipped
            # hashing (cellcounts.json records each file's sha256).
            "sha256": img.sha256 if img else None,
        })
    return {"files": decisions, "unrecognized": unrecognized, "quota": quota_json(project.owner)}


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #
def _session_json(session: UploadSession) -> dict:
    return {
        "action": "upload",
        "upload_id": str(session.id),
        "chunk_size": session.chunk_size,
        "total_chunks": session.total_chunks,
        "chunks_present": sorted(session.received),
        "replaces_image_id": str(session.replaces_id) if session.replaces_id else None,
    }


def init(project: Project, user, data: dict) -> dict:
    from projects.api import image_json

    parsed = parse_filename(data.get("filename"))
    if parsed is None:
        raise ApiError(400, "invalid_filename", "Filenames must look like A1_SNAP.tif, A1_CCK.tif or A1_CHR.tif.")
    size = data.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_FILE_BYTES:
        raise ApiError(400, "invalid_size", "That file is empty or too large to upload.")
    sha256 = str(data.get("sha256", "")).lower().removeprefix("sha256:")
    if not SHA256_RE.match(sha256):
        raise ApiError(400, "invalid_hash", "sha256 must be 64 hex characters")
    last_modified = data.get("last_modified")
    if not isinstance(last_modified, int) or isinstance(last_modified, bool):
        last_modified = None
    replace = bool(data.get("replace"))

    with transaction.atomic():
        # Serializes quota checks for this owner (see module docstring).
        owner = User.objects.select_for_update().get(pk=project.owner_id)

        existing = _existing_image(project, parsed)
        if existing is not None and existing.sha256 == sha256 and _original_exists(existing):
            if existing.client_mtime != last_modified:
                existing.client_mtime = last_modified
                existing.save(update_fields=["client_mtime", "updated_at"])
            return {"action": "skip", "image": image_json(existing)}

        # Identical content whose original is gone (e.g. it failed the hash
        # check and was discarded) falls through to a fresh upload without a
        # "changed" confirmation: nothing the user reviewed is being replaced.
        if existing is not None and existing.sha256 != sha256 and not replace:
            raise ApiError(409, "changed", f"{existing.filename} is already in this project with different "
                           "content. Replacing it discards its review edits.", image_id=str(existing.id))

        session = UploadSession.objects.filter(
            project=project, filename=parsed.filename, sha256=sha256, completed_at__isnull=True).first()
        if session is not None:
            if session.user_id != user.id:
                raise ApiError(409, "upload_in_progress",
                               f"{session.user.label} is already uploading {parsed.filename}.")
            return _session_json(session)

        credit = existing.size_bytes if existing is not None else 0
        used, limit = owner.storage_used_bytes(), owner.storage_quota_bytes
        if used - credit + size > limit:
            who = "your" if owner.id == user.id else f"{owner.label}'s"
            raise ApiError(413, "quota_exceeded", f"This would go over {who} storage quota.",
                           used=used, limit=limit, needed=size - credit)

        chunk_size = settings.CELLCOUNTS_UPLOAD_CHUNK_SIZE
        session = UploadSession.objects.create(
            project=project, user=user, filename=parsed.filename, prefix=parsed.prefix,
            channel=parsed.channel, sha256=sha256, size_bytes=size, client_mtime=last_modified,
            chunk_size=chunk_size, total_chunks=(size + chunk_size - 1) // chunk_size, replaces=existing)
    return _session_json(session)


# --------------------------------------------------------------------------- #
# chunks
# --------------------------------------------------------------------------- #
def expected_chunk_length(session: UploadSession, index: int) -> int:
    return min(session.chunk_size, session.size_bytes - index * session.chunk_size)


def write_chunk(session: UploadSession, index: int, stream, content_length: int | None) -> int:
    """Stream one chunk to its offset in the staging file. Reads at most the
    chunk's expected length, so a body that's too long can never spill into the
    next chunk's bytes; a short or long body is rejected without being recorded,
    and the client simply re-sends it."""
    if not 0 <= index < session.total_chunks:
        raise ApiError(400, "invalid_chunk", f"chunk {index} is out of range (0–{session.total_chunks - 1})")
    expected = expected_chunk_length(session, index)
    if content_length is not None and content_length != expected:
        raise ApiError(400, "chunk_length", f"chunk {index} should be {expected} bytes, got {content_length}")

    def blocks():
        remaining = expected
        while remaining > 0:
            block = stream.read(min(READ_BLOCK, remaining))
            if not block:
                return
            remaining -= len(block)
            yield block

    written = storage.write_at(storage.staging_path(session.id), index * session.chunk_size, blocks())
    if written != expected or stream.read(1):
        raise ApiError(400, "chunk_length", f"chunk {index} should be {expected} bytes")

    UploadSession.objects.filter(pk=session.pk).exclude(received__contains=[index]).update(
        received=models.Func(models.F("received"), models.Value(index), function="array_append",
                             output_field=UploadSession._meta.get_field("received")),
        updated_at=timezone.now())
    received = UploadSession.objects.filter(pk=session.pk).values_list("received", flat=True).get()
    return len(received)


# --------------------------------------------------------------------------- #
# complete
# --------------------------------------------------------------------------- #
def read_import_body(stream, content_encoding: str) -> dict | None:
    """The optional body of `complete`: a (usually gzipped) `.cells.json`.
    Both the compressed and decompressed sizes are capped, so a crafted body
    can't balloon in memory."""
    raw = stream.read(MAX_IMPORT_COMPRESSED + 1)
    if not raw:
        return None
    if len(raw) > MAX_IMPORT_COMPRESSED:
        raise ApiError(413, "import_too_large", "The imported cell data is too large.")
    if content_encoding.strip().lower() == "gzip":
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            raw = decoder.decompress(raw, MAX_IMPORT_BYTES)
        except zlib.error as exc:
            raise ApiError(400, "invalid_import", "The imported cell data isn't valid gzip.") from exc
        if decoder.unconsumed_tail:
            raise ApiError(413, "import_too_large", "The imported cell data is too large.")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ApiError(400, "invalid_import", "The imported cell data isn't valid JSON.") from exc
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_import", "expected a JSON object")
    return data


@dataclass
class ImportedCells:
    rows: list[dict]
    params: dict | None
    skipped: int  # cells dropped because they couldn't be read

    def summary(self) -> dict:
        return {"cells": len(self.rows), "skipped": self.skipped}


def _usable_rings(polygons) -> list:
    if not isinstance(polygons, list):
        return []
    return [ring for ring in polygons
            if isinstance(ring, list) and len(ring) >= 3
            and all(isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) for v in p)
                    for p in ring)]


def imported_cell_rows(payload) -> ImportedCells:
    """Rows for a `.cells.json` review file (its schema, unchanged).

    Lenient on purpose: this is someone's finished review work, so one
    unreadable cell (a degenerate ring, a bad field) is dropped and counted
    rather than throwing away every other cell and re-segmenting the image.
    Each cell keeps its own `edited` flag: untouched model cells stay
    unedited, unlike cells drawn in the web app."""
    if not isinstance(payload, dict) or not isinstance(payload.get("cells"), list):
        raise ApiError(400, "invalid_import", "imported_cells needs a cells list")
    rows: list[dict] = []
    seen: set[int] = set()
    skipped = 0
    for c in payload["cells"]:
        if not isinstance(c, dict):
            skipped += 1
            continue
        rings = _usable_rings(c.get("polygons"))
        try:
            row = validate_new_cell({**c, "polygons": rings})
        except ApiError:
            skipped += 1
            continue
        if row["cell_id"] in seen:
            skipped += 1
            continue
        seen.add(row["cell_id"])
        row["edited"] = bool(c.get("edited", False))
        rows.append(row)
    params = payload.get("params") if isinstance(payload.get("params"), dict) else None
    return ImportedCells(rows=rows, params=params, skipped=skipped)


def complete(session: UploadSession, user, import_data: dict | None) -> tuple[Image, dict | None]:
    """Returns the image, plus a summary of any imported review cells."""
    missing = session.missing_chunks()
    if missing:
        raise ApiError(409, "missing_chunks", f"Some of the file hasn't arrived yet ({len(missing)} missing).",
                       missing=missing[:50])
    staging = storage.staging_path(session.id)
    if not staging.is_file() or staging.stat().st_size != session.size_bytes:
        raise ApiError(409, "missing_chunks", "The uploaded data is incomplete; add the file again.")

    imported: ImportedCells | None = None
    if import_data is not None and import_data.get("imported_cells") is not None:
        imported = imported_cell_rows(import_data["imported_cells"])
    imported_rows = imported.rows if imported else None
    imported_params = imported.params if imported else None

    project = session.project
    with transaction.atomic():
        session = UploadSession.objects.select_for_update().get(pk=session.pk)
        if session.completed_at is not None:
            raise ApiError(409, "already_complete", "This upload has already been completed.")
        sample, _ = Sample.objects.get_or_create(project=project, prefix=session.prefix)
        image = (Image.objects.select_for_update()
                 .filter(project=project, sample=sample, channel=session.channel).first())
        purge_dirs: list[list[str]] = []
        # Re-uploading the same content (its original went missing) keeps the
        # cells already reviewed on the server; they're newer than anything a
        # .cells.json could bring.
        keep_cells = image is not None and image.sha256 == session.sha256 and image.cells.exists()
        if keep_cells:
            imported_rows = None

        if image is None:
            image = Image(project=project, sample=sample, channel=session.channel)
        else:
            queue.cancel_jobs(Job.objects.filter(image=image), reason="Replaced by a new upload")
            if not keep_cells:
                image.cells.all().delete()
                image.cells_version += 1
            if image.tiles_rev and image.tiles_rev != tiles_rev(session.sha256):
                purge_dirs.append([str(image.id), image.tiles_rev])

        image.filename = session.filename
        image.sha256 = session.sha256
        image.size_bytes = session.size_bytes
        image.client_mtime = session.client_mtime
        image.width = image.height = None
        image.status = ImageStatus.PREPARING
        image.error = ""
        image.tiles_rev = ""
        image.tile_levels = 0
        if not keep_cells:
            image.segmentation_params = imported_params
            image.processed_at = None
        image.uploaded_by = user

        final = storage.original_path(project.id, image.id)
        os.replace(staging, final)
        try:
            if imported_rows:
                image.cells_version += 1
            image.save()
            if imported_rows:
                # Review files carry no record of who made the edits, so edited_by stays empty.
                Cell.objects.bulk_create([Cell(image=image, **r) for r in imported_rows], batch_size=2000)
            session.completed_at = timezone.now()
            session.save(update_fields=["completed_at", "updated_at"])
            queue.enqueue(JobKind.PREPARE, user, image=image,
                          params={"imported": keep_cells or imported_rows is not None})
            if purge_dirs:
                queue.enqueue(JobKind.PURGE, user, params={"tile_dirs": purge_dirs})
        except Exception:
            os.replace(final, staging)
            raise
    return image, (imported.summary() if imported and imported_rows is not None else None)


def abort(session: UploadSession) -> None:
    storage.staging_path(session.id).unlink(missing_ok=True)
    session.delete()


def sweep_stale_uploads(now=None) -> int:
    """Called from the CPU worker's housekeeping: forget uploads nobody has
    touched for a week (their reserved quota goes with them)."""
    cutoff = (now or timezone.now()) - STALE_UPLOAD_AGE
    removed = 0
    for session in UploadSession.objects.filter(completed_at__isnull=True, updated_at__lt=cutoff):
        try:
            abort(session)
        except storage.StorageUnavailable:
            return removed
        removed += 1
    return removed


# --------------------------------------------------------------------------- #
# A folder's review data for an image that's already on the server
# --------------------------------------------------------------------------- #
def import_into_existing(image: Image, user, data: dict) -> dict:
    """Bring a folder's review data into an image that was uploaded without it
    (the TIFFs went up first, the folder's .cells.json files later).

    It only ever replaces the server's cells while nobody has edited
    the image in the web app; once someone has, the web version is kept and the
    caller is told so (409 `has_edits`). Re-importing data identical to what's
    already there is a quiet no-op, so adding the same folder twice is harmless.
    """
    sha256 = str(data.get("sha256", "")).lower().removeprefix("sha256:")
    if sha256 != image.sha256:
        raise ApiError(409, "content_mismatch",
                       f"The review data for {image.filename} is for a different version of the file.")
    imported = imported_cell_rows(data.get("imported_cells"))

    with transaction.atomic():
        image = Image.objects.select_for_update().get(pk=image.pk)
        if image.status in (ImageStatus.PREPARING, ImageStatus.SEGMENTING):
            raise ApiError(409, "busy", f"{image.filename} is still being processed on the server. Add the folder "
                           "again once it has finished to bring in its review work.")
        if not image.tiles_rev:
            raise ApiError(409, "not_ready",
                           f"{image.filename} couldn't be prepared on the server; add the file again.")

        def signature(rows):
            return {r["cell_id"]: (r["status"], r["edited"], r["source"], r["area"]) for r in rows}

        existing = signature({"cell_id": c.cell_id, "status": c.status, "edited": c.edited, "source": c.source,
                              "area": c.area} for c in image.cells.all())
        if existing == signature(imported.rows):
            return {"result": "unchanged"}
        if image.web_edited_at is not None:
            raise ApiError(409, "has_edits", f"{image.filename} has already been reviewed in the web app, so that "
                           "version was kept and the folder's review data wasn't imported.")

        queue.cancel_jobs(Job.objects.filter(image=image), reason="Replaced by imported review data")
        image.cells.all().delete()
        Cell.objects.bulk_create([Cell(image=image, **r) for r in imported.rows], batch_size=2000)
        image.cells_version += 1
        image.status = ImageStatus.DONE
        image.error = ""
        image.segmentation_params = imported.params or image.segmentation_params
        image.processed_at = timezone.now()
        image.save()

    from projects.api import image_json

    return {"result": "imported", **imported.summary(), "image": image_json(image)}

