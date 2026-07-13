"""
state.py — per-folder UI state (zoom, mask/outline mode, channel colors, last
image viewed), persisted separately from `cellcounts.json` so frequent UI-only
writes never touch the (much larger, per-image-sidecar) data files.

`zoom_multiplier` is relative to whatever image is currently on screen's own
fit-to-window scale, not an absolute pixel scale — the corpus's image dimensions
vary more than 40x, so an absolute remembered scale wouldn't generalize sensibly
from a small sample to a huge one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

STATE_DIR = Path.home() / ".cellcounts"
STATE_PATH = STATE_DIR / "state.json"

DEFAULT_CHANNEL_COLORS = {"SNAP": "#ff0000", "CCK": "#00ff00", "CHR": "#00ffff"}

DEFAULT_FOLDER_STATE = {
    "last_image": None,
    "render_mode": "outline",  # "outline" | "mask"
    "channel_colors": dict(DEFAULT_CHANNEL_COLORS),
    "zoom_multiplier": 1.0,
}


def _load_all() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_all(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(str(STATE_PATH) + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, STATE_PATH)


def get_folder_state(folder: Path) -> dict:
    stored = _load_all().get(str(folder.resolve()), {})
    state = dict(DEFAULT_FOLDER_STATE)
    state.update(stored)
    state["channel_colors"] = {**DEFAULT_CHANNEL_COLORS, **stored.get("channel_colors", {})}
    return state


def save_folder_state(folder: Path, state: dict) -> None:
    all_state = _load_all()
    all_state[str(folder.resolve())] = state
    _save_all(all_state)


# Two extra top-level keys in the same file, alongside the folder-path-keyed
# entries above — safe from collision since neither POSIX nor Windows absolute
# paths start with "_".
_LAST_FOLDER_KEY = "_last_folder"
_REOPEN_LAST_FOLDER_KEY = "_reopen_last_folder"


def get_last_folder() -> Path | None:
    value = _load_all().get(_LAST_FOLDER_KEY)
    return Path(value) if value else None


def save_last_folder(folder: Path) -> None:
    all_state = _load_all()
    all_state[_LAST_FOLDER_KEY] = str(folder.resolve())
    _save_all(all_state)


def get_reopen_last_folder() -> bool:
    return bool(_load_all().get(_REOPEN_LAST_FOLDER_KEY, False))


def save_reopen_last_folder(value: bool) -> None:
    all_state = _load_all()
    all_state[_REOPEN_LAST_FOLDER_KEY] = value
    _save_all(all_state)


_LAST_SERVER_URL_KEY = "_last_server_url"


def get_last_server_url() -> str:
    return _load_all().get(_LAST_SERVER_URL_KEY, "")


def save_last_server_url(url: str) -> None:
    all_state = _load_all()
    all_state[_LAST_SERVER_URL_KEY] = url
    _save_all(all_state)
