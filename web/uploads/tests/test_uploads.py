"""The chunked upload protocol: planning, dedupe/replace, quota, resume, chunk
validation, completion and desktop-sidecar import."""

from __future__ import annotations

import pytest
from django.urls import reverse

from core import storage
from processing.models import Job, JobKind
from projects.models import Cell, Image, ImageStatus, Membership, Project, Role
from uploads.models import UploadSession
from uploads.tests.helpers import TEST_CHUNK, init, post_json, put_chunk, sha, tiff_bytes, upload

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def small_chunks(settings):
    settings.CELLCOUNTS_UPLOAD_CHUNK_SIZE = TEST_CHUNK


@pytest.fixture
def project(user):
    return Project.objects.create(owner=user, name="Uploads")


@pytest.fixture
def data():
    return tiff_bytes()


def test_upload_creates_image_and_prepare_job(login, user, project, data):
    client = login(user)
    response = upload(client, project, "a1_cck.tif", data)
    assert response.status_code == 200
    image = Image.objects.get(project=project)
    assert (image.sample.prefix, image.channel, image.status) == ("A1", "CCK", ImageStatus.PREPARING)
    assert image.sha256 == sha(data) and image.size_bytes == len(data)
    assert storage.original_path(project.id, image.id).read_bytes() == data
    assert not list((storage.data_root() / "staging").iterdir())
    job = Job.objects.get(image=image)
    assert job.kind == JobKind.PREPARE and job.params == {"imported": False}
    assert response.json()["image"]["id"] == str(image.id)


def test_plan_recognizes_names_and_unchanged_files(login, user, project, data):
    client = login(user)
    upload(client, project, "A1_SNAP.tif", data, last_modified=111)
    Image.objects.update(status=ImageStatus.DONE)
    files = [
        {"name": "A1_SNAP.tif", "size": len(data), "last_modified": 111},
        {"name": "A1_SNAP.tif", "size": len(data), "last_modified": 222},
        {"name": "B2_CHR.tif", "size": 10, "last_modified": 1},
        {"name": "A1_CCL.tif", "size": 10, "last_modified": 1},
        {"name": "notes.txt", "size": 10, "last_modified": 1},
    ]
    plan = post_json(client, reverse("uploads:plan", args=[project.id]), {"files": files}).json()
    assert [f["decision"] for f in plan["files"]] == ["unchanged", "needs_hash", "needs_hash"]
    assert plan["unrecognized"] == ["A1_CCL.tif", "notes.txt"]
    assert plan["quota"]["used"] == len(data)


def test_same_content_is_skipped(login, user, project, data):
    client = login(user)
    upload(client, project, "A1_CCK.tif", data, last_modified=1)
    response = init(client, project, "A1_CCK.tif", data, last_modified=2)
    assert response.json()["action"] == "skip"
    assert Image.objects.get().client_mtime == 2
    assert Job.objects.count() == 1


def test_changed_content_needs_confirmation_then_replaces(login, user, project, data):
    client = login(user)
    upload(client, project, "A1_CCK.tif", data)
    image = Image.objects.get()
    Cell.objects.create(image=image, cell_id=1, polygons=[[[0, 0], [1, 0], [1, 1]]], centroid_x=0, centroid_y=0,
                        area=1, status="kept")
    image.status, image.tiles_rev, image.tile_levels = ImageStatus.DONE, "aaaaaaaaaaaa", 1
    image.save()

    other = tiff_bytes(seed=2)
    response = init(client, project, "A1_CCK.tif", other)
    assert response.status_code == 409 and response.json()["error"] == "changed"

    response = upload(client, project, "A1_CCK.tif", other, replace=True)
    assert response.status_code == 200
    image.refresh_from_db()
    assert image.sha256 == sha(other) and image.status == ImageStatus.PREPARING
    assert not image.cells.exists() and image.tiles_rev == ""
    purge = Job.objects.get(kind=JobKind.PURGE)
    assert purge.params == {"tile_dirs": [[str(image.id), "aaaaaaaaaaaa"]]}


def test_quota_is_charged_to_the_owner(login, make_user, user, project, data):
    bob = make_user("bob@example.com")
    Membership.objects.create(project=project, user=bob, role=Role.EDITOR)
    user.storage_quota_bytes = len(data) + 10
    user.save()
    bob_client = login(bob)
    assert upload(bob_client, project, "A1_SNAP.tif", data).status_code == 200
    response = init(bob_client, project, "A1_CCK.tif", tiff_bytes(seed=3))
    assert response.status_code == 413
    body = response.json()
    assert body["error"] == "quota_exceeded"
    assert body["detail"] == "This would go over alice's storage quota."
    assert body["limit"] == len(data) + 10


