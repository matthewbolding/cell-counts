"""
ws_client.py — persistent connection to the server's /ws/jobs endpoint.

Segmentation results used to be discovered by polling `GET /jobs/{id}` on a
timer; this replaces that with push: the server notifies every connected
client the instant a job's status actually changes (see server/jobs.py's
`_on_status_change` hook / server/app.py's `_broadcast_status_change`), so the
client learns about it immediately instead of within some polling interval.

Runs on its own daemon thread using `websockets.sync.client` (a plain
blocking connection, not asyncio) to fit this codebase's existing
thread-based style rather than introducing an event loop. Auto-reconnects
with exponential backoff on any disconnect (network blip, server restart,
Cloudflare's proxy recycling an idle-but-should-still-be-alive connection,
etc.) — `on_connect` fires once per successful (re)connection so the caller
(see job_router.py's `resync()`) can catch up on anything possibly missed
while disconnected, since messages sent while nobody's connected are simply
dropped server-side, not queued.

A failure inside `on_event`/`on_connect` itself is swallowed rather than
tearing down an otherwise-healthy connection over it -- the same "one bad
item doesn't take down the whole thread" precedent used elsewhere in this
project (e.g. app.py's background-thread crash handling).
"""

from __future__ import annotations

import base64
import json
import threading
import time
from typing import Callable

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

BACKOFF_INITIAL_SECONDS = 1
BACKOFF_MAX_SECONDS = 30


def _to_ws_url(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://"):] + "/ws/jobs"
    if base_url.startswith("http://"):
        return "ws://" + base_url[len("http://"):] + "/ws/jobs"
    return base_url + "/ws/jobs"


class JobEventsClient:
    def __init__(self, base_url: str, username: str, password: str,
                 on_event: Callable[[dict], None], on_connect: Callable[[], None]) -> None:
        self._ws_url = _to_ws_url(base_url)
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}"}
        self._on_event = on_event
        self._on_connect = on_connect
        self._stop_event = threading.Event()
        self._conn_lock = threading.Lock()
        self._conn = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Signals the reconnect loop to stop and closes whatever connection
        is currently open, to unblock a blocking recv()."""
        self._stop_event.set()
        with self._conn_lock:
            conn = self._conn
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _run(self) -> None:
        backoff = BACKOFF_INITIAL_SECONDS
        while not self._stop_event.is_set():
            try:
                with connect(self._ws_url, additional_headers=self._headers) as ws:
                    with self._conn_lock:
                        self._conn = ws
                    backoff = BACKOFF_INITIAL_SECONDS
                    try:
                        self._on_connect()
                    except Exception:
                        pass
                    while not self._stop_event.is_set():
                        message = ws.recv()
                        try:
                            self._on_event(json.loads(message))
                        except Exception:
                            pass  # one bad/unexpected message shouldn't drop the connection
            except (WebSocketException, OSError):
                pass  # reconnect below
            finally:
                with self._conn_lock:
                    self._conn = None

            if self._stop_event.is_set():
                return
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
