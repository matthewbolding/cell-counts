"""
jobs.py — persisted job store (SQLite) + single-worker segmentation queue.

One RTX 3080 Ti, one CellposeModel loaded once at startup, one background thread
pulling jobs off a queue and running them strictly one at a time. This is
deliberately not Celery/Redis: single-user, single-GPU, low request volume — a real
task queue would be pure overhead. What does matter, and is easy to get wrong:

- model.eval() is a long blocking call. It must never run on the asyncio event loop
  (which would freeze every other request, including status polls, for the duration
  of a job) — it only ever runs inside the dedicated worker thread below.
- Job status is persisted to SQLite, not just held in memory, so a server restart
  mid-job doesn't turn a client's in-flight poll into a bare 404 — it resolves to an
  explicit "error" status the client can react to (by re-uploading) instead of
  hanging forever.

`pause()`/`resume()` (client-facing: `POST /jobs/pause`/`/jobs/resume`) gate
`_ReorderableQueue.get()` itself, so "paused" means the worker simply never
pops another job_id off the queue -- whatever it already popped and is
running keeps running, completely unaffected, matching what the reviewer's
Stop/Start button actually promises. Not persisted (like `reorder()`, see
`_ReorderableQueue`'s own docstring) -- a restart already aborts every
outstanding job, so there's nothing meaningful to restore a pause into.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import torch

import segment
import uploads

log = logging.getLogger("jobs")

DB_PATH = Path("data/jobs.sqlite3")
JOB_FILES_ROOT = Path("data/jobs")

class _ReorderableQueue:
    """FIFO by default (`put`/`get`), but `reorder()` lets a caller reprioritize
    whatever's still waiting — used so the client's sidebar Queue panel can
    actually change segmentation order after a file's already been uploaded,
    not just before. Never touches anything the worker has already popped, and
    isn't persisted anywhere: a server restart already aborts every
    queued/processing job (see `init()` below), so there's nothing to restore.

    `pause()`/`resume()` gate `get()` itself (not just `put()`/scheduling), so
    the effect is exactly "don't start anything new" -- whatever the worker
    already popped and is actively running is completely unaffected, and
    resuming wakes `get()` immediately via the same condition variable rather
    than leaving it to notice on some poll interval.
    """

    def __init__(self) -> None:
        self._items: list[str] = []
        self._cond = threading.Condition()
        self._running = True

    def put(self, job_id: str) -> None:
        with self._cond:
            self._items.append(job_id)
            self._cond.notify_all()

    def get(self) -> str:
        """Block until an item is waiting AND the queue isn't paused."""
        with self._cond:
            while not (self._items and self._running):
                self._cond.wait()
            return self._items.pop(0)

    def pause(self) -> None:
        with self._cond:
            self._running = False

    def resume(self) -> None:
        with self._cond:
            self._running = True
            self._cond.notify_all()

    def is_running(self) -> bool:
        with self._cond:
            return self._running

    def reorder(self, order: list[str]) -> None:
        """Re-sort whatever's still waiting to match `order`. job_ids named in
        `order` that are no longer waiting (already popped, or never existed)
        are simply ignored. job_ids currently waiting but not named in `order`
        keep their existing relative position, sorted after everything that
        was named -- a stable sort makes this a one-liner."""
        with self._cond:
            rank = {job_id: i for i, job_id in enumerate(order)}
            self._items.sort(key=lambda job_id: (job_id not in rank, rank.get(job_id, 0)))


_model = None
_queue = _ReorderableQueue()
_worker_thread: threading.Thread | None = None
_db_lock = threading.Lock()
# Set by app.py at startup so _set_status can push each status transition out
# over the /ws/jobs websocket -- kept as a plain sync callback (not an asyncio
# import here) so this module stays pure-sync; app.py is the only place that
# needs to know about the event loop/websocket connections.
_on_status_change: Callable[[str, dict], None] | None = None


def set_status_change_hook(fn: Callable[[str, dict], None] | None) -> None:
    global _on_status_change
    _on_status_change = fn


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            filename TEXT NOT NULL,
            final_path TEXT NOT NULL,
            params TEXT,
            progress TEXT,
            result TEXT,
            error TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.commit()
    return conn


_conn: sqlite3.Connection | None = None


def init(cpu: bool = False, fp32: bool = False) -> None:
    """Load the model and start the worker thread. Call once at server startup."""
    global _model, _conn, _worker_thread
    _conn = _connect()

    with _db_lock:
        now = time.time()
        _conn.execute(
            "UPDATE jobs SET status='error', error='server restarted mid-job', updated_at=? "
            "WHERE status IN ('queued','processing')",
            (now,),
        )
        _conn.commit()

    log.info("Loading Cellpose model...")
    _model = segment.load_model(cpu=cpu, fp32=fp32)
    log.info("Model loaded.")

    _worker_thread = threading.Thread(target=_worker_loop, name="segment-worker", daemon=True)
    _worker_thread.start()


def model_loaded() -> bool:
    return _model is not None


