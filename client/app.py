#!/usr/bin/env python3
"""
app.py — Cell Counts client entry point.

On launch: prompts for the server credentials (one combined dialog), then a folder
to review. Scans the folder for `{PREFIX}_{CCK,CHR,SNAP}.tif` files and immediately
shows the review panel (review.py) — browse samples/channels, toggle mask/outline,
draw/delete cells, recolor channels — rather than making the user wait behind a
processing log. Hashing (compared against `cellcounts.json`) and upload/segmentation
of anything new or changed then run on a background thread via a `ProcessingQueue`
(processing_queue.py), which the review panel polls to show live per-channel
readiness dots and a reorderable pending-work list. The log stays reachable via
View > Show Log for diagnosing upload issues.

Network I/O runs on a background thread; all Tk widget updates from that thread are
marshalled onto the main thread via `after()`.
"""

from __future__ import annotations

import os
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import credentials
import state
from api_client import ApiClient, ApiError
from manifest import Manifest, ScannedFile, hash_file, scan_folder
from processing_queue import QUEUE_STATE_NAME, ProcessingQueue, QueueItem, load_persisted_order
from review import ReviewPanel
from statusbar import StatusBar

DEFAULT_SERVER_URL = os.environ.get("CELLCOUNTS_SERVER_URL", "https://research.matthewbolding.com")


