#!/usr/bin/env python3
"""
app.py — Cell Counts client entry point (Phase 1: orchestration only).

On launch: prompts for the server credentials, then a folder to review (same
folder-picker flow as the old cell_review.py). Scans the folder for
`{PREFIX}_{CCK,CHR,SNAP}.tif` files, hashes each one, and compares against
`cellcounts.json` in that folder — only new or changed files get uploaded to the
compute server (see SERVER.md) for segmentation; everything else is skipped. Results
are merged into `cellcounts.json` as each job completes, so progress survives a
mid-run interruption.

There is no review/editing UI yet — that's Phase 2, built on top of the manifest
this phase produces. This alone replaces the old count_cells.py end to end for all
three channels.

Network I/O runs on a background thread; all Tk widget updates from that thread are
marshalled onto the main thread via `after()`.
"""

from __future__ import annotations

import os
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from api_client import ApiClient, ApiError
from manifest import Manifest, hash_file, scan_folder
from statusbar import StatusBar

DEFAULT_SERVER_URL = os.environ.get("CELLCOUNTS_SERVER_URL", "https://research.matthewbolding.com")


class CellCountsApp(tk.Tk):
    def __init__(self, server_url: str = DEFAULT_SERVER_URL):
        super().__init__()
        self.title("Cell Counts")
        self.geometry("900x600")
        self.minsize(640, 400)

        self.server_url = server_url
        self.client: ApiClient | None = None
        self.folder: Path | None = None

        self._build_ui()
        self.after(200, self._startup)

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        self._build_menu()

        log_frame = ttk.Frame(self)
        log_frame.pack(fill="both", expand=True, padx=8, pady=8)
        self.log_text = tk.Text(log_frame, state="disabled", wrap="word")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        self.statusbar = StatusBar(self)
        self.statusbar.pack(side="bottom", fill="x")

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Open Folder...", command=self._open_folder_clicked, accelerator="Ctrl+O")
        filemenu.add_separator()
        filemenu.add_command(label="Quit", command=self.destroy)
        menubar.add_cascade(label="File", menu=filemenu)
        self.config(menu=menubar)
        self.bind("<Control-o>", lambda e: self._open_folder_clicked())

    # ------------------------------------------------------------------ #
    # UI-thread-safe helpers (safe to call from the background worker)
    # ------------------------------------------------------------------ #
    def _log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def ui_log(self, text: str) -> None:
        self.after(0, self._log, text)

    def ui_status(self, text: str) -> None:
        self.after(0, self.statusbar.set_message, text)

    def ui_light(self, state: str) -> None:
        self.after(0, self.statusbar.set_state, state)

    def ui_error(self, title: str, text: str) -> None:
        self.after(0, lambda: messagebox.showerror(title, text))

    # ------------------------------------------------------------------ #
    # Startup: credentials, then a folder
    # ------------------------------------------------------------------ #
    def _startup(self) -> None:
        username = simpledialog.askstring("Cell Counts — Sign in", "Username:", parent=self)
        if not username:
            self.destroy()
            return
        password = simpledialog.askstring("Cell Counts — Sign in", "Password:", parent=self, show="*")
        if password is None:
            self.destroy()
            return
        self.client = ApiClient(self.server_url, username, password)
        self._open_folder_clicked()

    def _open_folder_clicked(self) -> None:
        chosen = filedialog.askdirectory(title="Select the folder of TIFF images to review")
        if not chosen:
            return
        self.folder = Path(chosen)
        self._log(f"Folder: {self.folder}")
        threading.Thread(target=self._process_folder, args=(self.folder,), daemon=True).start()

    # ------------------------------------------------------------------ #
    # Background worker: hash, skip up-to-date, upload+process the rest
    # ------------------------------------------------------------------ #
    def _process_folder(self, folder: Path) -> None:
        self.ui_status("Scanning folder...")
        manifest = Manifest(folder)
        recognized, skipped = scan_folder(folder)

        if skipped:
            self.ui_log(f"Skipped {len(skipped)} file(s) with unrecognized names:")
            for p in skipped:
                self.ui_log(f"  - {p.name}")

        if not recognized:
            self.ui_status("No {PREFIX}_{CCK,CHR,SNAP}.tif files found in this folder.")
            self.ui_light("idle")
            return

        self.ui_log(f"Found {len(recognized)} recognized image(s); hashing...")
        to_process = []
        for i, sf in enumerate(recognized, 1):
            self.ui_status(f"Hashing {sf.path.name} ({i}/{len(recognized)})")
            file_hash = hash_file(sf.path)
            if manifest.needs_processing(sf.path.name, file_hash):
                to_process.append((sf, file_hash))

        up_to_date = len(recognized) - len(to_process)
        self.ui_log(f"{up_to_date} file(s) already up to date; {len(to_process)} need processing.")

        if not to_process:
            self.ui_status("All images already processed.")
            self.ui_light("idle")
            return

        self.ui_light("connecting")
        self.ui_status(f"Connecting to {self.server_url}...")
        try:
            health = self.client.health()
        except ApiError as exc:
            self.ui_light("error")
            self.ui_status(f"Could not reach server: {exc}")
            self.ui_log(f"ERROR connecting to server: {exc}")
            self.ui_error("Connection failed", str(exc))
            return
        self.ui_light("connected")
        self.ui_log(f"Connected to {self.server_url} "
                     f"(gpu={health.get('gpu')}, model_loaded={health.get('model_loaded')}).")

        n_ok = n_err = 0
        for i, (sf, file_hash) in enumerate(to_process, 1):
            label = f"{sf.path.name} ({i}/{len(to_process)})"
            self.ui_status(f"Uploading {label}...")
            self.ui_light("processing")
            try:
                job_id = self.client.upload_file(
                    sf.path, file_hash,
                    on_chunk=lambda done, total, label=label: self.ui_status(
                        f"Uploading {label}: chunk {done}/{total}"),
                )
                self.ui_status(f"Waiting for server to process {label}...")
                result = self.client.poll_job(
                    job_id,
                    on_tick=lambda job, label=label: self.ui_status(f"{job['status'].title()}: {label}"),
                )
                manifest.record_result(
                    sf.path.name, sf.prefix, sf.channel, file_hash,
                    result["width"], result["height"], result["params"], result["cells"],
                )
                n_kept = sum(1 for c in result["cells"] if c["status"] == "kept")
                self.ui_log(f"{sf.path.name}: {n_kept} cells kept "
                             f"({len(result['cells']) - n_kept} filtered).")
                n_ok += 1
            except ApiError as exc:
                manifest.record_error(sf.path.name, sf.prefix, sf.channel, file_hash, str(exc))
                self.ui_log(f"ERROR processing {sf.path.name}: {exc}")
                n_err += 1

        self.ui_light("connected" if n_err == 0 else "error")
        self.ui_status(f"Done. {n_ok} processed, {n_err} failed, {up_to_date} already up to date.")
        self.ui_log(f"Finished: {n_ok} processed, {n_err} failed.")


def main(argv=None) -> int:
    app = CellCountsApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
