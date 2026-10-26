"""
models.py — projects (one folder's worth of samples), who can see them, and
their samples, images and cells.

Cells are one row each rather than one JSON document per image. A review edit
then touches only the rows it changes instead of rewriting a multi-megabyte
document, exports and coexpression can filter `status='kept'` in SQL, and two
collaborators editing different images never contend. The per-cell fields are
exactly the `.cells.json` review-file schema, so importing a reviewed folder
is lossless.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

CHANNELS = ("SNAP", "CCK", "CHR")


class Role(models.TextChoices):
    OWNER = "owner", "Owner"
    EDITOR = "editor", "Editor"
    VIEWER = "viewer", "Viewer"


ROLE_RANK = {Role.VIEWER: 1, Role.EDITOR: 2, Role.OWNER: 3}


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="projects")
    name = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "name"], name="unique_project_name_per_owner"),
        ]

    def __str__(self) -> str:
        return self.name

    def role_for(self, user) -> str | None:
        if not user.is_authenticated:
            return None
        if self.owner_id == user.id:
            return Role.OWNER
        membership = self.memberships.filter(user=user).first()
        return membership.role if membership else None


class Membership(models.Model):
    """A collaborator on someone else's project. The owner never has a row here."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=10, choices=[(Role.EDITOR, "Editor"), (Role.VIEWER, "Viewer")])
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "user"], name="unique_membership")]

    def __str__(self) -> str:
        return f"{self.user} ({self.role}) on {self.project}"


class Sample(models.Model):
    """One animal+sample prefix (e.g. `A3`) within a project."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="samples")
    prefix = models.CharField(max_length=32)

    class Meta:
        ordering = ["prefix"]
        constraints = [models.UniqueConstraint(fields=["project", "prefix"], name="unique_sample_prefix")]

    def __str__(self) -> str:
        return self.prefix


class ImageStatus(models.TextChoices):
    PREPARING = "preparing", "Preparing"     # uploaded; verifying hash and building tiles
    QUEUED = "queued", "Queued"              # waiting for the GPU
    SEGMENTING = "segmenting", "Segmenting"
    DONE = "done", "Done"
    ERROR = "error", "Error"


class Image(models.Model):
    """One channel file of one sample. Replacing a file with different content
    keeps the same row (and URL) but resets its cells and tiles."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="images")
    sample = models.ForeignKey(Sample, on_delete=models.CASCADE, related_name="images")
    channel = models.CharField(max_length=4, choices=[(c, c) for c in CHANNELS])
    filename = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64)
    size_bytes = models.BigIntegerField()
    client_mtime = models.BigIntegerField(null=True, blank=True)  # File.lastModified (ms) at upload
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=ImageStatus.choices, default=ImageStatus.PREPARING)
    error = models.TextField(blank=True)
    tiles_rev = models.CharField(max_length=16, blank=True)
    tile_levels = models.PositiveSmallIntegerField(default=0)
    cells_version = models.PositiveIntegerField(default=0)
    segmentation_params = models.JSONField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    # Last review edit made in the web app. A folder's review data is only ever
    # imported over an image that has none (uploads/services.py).
    web_edited_at = models.DateTimeField(null=True, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["filename"]
        constraints = [
            models.UniqueConstraint(fields=["project", "filename"], name="unique_image_filename"),
            models.UniqueConstraint(fields=["sample", "channel"], name="unique_image_channel"),
        ]
        indexes = [models.Index(fields=["project", "status"])]

    def __str__(self) -> str:
        return self.filename

    @property
    def megapixels(self) -> float:
        if not self.width or not self.height:
            return 0.0
        return self.width * self.height / 1e6


class CellStatus(models.TextChoices):
    KEPT = "kept", "Kept"
    FILTERED = "filtered", "Filtered"


class Cell(models.Model):
    image = models.ForeignKey(Image, on_delete=models.CASCADE, related_name="cells")
    cell_id = models.PositiveIntegerField()
    polygons = models.JSONField()        # [[[x, y], ...], ...] — one or more rings
    centroid_x = models.FloatField()
    centroid_y = models.FloatField()
    area = models.PositiveIntegerField()
    status = models.CharField(max_length=8, choices=CellStatus.choices)
    filter_reason = models.CharField(max_length=16, blank=True)
    source = models.CharField(max_length=64, default="model")  # model | human | rescan:<job_id>
    edited = models.BooleanField(default=False)
    color = models.CharField(max_length=9, blank=True)
    edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    edited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["image", "cell_id"], name="unique_cell_id_per_image")]
        indexes = [models.Index(fields=["image", "status"])]

    def __str__(self) -> str:
        return f"cell {self.cell_id} of {self.image}"

    def as_dict(self) -> dict:
        """The `.cells.json` shape — what the frontend and exports consume."""
        return {
            "id": self.cell_id,
            "polygons": self.polygons,
            "centroid": [self.centroid_x, self.centroid_y],
            "area": self.area,
            "status": self.status,
            "filter_reason": self.filter_reason or None,
            "color": self.color or None,
            "source": self.source,
            "edited": self.edited,
        }


class ImageLock(models.Model):
    """Edit lease: one editor per image at a time. Renewed by the open review
    screen every ~20 s; an expired lease is free for anyone to take."""

    LEASE_SECONDS = 60

    image = models.OneToOneField(Image, on_delete=models.CASCADE, related_name="lock", primary_key=True)
    holder = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    expires_at = models.DateTimeField()

    def __str__(self) -> str:
        return f"{self.image} held by {self.holder}"

    def is_live(self) -> bool:
        return self.expires_at > timezone.now()


class ProjectUserState(models.Model):
    """Per-user, per-project UI state; it follows the user to any machine."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    state = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "user"], name="unique_ui_state")]

    def __str__(self) -> str:
        return f"UI state for {self.user} on {self.project}"
