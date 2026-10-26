"""Shared helpers for upload and pipeline tests: synthetic TIFFs and a client-side
upload driver that speaks the same protocol as the browser."""

from __future__ import annotations

import gzip
import hashlib
import io
import json

import numpy as np
import tifffile
from django.urls import reverse

TEST_CHUNK = 64 * 1024


def tiff_bytes(seed: int = 1, shape=(240, 320), n_cells: int = 40) -> bytes:
    from processing.management.commands.seed_demo import _render_cells

    rng = np.random.default_rng(seed)
    h, w = shape
    centers = np.column_stack([rng.uniform(15, w - 15, n_cells), rng.uniform(15, h - 15, n_cells)])
    array = _render_cells(shape, centers, rng.uniform(5, 8, n_cells), rng.uniform(3000, 6000, n_cells), rng)
    buf = io.BytesIO()
    tifffile.imwrite(buf, array)
    return buf.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def post_json(client, url, body, **extra):
    return client.post(url, json.dumps(body), content_type="application/json", **extra)


def init(client, project, name, data, last_modified=1_700_000_000_000, replace=False):
    return post_json(client, reverse("uploads:init", args=[project.id]), {
        "filename": name, "size": len(data), "sha256": sha(data), "last_modified": last_modified,
        "replace": replace})


def put_chunk(client, upload_id, index, payload: bytes):
    return client.put(reverse("uploads:chunk", args=[upload_id, index]), payload,
                      content_type="application/octet-stream")


def upload(client, project, name, data, *, last_modified=1_700_000_000_000, replace=False, imported=None,
           skip_chunks=()):
    """Run init -> chunks -> complete like the browser does. Returns the final
    response (complete, or init's when nothing needed uploading)."""
    response = init(client, project, name, data, last_modified, replace)
    body = response.json()
    if response.status_code != 200 or body["action"] != "upload":
        return response
    size = body["chunk_size"]
    for index in range(body["total_chunks"]):
        if index in body["chunks_present"] or index in skip_chunks:
            continue
        assert put_chunk(client, body["upload_id"], index, data[index * size:(index + 1) * size]).status_code == 200
    url = reverse("uploads:complete", args=[body["upload_id"]])
    if imported is None:
        return client.post(url)
    payload = gzip.compress(json.dumps({"imported_cells": imported}).encode())
    return client.post(url, payload, content_type="application/json", HTTP_CONTENT_ENCODING="gzip")
