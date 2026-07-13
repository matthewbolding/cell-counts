"""
processing_queue.py — thread-safe pending-work queue shared between app.py's
background worker thread and review.py's UI poll.

The review screen now appears immediately after a folder is picked (see app.py),
while hashing/upload/segmentation continues on a background thread. This is the
handoff point between the two: the worker thread drains it via `pop_next()`, the UI
thread reads it via `snapshot()`/`is_processing()` and reorders it via
`move_up`/`move_down`/`send_to_front`/`send_to_back` — all under one lock, no Tk
dependency here at all so it's safe to touch from either thread.

Reordering takes a *set* of filenames, not one at a time, so a multi-selection in
the queue panel can be bumped as a single block: `move_up`/`move_down` shift the
whole selected group past its nearest unselected neighbor one step at a time
(so a repeated press walks the block up/down while every item inside it keeps its
position relative to the others), and `send_to_front`/`send_to_back` move the whole
selection to the head/tail of the queue while preserving the selected items'
relative order among themselves.

Stop only prevents *starting the next* item — `pop_next()` only checks the resume
event between items, never interrupts an in-flight upload/poll. There's no
server-side job-cancel endpoint, so pausing mid-item isn't a safe option.

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
    status: str = "queued"  # "queued" | "processing"

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
            order = [i.filename for i in self._items if i.status in ("queued", "processing")]
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
        """Block until running, then claim and return the first queued item.
        Returns None once nothing queued remains."""
        while True:
            self._resume_event.wait()
            with self._lock:
                for item in self._items:
                    if item.status == "queued":
                        item.status = "processing"
                        return item
                return None

    def complete(self, item: QueueItem) -> None:
        with self._lock:
            try:
                self._items.remove(item)
            except ValueError:
                pass
        self._persist()

    # ------------------------------------------------------------------ #
    # UI-thread side
    # ------------------------------------------------------------------ #
    def snapshot(self) -> QueueSnapshot:
        with self._lock:
            return QueueSnapshot(items=list(self._items), running=self._resume_event.is_set())

    def is_processing(self, filename: str) -> bool:
        with self._lock:
            return any(i.filename == filename and i.status == "processing" for i in self._items)

    def _reorder(self, filenames: set[str], op) -> None:
        with self._lock:
            queued = [i for i in self._items if i.status == "queued"]
            others = [i for i in self._items if i.status != "queued"]
            selected = [it.filename in filenames for it in queued]
            op(queued, selected)
            self._items = others + queued
        self._persist()

    def move_up(self, filenames: set[str]) -> None:
        def op(queued, selected):
            # Ascending scan: each selected item swaps past the unselected item
            # directly above it. A selected item whose neighbor above is *also*
            # selected is left alone this pass — that's what keeps the block
            # moving as a unit instead of the items inside it reshuffling.
            for i in range(1, len(queued)):
                if selected[i] and not selected[i - 1]:
                    queued[i - 1], queued[i] = queued[i], queued[i - 1]
                    selected[i - 1], selected[i] = selected[i], selected[i - 1]
        self._reorder(filenames, op)

    def move_down(self, filenames: set[str]) -> None:
        def op(queued, selected):
            for i in range(len(queued) - 2, -1, -1):
                if selected[i] and not selected[i + 1]:
                    queued[i], queued[i + 1] = queued[i + 1], queued[i]
                    selected[i], selected[i + 1] = selected[i + 1], selected[i]
        self._reorder(filenames, op)

    def send_to_front(self, filenames: set[str]) -> None:
        def op(queued, selected):
            picked = [it for it, s in zip(queued, selected) if s]
            rest = [it for it, s in zip(queued, selected) if not s]
            queued[:] = picked + rest
        self._reorder(filenames, op)

    def send_to_back(self, filenames: set[str]) -> None:
        def op(queued, selected):
            picked = [it for it, s in zip(queued, selected) if s]
            rest = [it for it, s in zip(queued, selected) if not s]
            queued[:] = rest + picked
        self._reorder(filenames, op)

    def start(self) -> None:
        self._resume_event.set()
        self._persist()

    def stop(self) -> None:
        self._resume_event.clear()
        self._persist()

    @property
    def is_running(self) -> bool:
        return self._resume_event.is_set()
