"""
worker.py — the loop both workers run: claim a job, run its handler, record the
outcome, repeat.

Production runs exactly one CPU worker and one GPU worker (DESIGN.md). Neither
accepts connections; both only poll the job table. Stopping is graceful: SIGTERM
(what `docker compose stop` sends) lets the current job finish before the
process exits, so a deploy never leaves a half-written tile pyramid or cell set
behind. A job that's interrupted anyway (power cut, OOM kill) is picked up by
queue.recover_interrupted() the next time the worker starts.

Only the CPU worker does housekeeping, including sending notification emails:
the GPU worker runs on a network with no route to the internet.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time

from django.db import DatabaseError, close_old_connections, connection

from processing import handlers, housekeeping, notifications, queue, segmentation
from processing.models import Job, Resource

log = logging.getLogger("processing.worker")

NOTIFY_INTERVAL_SECONDS = 60
HOUSEKEEPING_INTERVAL_SECONDS = 300


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


class Worker:
    def __init__(self, resource: str, poll_seconds: float = 1.0):
        if resource not in Resource.values:
            raise ValueError(f"unknown resource {resource!r}")
        self.resource = resource
        self.poll_seconds = poll_seconds
        self.ctx = handlers.WorkerContext()
        self._stop = threading.Event()
        self._last_housekeeping = 0.0
        self._last_notify = 0.0

    # ------------------------------------------------------------------ #
    def install_signal_handlers(self) -> None:
        def request_stop(signum, _frame):
            log.info("Received %s; finishing the current job, then stopping.", signal.Signals(signum).name)
            self._stop.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

    def stop(self) -> None:
        self._stop.set()

    def start(self) -> None:
        requeued, failed = queue.recover_interrupted(self.resource)
        if requeued or failed:
            log.warning("Recovered interrupted %s jobs: %d re-queued, %d failed.", self.resource, requeued, failed)
        if self.resource == Resource.GPU:
            log.info("Loading segmentation model...")
            self.ctx.model = segmentation.load_model(cpu=_env_flag("CELLCOUNTS_CPU"),
                                                     fp32=_env_flag("CELLCOUNTS_FP32"))
            log.info("Model loaded.")

    # ------------------------------------------------------------------ #
    def run(self, once: bool = False) -> int:
        """Process jobs until stopped. With `once`, process everything currently
        claimable and return. Returns the number of jobs processed."""
        self.start()
        processed = 0
        while not self._stop.is_set():
            # Long-lived processes must recycle stale/broken DB connections
            # themselves (Django only does it per request). Skipped inside an
            # atomic block, where it would close the transaction's connection
            # (tests drive the loop that way).
            if not connection.in_atomic_block:
                close_old_connections()
            if self.resource == Resource.CPU:
                self.maybe_housekeep()
            job = queue.claim_next(self.resource)
            if job is None:
                if once:
                    break
                self._stop.wait(self.poll_seconds)
                continue
            self.run_job(job)
            processed += 1
        if self.resource == Resource.CPU and once:
            self.maybe_housekeep(force=True)
        return processed

    def run_job(self, job: Job) -> None:
        handler = handlers.HANDLERS[job.kind]
        started = time.monotonic()
        log.info("Starting %s job %s", job.kind, job.id)
        try:
            result = handler(job, self.ctx)
        except handlers.JobGone:
            log.info("%s job %s: its image/project went away while it ran; dropping the result.",
                     job.kind, job.id)
            self._record(queue.cancel_running, job, "The image was replaced or deleted while processing.")
        except Exception as exc:  # noqa: BLE001 — recorded on the job, never allowed to kill the loop
            if isinstance(exc, handlers.JobFailed):
                log.warning("%s job %s failed: %s", job.kind, job.id, exc)
            else:
                log.exception("%s job %s failed", job.kind, job.id)
            self._record(queue.fail, job, f"{type(exc).__name__}: {exc}")
        else:
            self._record(queue.finish, job, result)
            log.info("Finished %s job %s in %.1fs", job.kind, job.id, time.monotonic() - started)
        finally:
            if self.resource == Resource.GPU:
                segmentation.release_gpu_memory()

    @staticmethod
    def _record(fn, job: Job, arg) -> None:
        """Record a job's outcome. The job row itself may be gone by now (its
        project was deleted, cascading to its jobs) — that's not an error."""
        try:
            fn(job, arg)
        except (DatabaseError, Job.DoesNotExist):
            if Job.objects.filter(pk=job.pk).exists():
                raise
            log.info("Job %s was deleted while it ran.", job.id)

    # ------------------------------------------------------------------ #
    def maybe_housekeep(self, force: bool = False) -> None:
        """Notification emails go out every minute; the heavier sweeps (stale
        uploads, expired exports, daily summaries) every five."""
        now = time.monotonic()
        try:
            if force or now - self._last_notify >= NOTIFY_INTERVAL_SECONDS:
                self._last_notify = now
                notifications.send_pending()
            if force or now - self._last_housekeeping >= HOUSEKEEPING_INTERVAL_SECONDS:
                self._last_housekeeping = now
                housekeeping.run()
        except Exception:  # noqa: BLE001 — housekeeping must never take the worker down
            log.exception("Housekeeping failed")
