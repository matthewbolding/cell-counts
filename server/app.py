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
import logging
import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

import jobs
import uploads
from auth import require_auth

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


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    cpu = os.environ.get("CELLCOUNTS_CPU", "").lower() in ("1", "true", "yes")
    fp32 = os.environ.get("CELLCOUNTS_FP32", "").lower() in ("1", "true", "yes")
    jobs.init(cpu=cpu, fp32=fp32)
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
