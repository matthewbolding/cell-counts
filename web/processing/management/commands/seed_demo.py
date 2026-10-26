"""
seed_demo — fill a project with synthetic samples for local development.

Generates fluorescence-like uint16 TIFFs (noisy background plus Gaussian
"cells"), with CCK and CHR sharing a subset of positions so the Composite tab
has real coexpression to show, then tiles and segments them in-process with
the FakeModel. Never run against production data: it refuses unless
CELLCOUNTS_ENV=dev.

    uv run --env-file dev.env python manage.py seed_demo --owner you@example.com
"""

from __future__ import annotations

import hashlib

import numpy as np
import tifffile
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from core import storage
from processing import segmentation, tiles
from projects.models import CHANNELS, Cell, Image, ImageStatus, Project, Sample

SIZES = {"small": (2090, 1690), "medium": (4096, 3400), "large": (9000, 7400)}


def _render_cells(shape, centers, radii, brightness, rng) -> np.ndarray:
    h, w = shape
    img = rng.normal(400, 40, size=shape).astype(np.float32)
    for (cx, cy), r, b in zip(centers, radii, brightness, strict=True):
        x0, x1 = max(0, int(cx - 3 * r)), min(w, int(cx + 3 * r) + 1)
        y0, y1 = max(0, int(cy - 3 * r)), min(h, int(cy + 3 * r) + 1)
        yy, xx = np.mgrid[y0:y1, x0:x1]
        img[y0:y1, x0:x1] += b * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * (r / 1.6) ** 2)))
    return np.clip(img, 0, 65535).astype(np.uint16)


def synth_sample(shape, n_cells: int, rng) -> dict[str, np.ndarray]:
    h, w = shape
    centers = np.column_stack([rng.uniform(20, w - 20, n_cells), rng.uniform(20, h - 20, n_cells)])
    radii = rng.uniform(6, 11, n_cells)
    snap = _render_cells(shape, centers, radii, rng.uniform(2500, 6000, n_cells), rng)
    # CCK and CHR each light up a subset; about a third of each overlaps the other.
    idx = rng.permutation(n_cells)
    cck_idx = idx[: n_cells // 4]
    chr_idx = np.concatenate([idx[: n_cells // 12], idx[n_cells // 4: n_cells // 4 + n_cells // 6]])
    cck = _render_cells(shape, centers[cck_idx], radii[cck_idx], rng.uniform(2000, 5000, len(cck_idx)), rng)
    chr_ = _render_cells(shape, centers[chr_idx], radii[chr_idx], rng.uniform(2000, 5000, len(chr_idx)), rng)
    return {"SNAP": snap, "CCK": cck, "CHR": chr_}


class Command(BaseCommand):
    help = "Create a project of synthetic samples (development only)."

    def add_arguments(self, parser):
        parser.add_argument("--owner", required=True, help="email of an existing user")
        parser.add_argument("--name", default="Demo")
        parser.add_argument("--samples", type=int, default=4)
        parser.add_argument("--size", choices=SIZES, default="small")
        parser.add_argument("--with-large", action="store_true", help="also add one 9000x7400 sample")
        parser.add_argument("--seed", type=int, default=7)

    def handle(self, *args, **opts):
        if not settings.DEV:
            raise CommandError("seed_demo only runs with CELLCOUNTS_ENV=dev")
        try:
            owner = User.objects.get(email__iexact=opts["owner"])
        except User.DoesNotExist as exc:
            raise CommandError(f"no user {opts['owner']}") from exc

        rng = np.random.default_rng(opts["seed"])
        model = segmentation.FakeModel()
        project, _ = Project.objects.get_or_create(owner=owner, name=opts["name"])

        plan = [(f"A{i + 1}", SIZES[opts["size"]]) for i in range(opts["samples"])]
        if opts["with_large"]:
            plan.append(("B1", SIZES["large"]))

        for prefix, shape_wh in plan:
            w, h = shape_wh
            n_cells = int(w * h / 2500)
            self.stdout.write(f"{prefix}: {w}x{h}, ~{n_cells} cells")
            channels = synth_sample((h, w), n_cells, rng)
            sample, _ = Sample.objects.get_or_create(project=project, prefix=prefix)
            for channel in CHANNELS:
                self._make_image(project, sample, channel, channels[channel], model, owner)
        self.stdout.write(self.style.SUCCESS(f"Project “{project.name}” ready: /projects/{project.id}/"))

    def _make_image(self, project, sample, channel, array, model, owner) -> None:
        filename = f"{sample.prefix}_{channel}.tif"
        Image.objects.filter(project=project, filename=filename).delete()
        image = Image(project=project, sample=sample, channel=channel, filename=filename,
                      sha256="0" * 64, size_bytes=0, uploaded_by=owner)
        path = storage.original_path(project.id, image.id)
        tifffile.imwrite(path, array)
        image.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        image.size_bytes = path.stat().st_size
        image.height, image.width = array.shape
        image.tiles_rev = tiles.tiles_rev(image.sha256)
        image.tile_levels = tiles.build_pyramid(array, storage.tiles_dir(image.id, image.tiles_rev))

        result = segmentation.run(model, array)
        image.status = ImageStatus.DONE
        image.cells_version = 1
        image.segmentation_params = result["params"]
        image.processed_at = timezone.now()
        with transaction.atomic():
            image.save()
            Cell.objects.bulk_create([
                Cell(image=image, cell_id=c["id"], polygons=c["polygons"], centroid_x=c["centroid"][0],
                     centroid_y=c["centroid"][1], area=c["area"], status=c["status"],
                     filter_reason=c["filter_reason"] or "", source=c["source"], edited=False)
                for c in result["cells"]
            ], batch_size=2000)
        kept = sum(1 for c in result["cells"] if c["status"] == "kept")
        self.stdout.write(f"  {filename}: {len(result['cells'])} cells ({kept} kept), {image.tile_levels} levels")
