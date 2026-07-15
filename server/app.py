"""
app.py — FastAPI wiring for the cell-counts compute server.

Run with `uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1`. `--workers 1` is
mandatory, not a tuning knob: a second worker process would load a second
CellposeModel and likely OOM a single RTX 3080 Ti, and would also mean two
independent in-process job queues instead of one — see jobs.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

import jobs
import uploads
from auth import require_auth, require_auth_ws

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

STALE_SWEEP_INTERVAL_SECONDS = 3600


async def _sweep_loop() -> None:
    while True:
        await asyncio.sleep(STALE_SWEEP_INTERVAL_SECONDS)
        try:
            removed = uploads.sweep_stale_uploads()
            if removed:
                log.info("Swept %d stale upload(s)", removed)
        except Exception:
            log.exception("stale upload sweep failed")


# --------------------------------------------------------------------------- #
# Job status push (/ws/jobs) -- jobs.py's worker thread is plain sync
# (threading, no asyncio), so it can't await a websocket send directly.
# _broadcast_status_change (registered with jobs.py as a callback) bridges
# that sync call onto this module's asyncio event loop via
# run_coroutine_threadsafe, captured once at startup.
# --------------------------------------------------------------------------- #
_ws_clients: set[WebSocket] = set()
_ws_loop: asyncio.AbstractEventLoop | None = None


def _broadcast_status_change(job_id: str, job: dict) -> None:
    if _ws_loop is None or not _ws_clients:
        return
    payload = json.dumps(job)
    asyncio.run_coroutine_threadsafe(_broadcast(payload), _ws_loop)


async def _broadcast(payload: str) -> None:
    for ws in list(_ws_clients):
        try:
            await ws.send_text(payload)
        except Exception:
            _ws_clients.discard(ws)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    global _ws_loop
    cpu = os.environ.get("CELLCOUNTS_CPU", "").lower() in ("1", "true", "yes")
    fp32 = os.environ.get("CELLCOUNTS_FP32", "").lower() in ("1", "true", "yes")
    jobs.init(cpu=cpu, fp32=fp32)
    _ws_loop = asyncio.get_running_loop()
    jobs.set_status_change_hook(_broadcast_status_change)
    sweep_task = asyncio.create_task(_sweep_loop())
    try:
        yield
    finally:
        sweep_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweep_task


app = FastAPI(title="cell-counts compute server", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Health (unauthenticated — the client's connection light hits this)
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "gpu": _gpu_available(), "model_loaded": jobs.model_loaded()}


def _gpu_available() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available() or torch.backends.mps.is_available())
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# GPU memory diagnostics (authenticated — not for the client UI, for debugging
# "why does nvidia-smi show more than expected" without having to guess)
# --------------------------------------------------------------------------- #
@app.get("/admin/gpu")
def gpu_stats(_user: str = Depends(require_auth)) -> dict:
    return jobs.gpu_memory_stats()


# --------------------------------------------------------------------------- #
# Chunked upload
# --------------------------------------------------------------------------- #
class InitUploadRequest(BaseModel):
    filename: str
    total_size: int
    sha256: str
    chunk_size: int = 32 * 1024 * 1024


@app.post("/uploads/init")
def init_upload(req: InitUploadRequest, _user: str = Depends(require_auth)) -> dict:
    try:
        meta = uploads.init_upload(req.filename, req.total_size, req.sha256, req.chunk_size)
    except uploads.UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return meta.__dict__


@app.put("/uploads/{upload_id}/chunk/{index}")
async def upload_chunk(upload_id: str, index: int, request: Request,
                        _user: str = Depends(require_auth)) -> dict:
    data = await request.body()
    try:
        uploads.write_chunk(upload_id, index, data)
    except uploads.UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/uploads/{upload_id}/status")
def upload_status(upload_id: str, _user: str = Depends(require_auth)) -> dict:
    try:
        return uploads.status(upload_id)
    except uploads.UploadError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class CompleteUploadRequest(BaseModel):
    params: dict = {}


@app.post("/uploads/{upload_id}/complete")
def complete_upload(upload_id: str, req: CompleteUploadRequest = CompleteUploadRequest(),
                     _user: str = Depends(require_auth)) -> dict:
    try:
        final_path = uploads.complete_upload(upload_id)
    except uploads.UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job_id = jobs.enqueue(final_path, final_path.name, req.params)
    # The job made its own private copy — free the shared upload immediately
    # rather than waiting for the job to run (which may be a while if the queue
    # is backed up).
    uploads.cleanup_final(final_path)
    return {"job_id": job_id}


# --------------------------------------------------------------------------- #
# Job status
# --------------------------------------------------------------------------- #
@app.get("/jobs/{job_id}")
def get_job(job_id: str, _user: str = Depends(require_auth)) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id {job_id!r}")
    return job


class ReorderJobsRequest(BaseModel):
    order: list[str]


@app.post("/jobs/reorder")
def reorder_jobs(req: ReorderJobsRequest, _user: str = Depends(require_auth)) -> dict:
    jobs.reorder(req.order)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Job status push -- one message per status transition (same dict shape as
# GET /jobs/{id}), broadcast to every connected client. The client sends
# nothing meaningful; this just holds the connection open long enough to
# detect a disconnect and clean up _ws_clients.
# --------------------------------------------------------------------------- #
@app.websocket("/ws/jobs")
async def ws_jobs(websocket: WebSocket) -> None:
    user = require_auth_ws(websocket)
    if user is None:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(websocket)
