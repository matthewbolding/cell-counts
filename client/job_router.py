"""
job_router.py — routes /ws/jobs status-change events back to whichever
folder's Manifest/ProcessingQueue actually owns that job.

Multiple folders can have outstanding jobs simultaneously (see app.py's
`_active_processing_folders`), so a single global "whatever the current
folder is" isn't enough to route by -- routing has to be keyed by job_id.
Pure logic, no Tk, matching this project's other testable-without-a-GUI
modules (processing_queue.py, rescan.py).

Terminal-state handling (done/error) claims its route via a single
`dict.pop(job_id, None)` under the lock -- the one atomic linearization
point that makes this safe against a live websocket event and a resync/
register catch-up call racing for the same job and both resolving to the
same terminal state: whichever arrives second finds the route already gone
and no-ops, so manifest.record_*/batch.resolve() never double-fire.
"""

from __future__ import annotations

import threading
from typing import Callable

from api_client import ApiClient, ApiError
from manifest import Manifest, ScannedFile
from processing_queue import ProcessingQueue

TERMINAL_STATUSES = ("done", "error")


class RunBatch:
    """How many of one _run_processing call's jobs are still outstanding --
    lets that call block (via Condition.wait, not polling) until every one
    has resolved, purely to know when to print the final summary. Individual
    file results/statuses are already applied the instant each event
    arrives, independent of this wait."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._outstanding = 0
        self.n_ok = 0
        self.n_err = 0

    def add(self) -> None:
        with self._cond:
            self._outstanding += 1

    def resolve(self, ok: bool) -> None:
        with self._cond:
            self._outstanding -= 1
            if ok:
                self.n_ok += 1
            else:
                self.n_err += 1
            self._cond.notify_all()

    def wait_until_empty(self) -> None:
        with self._cond:
            while self._outstanding > 0:
                self._cond.wait()


class JobRouter:
    def __init__(self, client: ApiClient, on_log: Callable[[str], None] | None = None) -> None:
        self._client = client
        self._on_log = on_log or (lambda _text: None)
        self._lock = threading.Lock()
        self._routes: dict[str, dict] = {}

    def register(self, job_id: str, sf: ScannedFile, file_hash: str,
                 manifest: Manifest, queue: ProcessingQueue, batch: RunBatch) -> None:
        with self._lock:
            self._routes[job_id] = {
                "sf": sf, "file_hash": file_hash, "manifest": manifest,
                "queue": queue, "batch": batch,
            }
        batch.add()
        # Catch-up: closes the narrow race between obtaining this job_id and
        # this registration call, where the job could already have moved past
        # "queued" (or even finished) before the route existed to receive the
        # live event for it. One-shot, not a recurring check.
        try:
            job = self._client.get_job(job_id)
        except ApiError:
            return  # a live event, or the next resync(), will catch it
        self._apply(job_id, job)

    def handle_event(self, payload: dict) -> None:
        """payload is the raw dict off the websocket -- same shape as
        GET /jobs/{id}."""
        job_id = payload.get("job_id") or payload.get("id")
        if job_id:
            self._apply(job_id, payload)

    def resync(self) -> None:
        """Called once per successful (re)connection: a one-shot sweep over
        everything still registered, catching anything missed while
        disconnected -- including a 404 (server restarted, lost job
        history), which triggers a one-time resubmit."""
        with self._lock:
            job_ids = list(self._routes.keys())
        for job_id in job_ids:
            try:
                job = self._client.get_job(job_id)
            except ApiError as exc:
                if exc.status_code == 404:
                    self._resubmit(job_id)
                continue
            self._apply(job_id, job)

    def _resubmit(self, job_id: str) -> None:
        with self._lock:
            entry = self._routes.get(job_id)
        if entry is None:
            return
        sf, file_hash = entry["sf"], entry["file_hash"]
        self._on_log(f"{sf.path.name}: server no longer knows this job "
                      "(likely restarted) — re-uploading.")
        try:
            new_job_id = self._client.upload_file(sf.path, file_hash)
        except ApiError as exc:
            self._apply(job_id, {"status": "error", "error": str(exc)})
            return
        with self._lock:
            moved = self._routes.pop(job_id, None)
            if moved is not None:
                self._routes[new_job_id] = moved
        if moved is not None:
            moved["queue"].update_job_id(sf.path.name, new_job_id)

    def _apply(self, job_id: str, job: dict) -> None:
        status = job.get("status")

        if status not in TERMINAL_STATUSES:
            with self._lock:
                entry = self._routes.get(job_id)
            if entry is not None and status:
                entry["queue"].update_server_status(entry["sf"].path.name, status)
            return

        # Terminal: atomically claim this job_id so a racing duplicate
        # delivery only ever applies it once.
        with self._lock:
            entry = self._routes.pop(job_id, None)
        if entry is None:
            return

        sf, file_hash = entry["sf"], entry["file_hash"]
        manifest, queue, batch = entry["manifest"], entry["queue"], entry["batch"]
        if status == "done":
            result = job["result"]
            manifest.record_result(
                sf.path.name, sf.prefix, sf.channel, file_hash,
                result["width"], result["height"], result["params"], result["cells"],
            )
            n_kept = sum(1 for c in result["cells"] if c["status"] == "kept")
            self._on_log(f"{sf.path.name}: {n_kept} cells kept "
                          f"({len(result['cells']) - n_kept} filtered).")
            queue.remove(sf.path.name)
            batch.resolve(ok=True)
        else:  # "error"
            error = job.get("error") or "job failed"
            manifest.record_error(sf.path.name, sf.prefix, sf.channel, file_hash, error)
            self._on_log(f"ERROR processing {sf.path.name}: {error}")
            queue.remove(sf.path.name)
            batch.resolve(ok=False)
