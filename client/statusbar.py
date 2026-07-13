"""
statusbar.py — the bottom status bar + connection-state light.

Both widgets are plain Tk state holders with no threading logic of their own.
Network I/O in app.py runs on a background thread, so callers MUST marshal calls
into these widgets back onto the Tk main thread (e.g. via `root.after(0, ...)`) —
Tkinter widgets are not thread-safe.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

STATE_COLORS = {
    "idle": "#888888",
    "connecting": "#f5a623",
    "connected": "#2ecc71",
    "processing": "#3498db",
    "error": "#e74c3c",
}

STATE_LABELS = {
    "idle": "Idle",
    "connecting": "Connecting…",
    "connected": "Connected",
    "processing": "Processing…",
    "error": "Error",
}


class StatusBar(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)

        self.message_var = tk.StringVar(value="Ready")
        ttk.Label(self, textvariable=self.message_var, anchor="w").grid(
            row=0, column=0, sticky="ew", padx=(8, 4), pady=3)

        light_frame = ttk.Frame(self)
        light_frame.grid(row=0, column=1, sticky="e", padx=8, pady=3)

        self._light_canvas = tk.Canvas(light_frame, width=12, height=12, highlightthickness=0)
        self._light_canvas.pack(side="left", padx=(0, 4))
        self._light_id = self._light_canvas.create_oval(1, 1, 11, 11, fill=STATE_COLORS["idle"], outline="")

        self.state_var = tk.StringVar(value=STATE_LABELS["idle"])
        ttk.Label(light_frame, textvariable=self.state_var).pack(side="left")

    def set_message(self, text: str) -> None:
        self.message_var.set(text)

    def set_state(self, state: str) -> None:
        if state not in STATE_COLORS:
            raise ValueError(f"unknown status-light state {state!r}")
        self._light_canvas.itemconfig(self._light_id, fill=STATE_COLORS[state])
        self.state_var.set(STATE_LABELS[state])
