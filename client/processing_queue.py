"""
processing_queue.py — thread-safe pending-work queue shared between app.py's
background worker thread and review.py's UI poll.

The review screen now appears immediately after a folder is picked (see app.py),
while hashing/upload/segmentation continues on a background thread. This is the
handoff point between the two: the worker thread drains/updates it via
`pop_next()`/`finish_upload()`/`update_server_status()`/etc., the UI thread reads
it via `snapshot()`/`is_processing()` and reorders it via
`move_up`/`move_down`/`send_to_front`/`send_to_back` — all under one lock, no Tk
dependency here at all so it's safe to touch from either thread.

An item lives here for its *entire* outstanding lifetime, not just the upload
step: "local phase" (waiting to upload / uploading, `job_id is None`) followed by
"server phase" (uploaded, `job_id` set — waiting its turn on the GPU, or actively
segmenting) once `finish_upload()`/`track_server_job()` hands it off. It's only
removed once its job actually finishes (done or error) — see `remove()`. This is
what lets the sidebar show (and reorder) files that have already been uploaded,
not just ones still waiting to be — the real bottleneck is the single-GPU
segmentation queue on the server, which an upload finishing does nothing to
relieve.

Reordering takes a *set* of filenames, not one at a time, so a multi-selection in
the queue panel can be bumped as a single block: `move_up`/`move_down` shift the
whole selected group past its nearest unselected neighbor one step at a time
(so a repeated press walks the block up/down while every item inside it keeps its
position relative to the others), and `send_to_front`/`send_to_back` move the whole
selection to the head/tail of the queue while preserving the selected items'
relative order among themselves. Local-phase and server-phase items are reordered
independently (an item can't jump the upload boundary), and within each phase an
item already in flight (`"uploading"`/`"processing"`) is pinned in place — only
`"queued"` items are ever movable. Each reorder method returns the resulting
server-phase job_id order, so the caller can push it to the server's own
reorderable queue (`ApiClient.reorder_jobs`) — local-only reorders never touch
the network.

Stop, as far as *this module* is concerned, only prevents *starting the next
upload* — `pop_next()` only checks the resume event between items, never
interrupts an in-flight upload, and has no bearing on jobs already sitting
server-side. `start()`/`stop()` here is one half of the reviewer-facing pause
story; the other half is a *separate* call to the server's own
`/jobs/pause`/`/jobs/resume` (see `server/jobs.py`'s `_ReorderableQueue.pause`)
that CellCountsApp's Process menu (app.py's `_on_pause_uploads_toggle`/
`_on_pause_segmenting_toggle` -- Stop/Start Uploads and Stop/Start Segmenting
are independent entries there) makes in tandem, not this module. There's no
server-side job-*cancel* endpoint, so pausing mid-item (upload or
segmentation) isn't a safe option either side of that split.

Persistence: if constructed with `persist_path`, the queue's order and running
state are written to that file (atomically, best-effort) after every mutation, and
`load_persisted_order()` reads it back. This is what lets a custom queue order
(and a paused/running state) survive quitting and reopening the app on the same
folder — app.py applies the persisted order when it rebuilds the pending-work list
from a fresh hash scan, since the scan itself has no memory of prior ordering.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from manifest import ScannedFile

QUEUE_STATE_NAME = "cellcounts.queue.json"  # sits next to cellcounts.json in the folder


def load_persisted_order(persist_path: Path) -> dict | None:
    if not persist_path.exists():
        return None
    try:
        return json.loads(persist_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass
class QueueItem:
    sf: ScannedFile
    file_hash: str
    # Local phase (job_id is None): "queued" (waiting to upload) | "uploading".
    # Server phase (job_id is set): "queued" | "processing" -- mirrors the
    # server's own job status vocabulary verbatim (see server/jobs.py), copied
    # straight through by update_server_status() with no translation needed.
    status: str = "queued"
    job_id: str | None = None

    @property
    def filename(self) -> str:
        return self.sf.path.name


@dataclass
class QueueSnapshot:
    items: list[QueueItem] = field(default_factory=list)
    running: bool = True


class ProcessingQueue:
    def __init__(self, persist_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._items: list[QueueItem] = []
        self._resume_event = threading.Event()
        self._resume_event.set()  # auto-runs by default, matching prior behavior
        self.persist_path = persist_path

    def _persist(self) -> None:
        if self.persist_path is None:
            return
        with self._lock:
            # Every item still in `_items` is by definition still outstanding
            # (local or server phase) -- nothing else is ever kept here.
            order = [i.filename for i in self._items]
            running = self._resume_event.is_set()
        data = {"order": order, "running": running}
        tmp_path = Path(str(self.persist_path) + ".tmp")
        try:
            tmp_path.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp_path, self.persist_path)
        except OSError:
            pass  # best-effort — queue ordering is a convenience, not critical data

    # ------------------------------------------------------------------ #
    # Worker-thread side
    # ------------------------------------------------------------------ #
    def enqueue(self, items: list[QueueItem]) -> None:
        with self._lock:
            self._items = list(items)
        self._persist()

    def pop_next(self) -> QueueItem | None:
        """Block until running, then claim and return the first not-yet-uploaded
        item. Returns None once nothing local-phase remains queued (server-phase
        items, already past upload, are untouched by this)."""
        while True:
            self._resume_event.wait()
            with self._lock:
                for item in self._items:
                    if item.job_id is None and item.status == "queued":
                        item.status = "uploading"
                        return item
                return None

    def finish_upload(self, item: QueueItem, job_id: str) -> None:
        """Upload succeeded: the item leaves the local phase and re-enters the
        queue as a server-phase item instead of disappearing -- its
        segmentation job is still outstanding, just no longer this client's
        problem to drive."""
        with self._lock:
            try:
                self._items.remove(item)
            except ValueError:
                pass
            self._items.append(QueueItem(sf=item.sf, file_hash=item.file_hash,
                                          status="queued", job_id=job_id))
        self._persist()

    def track_server_job(self, sf: ScannedFile, file_hash: str, job_id: str,
                          status: str = "queued") -> None:
        """Add an item that's already past the upload phase straight into the
        queue -- used for jobs resumed from a previous session, which never go
        through pop_next()/finish_upload() here."""
        with self._lock:
            self._items.append(QueueItem(sf=sf, file_hash=file_hash, status=status, job_id=job_id))
        self._persist()

    def update_server_status(self, filename: str, status: str) -> None:
        """Live status update from the poll loop's on_tick (mirrors the
        server's own job status verbatim -- see server/jobs.py)."""
        with self._lock:
            for i in self._items:
                if i.filename == filename and i.job_id is not None:
                    i.status = status
                    break
        self._persist()

    def update_job_id(self, filename: str, job_id: str) -> None:
        """Keeps a server-phase item's job_id accurate after the rare
        404-resubmit fallback (server restarted, lost its job history) issues
        a new one -- otherwise a later reorder would push a stale/unknown id."""
        with self._lock:
            for i in self._items:
                if i.filename == filename:
                    i.job_id = job_id
                    break
        self._persist()

    def remove(self, filename: str) -> None:
        """Drop an item entirely -- its upload failed (nothing to poll), or its
        job finished (done/error, now authoritative in the manifest instead)."""
        with self._lock:
            self._items = [i for i in self._items if i.filename != filename]
        self._persist()

    # ------------------------------------------------------------------ #
    # UI-thread side
    # ------------------------------------------------------------------ #
    def snapshot(self) -> QueueSnapshot:
        with self._lock:
            # Server-phase items first: genuine pipeline order (segmenting now
            # → queued server-side → uploading now → waiting to upload), and
            # keeps rows grouped by phase for the reorder buttons below.
            server = [i for i in self._items if i.job_id is not None]
            local = [i for i in self._items if i.job_id is None]
            return QueueSnapshot(items=server + local, running=self._resume_event.is_set())

    def is_processing(self, filename: str) -> bool:
        """True only for the narrow "uploading right now, manifest not updated
        yet" gap (see review.py's _channel_status docstring) -- server-phase
        visibility on the sidebar dot already comes for free from the
        manifest's own status, set the moment the upload completes."""
        with self._lock:
            return any(i.filename == filename and i.job_id is None and i.status == "uploading"
                       for i in self._items)

    def _reorder(self, filenames: set[str], op) -> list[str]:
        """Apply `op` independently within each phase (an item can never jump
        the upload boundary) and return the resulting server-phase job_id
        order, so the caller can push it to the server's own reorderable
        queue. A selection normally falls entirely within one phase, since
        rows are grouped by phase in `snapshot()`."""
        with self._lock:
            local = [i for i in self._items if i.job_id is None]
            server = [i for i in self._items if i.job_id is not None]
            touched_server = any(i.filename in filenames for i in server)
            for group in (local, server):
                movable = [i for i in group if i.status == "queued"]
                pinned = [i for i in group if i.status != "queued"]
                selected = [it.filename in filenames for it in movable]
                op(movable, selected)
                group[:] = pinned + movable
            self._items = server + local
            # Only report the server order back if this selection actually
            # touched a server-phase item -- a purely local reorder (the
            # common case whenever nothing's been uploaded yet) has no reason
            # to hit the network, even if unrelated server-phase items exist.
            server_order = [i.job_id for i in server] if touched_server else []
        self._persist()
        return server_order

    def move_up(self, filenames: set[str]) -> list[str]:
        def op(queued, selected):
            # Ascending scan: each selected item swaps past the unselected item
            # directly above it. A selected item whose neighbor above is *also*
            # selected is left alone this pass — that's what keeps the block
            # moving as a unit instead of the items inside it reshuffling.
            for i in range(1, len(queued)):
                if selected[i] and not selected[i - 1]:
                    queued[i - 1], queued[i] = queued[i], queued[i - 1]
                    selected[i - 1], selected[i] = selected[i], selected[i - 1]
        return self._reorder(filenames, op)

    def move_down(self, filenames: set[str]) -> list[str]:
        def op(queued, selected):
            for i in range(len(queued) - 2, -1, -1):
                if selected[i] and not selected[i + 1]:
                    queued[i], queued[i + 1] = queued[i + 1], queued[i]
                    selected[i], selected[i + 1] = selected[i + 1], selected[i]
        return self._reorder(filenames, op)

    def send_to_front(self, filenames: set[str]) -> list[str]:
        def op(queued, selected):
            picked = [it for it, s in zip(queued, selected) if s]
            rest = [it for it, s in zip(queued, selected) if not s]
            queued[:] = picked + rest
        return self._reorder(filenames, op)

    def send_to_back(self, filenames: set[str]) -> list[str]:
        def op(queued, selected):
            picked = [it for it, s in zip(queued, selected) if s]
            rest = [it for it, s in zip(queued, selected) if not s]
            queued[:] = rest + picked
        return self._reorder(filenames, op)

    def start(self) -> None:
        self._resume_event.set()
        self._persist()

    def stop(self) -> None:
        self._resume_event.clear()
        self._persist()

    @property
    def is_running(self) -> bool:
        return self._resume_event.is_set()
