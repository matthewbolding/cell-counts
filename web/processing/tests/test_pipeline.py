"""
The pipeline end to end, driving the real worker loop (FakeModel on the GPU
side): upload -> prepare (verify + tiles) -> segment -> cells; failure and
replacement paths; rescans; exports; purge.
"""

from __future__ import annotations

import io
import json

import openpyxl
import pytest
from django.urls import reverse

from core import storage
from processing.models import Export, Job, JobKind, JobStatus
from processing.worker import Worker
from projects.models import Cell, Image, ImageStatus, Project
from uploads.tests.helpers import TEST_CHUNK, post_json, tiff_bytes, upload

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def small_chunks(settings):
    settings.CELLCOUNTS_UPLOAD_CHUNK_SIZE = TEST_CHUNK


@pytest.fixture
def project(user):
    return Project.objects.create(owner=user, name="Pipeline")


def run(resource: str) -> int:
    return Worker(resource, poll_seconds=0).run(once=True)


def process_all() -> None:
    run("cpu")
    run("gpu")


def test_upload_to_cells(login, user, project):
    client = login(user)
    upload(client, project, "A1_SNAP.tif", tiff_bytes(shape=(600, 900), n_cells=80))
    image = Image.objects.get()

    assert run("cpu") == 1
    image.refresh_from_db()
    assert (image.width, image.height, image.status) == (900, 600, ImageStatus.QUEUED)
    assert image.tile_levels == 2 and image.tiles_rev == image.sha256[:12]
    tiles_dir = storage.tiles_dir(image.id, image.tiles_rev)
    assert sorted(p.name for p in (tiles_dir / "0").iterdir()) == ["0_0.png", "0_1.png", "1_0.png", "1_1.png"]
    assert [p.name for p in (tiles_dir / "1").iterdir()] == ["0_0.png"]

    assert run("gpu") == 1
    image.refresh_from_db()
    assert image.status == ImageStatus.DONE and image.cells_version == 1
    kept = image.cells.filter(status="kept").count()
    assert kept > 40
    assert Job.objects.get(kind=JobKind.SEGMENT).result == {"n_cells": image.cells.count(), "n_kept": kept}

    # The review screen sees it.
    status = client.get(reverse("projects:api_project_status", args=[project.id])).json()
    assert status["images"][0]["status"] == "done" and status["images"][0]["tiles"]["levels"] == 2


def test_corrupted_upload_is_reported(login, user, project):
    upload(login(user), project, "A1_SNAP.tif", tiff_bytes())
    image = Image.objects.get()
    path = storage.original_path(project.id, image.id)
    path.write_bytes(path.read_bytes()[:-10] + b"0123456789")
    run("cpu")
    image.refresh_from_db()
    assert image.status == ImageStatus.ERROR
    assert image.error == "The upload was corrupted in transit — add the file again."
    assert not path.exists()
    assert Job.objects.get().status == JobStatus.ERROR


def test_unreadable_tiff_is_reported(login, user, project):
    upload(login(user), project, "A1_SNAP.tif", b"definitely not a tiff" * 100)
    run("cpu")
    image = Image.objects.get()
    assert image.status == ImageStatus.ERROR and image.error == "This file couldn't be read as a TIFF image."


def test_replacement_during_segmentation_drops_stale_results(login, user, project, monkeypatch):
    client = login(user)
    upload(client, project, "A1_SNAP.tif", tiff_bytes(seed=1))
    run("cpu")

    from processing import segmentation

    real_run = segmentation.run

    def run_then_replace(model, gray, params=None):
        result = real_run(model, gray, params)
        # A new upload of different content lands while the GPU is busy.
        upload(client, project, "A1_SNAP.tif", tiff_bytes(seed=9), replace=True)
        return result

    monkeypatch.setattr(segmentation, "run", run_then_replace)
    run("gpu")
    image = Image.objects.get()
    assert image.status == ImageStatus.PREPARING and not image.cells.exists()
    assert Job.objects.filter(kind=JobKind.SEGMENT).get().status == JobStatus.CANCELLED


