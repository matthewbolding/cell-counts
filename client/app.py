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
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import credentials
import state
from api_client import ApiClient, ApiError
from job_router import JobRouter, RunBatch
from manifest import Manifest, ScannedFile, hash_file, scan_folder
from processing_queue import QUEUE_STATE_NAME, ProcessingQueue, QueueItem, load_persisted_order
from review import ReviewPanel
from statusbar import StatusBar
from ws_client import JobEventsClient

DEFAULT_SERVER_URL = os.environ.get("CELLCOUNTS_SERVER_URL", "")
# Fixed positions of the Process menu's two entries -- entryconfig by label
# stops working once the label itself has been changed (see _build_menu),
# so every reference to these entries goes through the index instead.
PROCESSMENU_UPLOADS_INDEX = 0
PROCESSMENU_SEGMENTING_INDEX = 1


class LoginDialog(tk.Toplevel):
    """One modal form for the server endpoint, username, and password, instead of
    several sequential simpledialog popups — asking for each separately felt
    clunky."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Cell Counts — Sign in")
        self.resizable(False, False)
        self.result: tuple[str, str, str, bool] | None = None

        form = ttk.Frame(self, padding=16)
        form.pack(fill="both", expand=True)

        ttk.Label(form, text="Server:").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.server_entry = ttk.Entry(form, width=32)
        self.server_entry.grid(row=0, column=1, pady=(0, 8))
        default_server = state.get_last_server_url() or DEFAULT_SERVER_URL
        if default_server:
            self.server_entry.insert(0, default_server)

        ttk.Label(form, text="Username:").grid(row=1, column=0, sticky="w", pady=(0, 8))
        self.username_entry = ttk.Entry(form, width=32)
        self.username_entry.grid(row=1, column=1, pady=(0, 8))

        ttk.Label(form, text="Password:").grid(row=2, column=0, sticky="w")
        self.password_entry = ttk.Entry(form, width=32, show="*")
        self.password_entry.grid(row=2, column=1)

        self.remember_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="Remember me on this computer", variable=self.remember_var).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        last_folder = state.get_last_folder()
        self.reopen_var = tk.BooleanVar(value=state.get_reopen_last_folder() if last_folder else False)
        reopen_cb = ttk.Checkbutton(form, text="Open the same folder as last time", variable=self.reopen_var)
        if last_folder is None:
            reopen_cb.configure(state="disabled")
        reopen_cb.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
        if last_folder is not None:
            ttk.Label(form, text=str(last_folder), foreground="#666").grid(
                row=5, column=0, columnspan=2, sticky="w", padx=(20, 0))

        remembered = credentials.load()
        if remembered is not None:
            username, password = remembered
            self.username_entry.insert(0, username)
            self.password_entry.insert(0, password)
            self.remember_var.set(True)

        buttons = ttk.Frame(form)
        buttons.grid(row=6, column=0, columnspan=2, pady=(16, 0), sticky="e")
        ttk.Button(buttons, text="Cancel", command=self._on_cancel).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="Sign In", command=self._on_submit, default="active").pack(side="right")

        self.bind("<Return>", lambda e: self._on_submit())
        self.bind("<Escape>", lambda e: self._on_cancel())
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self.transient(parent)
        if not default_server:
            initial_focus = self.server_entry
        elif remembered:
            initial_focus = self.password_entry
        else:
            initial_focus = self.username_entry
        initial_focus.focus_set()
        self.update_idletasks()
        self.grab_set()
        self.wait_window(self)

    def _on_submit(self) -> None:
        server_url = self.server_entry.get().strip().rstrip("/")
        username = self.username_entry.get().strip()
        password = self.password_entry.get()
        if not server_url or not username or not password:
            return
        if self.remember_var.get():
            credentials.save(username, password)
        else:
            credentials.clear()
        state.save_last_server_url(server_url)
        state.save_reopen_last_folder(self.reopen_var.get())
        self.result = (server_url, username, password, self.reopen_var.get())
        self.destroy()

    def _on_cancel(self) -> None:
        self.result = None
        self.destroy()


class CellCountsApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Cell Counts")
        self.geometry("1100x700")
        self.minsize(800, 500)

        self.server_url: str | None = None
        self.client: ApiClient | None = None
        self.job_router: JobRouter | None = None
        self.job_events: JobEventsClient | None = None
        self.folder: Path | None = None
        self.manifest: Manifest | None = None
        self.queue: ProcessingQueue | None = None
        self.review_panel: ReviewPanel | None = None
        # Folders with an active background processing thread — guards against
        # starting a second one for the same folder (e.g. re-picking it, or
        # "open the same folder as last time" racing an already-running pass),
        # which would otherwise let two threads both decide the same file
        # "needs processing" before either records anything.
        self._active_processing_folders: set[Path] = set()

        # Shared with ReviewPanel (passed into its constructor) so the
        # Process menu here and the sidebar's combined Start/Stop button stay
        # in sync for free -- both surfaces read/write the same two Vars
        # instead of needing an explicit sync step.
        self.pause_uploads_var = tk.BooleanVar(value=False)
        self.pause_segmenting_var = tk.BooleanVar(value=False)

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

        # Independent counterparts to the sidebar's combined Start/Stop button
        # (review.py's _toggle_queue_running) -- same two underlying
        # mechanisms (queue.stop/start for uploads, client.pause_jobs/
        # resume_jobs for segmentation), just controllable separately. Plain
        # commands whose *label* swaps Stop<->Start (not checkbuttons -- a
        # checkmark would say less at a glance than the label itself naming
        # the action a click performs), referenced by the fixed indices below
        # (PROCESSMENU_UPLOADS_INDEX/_SEGMENTING_INDEX) since entryconfig by
        # label stops working once the label's been changed once. A trace on
        # each Var (not a call at every mutation site) is what keeps the
        # label right regardless of whether the change came from here or
        # from the sidebar button -- both write to the same two Vars.
        self.processmenu = tk.Menu(menubar, tearoff=0)
        self.processmenu.add_command(label="Stop Uploads", command=self._on_pause_uploads_toggle,
                                      state="disabled")
        self.processmenu.add_command(label="Stop Segmenting", command=self._on_pause_segmenting_toggle,
                                      state="disabled")
        menubar.add_cascade(label="Process", menu=self.processmenu)
        self.pause_uploads_var.trace_add("write", self._sync_pause_uploads_label)
        self.pause_segmenting_var.trace_add("write", self._sync_pause_segmenting_label)

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
        server_url, username, password, reopen_last = login_result
        self.server_url = server_url
        self.client = ApiClient(self.server_url, username, password)
        self.job_router = JobRouter(self.client, on_log=self.ui_log)
        self.job_events = JobEventsClient(self.server_url, username, password,
                                           on_event=self.job_router.handle_event,
                                           on_connect=self.job_router.resync)
        self.job_events.start()

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

        if folder in self._active_processing_folders:
            self.ui_log("This folder is already being processed in the background from an earlier "
                        "open — not starting a second pass.")
            return
        self._active_processing_folders.add(folder)
        threading.Thread(target=self._process_folder, args=(folder, manifest, recognized, self.queue),
                          daemon=True).start()

    def _on_close(self) -> None:
        if self.review_panel is not None:
            self.review_panel.close()
        if self.job_events is not None:
            self.job_events.stop()
        self.destroy()

    def _show_review_panel(self, manifest: Manifest, recognized: list[ScannedFile],
                            queue: ProcessingQueue) -> None:
        if self.review_panel is not None:
            self.review_panel.close()
            self.review_panel.destroy()
        self.manifest = manifest
        self.review_panel = ReviewPanel(self.content_frame, self.folder, manifest, self.statusbar,
                                         recognized, queue, self.client,
                                         self.pause_uploads_var, self.pause_segmenting_var)
        self.viewmenu.entryconfig("Show Review", state="normal")
        self.filemenu.entryconfig("Export Data...", state="normal")
        self.processmenu.entryconfig(PROCESSMENU_UPLOADS_INDEX, state="normal")
        self.processmenu.entryconfig(PROCESSMENU_SEGMENTING_INDEX, state="normal")
        self._show_review()

    def _sync_pause_uploads_label(self, *_args) -> None:
        label = "Start Uploads" if self.pause_uploads_var.get() else "Stop Uploads"
        self.processmenu.entryconfig(PROCESSMENU_UPLOADS_INDEX, label=label)

    def _sync_pause_segmenting_label(self, *_args) -> None:
        label = "Start Segmenting" if self.pause_segmenting_var.get() else "Stop Segmenting"
        self.processmenu.entryconfig(PROCESSMENU_SEGMENTING_INDEX, label=label)

    def _on_pause_uploads_toggle(self) -> None:
        if self.queue is None:
            return
        pause = not self.pause_uploads_var.get()
        self.pause_uploads_var.set(pause)
        if pause:
            self.queue.stop()
        else:
            self.queue.start()

    def _on_pause_segmenting_toggle(self) -> None:
        if self.client is None:
            return
        pause = not self.pause_segmenting_var.get()
        self.pause_segmenting_var.set(pause)
        threading.Thread(target=self._do_pause_segmenting, args=(pause,), daemon=True).start()

    def _do_pause_segmenting(self, pause: bool) -> None:
        try:
            if pause:
                self.client.pause_jobs()
            else:
                self.client.resume_jobs()
        except ApiError as exc:
            verb = "pause" if pause else "resume"
            self.ui_log(f"Couldn't {verb} server segmentation: {exc}")

    def _export_data_clicked(self) -> None:
        if self.review_panel is not None:
            self.review_panel.export_data()

    # ------------------------------------------------------------------ #
    # Background worker: hash, skip up-to-date, then drain the queue
    # ------------------------------------------------------------------ #
    def _process_folder(self, folder: Path, manifest: Manifest, recognized: list[ScannedFile],
                         queue: ProcessingQueue) -> None:
        try:
            self._run_processing(folder, manifest, recognized, queue)
        except Exception as exc:  # noqa: BLE001 — this is a background thread; an
            # uncaught exception here would otherwise just die silently (no error
            # dialog, no log line, no status update) leaving every dot frozen
            # wherever it was, indistinguishable from things still genuinely being
            # in progress. Surface it instead of losing it.
            traceback.print_exc()
            self.ui_light("error")
            self.ui_status(f"Processing stopped unexpectedly: {exc}")
            self.ui_log(f"ERROR: background processing crashed: {exc}")
        finally:
            self._active_processing_folders.discard(folder)

    def _reconcile_outstanding_jobs(self, manifest: Manifest, recognized: list[ScannedFile]) -> None:
        """One-shot status check (not the blocking `poll_job`) for every
        recognized file the manifest still has marked "processing" from a
        previous session — updates the manifest immediately for anything the
        server already finished. Best-effort: called only after the health
        check above already confirmed the server's reachable, and any failure
        here (a single request, not a job) just leaves that file to be handled
        by the normal resume-and-poll path further down, so this can't make
        things worse, only faster when it works.
        """
        outstanding = [sf for sf in recognized
                       if manifest.data["images"].get(sf.path.name, {}).get("status") == "processing"]
        if not outstanding:
            return

        self.ui_status(f"Checking on {len(outstanding)} job(s) from a previous session...")
        reconciled = 0
        for sf in outstanding:
            entry = manifest.data["images"][sf.path.name]
            job_id = entry.get("job_id")
            prefix, channel, file_hash = entry.get("prefix"), entry.get("channel"), entry.get("hash")
            if not job_id or not prefix or not channel or not file_hash:
                continue  # malformed/legacy entry -- leave as "processing", the resume/poll path below will sort it out
            try:
                job = self.client.get_job(job_id)
            except ApiError:
                continue  # left as "processing" -- the normal resume/poll path below will sort it out
            try:
                if job["status"] == "done":
                    result = job["result"]
                    manifest.record_result(sf.path.name, prefix, channel, file_hash,
                                            result["width"], result["height"], result["params"], result["cells"])
                    reconciled += 1
                elif job["status"] == "error":
                    manifest.record_error(sf.path.name, prefix, channel, file_hash,
                                           job.get("error") or "job failed")
                    reconciled += 1
            except (KeyError, TypeError) as exc:
                # Unexpected job payload shape -- don't let one odd entry abort
                # reconciling the rest, and don't let it kill the whole run either.
                self.ui_log(f"{sf.path.name}: couldn't reconcile previous job ({exc}); will resume normally.")

        if reconciled:
            self.ui_log(f"{reconciled}/{len(outstanding)} file(s) had already finished on the server "
                         "while this app was closed.")

    def _run_processing(self, folder: Path, manifest: Manifest, recognized: list[ScannedFile],
                         queue: ProcessingQueue) -> None:
        if not recognized:
            self.ui_status("No {PREFIX}_{CCK,CHR,SNAP}.tif files found in this folder.")
            self.ui_light("idle")
            return

        # Connect first, before doing anything slow. This also means we don't
        # spend minutes re-hashing a large folder (every recognized file gets
        # read in full for its SHA256 below) only to discover the server was
        # unreachable the whole time.
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

        # Reconcile pass: the manifest may still say "processing" for files
        # whose jobs actually finished on the server while this app was
        # closed — the hash-scan below is what would normally discover that,
        # but it reads every recognized file in full to compute its SHA256
        # first, which can take a long time over a large folder. Until that
        # finishes, the sidebar would keep showing "processing" for files that
        # have actually been done for a while. This is a handful of cheap
        # status-only requests (no file I/O), so it runs first and updates the
        # manifest immediately for anything the server already resolved.
        self._reconcile_outstanding_jobs(manifest, recognized)

        self.ui_log(f"Found {len(recognized)} recognized image(s); hashing...")
        to_process: list[QueueItem] = []
        resumed: list[tuple[ScannedFile, str, str]] = []  # still genuinely outstanding after reconciling
        for i, sf in enumerate(recognized, 1):
            self.ui_status(f"Hashing {sf.path.name} ({i}/{len(recognized)})")
            file_hash = hash_file(sf.path)
            pending_job_id = manifest.pending_job(sf.path.name, file_hash)
            if pending_job_id:
                resumed.append((sf, file_hash, pending_job_id))
            elif manifest.needs_processing(sf.path.name, file_hash):
                to_process.append(QueueItem(sf=sf, file_hash=file_hash))

        up_to_date = len(recognized) - len(to_process) - len(resumed)
        resumed_note = f"; {len(resumed)} resuming from a previous session" if resumed else ""
        self.ui_log(f"{up_to_date} file(s) already up to date; {len(to_process)} need processing{resumed_note}.")

        if not to_process and not resumed:
            self.ui_status("All images already processed.")
            self.ui_light("idle")
            return

        if to_process:
            # Restore a previously-saved queue order/paused-state for this
            # folder, if any — the hash scan above has no memory of how the
            # user last arranged the queue, so without this every relaunch
            # would reset back to plain scan order. Files not mentioned in the
            # saved order (new since last time) sort after everything that
            # was, in their natural scan order.
            persisted = load_persisted_order(queue.persist_path) if queue.persist_path else None
            if persisted:
                order_index = {fn: i for i, fn in enumerate(persisted.get("order", []))}
                to_process.sort(key=lambda item: order_index.get(item.filename, len(order_index)))
                if not persisted.get("running", True):
                    queue.stop()
                    self.after(0, self.pause_uploads_var.set, True)
            queue.enqueue(to_process)

        # Uploading proceeds as before (one file at a time, queue-ordered), but
        # results are no longer pulled down by polling at all: the server
        # pushes each status transition over /ws/jobs the instant it happens
        # (see job_router.py / ws_client.py), and self.job_router applies it
        # to this manifest/queue immediately, regardless of what else this
        # thread is doing. `batch` just tracks how many of *this* run's jobs
        # are still outstanding so the final summary below can wait for them
        # (via a Condition, not a timer) without gating any individual file's
        # visible status/result on that wait.
        if resumed:
            self.ui_log(f"Resuming {len(resumed)} job(s) already in progress from a previous session.")
        batch = RunBatch()
        # Resumed jobs are already past the upload phase but never went through
        # pop_next()/finish_upload() this session -- track them explicitly so
        # they're visible/reorderable in the sidebar too, not just newly
        # uploaded files.
        for sf, file_hash, job_id in resumed:
            queue.track_server_job(sf, file_hash, job_id)
            self.job_router.register(job_id, sf, file_hash, manifest, queue, batch)

        n_upload_err = 0
        uploaded = 0
        total_upload = len(to_process)
        while (item := queue.pop_next()) is not None:
            sf, file_hash = item.sf, item.file_hash
            uploaded += 1
            label = f"{sf.path.name} ({uploaded}/{total_upload})"
            self.ui_status(f"Uploading {label}...")
            self.ui_light("processing")
            try:
                job_id = self.client.upload_file(
                    sf.path, file_hash,
                    on_chunk=lambda done, total, label=label: self.ui_status(
                        f"Uploading {label}: chunk {done}/{total}"),
                )
                manifest.record_submitted(sf.path.name, sf.prefix, sf.channel, file_hash, job_id)
                queue.finish_upload(item, job_id)
                self.job_router.register(job_id, sf, file_hash, manifest, queue, batch)
            except ApiError as exc:
                manifest.record_error(sf.path.name, sf.prefix, sf.channel, file_hash, str(exc))
                self.ui_log(f"ERROR uploading {sf.path.name}: {exc}")
                n_upload_err += 1
                queue.remove(item.filename)

        n_submitted = len(resumed) + (uploaded - n_upload_err)
        if n_submitted:
            self.ui_log(f"{n_submitted} file(s) queued on the server — segmentation continues "
                         "even if this app is closed now.")

        # Nothing from here on is needed for the server to keep working — this
        # just waits (event-driven, no polling) for every job registered above
        # to resolve via a pushed status change.
        batch.wait_until_empty()

        n_ok = batch.n_ok
        n_err = batch.n_err + n_upload_err
        self.ui_light("connected" if n_err == 0 else "error")
        self.ui_status(f"Done. {n_ok} processed, {n_err} failed, {up_to_date} already up to date.")
        self.ui_log(f"Finished: {n_ok} processed, {n_err} failed.")


def main(argv=None) -> int:
    app = CellCountsApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
