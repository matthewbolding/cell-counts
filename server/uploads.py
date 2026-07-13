"""
uploads.py — chunked upload receive/resume/reassemble/verify.

Files in the corpus run up to ~280MB, well past Cloudflare free tier's 100MB request
body cap, so the client splits large files into (default 32MB) chunks and each chunk
is its own short-lived request — the ~100s idle timeout on the proxy chain is a
non-issue per-chunk regardless of how long the overall transfer takes.

`upload_id` is derived deterministically from (filename, sha256), so re-`init`ing the
same file is idempotent: a client that got interrupted mid-upload can call `init`
again, get the same `upload_id` back, and only re-send the chunks `status` reports
missing — no wasted re-transfer of bytes already on disk.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

STAGING_ROOT = Path("data/staging")
FINAL_ROOT = Path("data/incoming")
STALE_AFTER_SECONDS = 24 * 3600


class UploadError(Exception):
    pass


def _upload_id(filename: str, sha256: str) -> str:
    return hashlib.sha256(f"{filename}:{sha256}".encode("utf-8")).hexdigest()[:32]


def _meta_path(upload_id: str) -> Path:
    return STAGING_ROOT / upload_id / "meta.json"


def _chunk_path(upload_id: str, index: int) -> Path:
    return STAGING_ROOT / upload_id / f"{index}.part"


@dataclass
class UploadMeta:
    upload_id: str
    filename: str
    total_size: int
    sha256: str
    chunk_size: int
    total_chunks: int
    created_at: float


def init_upload(filename: str, total_size: int, sha256: str, chunk_size: int) -> UploadMeta:
    if total_size <= 0 or chunk_size <= 0:
        raise UploadError("total_size and chunk_size must be positive")
    upload_id = _upload_id(filename, sha256)
    meta_path = _meta_path(upload_id)
    if meta_path.exists():
        existing = json.loads(meta_path.read_text())
        return UploadMeta(**existing)

    total_chunks = (total_size + chunk_size - 1) // chunk_size
    meta = UploadMeta(
        upload_id=upload_id, filename=filename, total_size=total_size,
        sha256=sha256, chunk_size=chunk_size, total_chunks=total_chunks,
        created_at=time.time(),
    )
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta.__dict__))
    return meta


def _load_meta(upload_id: str) -> UploadMeta:
    meta_path = _meta_path(upload_id)
    if not meta_path.exists():
        raise UploadError(f"unknown upload_id {upload_id!r} (call /uploads/init first)")
    return UploadMeta(**json.loads(meta_path.read_text()))


def write_chunk(upload_id: str, index: int, data: bytes) -> None:
    meta = _load_meta(upload_id)
    if not (0 <= index < meta.total_chunks):
        raise UploadError(f"chunk index {index} out of range (0..{meta.total_chunks - 1})")
    path = _chunk_path(upload_id, index)
    path.write_bytes(data)


def present_chunks(upload_id: str) -> list[int]:
    meta = _load_meta(upload_id)
    dir_ = STAGING_ROOT / upload_id
    present = []
    for i in range(meta.total_chunks):
        if _chunk_path(upload_id, i).exists():
            present.append(i)
    return present


def status(upload_id: str) -> dict:
    meta = _load_meta(upload_id)
    return {
        "upload_id": upload_id,
        "filename": meta.filename,
        "total_chunks": meta.total_chunks,
        "chunks_present": present_chunks(upload_id),
    }


def complete_upload(upload_id: str) -> Path:
    """Reassemble parts in order, verify the full-file hash, atomically promote."""
    meta = _load_meta(upload_id)
    present = set(present_chunks(upload_id))
    missing = [i for i in range(meta.total_chunks) if i not in present]
    if missing:
        raise UploadError(f"missing chunks: {missing[:10]}{'...' if len(missing) > 10 else ''}")

    staging_dir = STAGING_ROOT / upload_id
    assembled_tmp = staging_dir / "assembled.tmp"
    hasher = hashlib.sha256()
    with assembled_tmp.open("wb") as out:
        for i in range(meta.total_chunks):
            chunk = _chunk_path(upload_id, i).read_bytes()
            hasher.update(chunk)
            out.write(chunk)

    digest = hasher.hexdigest()
    if digest != meta.sha256:
        assembled_tmp.unlink(missing_ok=True)
        raise UploadError(f"hash mismatch after reassembly: expected {meta.sha256}, got {digest}")

    final_dir = FINAL_ROOT / upload_id
    final_dir.mkdir(parents=True, exist_ok=True)
    final_path = final_dir / meta.filename
    assembled_tmp.replace(final_path)

    shutil.rmtree(staging_dir, ignore_errors=True)
    return final_path


def sweep_stale_uploads(older_than_seconds: float = STALE_AFTER_SECONDS) -> int:
    """Remove abandoned staging directories (interrupted/never-completed uploads)."""
    if not STAGING_ROOT.exists():
        return 0
    now = time.time()
    removed = 0
    for child in STAGING_ROOT.iterdir():
        meta_path = child / "meta.json"
        if not meta_path.exists():
            continue
        try:
            created_at = json.loads(meta_path.read_text())["created_at"]
        except (OSError, ValueError, KeyError):
            created_at = 0
        if now - created_at > older_than_seconds:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed


def cleanup_final(final_path: Path) -> None:
    """Called by jobs.py once a job has consumed the reassembled file."""
    shutil.rmtree(final_path.parent, ignore_errors=True)