def test_open_upload_reserves_quota(login, user, project, data):
    user.storage_quota_bytes = len(data) + 10
    user.save()
    client = login(user)
    assert init(client, project, "A1_SNAP.tif", data).status_code == 200  # started, not finished
    assert init(client, project, "A1_CCK.tif", tiff_bytes(seed=4)).status_code == 413


def test_resume_reports_received_chunks(login, user, project, data):
    client = login(user)
    first = init(client, project, "A1_CHR.tif", data).json()
    assert first["total_chunks"] > 2
    assert put_chunk(client, first["upload_id"], 1, data[TEST_CHUNK:2 * TEST_CHUNK]).status_code == 200
    again = init(client, project, "A1_CHR.tif", data).json()
    assert again["upload_id"] == first["upload_id"] and again["chunks_present"] == [1]

    response = client.post(reverse("uploads:complete", args=[first["upload_id"]]))
    assert response.status_code == 409 and response.json()["error"] == "missing_chunks"
    assert upload(client, project, "A1_CHR.tif", data).status_code == 200


def test_chunk_validation(login, make_user, user, project, data):
    client = login(user)
    body = init(client, project, "A1_SNAP.tif", data).json()
    upload_id = body["upload_id"]
    assert put_chunk(client, upload_id, 0, data[:100]).json()["error"] == "chunk_length"
    assert put_chunk(client, upload_id, body["total_chunks"], b"x").json()["error"] == "invalid_chunk"
    # Somebody else can't write into my upload, even with access to the project.
    bob = make_user("bob@example.com")
    Membership.objects.create(project=project, user=bob, role=Role.EDITOR)
    assert put_chunk(login(bob), upload_id, 0, data[:TEST_CHUNK]).status_code == 404
    assert UploadSession.objects.get().received == []


def test_viewers_and_strangers_cannot_upload(login, make_user, project, data):
    viewer = make_user("viewer@example.com")
    Membership.objects.create(project=project, user=viewer, role=Role.VIEWER)
    assert init(login(viewer), project, "A1_SNAP.tif", data).status_code == 403
    assert init(login(make_user("stranger@example.com")), project, "A1_SNAP.tif", data).status_code == 404


def test_rejects_bad_names_and_hashes(login, user, project, data):
    client = login(user)
    assert init(client, project, "notes.tif", data).json()["error"] == "invalid_filename"
    response = post_json(client, reverse("uploads:init", args=[project.id]),
                         {"filename": "A1_SNAP.tif", "size": len(data), "sha256": "abc"})
    assert response.json()["error"] == "invalid_hash"


def test_desktop_sidecar_import(login, user, project, data):
    client = login(user)
    cells = [
        {"id": 1, "polygons": [[[1, 1], [9, 1], [9, 9], [1, 9]]], "centroid": [5, 5], "area": 64,
         "status": "kept", "filter_reason": None, "color": None, "source": "model", "edited": False},
        {"id": 2, "polygons": [[[20, 20], [29, 20], [29, 29]]], "centroid": [26, 23], "area": 40,
         "status": "filtered", "filter_reason": "dim", "color": None, "source": "model", "edited": True},
    ]
    response = upload(client, project, "A1_SNAP.tif", data, imported={"cells": cells, "params": {"diameter": 0}})
    assert response.status_code == 200
    image = Image.objects.get()
    stored = {c.cell_id: c for c in image.cells.all()}
    assert stored[1].edited is False and stored[2].filter_reason == "dim" and stored[2].edited
    assert image.segmentation_params == {"diameter": 0}
    assert Job.objects.get(kind=JobKind.PREPARE).params == {"imported": True}


def test_abort_discards_staging(login, user, project, data):
    client = login(user)
    body = init(client, project, "A1_SNAP.tif", data).json()
    put_chunk(client, body["upload_id"], 0, data[:TEST_CHUNK])
    assert client.delete(reverse("uploads:abort", args=[body["upload_id"]])).status_code == 204
    assert not UploadSession.objects.exists()
    assert not list((storage.data_root() / "staging").iterdir())
