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
"""

from __future__ import annotations

import json
import logging
import queue
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import segment
import uploads

log = logging.getLogger("jobs")

DB_PATH = Path("data/jobs.sqlite3")
JOB_FILES_ROOT = Path("data/jobs")

_model = None
_queue: "queue.Queue[str]" = queue.Queue()
_worker_thread: threading.Thread | None = None
_db_lock = threading.Lock()


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
