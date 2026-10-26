"""
models.py — one row per file being uploaded.

Same protocol as the desktop client's server (init / chunk / status / complete),
with the chunk bookkeeping moved from marker files into Postgres: chunks can
arrive in any order or concurrently, each one is written in place at its offset
(core.storage.write_at) and recorded here, and `complete` only has to check the
received set — there's no reassembly copy. Re-initing the same
(project, filename, sha256) returns the existing session, which is what makes an
interrupted upload resumable.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models


class UploadSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="uploads")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    filename = models.CharField(max_length=255)
    prefix = models.CharField(max_length=32)
    channel = models.CharField(max_length=4)
    sha256 = models.CharField(max_length=64)
    size_bytes = models.BigIntegerField()
    client_mtime = models.BigIntegerField(null=True, blank=True)
    chunk_size = models.PositiveIntegerField()
    total_chunks = models.PositiveIntegerField()
    received = ArrayField(models.PositiveIntegerField(), default=list, blank=True)
    replaces = models.ForeignKey(
        "projects.Image", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "filename", "sha256"],
                condition=models.Q(completed_at__isnull=True),
                name="unique_open_upload",
            ),
        ]

    def __str__(self) -> str:
        return f"upload of {self.filename} ({len(self.received)}/{self.total_chunks} chunks)"

    def missing_chunks(self) -> list[int]:
        got = set(self.received)
        return [i for i in range(self.total_chunks) if i not in got]
