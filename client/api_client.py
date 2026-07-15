"""
api_client.py — talks to the compute server: auth, chunked upload w/ resume, job polling.

Credentials are held only in memory for the process lifetime (never written to
disk) and re-sent as HTTP Basic auth on every request — fine here because the
server is only reachable over HTTPS (Cloudflare + Nginx Proxy Manager terminate
TLS in front of it).

Uploads are resumable: `/uploads/init` is idempotent per (filename, sha256), so
re-initiating an upload that was interrupted returns the same `upload_id`, and
`/uploads/{id}/status` reports which chunks are already on disk — only the missing
chunks are re-sent.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

import requests

DEFAULT_CHUNK_SIZE = 32 * 1024 * 1024
DEFAULT_TIMEOUT = 30
POLL_FAST_WINDOW_SECONDS = 30
POLL_FAST_INTERVAL = 2
POLL_SLOW_INTERVAL = 8


class ApiError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ApiClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.auth = (username, password)
        self.timeout = timeout
        self.session = requests.Session()

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _raise_for_status(self, resp: requests.Response) -> None:
        if resp.status_code == 401:
            raise ApiError("Authentication failed — check username/password.", status_code=401)
        if not resp.ok:
            try:
                detail = resp.json().get("detail")
            except (ValueError, AttributeError):
                detail = resp.text
            raise ApiError(f"Server error {resp.status_code}: {detail}", status_code=resp.status_code)

    def health(self) -> dict:
        resp = self.session.get(self._url("/health"), timeout=self.timeout)
        self._raise_for_status(resp)
        return resp.json()

    def upload_file(self, path: Path, file_hash: str, params: dict | None = None,
                     chunk_size: int = DEFAULT_CHUNK_SIZE,
                     on_chunk: Callable[[int, int], None] | None = None) -> str:
        """Upload `path` (resuming if partially uploaded already) and enqueue a
        segmentation job. Returns the job_id."""
        sha256_hex = file_hash.split(":", 1)[1] if ":" in file_hash else file_hash
        total_size = path.stat().st_size

        resp = self.session.post(
            self._url("/uploads/init"), auth=self.auth, timeout=self.timeout,
            json={"filename": path.name, "total_size": total_size,
                  "sha256": sha256_hex, "chunk_size": chunk_size},
        )
        self._raise_for_status(resp)
        meta = resp.json()
        upload_id, total_chunks = meta["upload_id"], meta["total_chunks"]

        status_resp = self.session.get(
            self._url(f"/uploads/{upload_id}/status"), auth=self.auth, timeout=self.timeout)
        self._raise_for_status(status_resp)
        present = set(status_resp.json()["chunks_present"])

        with path.open("rb") as f:
            for index in range(total_chunks):
                if index in present:
                    if on_chunk:
                        on_chunk(index + 1, total_chunks)
                    continue
                f.seek(index * chunk_size)
                chunk = f.read(chunk_size)
                r = self.session.put(
                    self._url(f"/uploads/{upload_id}/chunk/{index}"),
                    auth=self.auth, data=chunk, timeout=self.timeout)
                self._raise_for_status(r)
                if on_chunk:
                    on_chunk(index + 1, total_chunks)

        complete_resp = self.session.post(
            self._url(f"/uploads/{upload_id}/complete"), auth=self.auth, timeout=self.timeout,
            json={"params": params or {}})
        self._raise_for_status(complete_resp)
        return complete_resp.json()["job_id"]

    def reorder_jobs(self, job_ids: list[str]) -> None:
        """Reprioritize whatever's still queued server-side to match
        `job_ids`'s order. Best-effort on the server (unknown/already-started
        ids are silently ignored), so nothing to return here."""
        resp = self.session.post(
            self._url("/jobs/reorder"), auth=self.auth, timeout=self.timeout,
            json={"order": job_ids})
        self._raise_for_status(resp)

    def get_job(self, job_id: str) -> dict[str, Any]:
        """One-shot, non-blocking status check — unlike `poll_job`, returns
        immediately with whatever the job's current status is instead of
        waiting for it to finish. Used to cheaply check on jobs left over from
        a previous session before committing to a slow full folder rescan."""
        resp = self.session.get(self._url(f"/jobs/{job_id}"), auth=self.auth, timeout=self.timeout)
        self._raise_for_status(resp)
        return resp.json()

    def poll_job(self, job_id: str, on_tick: Callable[[dict], None] | None = None,
                 timeout_seconds: float = 3600) -> dict[str, Any]:
        """Block (with backoff) until the job is done, returning its result dict."""
        start = time.time()
        while True:
            job = self.get_job(job_id)
            if on_tick:
                on_tick(job)

            if job["status"] == "done":
                return job["result"]
            if job["status"] == "error":
                raise ApiError(job.get("error") or "Segmentation job failed with no error detail.")

            if time.time() - start > timeout_seconds:
                raise ApiError(f"Job {job_id} timed out after {timeout_seconds:.0f}s.")
            elapsed = time.time() - start
            time.sleep(POLL_FAST_INTERVAL if elapsed < POLL_FAST_WINDOW_SECONDS else POLL_SLOW_INTERVAL)
