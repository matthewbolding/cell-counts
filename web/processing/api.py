"""
api.py — queue, rescan and export endpoints (DESIGN.md, "Queue, rescans, exports").

Rescans and exports are asynchronous: the POST creates the work and returns at
once, and the browser polls. Neither could be a single request anyway — a
rescan sweep queues behind whatever the GPU is doing, and Cloudflare cuts any
request off at ~100 s.
"""

from __future__ import annotations

import math

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse
from django.urls import reverse
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_GET

from core import storage
from core.api import ApiError, api_view, parse_json
from processing import queue
from processing.export import download_filename
from processing.models import ACTIVE_STATUSES, Export, Job, JobKind, JobStatus, RescanSweep
from projects.access import get_image_for, get_project_for
from projects.models import Image, ImageStatus, Role

MAX_RESCAN_COMBOS = 25
MIN_RESCAN_SIZE = 10           # image px per side
MAX_RESCAN_PIXELS = 4096 * 4096
MAX_EXPORT_PREFIXES = 10_000


# --------------------------------------------------------------------------- #
# Queue
# --------------------------------------------------------------------------- #
def _job_ids(data: dict) -> list[str]:
    ids = data.get("job_ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids) or len(ids) > 10_000:
        raise ApiError(400, "invalid_request", "expected {job_ids: [...]}")
    return ids


def _paused(data: dict) -> bool:
    paused = data.get("paused")
    if not isinstance(paused, bool):
        raise ApiError(400, "invalid_request", "expected {paused: true|false}")
    return paused


@api_view(["GET"])
def queue_overview(request):
    return queue.overview(request.user)


@api_view(["POST"])
def queue_reorder(request):
    return {"moved": queue.reorder(request.user, _job_ids(parse_json(request)))}


@api_view(["POST"])
def queue_pause(request):
    paused = _paused(parse_json(request))
    queue.set_user_paused(request.user, paused)
    return {"segmenting_paused": paused}


@api_view(["POST"])
def queue_cancel(request):
    """Cancel queued jobs: your own, or anyone's for an admin. A running job
    always finishes. A cancelled image becomes an error ("Cancelled") so it
    shows up for Retry rather than looking stuck."""
    ids = _job_ids(parse_json(request))
    jobs = Job.objects.filter(pk__in=ids, status=JobStatus.QUEUED)
    if not request.user.is_staff:
        jobs = jobs.filter(user=request.user)
    image_ids = list(jobs.filter(kind__in=(JobKind.PREPARE, JobKind.SEGMENT)).values_list("image_id", flat=True))
    with transaction.atomic():
        cancelled = queue.cancel_jobs(jobs)
        Image.objects.filter(pk__in=image_ids, status__in=(ImageStatus.QUEUED, ImageStatus.PREPARING)).update(
            status=ImageStatus.ERROR, error="Cancelled")
    return {"cancelled": cancelled}


@api_view(["POST"])
def admin_queue_pause(request):
    if not request.user.is_staff:
        raise ApiError(403, "forbidden", "Only administrators can pause the GPU.")
    paused = _paused(parse_json(request))
    queue.set_gpu_paused(paused, by=request.user)
    return {"gpu_paused": paused}


# --------------------------------------------------------------------------- #
# Rescans
# --------------------------------------------------------------------------- #
def _numbers(value, name: str, low: float, high: float) -> list[float]:
    if not isinstance(value, list) or not value:
        raise ApiError(400, "invalid_request", f"{name} needs at least one number")
    out = []
    for v in value:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not low <= v <= high:
            raise ApiError(400, "invalid_request", f"{name} values must be numbers between {low:g} and {high:g}")
        out.append(float(v))
    return list(dict.fromkeys(out))


@api_view(["POST"])
def create_rescan(request, image_id):
    image, _ = get_image_for(request.user, image_id, Role.EDITOR)
    if image.status != ImageStatus.DONE or not image.width or not image.height:
        raise ApiError(409, "not_ready", "Rescan needs an image that has finished processing.")
    data = parse_json(request)
    diameters = _numbers(data.get("diameters"), "Diameter", 0, 1000)
    cellprobs = _numbers(data.get("cellprobs"), "Cell probability threshold", -10, 10)
    if len(diameters) * len(cellprobs) > MAX_RESCAN_COMBOS:
        raise ApiError(400, "too_many_combinations",
                       f"That's {len(diameters) * len(cellprobs)} combinations; the limit is {MAX_RESCAN_COMBOS}.")

    rect = data.get("rect")
    if not isinstance(rect, list) or len(rect) != 4 or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in rect):
        raise ApiError(400, "invalid_request", "rect must be [x0, y0, x1, y1]")
    x0, x1 = sorted((rect[0], rect[2]))
    y0, y1 = sorted((rect[1], rect[3]))
    x0, x1 = max(0.0, x0), min(float(image.width), x1)
    y0, y1 = max(0.0, y0), min(float(image.height), y1)
    if x1 - x0 < MIN_RESCAN_SIZE or y1 - y0 < MIN_RESCAN_SIZE:
        raise ApiError(400, "region_too_small", "Drag a larger region to rescan.")
    if (x1 - x0) * (y1 - y0) > MAX_RESCAN_PIXELS:
        raise ApiError(400, "region_too_large", "That region is too large to rescan; drag a smaller one.")

    megapixels = (x1 - x0) * (y1 - y0) / 1e6
    with transaction.atomic():
        sweep = RescanSweep.objects.create(image=image, user=request.user, rect=[x0, y0, x1, y1])
        combos = []
        for index, (diameter, cellprob) in enumerate((d, c) for d in diameters for c in cellprobs):
            job = queue.enqueue(JobKind.RESCAN, request.user, image=image, sweep=sweep, params={
                "diameter": diameter, "cellprob_threshold": cellprob, "combo_index": index,
                "megapixels": megapixels})
            combos.append({"job_id": str(job.id), "diameter": diameter, "cellprob_threshold": cellprob})
    return {"sweep_id": str(sweep.id), "rect": sweep.rect, "combos": combos}