def test_imported_cells_skip_segmentation(login, user, project):
    cells = [{"id": 1, "polygons": [[[1, 1], [9, 1], [9, 9]]], "centroid": [6, 4], "area": 30,
              "status": "kept", "source": "model", "edited": False}]
    upload(login(user), project, "A1_SNAP.tif", tiff_bytes(), imported={"cells": cells})
    run("cpu")
    image = Image.objects.get()
    assert image.status == ImageStatus.DONE and image.cells.count() == 1
    assert not Job.objects.filter(kind=JobKind.SEGMENT).exists()


# --------------------------------------------------------------------------- #
# Rescan
# --------------------------------------------------------------------------- #
def test_rescan_sweep(login, user, project):
    client = login(user)
    upload(client, project, "A1_SNAP.tif", tiff_bytes(shape=(400, 500), n_cells=60))
    process_all()
    image = Image.objects.get()

    url = reverse("processing:api_create_rescan", args=[image.id])
    response = post_json(client, url, {"rect": [300, 250, 100, 50], "diameters": [0, 15], "cellprobs": [0]})
    assert response.status_code == 200
    body = response.json()
    assert body["rect"] == [100, 50, 300, 250] and len(body["combos"]) == 2

    detail_url = reverse("processing:api_rescan", args=[body["sweep_id"]])
    assert client.get(detail_url).json()["status"] == "running"
    assert run("gpu") == 2
    detail = client.get(detail_url).json()
    assert detail["status"] == "done"
    assert [c["diameter"] for c in detail["combos"]] == [0, 15]
    cells = detail["combos"][0]["cells"]
    assert cells, "the fake model should find blobs in the crop"
    for cell in cells:  # translated back into full-image coordinates
        x, y = cell["centroid"]
        assert 100 <= x <= 300 and 50 <= y <= 250


def test_rescans_run_before_bulk_work(login, user, project):
    client = login(user)
    upload(client, project, "A1_SNAP.tif", tiff_bytes(seed=1))
    upload(client, project, "A1_CCK.tif", tiff_bytes(seed=2))
    run("cpu")
    snap = Image.objects.get(channel="SNAP")
    Image.objects.filter(pk=snap.pk).update(status=ImageStatus.DONE)  # pretend SNAP finished earlier
    post_json(client, reverse("processing:api_create_rescan", args=[snap.id]),
              {"rect": [0, 0, 50, 50], "diameters": [0], "cellprobs": [0]})
    worker = Worker("gpu", poll_seconds=0)
    worker.start()
    from processing import queue

    first = queue.claim_next("gpu")
    assert first.kind == JobKind.RESCAN


@pytest.mark.parametrize("body, error", [
    ({"rect": [0, 0, 5, 5], "diameters": [0], "cellprobs": [0]}, "region_too_small"),
    ({"rect": [0, 0, 50, 50], "diameters": list(range(6)), "cellprobs": list(range(5))}, "too_many_combinations"),
    ({"rect": [0, 0, 50, 50], "diameters": [], "cellprobs": [0]}, "invalid_request"),
    ({"rect": [0, 0, 50], "diameters": [0], "cellprobs": [0]}, "invalid_request"),
])
def test_rescan_validation(login, user, project, body, error):
    image = Image.objects.create(project=project, sample=project.samples.create(prefix="A1"), channel="SNAP",
                                 filename="A1_SNAP.tif", sha256="a" * 64, size_bytes=1, width=500, height=400,
                                 status=ImageStatus.DONE)
    response = post_json(login(user), reverse("processing:api_create_rescan", args=[image.id]), body)
    assert response.json()["error"] == error


def test_rescan_results_are_private(login, make_user, user, project):
    client = login(user)
    upload(client, project, "A1_SNAP.tif", tiff_bytes())
    process_all()
    image = Image.objects.get()
    sweep_id = post_json(client, reverse("processing:api_create_rescan", args=[image.id]),
                         {"rect": [0, 0, 50, 50], "diameters": [0], "cellprobs": [0]}).json()["sweep_id"]
    other = login(make_user("bob@example.com"))
    assert other.get(reverse("processing:api_rescan", args=[sweep_id])).status_code == 404


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def _sheet(workbook, name):
    rows = list(workbook[name].iter_rows(values_only=True))
    return [dict(zip(rows[0], r, strict=True)) for r in rows[1:]]


