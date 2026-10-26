"""
Projects: visibility and roles, sharing, deletion, the review-screen API (cell
ops, edit leases, UI state, retry), tile authorization, and coexpression against
reference results.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.urls import reverse

from core import storage
from processing.models import Job, JobKind
from projects.models import Cell, Image, ImageLock, ImageStatus, Membership, Project, Role, Sample

pytestmark = pytest.mark.django_db



def square(cx: float, cy: float, r: float) -> list[list[list[float]]]:
    return [[[cx - r, cy - r], [cx + r, cy - r], [cx + r, cy + r], [cx - r, cy + r]]]


def add_cell(image: Image, cell_id: int, cx: float, cy: float, r: float = 5, status: str = "kept") -> Cell:
    return Cell.objects.create(image=image, cell_id=cell_id, polygons=square(cx, cy, r), centroid_x=cx,
                               centroid_y=cy, area=int((2 * r) ** 2), status=status)


@pytest.fixture
def project(user) -> Project:
    return Project.objects.create(owner=user, name="Cohort A")


@pytest.fixture
def sample(project) -> Sample:
    return Sample.objects.create(project=project, prefix="A1")


def make_image(project, sample, channel="CCK", status=ImageStatus.DONE, **extra) -> Image:
    fields = {"sha256": "a" * 64, "size_bytes": 1000, "width": 200, "height": 100, **extra}
    return Image.objects.create(project=project, sample=sample, channel=channel,
                                filename=f"{sample.prefix}_{channel}.tif", status=status, **fields)


@pytest.fixture
def image(project, sample) -> Image:
    img = make_image(project, sample, cells_version=1)
    for i in range(1, 4):
        add_cell(img, i, 20 * i, 50)
    return img


def post_ops(client, image, ops, version=None):
    body = {"base_version": image.cells_version if version is None else version, "ops": ops}
    return client.post(reverse("projects:api_cell_ops", args=[image.id]), json.dumps(body),
                       content_type="application/json")


# --------------------------------------------------------------------------- #
# Pages, visibility, sharing
# --------------------------------------------------------------------------- #
def test_create_project_and_reject_duplicate_name(login, user):
    client = login(user)
    response = client.post(reverse("projects:list"), {"name": "Cohort B"})
    project = Project.objects.get(owner=user, name="Cohort B")
    assert response.url == reverse("projects:workspace", args=[project.id])
    response = client.post(reverse("projects:list"), {"name": "Cohort B"})
    assert response.status_code == 200
    assert Project.objects.filter(owner=user, name="Cohort B").count() == 1


def test_other_users_cannot_see_a_project(login, make_user, project, image):
    stranger = login(make_user("bob@example.com"))
    assert stranger.get(reverse("projects:workspace", args=[project.id])).status_code == 404
    assert stranger.get(reverse("projects:api_project", args=[project.id])).status_code == 404
    assert stranger.get(reverse("projects:api_image_cells", args=[image.id])).status_code == 404
    assert post_ops(stranger, image, []).status_code == 404


def test_share_and_roles(login, make_user, user, project, image):
    bob = make_user("bob@example.com")
    owner = login(user)
    owner.post(reverse("projects:share", args=[project.id]), {"user_id": bob.pk, "role": Role.VIEWER})
    assert Membership.objects.get(project=project, user=bob).role == Role.VIEWER

    bob_client = login(bob)
    detail = bob_client.get(reverse("projects:api_project", args=[project.id])).json()
    assert detail["role"] == "viewer" and detail["can_edit"] is False
    assert post_ops(bob_client, image, []).status_code == 403
    lock = bob_client.post(reverse("projects:api_image_lock", args=[image.id])).json()
    assert lock == {"held_by_me": False, "holder": None, "read_only": True}

    owner = login(user)
    owner.post(reverse("projects:share", args=[project.id]), {"user_id": bob.pk, "role": Role.EDITOR})
    bob_client = login(bob)
    response = post_ops(bob_client, image, [{"op": "update", "changes": [{"id": 1, "status": "filtered"}]}])
    assert response.status_code == 200
    cell = Cell.objects.get(image=image, cell_id=1)
    assert cell.status == "filtered" and cell.edited and cell.edited_by == bob
    # Collaborators can't manage sharing.
    bob_client.post(reverse("projects:share", args=[project.id]), {"user_id": bob.pk, "role": Role.EDITOR})
    assert bob_client.post(reverse("projects:rename", args=[project.id]), {"name": "x"}).status_code == 404


def test_member_can_leave_owner_can_remove(login, make_user, user, project):
    bob = make_user("bob@example.com")
    carol = make_user("carol@example.com")
    m_bob = Membership.objects.create(project=project, user=bob, role=Role.EDITOR)
    m_carol = Membership.objects.create(project=project, user=carol, role=Role.EDITOR)

    bob_client = login(bob)
    # Bob can't remove Carol…
    assert bob_client.post(reverse("projects:unshare", args=[project.id, m_carol.id])).status_code == 404
    # …but can leave.
    response = bob_client.post(reverse("projects:unshare", args=[project.id, m_bob.id]))
    assert response.url == reverse("projects:list")
    owner = login(user)
    owner.post(reverse("projects:unshare", args=[project.id, m_carol.id]))
    assert not Membership.objects.filter(project=project).exists()


def test_delete_requires_exact_name_and_queues_purge(login, user, project, image):
    client = login(user)
    client.post(reverse("projects:delete", args=[project.id]), {"confirm_name": "cohort a"})
    assert Project.objects.filter(pk=project.pk).exists()
    client.post(reverse("projects:delete", args=[project.id]), {"confirm_name": "Cohort A"})
    assert not Project.objects.filter(pk=project.pk).exists()
    purge = Job.objects.get(kind=JobKind.PURGE)
    assert purge.params == {"project_id": str(project.id), "image_ids": [str(image.id)]}


# --------------------------------------------------------------------------- #
# Bootstrap and status
# --------------------------------------------------------------------------- #
def test_bootstrap_and_status(login, user, project, sample, image):
    make_image(project, sample, "SNAP", status=ImageStatus.QUEUED)
    Sample.objects.create(project=project, prefix="A10")
    Sample.objects.create(project=project, prefix="A2")
    client = login(user)
    detail = client.get(reverse("projects:api_project", args=[project.id])).json()
    assert [s["prefix"] for s in detail["samples"]] == ["A1", "A2", "A10"]  # numeric, not string, order
    a1 = detail["samples"][0]["channels"]
    assert a1["CCK"]["status"] == "done" and a1["SNAP"]["status"] == "queued" and a1["CHR"] is None
    assert detail["role"] == "owner" and detail["upload_chunk_size"] == 32 * 1024 * 1024

    status = client.get(reverse("projects:api_project_status", args=[project.id])).json()
    assert {i["status"] for i in status["images"]} == {"done", "queued"}
    assert status["queue"] == [] and status["gpu_paused"] is False


def test_ui_state_keeps_only_valid_keys(login, user, project):
    client = login(user)
    url = reverse("projects:api_ui_state", args=[project.id])
    body = {"render_mode": "mask", "zoom_multiplier": 2.5, "channel_colors": {"CCK": "#12ab34"},
            "selection_style": "sideways", "evil": "<script>"}
    state = client.put(url, json.dumps(body), content_type="application/json").json()["ui_state"]
    assert state == {"render_mode": "mask", "zoom_multiplier": 2.5, "channel_colors": {"CCK": "#12ab34"}}
    state = client.put(url, json.dumps({"draw_style": "polygon"}), content_type="application/json").json()
    assert state["ui_state"]["render_mode"] == "mask" and state["ui_state"]["draw_style"] == "polygon"


# --------------------------------------------------------------------------- #
# Cell ops
# --------------------------------------------------------------------------- #
def test_ops_apply_in_order_and_bump_version(login, user, image):
    client = login(user)
    new_cell = {"id": 10, "polygons": square(150, 50, 4), "centroid": [150, 50], "area": 64,
                "status": "kept", "source": "human", "edited": True}
    response = post_ops(client, image, [
        {"op": "update", "changes": [{"id": 1, "status": "filtered", "edited": True}]},
        {"op": "add", "cells": [new_cell]},
        {"op": "remove", "ids": [2]},
    ])
    assert response.status_code == 200 and response.json() == {"version": 2, "touched": 3}
    payload = client.get(reverse("projects:api_image_cells", args=[image.id])).json()
    cells = {c["id"]: c for c in payload["cells"]}
    assert set(cells) == {1, 3, 10}
    assert cells[1]["status"] == "filtered" and cells[10]["source"] == "human"


def test_ops_are_all_or_nothing(login, user, image):
    client = login(user)
    response = post_ops(client, image, [
        {"op": "update", "changes": [{"id": 1, "status": "filtered"}]},
        {"op": "remove", "ids": [99]},
    ])
    assert response.status_code == 409 and response.json()["error"] == "unknown_cell"
    assert Cell.objects.get(image=image, cell_id=1).status == "kept"
    image.refresh_from_db()
    assert image.cells_version == 1


def test_stale_version_is_rejected(login, user, image):
    client = login(user)
    response = post_ops(client, image, [], version=0)
    assert response.status_code == 409 and response.json() == {
        "error": "version_conflict", "detail": "This image changed since you loaded it.", "version": 1}


@pytest.mark.parametrize("bad_cell, error", [
    ({"id": 3, "polygons": square(1, 1, 1), "centroid": [1, 1], "area": 4}, "duplicate_cell"),
    ({"id": 20, "polygons": [[[0, 0], [1, 1]]], "centroid": [1, 1], "area": 4}, "invalid_cell"),
    ({"id": 20, "polygons": square(1, 1, 1), "centroid": [1, 1], "area": 4, "source": "evil"},
     "invalid_cell"),
    ({"id": 20, "polygons": square(1, 1, 1), "centroid": "x", "area": 4}, "invalid_cell"),
    ({"id": True, "polygons": square(1, 1, 1), "centroid": [1, 1], "area": 4}, "invalid_cell"),
])
def test_added_cells_are_validated(login, user, image, bad_cell, error):
    client = login(user)
    response = post_ops(client, image, [{"op": "add", "cells": [bad_cell]}])
    assert response.json()["error"] == error
    assert Cell.objects.filter(image=image).count() == 3


def test_rescan_source_accepted(login, user, image):
    client = login(user)
    cell = {"id": 20, "polygons": square(1, 1, 1), "centroid": [1, 1], "area": 4,
            "source": "rescan:0f5a3c4e-1b2d-4e5f-8a9b-0c1d2e3f4a5b"}
    assert post_ops(client, image, [{"op": "add", "cells": [cell]}]).status_code == 200


def test_edit_lease_blocks_second_editor(login, make_user, user, project, image):
    bob = make_user("bob@example.com")
    Membership.objects.create(project=project, user=bob, role=Role.EDITOR)
    owner = login(user)
    assert owner.post(reverse("projects:api_image_lock", args=[image.id])).json()["held_by_me"] is True

    bob_client = login(bob)
    lock = bob_client.post(reverse("projects:api_image_lock", args=[image.id])).json()
    assert lock["held_by_me"] is False and lock["holder"] == user.label
    response = post_ops(bob_client, image, [{"op": "update", "changes": [{"id": 1, "status": "filtered"}]}])
    assert response.status_code == 423

    owner = login(user)
    assert owner.delete(reverse("projects:api_image_lock", args=[image.id])).status_code == 204
    bob_client = login(bob)
    assert bob_client.post(reverse("projects:api_image_lock", args=[image.id])).json()["held_by_me"] is True


def test_expired_lease_is_free(login, make_user, user, project, image):
    from datetime import timedelta

    from django.utils import timezone

    bob = make_user("bob@example.com")
    Membership.objects.create(project=project, user=bob, role=Role.EDITOR)
    ImageLock.objects.create(image=image, holder=user, expires_at=timezone.now() - timedelta(seconds=1))
    bob_client = login(bob)
    assert bob_client.post(reverse("projects:api_image_lock", args=[image.id])).json()["held_by_me"] is True


def test_cannot_edit_unfinished_image(login, user, project, sample):
    img = make_image(project, sample, "CHR", status=ImageStatus.SEGMENTING)
    client = login(user)
    assert post_ops(client, img, []).json()["error"] == "not_ready"


def test_retry_requeues_failed_images(login, user, project, sample):
    tiled = make_image(project, sample, "SNAP", status=ImageStatus.ERROR, error="boom",
                       tiles_rev="abc123abc123", tile_levels=2)
    untiled = make_image(project, sample, "CHR", status=ImageStatus.ERROR, error="boom", width=None)
    client = login(user)
    response = client.post(reverse("projects:api_retry_images"),
                           json.dumps({"image_ids": [str(tiled.id), str(untiled.id)]}),
                           content_type="application/json")
    assert set(response.json()["retried"]) == {str(tiled.id), str(untiled.id)}
    tiled.refresh_from_db()
    untiled.refresh_from_db()
    assert tiled.status == ImageStatus.QUEUED and untiled.status == ImageStatus.PREPARING
    assert Job.objects.get(image=tiled).kind == JobKind.SEGMENT
    assert Job.objects.get(image=untiled).kind == JobKind.PREPARE


# --------------------------------------------------------------------------- #
# Tiles
# --------------------------------------------------------------------------- #
def test_tiles_are_authorized(login, make_user, user, image):
    rev = "abcdef012345"
    tile = storage.tiles_dir(image.id, rev) / "0" / "0_0.png"
    tile.parent.mkdir(parents=True)
    tile.write_bytes(b"\x89PNG fake")
    url = f"/tiles/{image.id}/{rev}/0/0_0.png"

    response = login(user).get(url)
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"\x89PNG fake"
    assert "immutable" in response["Cache-Control"]

    assert login(make_user("bob@example.com")).get(url).status_code == 404
    assert login(user).get(f"/tiles/{image.id}/{rev}/0/1_0.png").status_code == 404
    assert login(user).get(f"/tiles/{image.id}/{rev}/0/..%2F0_0.png").status_code == 404


def test_tiles_use_x_accel_in_production(login, user, image, settings):
    settings.CELLCOUNTS_USE_X_ACCEL = True
    rev = "abcdef012345"
    tile = storage.tiles_dir(image.id, rev) / "1" / "2_3.png"
    tile.parent.mkdir(parents=True)
    tile.write_bytes(b"x")
    response = login(user).get(f"/tiles/{image.id}/{rev}/1/2_3.png")
    assert response["X-Accel-Redirect"] == f"/_protected/tiles/{image.id}/{rev}/1/2_3.png"
    assert response.content == b""


def test_storage_refuses_without_volume_marker(settings, tmp_path):
    settings.CELLCOUNTS_DATA_ROOT = tmp_path / "not-mounted"
    with pytest.raises(storage.StorageUnavailable):
        storage.staging_path("00000000-0000-0000-0000-000000000000")


# --------------------------------------------------------------------------- #
# Coexpression: the desktop version's results for this layout, kept in
# fixtures/coexpression.json (cck_id, chr_id, overlap fraction per pair).
# --------------------------------------------------------------------------- #
COEXPRESSION = json.loads((Path(__file__).parent / "fixtures" / "coexpression.json").read_text())


def test_composite_matches_reference(login, user, project, sample):
    import numpy as np

    from processing.coexpression import compute_coexpression

    rng = np.random.default_rng(3)
    snap, cck, chr_ = (make_image(project, sample, ch) for ch in ("SNAP", "CCK", "CHR"))
    cck_cells, chr_cells = [], []
    for i in range(1, 120):
        x, y, r = rng.uniform(10, 490), rng.uniform(10, 290), rng.uniform(3, 8)
        cck_cells.append(add_cell(cck, i, x, y, r))
        dx, dy = rng.normal(0, r * 0.8, 2)
        chr_cells.append(add_cell(chr_, i, x + dx, y + dy, rng.uniform(3, 8),
                                  status="filtered" if i % 9 == 0 else "kept"))
    for i in range(1, 50):
        add_cell(snap, i, i * 5, 20, status="filtered" if i % 7 == 0 else "kept")

    as_dicts = [[c.as_dict() for c in cells] for cells in (cck_cells, chr_cells)]
    ours = compute_coexpression(*as_dicts)
    assert [list(p) for p in ours.pairs] == COEXPRESSION["pairs"] and ours.pairs
    assert sorted(ours.cck_ids) == COEXPRESSION["cck_ids"] and sorted(ours.chr_ids) == COEXPRESSION["chr_ids"]

    payload = login(user).get(reverse("projects:api_composite", args=[sample.id])).json()
    assert payload["available"] is True
    assert payload["n_pairs"] == len(COEXPRESSION["pairs"])
    assert payload["snap_kept"] == 49 - 7
    assert payload["rate_pct"] == round(len(COEXPRESSION["pairs"]) / 42 * 100, 2)


def test_composite_unavailable_until_all_channels_done(login, user, project, sample):
    make_image(project, sample, "SNAP")
    make_image(project, sample, "CCK", status=ImageStatus.SEGMENTING)
    make_image(project, sample, "CHR")
    assert login(user).get(reverse("projects:api_composite", args=[sample.id])).json() == {"available": False}


def test_project_list_totals_are_not_multiplied_by_joins(login, user, project, sample):
    """Regression: annotating counts and a sum over the samples/images joins at
    once multiplied the size by the number of samples."""
    other = Sample.objects.create(project=project, prefix="A2")
    make_image(project, sample, "SNAP", size_bytes=100)
    make_image(project, sample, "CCK", size_bytes=200, status=ImageStatus.QUEUED)
    make_image(project, other, "SNAP", size_bytes=300)
    response = login(user).get(reverse("projects:list"))
    row = next(p for p in response.context["mine"] if p.pk == project.pk)
    assert (row.n_samples, row.n_images, row.n_done, row.size_bytes) == (2, 3, 2, 600)
