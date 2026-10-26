"""
api.py — JSON endpoints behind the review screen (DESIGN.md, "JSON API").

Cell edits arrive as small batches of operations (update / add / remove) rather
than whole documents. Each batch is applied atomically against the image's
current `cells_version`; the client sends the version it last saw, and a
mismatch is a 409 so it can reload instead of silently overwriting someone
else's change. Editing also requires the image's edit lease (ImageLock), which
the review screen takes when an editor opens an image and renews while it
stays open.
"""

from __future__ import annotations

import re
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

from core import storage
from core.api import ApiError, api_view, parse_json
from processing import queue
from processing.coexpression import compute_coexpression
from processing.models import JobKind, QueueState, UserQueueState
from projects.access import get_image_for, get_project_for, get_sample_for, require_role
from projects.models import (
    CHANNELS,
    Cell,
    CellStatus,
    Image,
    ImageLock,
    ImageStatus,
    ProjectUserState,
    Role,
)

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
SOURCE_RE = re.compile(r"^(human|model|rescan:[0-9a-f-]{32,36})$")
MAX_POINTS_PER_RING = 20_000
MAX_OPS_CELLS = 20_000


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def image_json(image: Image) -> dict:
    tiles = None
    if image.tiles_rev and image.tile_levels:
        tiles = {"rev": image.tiles_rev, "levels": image.tile_levels, "tile_size": 512}
    return {
        "id": str(image.id),
        "filename": image.filename,
        "channel": image.channel,
        "status": image.status,
        "error": image.error or None,
        "width": image.width,
        "height": image.height,
        "cells_version": image.cells_version,
        "tiles": tiles,
    }


def _samples_json(project) -> list[dict]:
    images = list(project.images.select_related("sample").order_by("filename"))
    samples: dict = {}
    for s in project.samples.all():
        samples[s.id] = {"id": str(s.id), "prefix": s.prefix, "channels": dict.fromkeys(CHANNELS)}
    for img in images:
        samples[img.sample_id]["channels"][img.channel] = image_json(img)
    return sorted(samples.values(), key=lambda s: _prefix_sort_key(s["prefix"]))


def _prefix_sort_key(prefix: str):
    """A1, A2, …, A10, B1 — letters then the number, not plain string order."""
    m = re.match(r"^([A-Za-z]+)(\d+)$", prefix)
    return (m.group(1).upper(), int(m.group(2))) if m else (prefix, 0)


# --------------------------------------------------------------------------- #
# Project bootstrap / polling / UI state
# --------------------------------------------------------------------------- #
@api_view(["GET"])
def project_detail(request, project_id):
    project, role = get_project_for(request.user, project_id)
    ui = ProjectUserState.objects.filter(project=project, user=request.user).first()
    return {
        "id": str(project.id),
        "name": project.name,
        "role": role,
        "can_edit": role in (Role.OWNER, Role.EDITOR),
        "owner": project.owner.label,
        "samples": _samples_json(project),
        "ui_state": ui.state if ui else {},
        "upload_chunk_size": settings.CELLCOUNTS_UPLOAD_CHUNK_SIZE,
        "me": {"id": request.user.id, "label": request.user.label, "is_staff": request.user.is_staff},
    }


@api_view(["GET"])
def project_status(request, project_id):
    """Polled every couple of seconds by the review screen: every image's
    status/version (so readiness dots and newly finished cells update) and this
    project's rows for the Queue panel."""
    project, _ = get_project_for(request.user, project_id)
    images = [
        {"id": str(i.id), "sample_id": str(i.sample_id), "status": i.status, "error": i.error or None,
         "cells_version": i.cells_version, "width": i.width, "height": i.height,
         "tiles": ({"rev": i.tiles_rev, "levels": i.tile_levels, "tile_size": 512}
                   if i.tiles_rev and i.tile_levels else None)}
        for i in project.images.all()
    ]
    state = UserQueueState.objects.filter(user=request.user).first()
    return {
        "images": images,
        "queue": queue.project_queue_rows(project, request.user),
        "segmenting_paused": bool(state and state.segmenting_paused),
        "gpu_paused": QueueState.get().gpu_paused,
    }


