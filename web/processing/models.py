"""
models.py — the job table both workers pull from, plus queue state.

A job is anything a worker does: `prepare` (CPU: verify hash, read dimensions,
build tiles), `segment` and `rescan` (GPU: Cellpose), `export` (CPU: build an
XLSX), `purge` (CPU: delete files left behind by a deleted project or a replaced
image — over NFS that can take longer than a web request should).

Workers poll this table; nothing ever connects to a worker. How the GPU worker
picks the next job (rescans first, then users take turns) lives in queue.py.
"""

from __future__ import annotations

import time
import uuid

from django.conf import settings
from django.db import models


class JobKind(models.TextChoices):
    PREPARE = "prepare", "Prepare"
    SEGMENT = "segment", "Segment"
    RESCAN = "rescan", "Rescan"
    EXPORT = "export", "Export"
    PURGE = "purge", "Purge files"  # delete a removed project's/image's files off the NAS


class Resource(models.TextChoices):
    CPU = "cpu", "CPU"
    GPU = "gpu", "GPU"


KIND_RESOURCE = {
    JobKind.PREPARE: Resource.CPU,
    JobKind.EXPORT: Resource.CPU,
    JobKind.PURGE: Resource.CPU,
    JobKind.SEGMENT: Resource.GPU,
    JobKind.RESCAN: Resource.GPU,
}


class JobStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    DONE = "done", "Done"
    ERROR = "error", "Error"
    CANCELLED = "cancelled", "Cancelled"


ACTIVE_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)

PRIORITY_INTERACTIVE = 0  # someone is waiting at the screen (rescans)
PRIORITY_BULK = 10


def default_position() -> int:
    """New jobs go to the end of their owner's line. Microseconds since the epoch
    keeps insertion order without a sequence; reorder() rewrites positions."""
    return time.time_ns() // 1000


class Job(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=8, choices=JobKind.choices)
    resource = models.CharField(max_length=3, choices=Resource.choices)
    priority = models.PositiveSmallIntegerField(default=PRIORITY_BULK)
    status = models.CharField(max_length=10, choices=JobStatus.choices, default=JobStatus.QUEUED)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="jobs")
    project = models.ForeignKey("projects.Project", null=True, on_delete=models.CASCADE, related_name="jobs")
    image = models.ForeignKey("projects.Image", null=True, blank=True, on_delete=models.CASCADE,
                              related_name="jobs")
    sweep = models.ForeignKey("RescanSweep", null=True, blank=True, on_delete=models.CASCADE,
                              related_name="jobs")
    export = models.ForeignKey("Export", null=True, blank=True, on_delete=models.CASCADE,
                               related_name="jobs")
    position = models.BigIntegerField(default=default_position)
    params = models.JSONField(default=dict, blank=True)
    result = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    megapixels = models.FloatField(default=0.0)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # Notification bookkeeping. Emails are sent by the CPU worker's housekeeping
    # pass, never by the GPU worker (which has no route to the internet), so
    # these flags are how a finished job is handed over for emailing.
    notified = models.BooleanField(default=False)          # covered by a batch-finished pass
    failure_notified = models.BooleanField(default=False)  # covered by a failure-email pass

    class Meta:
        indexes = [
            models.Index(fields=["resource", "status", "priority", "position"]),
            models.Index(fields=["user", "status"]),
            models.Index(fields=["project", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.id} ({self.status})"

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None


class QueueState(models.Model):
    """Singleton (pk=1): the admin's global GPU pause."""

    gpu_paused = models.BooleanField(default=False)
    paused_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return "GPU paused" if self.gpu_paused else "GPU running"

    @classmethod
    def get(cls) -> QueueState:
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class UserQueueState(models.Model):
    """Per-user queue settings: the user's own Stop/Start Segmenting, and when the
    GPU last started one of their bulk jobs (round-robin bookkeeping)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="queue_state", primary_key=True)
    segmenting_paused = models.BooleanField(default=False)
    last_served_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"queue state for {self.user}"

    @classmethod
    def for_user(cls, user) -> UserQueueState:
        obj, _ = cls.objects.get_or_create(user=user)
        return obj


class RescanSweep(models.Model):
    """One rescan request: a rectangle on one image, tried with every
    (diameter, cellprob_threshold) combination — one GPU job per combination."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    image = models.ForeignKey("projects.Image", on_delete=models.CASCADE, related_name="rescans")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    rect = models.JSONField()  # [x0, y0, x1, y1] in full-image pixels
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"rescan of {self.image} {self.rect}"


class Export(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="exports")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    prefixes = models.JSONField()
    include_summary = models.BooleanField(default=True)
    include_cells = models.BooleanField(default=True)
    filename = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.filename
