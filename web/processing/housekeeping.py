"""
housekeeping.py — the CPU worker's periodic sweeps (every few minutes, see
worker.py): forget abandoned uploads, expire old exports, send daily summaries.
Each step is independent and idempotent, so a skipped or repeated run is harmless.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from core import storage
from processing import notifications
from processing.models import Export
from uploads.services import sweep_stale_uploads

log = logging.getLogger(__name__)

EXPORT_LIFETIME = timedelta(hours=24)


def expire_exports(now=None) -> int:
    cutoff = (now or timezone.now()) - EXPORT_LIFETIME
    removed = 0
    for exp in Export.objects.filter(created_at__lt=cutoff):
        storage.export_path(exp.id).unlink(missing_ok=True)
        exp.delete()
        removed += 1
    return removed


def run() -> None:
    if not storage.volume_available():
        log.error("Data volume isn't mounted; skipping file housekeeping.")
    else:
        if n := sweep_stale_uploads():
            log.info("Removed %d abandoned upload(s).", n)
        if n := expire_exports():
            log.info("Removed %d expired export(s).", n)
    notifications.send_daily_summaries()