@api_view(["GET"])
def project_samples(request, project_id):
    """The full sample list again — the review screen refetches this when a
    status poll mentions an image it has never seen (a new upload)."""
    project, _ = get_project_for(request.user, project_id)
    return {"samples": _samples_json(project)}


UI_STATE_VALIDATORS = {
    "last_image_id": lambda v: v is None or isinstance(v, str),
    "render_mode": lambda v: v in ("outline", "mask"),
    "channel_colors": lambda v: isinstance(v, dict) and all(
        k in CHANNELS and isinstance(c, str) and HEX_COLOR.match(c) for k, c in v.items()),
    "zoom_multiplier": lambda v: isinstance(v, (int, float)) and 0.001 <= v <= 10_000,
    "selection_style": lambda v: v in ("lasso", "rectangle"),
    "draw_style": lambda v: v in ("freehand", "polygon"),
}


@api_view(["PUT"])
def ui_state(request, project_id):
    project, _ = get_project_for(request.user, project_id)
    data = parse_json(request)
    clean = {k: v for k, v in data.items() if k in UI_STATE_VALIDATORS and UI_STATE_VALIDATORS[k](v)}
    with transaction.atomic():
        obj, _ = ProjectUserState.objects.select_for_update().get_or_create(
            project=project, user=request.user)
        obj.state = {**obj.state, **clean}
        obj.save(update_fields=["state", "updated_at"])
    return {"ui_state": obj.state}


# --------------------------------------------------------------------------- #
# Cells
# --------------------------------------------------------------------------- #
@api_view(["GET"])
def image_cells(request, image_id):
    image, _ = get_image_for(request.user, image_id)
    cells = [c.as_dict() for c in image.cells.order_by("cell_id")]
    return {"image_id": str(image.id), "version": image.cells_version, "cells": cells}


def _is_point(p) -> bool:
    return (isinstance(p, list) and len(p) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p))


def validate_new_cell(c) -> dict:
    if not isinstance(c, dict):
        raise ApiError(400, "invalid_cell", "each added cell must be an object")
    cid, polygons, centroid = c.get("id"), c.get("polygons"), c.get("centroid")
    if not isinstance(cid, int) or isinstance(cid, bool) or cid < 1:
        raise ApiError(400, "invalid_cell", "cell id must be a positive integer")
    if (not isinstance(polygons, list) or not polygons
            or not all(isinstance(r, list) and 3 <= len(r) <= MAX_POINTS_PER_RING and all(map(_is_point, r))
                       for r in polygons)):
        raise ApiError(400, "invalid_cell", f"cell {cid}: polygons must be rings of 3+ [x, y] points")
    if not _is_point(centroid):
        raise ApiError(400, "invalid_cell", f"cell {cid}: centroid must be [x, y]")
    area = c.get("area", 0)
    if not isinstance(area, int) or area < 0:
        raise ApiError(400, "invalid_cell", f"cell {cid}: area must be a non-negative integer")
    status = c.get("status", CellStatus.KEPT)
    if status not in CellStatus.values:
        raise ApiError(400, "invalid_cell", f"cell {cid}: unknown status")
    source = c.get("source", "human")
    if not isinstance(source, str) or not SOURCE_RE.match(source):
        raise ApiError(400, "invalid_cell", f"cell {cid}: unknown source")
    color = c.get("color") or ""
    if color and not HEX_COLOR.match(color):
        raise ApiError(400, "invalid_cell", f"cell {cid}: color must be #rrggbb")
    return {"cell_id": cid, "polygons": polygons, "centroid_x": float(centroid[0]),
            "centroid_y": float(centroid[1]), "area": area, "status": status,
            "filter_reason": c.get("filter_reason") or "", "source": source,
            "edited": bool(c.get("edited", True)), "color": color}