class LoginDialog(tk.Toplevel):
    """One modal form for username + password, instead of two sequential
    simpledialog popups — asking for both fields separately felt clunky."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Cell Counts — Sign in")
        self.resizable(False, False)
        self.result: tuple[str, str, bool] | None = None

        form = ttk.Frame(self, padding=16)
        form.pack(fill="both", expand=True)

        ttk.Label(form, text="Username:").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.username_entry = ttk.Entry(form, width=28)
        self.username_entry.grid(row=0, column=1, pady=(0, 8))

        ttk.Label(form, text="Password:").grid(row=1, column=0, sticky="w")
        self.password_entry = ttk.Entry(form, width=28, show="*")
        self.password_entry.grid(row=1, column=1)

        self.remember_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="Remember me on this computer", variable=self.remember_var).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

        last_folder = state.get_last_folder()
        self.reopen_var = tk.BooleanVar(value=state.get_reopen_last_folder() if last_folder else False)
        reopen_cb = ttk.Checkbutton(form, text="Open the same folder as last time", variable=self.reopen_var)
        if last_folder is None:
            reopen_cb.configure(state="disabled")
        reopen_cb.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        if last_folder is not None:
            ttk.Label(form, text=str(last_folder), foreground="#666").grid(
                row=4, column=0, columnspan=2, sticky="w", padx=(20, 0))

        remembered = credentials.load()
        if remembered is not None:
            username, password = remembered
            self.username_entry.insert(0, username)
            self.password_entry.insert(0, password)
            self.remember_var.set(True)

        buttons = ttk.Frame(form)
        buttons.grid(row=5, column=0, columnspan=2, pady=(16, 0), sticky="e")
        ttk.Button(buttons, text="Cancel", command=self._on_cancel).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="Sign In", command=self._on_submit, default="active").pack(side="right")

        self.bind("<Return>", lambda e: self._on_submit())
        self.bind("<Escape>", lambda e: self._on_cancel())
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self.transient(parent)
        (self.password_entry if remembered else self.username_entry).focus_set()
        self.update_idletasks()
        self.grab_set()
        self.wait_window(self)

    def _on_submit(self) -> None:
        username = self.username_entry.get().strip()
        password = self.password_entry.get()
        if not username or not password:
            return
        if self.remember_var.get():
            credentials.save(username, password)
        else:
            credentials.clear()
        state.save_reopen_last_folder(self.reopen_var.get())
        self.result = (username, password, self.reopen_var.get())
        self.destroy()

    def _on_cancel(self) -> None:
        self.result = None
        self.destroy()


class CellCountsApp(tk.Tk):
    def __init__(self, server_url: str = DEFAULT_SERVER_URL):
        super().__init__()
        self.title("Cell Counts")
        self.geometry("1100x700")
        self.minsize(800, 500)

        self.server_url = server_url
        self.client: ApiClient | None = None
        self.folder: Path | None = None
        self.manifest: Manifest | None = None
        self.queue: ProcessingQueue | None = None
        self.review_panel: ReviewPanel | None = None

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._startup)

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        self._build_menu()

        self.content_frame = ttk.Frame(self)
        self.content_frame.pack(fill="both", expand=True)

        self.log_frame = ttk.Frame(self.content_frame)
        self.log_text = tk.Text(self.log_frame, state="disabled", wrap="word")
        log_scroll = ttk.Scrollbar(self.log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        log_scroll.pack(side="right", fill="y", pady=8)
        self.log_frame.pack(fill="both", expand=True)

        self.statusbar = StatusBar(self)
        self.statusbar.pack(side="bottom", fill="x")

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        self.filemenu = tk.Menu(menubar, tearoff=0)
        self.filemenu.add_command(label="Open Folder...", command=self._open_folder_clicked,
                                   accelerator="Ctrl+O")
        self.filemenu.add_separator()
        self.filemenu.add_command(label="Export Data...", command=self._export_data_clicked, state="disabled")
        self.filemenu.add_separator()
        self.filemenu.add_command(label="Quit", command=self._on_close)
        menubar.add_cascade(label="File", menu=self.filemenu)

        self.viewmenu = tk.Menu(menubar, tearoff=0)
        self.viewmenu.add_command(label="Show Log", command=self._show_log)
        self.viewmenu.add_command(label="Show Review", command=self._show_review, state="disabled")
        menubar.add_cascade(label="View", menu=self.viewmenu)

        self.config(menu=menubar)
        self.bind("<Control-o>", lambda e: self._open_folder_clicked())

    def _show_log(self) -> None:
        if self.review_panel is not None:
            self.review_panel.pack_forget()
        self.log_frame.pack(fill="both", expand=True)

    def _show_review(self) -> None:
        if self.review_panel is None:
            return
        self.log_frame.pack_forget()
        self.review_panel.pack(fill="both", expand=True)

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
        login_result = LoginDialog(self).result
        if login_result is None:
            self.destroy()
            return
        username, password, reopen_last = login_result
        self.client = ApiClient(self.server_url, username, password)

        last_folder = state.get_last_folder()
        if reopen_last and last_folder is not None and last_folder.is_dir():
            self._open_folder(last_folder)
        else:
            self._open_folder_clicked()

    def _open_folder_clicked(self) -> None:
        chosen = filedialog.askdirectory(title="Select the folder of TIFF images to review")
        if not chosen:
            return
        self._open_folder(Path(chosen))

    def _open_folder(self, folder: Path) -> None:
        if self.review_panel is not None:
            self.review_panel.close()
            self.review_panel.destroy()
            self.review_panel = None
            self.viewmenu.entryconfig("Show Review", state="disabled")
            self.filemenu.entryconfig("Export Data...", state="disabled")
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

        self.folder = folder
        self._log(f"Folder: {self.folder}")
        state.save_last_folder(self.folder)

        # Manifest load + folder scan are both cheap (one small JSON read, one
        # rglob + filename-regex pass — no hashing, no per-image I/O), so we do
        # them synchronously and put the review screen up immediately rather than
        # making the user watch the log while the (much slower) hash/upload/segment
        # pass runs on a background thread below.
        manifest = Manifest(self.folder)
        recognized, skipped = scan_folder(self.folder)
        self.queue = ProcessingQueue(persist_path=self.folder / QUEUE_STATE_NAME)
        self._show_review_panel(manifest, recognized, self.queue)

        if skipped:
            self.ui_log(f"Skipped {len(skipped)} file(s) with unrecognized names:")
            for p in skipped:
                self.ui_log(f"  - {p.name}")

        threading.Thread(target=self._process_folder, args=(self.folder, manifest, recognized, self.queue),
                          daemon=True).start()

    def _on_close(self) -> None:
        if self.review_panel is not None:
            self.review_panel.close()
        self.destroy()

    def _show_review_panel(self, manifest: Manifest, recognized: list[ScannedFile],
                            queue: ProcessingQueue) -> None:
        if self.review_panel is not None:
            self.review_panel.close()
            self.review_panel.destroy()
        self.manifest = manifest
        self.review_panel = ReviewPanel(self.content_frame, self.folder, manifest, self.statusbar,
                                         recognized, queue, self.client)
        self.viewmenu.entryconfig("Show Review", state="normal")
        self.filemenu.entryconfig("Export Data...", state="normal")
        self._show_review()

    def _export_data_clicked(self) -> None:
        if self.review_panel is not None:
            self.review_panel.export_data()

    # ------------------------------------------------------------------ #
    # Background worker: hash, skip up-to-date, then drain the queue
    # ------------------------------------------------------------------ #
    def _process_folder(self, folder: Path, manifest: Manifest, recognized: list[ScannedFile],
                         queue: ProcessingQueue) -> None:
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
                to_process.append(QueueItem(sf=sf, file_hash=file_hash))

        up_to_date = len(recognized) - len(to_process)
        self.ui_log(f"{up_to_date} file(s) already up to date; {len(to_process)} need processing.")

        if not to_process:
            self.ui_status("All images already processed.")
            self.ui_light("idle")
            return

        # Restore a previously-saved queue order/paused-state for this folder, if
        # any — the hash scan above has no memory of how the user last arranged
        # the queue, so without this every relaunch would reset back to plain
        # scan order. Files not mentioned in the saved order (new since last time)
        # sort after everything that was, in their natural scan order.
        persisted = load_persisted_order(queue.persist_path) if queue.persist_path else None
        if persisted:
            order_index = {fn: i for i, fn in enumerate(persisted.get("order", []))}
            to_process.sort(key=lambda item: order_index.get(item.filename, len(order_index)))
            if not persisted.get("running", True):
                queue.stop()

        queue.enqueue(to_process)

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
        while (item := queue.pop_next()) is not None:
            sf, file_hash = item.sf, item.file_hash
            label = f"{sf.path.name} ({n_ok + n_err + 1}/{len(to_process)})"
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
            finally:
                queue.complete(item)

        self.ui_light("connected" if n_err == 0 else "error")
        self.ui_status(f"Done. {n_ok} processed, {n_err} failed, {up_to_date} already up to date.")
        self.ui_log(f"Finished: {n_ok} processed, {n_err} failed.")


def main(argv=None) -> int:
    app = CellCountsApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