def gpu_memory_stats() -> dict[str, Any]:
    """`allocated` is memory actually held by live tensors right now — if this
    is small while `reserved` (what `nvidia-smi` shows) is large, that's
    PyTorch's caching allocator sitting on freed memory as a cache, not a leak.
    `max_*` are the high-water marks since the process started (or since the
    last `reset_peak_stats`), useful for telling "this session touched one huge
    image" apart from "this has been climbing steadily," after the fact.
    """
    if not torch.cuda.is_available():
        return {"gpu": False}
    return {
        "gpu": True,
        "allocated_mb": round(torch.cuda.memory_allocated() / 1024**2, 1),
        "reserved_mb": round(torch.cuda.memory_reserved() / 1024**2, 1),
        "max_allocated_mb": round(torch.cuda.max_memory_allocated() / 1024**2, 1),
        "max_reserved_mb": round(torch.cuda.max_memory_reserved() / 1024**2, 1),
    }


def enqueue(final_path: Path, filename: str, params: dict[str, Any] | None = None) -> str:
    """Give this job its own private copy of the uploaded file, under a
    job_id-keyed directory, rather than pointing at the shared upload's
    `final_path` directly. `upload_id` (and therefore `final_path`) is
    deterministic from (filename, sha256) — if the same file gets submitted more
    than once before the first submission's job has run (e.g. the client
    resubmits after being closed mid-processing), both submissions would
    otherwise share one physical file, and whichever job finished first would
    delete it out from under the other (`uploads.cleanup_final`) — a real
    `FileNotFoundError` observed in production. Each job now only ever deletes
    its own copy.
    """
    job_id = uuid.uuid4().hex
    job_dir = JOB_FILES_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    job_path = job_dir / filename
    shutil.copyfile(final_path, job_path)

    now = time.time()
    with _db_lock:
        _conn.execute(
            "INSERT INTO jobs (id, status, filename, final_path, params, progress, result, error, "
            "created_at, updated_at) VALUES (?, 'queued', ?, ?, ?, NULL, NULL, NULL, ?, ?)",
            (job_id, filename, str(job_path), json.dumps(params or {}), now, now),
        )
        _conn.commit()
    _queue.put(job_id)
    return job_id


def reorder(order: list[str]) -> None:
    """Reprioritize whatever's still waiting to run, per the client's sidebar
    Queue panel. Best-effort: job_ids the queue no longer recognizes are
    silently ignored (see `_ReorderableQueue.reorder`)."""
    _queue.reorder(order)


def pause() -> None:
    """Stop starting new jobs -- whatever's already running (the worker
    already popped it) finishes normally and is completely unaffected."""
    _queue.pause()


def resume() -> None:
    _queue.resume()


def is_running() -> bool:
    return _queue.is_running()


def get(job_id: str) -> dict | None:
    with _db_lock:
        row = _conn.execute(
            "SELECT id, status, filename, progress, result, error, created_at, updated_at "
            "FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    if row is None:
        return None
    keys = ["id", "status", "filename", "progress", "result", "error", "created_at", "updated_at"]
    job = dict(zip(keys, row))
    if job["result"]:
        job["result"] = json.loads(job["result"])
    if job["progress"]:
        job["progress"] = json.loads(job["progress"])
    return job


def _set_status(job_id: str, **fields: Any) -> None:
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [job_id]
    with _db_lock:
        _conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", values)
        _conn.commit()
    if _on_status_change is not None:
        job = get(job_id)
        if job is not None:
            _on_status_change(job_id, job)


def _worker_loop() -> None:
    while True:
        job_id = _queue.get()
        try:
            _run_job(job_id)
        except Exception:
            log.exception("job %s crashed the worker loop", job_id)


def _run_job(job_id: str) -> None:
    with _db_lock:
        row = _conn.execute(
            "SELECT final_path, filename, params FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
    if row is None:
        log.error("job %s vanished from the store before it could run", job_id)
        return
    final_path_str, filename, params_json = row
    final_path = Path(final_path_str)
    params = json.loads(params_json) if params_json else {}

    _set_status(job_id, status="processing")
    try:
        result = segment.run(_model, final_path, params)
        _set_status(job_id, status="done", result=json.dumps(result))
    except Exception as exc:  # noqa: BLE001 — reported to the client, not swallowed
        log.exception("job %s (%s) failed", job_id, filename)
        _set_status(job_id, status="error", error=str(exc))
    finally:
        uploads.cleanup_final(final_path)
        # PyTorch's caching allocator never hands memory back to the driver on
        # its own — it just keeps whatever peak it's needed as a cache for next
        # time. The corpus varies wildly in size (small crops up to ~100+
        # megapixel images), so that peak climbs a lot over a session even
        # though only one image is ever segmented at a time. This returns
        # whatever's genuinely unused back to the driver after every job, so
        # `nvidia-smi` reflects real recent need instead of a session-long high
        # water mark. Cheap: nothing is still using that memory at this point,
        # and the next job's first allocation just re-requests it from the
        # driver (a handful of milliseconds against jobs that run tens of
        # seconds to minutes).
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