def _check_lock(image: Image, user) -> None:
    lock = ImageLock.objects.select_for_update().filter(image=image).first()
    now = timezone.now()
    if lock and lock.is_live() and lock.holder_id != user.id:
        raise ApiError(423, "locked", f"{lock.holder.label} is editing this image.", holder=lock.holder.label)
    ImageLock.objects.update_or_create(
        image=image,
        defaults={"holder": user, "expires_at": now + timedelta(seconds=ImageLock.LEASE_SECONDS)})


@api_view(["POST"])
def cell_ops(request, image_id):
    image, _ = get_image_for(request.user, image_id, Role.EDITOR)
    data = parse_json(request)
    ops = data.get("ops")
    base_version = data.get("base_version")
    if not isinstance(ops, list) or not isinstance(base_version, int):
        raise ApiError(400, "invalid_ops", "expected {base_version: int, ops: [...]}")

    now = timezone.now()
    with transaction.atomic():
        image = Image.objects.select_for_update().get(pk=image.pk)
        if image.status != ImageStatus.DONE:
            raise ApiError(409, "not_ready", "This image hasn't finished processing.")
        if image.cells_version != base_version:
            raise ApiError(409, "version_conflict", "This image changed since you loaded it.",
                           version=image.cells_version)
        _check_lock(image, request.user)

        touched = 0
        for op in ops:
            kind = op.get("op") if isinstance(op, dict) else None
            if kind == "update":
                changes = op.get("changes")
                if not isinstance(changes, list) or len(changes) > MAX_OPS_CELLS:
                    raise ApiError(400, "invalid_ops", "update needs a list of changes")
                by_id = {}
                for ch in changes:
                    if (not isinstance(ch, dict) or not isinstance(ch.get("id"), int)
                            or ch.get("status") not in CellStatus.values):
                        raise ApiError(400, "invalid_ops", "each change needs an id and a status")
                    by_id[ch["id"]] = ch
                cells = list(image.cells.filter(cell_id__in=list(by_id)))
                if len(cells) != len(by_id):
                    raise ApiError(409, "unknown_cell", "Some of those cells no longer exist.")
                for cell in cells:
                    ch = by_id[cell.cell_id]
                    cell.status = ch["status"]
                    cell.edited = bool(ch.get("edited", True))
                    cell.edited_by = request.user
                    cell.edited_at = now
                Cell.objects.bulk_update(cells, ["status", "edited", "edited_by", "edited_at"])
                touched += len(cells)
            elif kind == "add":
                new = op.get("cells")
                if not isinstance(new, list) or len(new) > MAX_OPS_CELLS:
                    raise ApiError(400, "invalid_ops", "add needs a list of cells")
                rows = [validate_new_cell(c) for c in new]
                ids = [r["cell_id"] for r in rows]
                if len(set(ids)) != len(ids) or image.cells.filter(cell_id__in=ids).exists():
                    raise ApiError(409, "duplicate_cell", "A cell with that id already exists.")
                Cell.objects.bulk_create(
                    [Cell(image=image, edited_by=request.user, edited_at=now, **r) for r in rows])
                touched += len(rows)
            elif kind == "remove":
                ids = op.get("ids")
                if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
                    raise ApiError(400, "invalid_ops", "remove needs a list of ids")
                deleted, _ = image.cells.filter(cell_id__in=ids).delete()
                if deleted != len(set(ids)):
                    raise ApiError(409, "unknown_cell", "Some of those cells no longer exist.")
                touched += deleted
            else:
                raise ApiError(400, "invalid_ops", f"unknown op {kind!r}")

        image.cells_version += 1
        image.web_edited_at = now
        image.save(update_fields=["cells_version", "web_edited_at", "updated_at"])
    return {"version": image.cells_version, "touched": touched}


@api_view(["POST", "DELETE"])
def image_lock(request, image_id):
    image, role = get_image_for(request.user, image_id)
    if request.method == "DELETE":
        ImageLock.objects.filter(image=image, holder=request.user).delete()
        return HttpResponse(status=204)
    if role == Role.VIEWER:
        return {"held_by_me": False, "holder": None, "read_only": True}
    now = timezone.now()
    with transaction.atomic():
        lock = ImageLock.objects.select_for_update().filter(image=image).first()
        if lock and lock.is_live() and lock.holder_id != request.user.id:
            return {"held_by_me": False, "holder": lock.holder.label,
                    "expires_at": lock.expires_at.isoformat()}
        ImageLock.objects.update_or_create(
            image=image,
            defaults={"holder": request.user, "expires_at": now + timedelta(seconds=ImageLock.LEASE_SECONDS)})
    return {"held_by_me": True, "holder": request.user.label}


