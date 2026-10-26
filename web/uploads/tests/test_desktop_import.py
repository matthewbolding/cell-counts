"""
Bringing review work from the desktop version of Cell Counts into the web app.

A reviewed folder holds `cellcounts.json` (one entry per image: sha256,
status, segmentation params) and a `<file>.cells.json` per image. The folder
written here is in exactly that format. Its cells, and that folder's export
numbers, are in fixtures/desktop_review.json, produced by the desktop code
itself, with edits of every kind a reviewer makes: toggled detections, deleted
cells, a hand-drawn cell and a merged rescan cell. The folder is uploaded the
way the browser does it (uploads/uploader.ts): a file's review data is sent
only when cellcounts.json says it finished processing and its recorded sha256
matches the file. Afterwards every cell must match the sidecar exactly, and the
export must give the same numbers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.urls import reverse

from processing.export import build_export_rows
from processing.models import Job, JobKind
from processing.worker import Worker
from projects.models import Image, ImageStatus, Project
from uploads.tests.helpers import TEST_CHUNK, post_json, sha, tiff_bytes, upload

pytestmark = pytest.mark.django_db

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "desktop_review.json").read_text())
TIFF_SEEDS = {"SNAP": 11, "CCK": 12, "CHR": 13}  # the images FIXTURE's cells were found in


@pytest.fixture(autouse=True)
def small_chunks(settings):
    settings.CELLCOUNTS_UPLOAD_CHUNK_SIZE = TEST_CHUNK


@pytest.fixture
def project(user):
    return Project.objects.create(owner=user, name="From desktop")


def write_desktop_folder(folder: Path) -> None:
    """A reviewed folder as the desktop version left it: the TIFFs, a
    `.cells.json` per image, and `cellcounts.json` (schema 2) marking each one
    done with its sha256."""
    images = {}
    for name, entry in FIXTURE["images"].items():
        path = folder / name
        path.write_bytes(tiff_bytes(seed=TIFF_SEEDS[entry["channel"]], shape=(400, 520), n_cells=90))
        (folder / f"{name}.cells.json").write_text(json.dumps({"cells": entry["cells"]}))
        st = path.stat()
        images[name] = {
            "prefix": entry["prefix"], "channel": entry["channel"], "hash": f"sha256:{sha(path.read_bytes())}",
            "mtime": st.st_mtime, "size": st.st_size, "width": entry["width"], "height": entry["height"],
            "status": "done", "processed_at": "2026-10-26T00:00:00+00:00", "params": entry["params"], "error": None,
        }
    (folder / "cellcounts.json").write_text(json.dumps({"schema_version": 2, "images": images}))


def desktop_review_data(folder: Path, name: str, data: bytes) -> dict | None:
    """uploader.ts desktopCells(): only for finished files whose content matches."""
    entry = json.loads((folder / "cellcounts.json").read_text())["images"].get(name)
    if not entry or entry.get("status") != "done" or entry.get("hash") != f"sha256:{sha(data)}":
        return None
    cells = json.loads((folder / f"{name}.cells.json").read_text())["cells"]
    return {"cells": cells, "params": entry.get("params")}


def normalize(cells: list[dict]) -> list[dict]:
    keys = ("id", "polygons", "centroid", "area", "status", "filter_reason", "color", "source", "edited")
    return sorted(({k: c.get(k) for k in keys} for c in cells), key=lambda c: c["id"])


def web_cells(client, image: Image) -> list[dict]:
    return client.get(reverse("projects:api_image_cells", args=[image.id])).json()["cells"]


def test_desktop_folder_arrives_intact(tmp_path, login, user, project):
    folder = tmp_path / "A1-folder"
    folder.mkdir()
    write_desktop_folder(folder)

    client = login(user)
    for name in ("A1_SNAP.tif", "A1_CCK.tif", "A1_CHR.tif"):
        data = (folder / name).read_bytes()
        review = desktop_review_data(folder, name, data)
        response = upload(client, project, name, data, last_modified=1, imported=review)
        assert response.status_code == 200
        assert response.json()["imported"]["skipped"] == 0
    Worker("cpu", poll_seconds=0).run(once=True)

    assert not Job.objects.filter(kind=JobKind.SEGMENT).exists(), "reviewed images must not be re-segmented"
    for image in Image.objects.filter(project=project):
        assert image.status == ImageStatus.DONE
        sidecar = json.loads((folder / f"{image.filename}.cells.json").read_text())["cells"]
        assert normalize(web_cells(client, image)) == normalize(sidecar), image.filename

    # Same numbers as the desktop version's own export of that folder.
    web_summary, web_cell_rows = build_export_rows(project, ["A1"])
    assert web_summary == FIXTURE["export"]["summary"]
    assert sorted(web_cell_rows, key=lambda row: (row["channel"], row["cell_id"])) == FIXTURE["export"]["cells"]
    assert web_summary[0]["coexpressing_pairs"] not in (0, "###")


def test_review_data_added_after_the_tiffs(tmp_path, login, user, project):
    """TIFFs uploaded on their own first (so the server segmented them), the
    .cells.json files added later: the desktop review replaces the server's
    unreviewed segmentation."""
    folder = tmp_path / "A1-folder"
    folder.mkdir()
    write_desktop_folder(folder)
    client = login(user)
    for name in ("A1_SNAP.tif", "A1_CCK.tif"):
        upload(client, project, name, (folder / name).read_bytes(), last_modified=1)
    Worker("cpu", poll_seconds=0).run(once=True)
    Worker("gpu", poll_seconds=0).run(once=True)

    # Re-adding the folder: plan says the files are unchanged and hands back the
    # server's sha256, which is how the browser matches cellcounts.json to them.
    plan = post_json(client, reverse("uploads:plan", args=[project.id]), {"files": [
        {"name": n, "size": (folder / n).stat().st_size, "last_modified": 1} for n in ("A1_SNAP.tif", "A1_CCK.tif")]})
    decisions = {f["name"]: f for f in plan.json()["files"]}
    snap = Image.objects.get(filename="A1_SNAP.tif")
    assert decisions["A1_SNAP.tif"]["decision"] == "unchanged"
    assert decisions["A1_SNAP.tif"]["sha256"] == snap.sha256

    url = reverse("uploads:import_cells", args=[snap.id])
    review = desktop_review_data(folder, "A1_SNAP.tif", (folder / "A1_SNAP.tif").read_bytes())
    body = {"sha256": decisions["A1_SNAP.tif"]["sha256"], "imported_cells": review}
    result = post_json(client, url, body).json()
    assert result["result"] == "imported" and result["skipped"] == 0
    snap.refresh_from_db()
    assert snap.status == ImageStatus.DONE
    sidecar = json.loads((folder / "A1_SNAP.tif.cells.json").read_text())["cells"]
    assert normalize(web_cells(client, snap)) == normalize(sidecar)

    # Adding the same folder again changes nothing.
    assert post_json(client, url, body).json() == {"result": "unchanged"}


def test_web_edits_are_never_overwritten(tmp_path, login, user, project):
    folder = tmp_path / "A1-folder"
    folder.mkdir()
    write_desktop_folder(folder)
    client = login(user)
    data = (folder / "A1_CCK.tif").read_bytes()
    upload(client, project, "A1_CCK.tif", data, last_modified=1)
    Worker("cpu", poll_seconds=0).run(once=True)
    Worker("gpu", poll_seconds=0).run(once=True)
    image = Image.objects.get()

    first = web_cells(client, image)[0]
    post_json(client, reverse("projects:api_cell_ops", args=[image.id]), {
        "base_version": image.cells_version,
        "ops": [{"op": "update", "changes": [{"id": first["id"], "status": "filtered", "edited": True}]}]})

    review = desktop_review_data(folder, "A1_CCK.tif", data)
    response = post_json(client, reverse("uploads:import_cells", args=[image.id]),
                         {"sha256": image.sha256, "imported_cells": review})
    assert response.status_code == 409 and response.json()["error"] == "has_edits"
    assert web_cells(client, image)[0]["status"] == "filtered"


def test_import_refuses_data_for_different_content(login, user, project):
    data = tiff_bytes(seed=1)
    upload(login(user), project, "A1_SNAP.tif", data)
    Worker("cpu", poll_seconds=0).run(once=True)
    image = Image.objects.get()
    response = post_json(login(user), reverse("uploads:import_cells", args=[image.id]),
                         {"sha256": "0" * 64, "imported_cells": {"cells": []}})
    assert response.json()["error"] == "content_mismatch"


def test_unreadable_cells_are_skipped_not_fatal(login, user, project):
    good = {"id": 1, "polygons": [[[1, 1], [9, 1], [9, 9]]], "centroid": [6, 4], "area": 30,
            "status": "kept", "source": "model", "edited": False}
    two_point_ring = {**good, "id": 2, "polygons": [[[1, 1], [2, 2]]]}
    mixed_rings = {**good, "id": 3, "polygons": [[[1, 1], [2, 2]], [[20, 20], [30, 20], [30, 30]]]}
    bad_status = {**good, "id": 4, "status": "maybe"}
    response = upload(login(user), project, "A1_SNAP.tif", tiff_bytes(),
                      imported={"cells": [good, two_point_ring, mixed_rings, bad_status, good]})
    assert response.json()["imported"] == {"cells": 2, "skipped": 3}
    image = Image.objects.get()
    assert sorted(image.cells.values_list("cell_id", flat=True)) == [1, 3]
    assert image.cells.get(cell_id=3).polygons == [[[20, 20], [30, 20], [30, 30]]]