def _own_sweep(request, sweep_id) -> RescanSweep:
    sweep = RescanSweep.objects.filter(pk=sweep_id, user=request.user).select_related("image").first()
    if sweep is None:
        raise Http404
    return sweep


def _combo_error(job: Job) -> str | None:
    if job.status == JobStatus.ERROR:
        return "This combination failed to run."
    if job.status == JobStatus.CANCELLED:
        return "Cancelled."
    return None


@api_view(["GET", "DELETE"])
def rescan_detail(request, sweep_id):
    sweep = _own_sweep(request, sweep_id)
    jobs = sorted(sweep.jobs.all(), key=lambda j: j.params.get("combo_index", 0))
    if request.method == "DELETE":
        queue.cancel_jobs(Job.objects.filter(sweep=sweep))
        return HttpResponse(status=204)
    active = any(j.status in ACTIVE_STATUSES for j in jobs)
    return {
        "sweep_id": str(sweep.id),
        "image_id": str(sweep.image_id),
        "rect": sweep.rect,
        "status": "running" if active else "done",
        "combos": [{
            "job_id": str(j.id),
            "diameter": j.params.get("diameter"),
            "cellprob_threshold": j.params.get("cellprob_threshold"),
            "status": j.status,
            "cells": (j.result or {}).get("cells") if j.status == JobStatus.DONE else None,
            "error": _combo_error(j),
        } for j in jobs],
    }


# --------------------------------------------------------------------------- #
# Exports
# --------------------------------------------------------------------------- #
@api_view(["POST"])
def create_export(request, project_id):
    project, _ = get_project_for(request.user, project_id)
    data = parse_json(request)
    prefixes = data.get("prefixes")
    if (not isinstance(prefixes, list) or not prefixes or len(prefixes) > MAX_EXPORT_PREFIXES
            or not all(isinstance(p, str) for p in prefixes)):
        raise ApiError(400, "invalid_request", "Select at least one sample to export.")
    include_summary, include_cells = bool(data.get("summary")), bool(data.get("cells"))
    if not include_summary and not include_cells:
        raise ApiError(400, "invalid_request", "Select at least Summary or Cells to include.")
    known = set(project.samples.filter(prefix__in=prefixes).values_list("prefix", flat=True))
    unknown = [p for p in prefixes if p not in known]
    if unknown:
        raise ApiError(400, "unknown_sample", f"Not in this project: {', '.join(unknown[:5])}")

    with transaction.atomic():
        export = Export.objects.create(
            project=project, user=request.user, prefixes=list(dict.fromkeys(prefixes)),
            include_summary=include_summary, include_cells=include_cells,
            filename=download_filename(project.name))
        queue.enqueue(JobKind.EXPORT, request.user, project=project, export=export)
    return {"export_id": str(export.id)}


def _own_export(request, export_id) -> Export:
    export = Export.objects.filter(pk=export_id, user=request.user).first()
    if export is None:
        raise Http404
    return export


@api_view(["GET"])
def export_detail(request, export_id):
    export = _own_export(request, export_id)
    job = export.jobs.order_by("-created_at").first()
    status = job.status if job else JobStatus.ERROR
    ready = status == JobStatus.DONE and storage.export_path(export.id).is_file()
    return {
        "export_id": str(export.id),
        "status": status,
        "filename": export.filename,
        "download_url": reverse("processing:export_download", args=[export.id]) if ready else None,
        "error": "The export couldn't be created." if status in (JobStatus.ERROR, JobStatus.CANCELLED) else None,
    }


@require_GET
@login_required
def export_download(request, export_id):
    export = _own_export(request, export_id)
    try:
        path = storage.export_path(export.id)
    except storage.StorageUnavailable as exc:
        raise Http404 from exc
    if not path.is_file():
        raise Http404
    content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if settings.CELLCOUNTS_USE_X_ACCEL:
        response = HttpResponse(content_type=content_type)
        response["X-Accel-Redirect"] = f"/_protected/exports/{export.id}.xlsx"
        response["Content-Disposition"] = content_disposition_header(as_attachment=True, filename=export.filename)
        return response
    return FileResponse(path.open("rb"), as_attachment=True, filename=export.filename, content_type=content_type)