@api_view(["POST"])
def retry_images(request):
    data = parse_json(request)
    ids = data.get("image_ids")
    if not isinstance(ids, list):
        raise ApiError(400, "invalid_request", "expected {image_ids: [...]}")
    retried = []
    for image in Image.objects.filter(pk__in=ids[:1000], status=ImageStatus.ERROR).select_related("project"):
        require_role(image.project, request.user, Role.EDITOR)
        if image.tiles_rev and image.width:
            queue.enqueue_segmentation(image, request.user)
        else:
            image.status = ImageStatus.PREPARING
            image.error = ""
            image.save(update_fields=["status", "error", "updated_at"])
            queue.enqueue(JobKind.PREPARE, request.user, image=image)
        retried.append(str(image.id))
    return {"retried": retried}


# --------------------------------------------------------------------------- #
# Composite (coexpression)
# --------------------------------------------------------------------------- #
@api_view(["GET"])
def sample_composite(request, sample_id):
    sample, _ = get_sample_for(request.user, sample_id)
    images = {img.channel: img for img in sample.images.all()}
    if not all(ch in images and images[ch].status == ImageStatus.DONE for ch in CHANNELS):
        return {"available": False}

    key = "composite:{}:{}:{}:{}".format(
        sample.id, *(f"{images[ch].id}.{images[ch].cells_version}" for ch in CHANNELS))
    cached = cache.get(key)
    if cached is not None:
        return cached

    def kept(img):
        return [c.as_dict() for c in img.cells.filter(status=CellStatus.KEPT)]

    result = compute_coexpression(kept(images["CCK"]), kept(images["CHR"]))
    snap_kept = images["SNAP"].cells.filter(status=CellStatus.KEPT).count()
    n_pairs = len(result.pairs)
    payload = {
        "available": True,
        "pairs": [list(p) for p in result.pairs],
        "cck_ids": sorted(result.cck_ids),
        "chr_ids": sorted(result.chr_ids),
        "n_pairs": n_pairs,
        "snap_kept": snap_kept,
        # A rate against zero cells is undefined, not zero (same rule as export).
        "rate_pct": round(n_pairs / snap_kept * 100, 2) if snap_kept else None,
    }
    cache.set(key, payload, 60 * 60)
    return payload


# --------------------------------------------------------------------------- #
# Tiles. Every tile request is authorized here (with a short per-user cache so
# a screenful of tiles doesn't mean a screenful of permission queries); in
# production nginx then serves the file itself via X-Accel-Redirect.
# --------------------------------------------------------------------------- #
TILE_NAME = re.compile(r"^\d{1,5}_\d{1,5}\.png$")


@require_GET
def tile(request, image_id, rev, level, name):
    if not request.user.is_authenticated or not TILE_NAME.match(name):
        raise Http404
    cache_key = f"tile-access:{request.user.id}:{image_id}"
    if not cache.get(cache_key):
        get_image_for(request.user, image_id)  # 404s without access
        cache.set(cache_key, True, 300)
    try:
        path = storage.tiles_dir(image_id, rev) / str(level) / name
    except storage.StorageUnavailable as exc:
        raise Http404 from exc
    if not path.is_file():
        raise Http404
    headers = {"Cache-Control": "private, max-age=31536000, immutable"}
    if settings.CELLCOUNTS_USE_X_ACCEL:
        response = HttpResponse(content_type="image/png", headers=headers)
        response["X-Accel-Redirect"] = f"/_protected/tiles/{image_id}/{rev}/{level}/{name}"
        return response
    return FileResponse(path.open("rb"), content_type="image/png", headers=headers)