def test_export_workbook(login, user, project):
    client = login(user)
    for ch, seed in (("SNAP", 1), ("CCK", 2), ("CHR", 3)):
        upload(client, project, f"A1_{ch}.tif", tiff_bytes(seed=seed))
    upload(client, project, "A2_SNAP.tif", tiff_bytes(seed=4))
    process_all()
    # A2's SNAP is processed but has no kept cells: a real 0, not ###.
    a2 = Image.objects.get(filename="A2_SNAP.tif")
    a2.cells.update(status="filtered")

    response = post_json(client, reverse("processing:api_create_export", args=[project.id]),
                         {"prefixes": ["A1", "A2"], "summary": True, "cells": True})
    export_id = response.json()["export_id"]
    detail_url = reverse("processing:api_export", args=[export_id])
    assert client.get(detail_url).json()["download_url"] is None
    run("cpu")
    detail = client.get(detail_url).json()
    assert detail["status"] == "done" and detail["filename"] == "Pipeline_export.xlsx"

    download = client.get(detail["download_url"])
    assert download.status_code == 200
    assert 'filename="Pipeline_export.xlsx"' in download["Content-Disposition"]
    workbook = openpyxl.load_workbook(io.BytesIO(b"".join(download.streaming_content)))
    summary = {row["prefix"]: row for row in _sheet(workbook, "Summary")}
    assert summary["A2"]["snap_kept"] == 0
    assert summary["A2"]["cck_kept"] == "###" and summary["A2"]["coexpression_rate_pct"] == "###"
    assert isinstance(summary["A1"]["coexpressing_pairs"], int)
    cells = _sheet(workbook, "Cells")
    assert {r["channel"] for r in cells} == {"SNAP", "CCK", "CHR"}
    assert all(r["coexpressing"] in (None, "") for r in cells if r["channel"] == "SNAP")
    assert Cell.objects.filter(image__project=project, status="kept").count() == len(cells)


def test_export_validation_and_privacy(login, make_user, user, project):
    client = login(user)
    url = reverse("processing:api_create_export", args=[project.id])
    project.samples.create(prefix="A1")
    assert post_json(client, url, {"prefixes": ["A1"], "summary": False, "cells": False}).status_code == 400
    assert post_json(client, url, {"prefixes": ["Z9"], "summary": True}).json()["error"] == "unknown_sample"
    export_id = post_json(client, url, {"prefixes": ["A1"], "summary": True}).json()["export_id"]
    other = login(make_user("bob@example.com"))
    assert other.get(reverse("processing:api_export", args=[export_id])).status_code == 404
    assert other.get(reverse("processing:export_download", args=[export_id])).status_code == 404


def test_exports_expire(user, project):
    from datetime import timedelta

    from django.utils import timezone

    from processing.housekeeping import expire_exports

    old = Export.objects.create(project=project, user=user, prefixes=["A1"], filename="x.xlsx")
    Export.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(hours=25))
    storage.export_path(old.id).write_bytes(b"x")
    assert expire_exports() == 1
    assert not storage.export_path(old.id).exists()


# --------------------------------------------------------------------------- #
# Purge
# --------------------------------------------------------------------------- #
def test_deleting_a_project_purges_its_files(login, user, project):
    client = login(user)
    upload(client, project, "A1_SNAP.tif", tiff_bytes())
    run("cpu")
    image = Image.objects.get()
    originals = storage.data_root() / "originals" / str(project.id)
    tiles = storage.tiles_root(image.id)
    assert originals.is_dir() and tiles.is_dir()

    client.post(reverse("projects:delete", args=[project.id]), {"confirm_name": "Pipeline"})
    run("cpu")
    assert not originals.exists() and not tiles.exists()
    assert json.loads(json.dumps(Job.objects.get(kind=JobKind.PURGE).result)) == {"removed": 2}
