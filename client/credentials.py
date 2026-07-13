"""
credentials.py — optional "remember me" storage for the sign-in dialog.

Stored in the user's home directory (`~/.cellcounts/credentials.json`), separate
from folder state and never part of the repo. Written with owner-only file
permissions (chmod 600) on POSIX systems as a baseline precaution. This is a
lightweight local-convenience store for this app's one shared login, not a
hardened credential vault — appropriate for an internal research tool with a
single shared account, not a place to reuse a password from anywhere else.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

CREDENTIALS_PATH = Path.home() / ".cellcounts" / "credentials.json"


def load() -> tuple[str, str] | None:
    if not CREDENTIALS_PATH.exists():
        return None
    try:
        data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
        return data["username"], data["password"]
    except (OSError, ValueError, KeyError):
        return None


def save(username: str, password: str) -> None:
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(str(CREDENTIALS_PATH) + ".tmp")
    tmp_path.write_text(json.dumps({"username": username, "password": password}), encoding="utf-8")
    os.replace(tmp_path, CREDENTIALS_PATH)
    try:
        os.chmod(CREDENTIALS_PATH, stat.S_IRUSR | stat.S_IWUSR)  # owner read/write only
    except OSError:
        pass  # best-effort — e.g. unsupported on this filesystem


def clear() -> None:
    try:
        CREDENTIALS_PATH.unlink()
    except FileNotFoundError:
        pass
