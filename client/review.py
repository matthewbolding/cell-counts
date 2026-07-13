"""
review.py — the review panel: browse samples/channels, toggle mask/outline, draw
or delete cells, recolor channels, persist program state.

Renders its own viewport (see rendering.py's module docstring for why: one
composited raster per redraw instead of per-cell Tk Canvas items) rather than using
Tk's native canvas scrolling — canvas-relative click coordinates map directly to
`origin + event/scale` with nothing else to account for. Layering Tk's own
scrollregion/xview on top of that would double-count the pan offset the further
you've panned, since this panel already tracks pan itself.
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk

from PIL import ImageTk

import coexpression
import export
import geometry
import imaging
import rendering
import state
from manifest import Manifest, ScannedFile
from processing_queue import ProcessingQueue
from statusbar import STATE_COLORS

MIN_SCALE, MAX_SCALE = 0.01, 10.0
ZOOM_STEP = 1.25
PAN_STEP = 60  # screen px per wheel notch
DRAG_THRESHOLD = 4  # screen px before a press+move counts as a drag, not a click
DOUBLE_CLICK_SECONDS = 0.4
DOUBLE_CLICK_SCREEN_PX = 5
OVERLAY_DEBOUNCE_MS = 80  # re-render the (expensive) overlay this long after a gesture settles
FLUSH_CHECK_INTERVAL_MS = 500
FLUSH_IDLE_SECONDS = 1.0
QUEUE_POLL_MS = 500  # how often the sidebar dots + queue panel refresh from the background worker
CHANNELS = ("SNAP", "CCK", "CHR")

# Readiness-dot colors, reusing the same palette as the connection light in the
# status bar so the two indicators read consistently.
DOT_COLORS = {
    "not_ready": STATE_COLORS["error"],
    "processing": STATE_COLORS["processing"],
    "ready": STATE_COLORS["connected"],
}
ROW_BG = "#f0f0f0"
ROW_BG_SELECTED = "#cce0ff"
# Queue rows use the same base look as Samples rows (ROW_BG/ROW_BG_SELECTED) plus
# two of their own: a fill for "this file is being processed right now" (kept
# distinct from the blue selection color so a row that's both selected and
# in-flight doesn't read as ambiguous) and a border color that marks selection via
# an outline instead of a fill, so it layers on top of the processing fill cleanly.
QUEUE_PROCESSING_BG = "#fff2cc"
QUEUE_SELECTED_BORDER = "#2980b9"
# ttk.Scrollbar's default theme renders near-white on some platforms, which
# disappears against the light widgets it sits next to — plain tk.Scrollbar with
# explicit colors instead, for one that's actually visible everywhere.
SCROLLBAR_COLORS = dict(background="#a0a0a0", troughcolor="#e8e8e8", activebackground="#808080",
                         highlightthickness=0, bd=0)
# The Composite tab's coexpressing-cell outline — a fixed attention color, not a
# channel color, so it doesn't collide with the existing "filtered = gray"
# convention or either channel's own color.
COEXPRESS_HIGHLIGHT_COLOR = "#ffffff"


# ---------------------------------------------------------------------- #
# Undo/redo: one stack per (currently open) image, unbounded depth. A batch
# operation (e.g. drag-select marking many cells at once) is a single
# _ModifyAction with multiple changes, so one Ctrl+Z undoes the whole batch —
# not cell-by-cell.
# ---------------------------------------------------------------------- #
class _AddAction:
    """A cell was added (drawn)."""

    def __init__(self, cell: dict):
        self.cell = cell

    def undo(self, cells: list[dict]) -> None:
        cells.remove(self.cell)

    def redo(self, cells: list[dict]) -> None:
        cells.append(self.cell)


class _RemoveAction:
    """A cell was deleted."""

    def __init__(self, cell: dict):
        self.cell = cell

    def undo(self, cells: list[dict]) -> None:
        cells.append(self.cell)

    def redo(self, cells: list[dict]) -> None:
        cells.remove(self.cell)


class _ModifyAction:
    """One or more cells had their status/edited flag changed in place (single
    click toggle, or a whole drag-select batch as one action)."""

    def __init__(self, changes: list[tuple[dict, tuple, tuple]]):
        self.changes = changes  # (cell, (before_status, before_edited), (after_status, after_edited))

    def undo(self, cells: list[dict]) -> None:
        for cell, before, _after in self.changes:
            cell["status"], cell["edited"] = before

    def redo(self, cells: list[dict]) -> None:
        for cell, _before, after in self.changes:
            cell["status"], cell["edited"] = after


class _ExportDialog(tk.Toplevel):
    """File > Export Data... — pick which samples to include and which sheet(s).

    A dedicated dialog rather than overloading the Samples sidebar's click
    behavior with a second, unrelated meaning (multi-select-for-export vs.
    click-to-navigate) — this keeps every export decision in one clearly-labeled
    place instead of splitting them between a sidebar gesture and a follow-up
    prompt.
    """

    def __init__(self, parent, samples: dict[str, dict[str, str]], channel_status_fn):
        super().__init__(parent)
        self.title("Export Data")
        self.resizable(False, False)
        self.result: tuple[list[str], bool, bool] | None = None

        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Samples to export:", font=("", 10, "bold")).pack(anchor="w")
        list_wrap = ttk.Frame(outer)
        list_wrap.pack(fill="both", expand=True, pady=(4, 6))
        canvas = tk.Canvas(list_wrap, highlightthickness=0, bg=ROW_BG, width=260, height=200)
        scroll = tk.Scrollbar(list_wrap, orient="vertical", command=canvas.yview, **SCROLLBAR_COLORS)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        rows_frame = tk.Frame(canvas, bg=ROW_BG)
        window = canvas.create_window((0, 0), window=rows_frame, anchor="nw")
        rows_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(window, width=e.width))
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        canvas.bind("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
        canvas.bind("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))

        # Readiness dots alongside each checkbox — same colors as the sidebar —
        # so it's obvious at a glance which samples have real data to offer
        # before picking them, without having to leave this dialog to check.
        self._sample_vars: dict[str, tk.BooleanVar] = {}
        for prefix in sorted(samples.keys()):
            var = tk.BooleanVar(value=True)
            row = tk.Frame(rows_frame, bg=ROW_BG)
            row.pack(fill="x")
            tk.Checkbutton(row, text=prefix, variable=var, bg=ROW_BG, anchor="w",
                            activebackground=ROW_BG, highlightthickness=0).pack(
                side="left", fill="x", expand=True, padx=(4, 2), pady=2)
            for ch in CHANNELS:
                dot = tk.Canvas(row, width=10, height=10, highlightthickness=0, bg=ROW_BG)
                dot.create_oval(1, 1, 9, 9, outline="", fill=DOT_COLORS[channel_status_fn(prefix, ch)])
                dot.pack(side="left", padx=2)
            self._sample_vars[prefix] = var

        select_row = ttk.Frame(outer)
        select_row.pack(fill="x", pady=(0, 10))
        ttk.Button(select_row, text="Select All", command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(select_row, text="Select None", command=lambda: self._set_all(False)).pack(
            side="left", padx=(6, 0))

        ttk.Separator(outer, orient="horizontal").pack(fill="x", pady=(0, 10))

        ttk.Label(outer, text="Include:", font=("", 10, "bold")).pack(anchor="w")
        self.summary_var = tk.BooleanVar(value=True)
        self.cells_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(outer, text="Summary sheet (one row per sample)",
                         variable=self.summary_var).pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(outer, text="Cells sheet (one row per kept cell)",
                         variable=self.cells_var).pack(anchor="w")

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(16, 0))
        ttk.Button(buttons, text="Cancel", command=self._on_cancel).pack(side="right")
        ttk.Button(buttons, text="Export...", command=self._on_submit, default="active").pack(
            side="right", padx=(0, 6))

        self.bind("<Escape>", lambda e: self._on_cancel())
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.transient(parent)
        self.update_idletasks()
        self.grab_set()
        self.wait_window(self)

    def _set_all(self, value: bool) -> None:
        for var in self._sample_vars.values():
            var.set(value)

    def _on_submit(self) -> None:
        prefixes = [p for p, var in self._sample_vars.items() if var.get()]
        include_summary = self.summary_var.get()
        include_cells = self.cells_var.get()
        if not prefixes:
            messagebox.showinfo("Export Data", "Select at least one sample to export.", parent=self)
            return
        if not include_summary and not include_cells:
            messagebox.showinfo("Export Data", "Select at least Summary or Cells to include.", parent=self)
            return
        self.result = (sorted(prefixes), include_summary, include_cells)
        self.destroy()

    def _on_cancel(self) -> None:
        self.result = None
        self.destroy()


class ReviewPanel(ttk.Frame):
    def __init__(self, parent, folder: Path, manifest: Manifest, statusbar,
                 recognized: list[ScannedFile], queue: ProcessingQueue):
        super().__init__(parent)
        self.folder = folder
        self.manifest = manifest
        self.statusbar = statusbar
        self.recognized = recognized
        self.queue = queue

        self.image_cache = imaging.DisplayImageCache()
        self.ui_state = state.get_folder_state(folder)

        self.samples = self._build_sample_index()
        self._sample_rows: dict[str, dict] = {}
        self._queue_display_items: list = []
        self._queue_display_signature: list = []
        self._queue_row_widgets: dict[str, dict] = {}
        self._queue_selected: set[str] = set()
        self._queue_last_clicked: str | None = None
        self._known_done: set[str] = set()

        self.current_prefix: str | None = None
        self.current_channel: str | None = None
        self.current_filename: str | None = None
        self.current_cells: list[dict] = []

        # Composite ("Composite" tab): view-only overlay of a sample's CCK+CHR
        # kept cells with coexpressing ones outlined. Reuses the single-channel
        # viewport machinery by pointing current_filename at that sample's SNAP
        # image (the background art) — see _select_composite.
        self.viewing_composite = False
        self.composite_cck_cells: list[dict] = []
        self.composite_chr_cells: list[dict] = []
        self.composite_result: coexpression.CoexpressionResult | None = None
        self.composite_snap_kept = 0

        self.mode = "review"  # "review" | "draw" | "delete"
        self.draw_points: list[tuple[float, float]] = []
        self._last_draw_click_time: float | None = None
        self._last_draw_click_screen: tuple[int, int] | None = None

        # Undo/redo, keyed per filename so switching channels/samples and back
        # doesn't lose history; unbounded depth, cleared for an image only if it
        # never had any edits.
        self._undo_stacks: dict[str, list] = {}
        self._redo_stacks: dict[str, list] = {}

        self.origin = (0.0, 0.0)  # image-space top-left of the viewport
        self.scale = 1.0
        self._outlines_hidden = False

        self._dirty_filename: str | None = None
        self._dirty_since: float | None = None
        self._flush_timer_id: str | None = None
        self._overlay_timer_id: str | None = None
        self._queue_poll_id: str | None = None

        self._press_pos: tuple[int, int] | None = None
        self._dragging = False
        self._drag_rect_id: str | None = None
        self._canvas_image_id: str | None = None
        self._photo = None  # keep a reference so Tk doesn't garbage-collect it

        self._build_ui()
        self._bind_canvas_events()
        self._bind_global_keys()
        self._populate_sample_rows()
        self._select_initial_image()
        self._schedule_flush_check()
        self._schedule_queue_poll()

    # ------------------------------------------------------------------ #
    # Sample index
    # ------------------------------------------------------------------ #
    def _build_sample_index(self) -> dict[str, dict[str, str]]:
        """Sourced from the folder scan (every {PREFIX}_{CHANNEL}.tif that exists on
        disk), not from the manifest — so a sample shows up in the sidebar the
        instant the folder's opened, before anything's been hashed or processed."""
        samples: dict[str, dict[str, str]] = {}
        for sf in self.recognized:
            samples.setdefault(sf.prefix, {})[sf.channel] = sf.path.name
        return samples

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(side="top", fill="x", padx=6, pady=4)

        self.mode_var = tk.StringVar(value="review")
        self.mode_radio_buttons: list[ttk.Radiobutton] = []
        for label, value in [("Review", "review"), ("Draw", "draw"), ("Delete", "delete")]:
            rb = ttk.Radiobutton(toolbar, text=label, value=value, variable=self.mode_var,
                                  command=self._on_mode_change)
            rb.pack(side="left", padx=2)
            self.mode_radio_buttons.append(rb)

        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)
        self.render_mode_btn = ttk.Button(toolbar, text="Mode: Outline", command=self._toggle_render_mode)
        self.render_mode_btn.pack(side="left", padx=2)

        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(toolbar, text="Colors:").pack(side="left")
        self.color_buttons: dict[str, tk.Button] = {}
        for ch in CHANNELS:
            btn = tk.Button(toolbar, text=ch, width=5, command=lambda c=ch: self._pick_color(c))
            btn.pack(side="left", padx=2)
            self.color_buttons[ch] = btn

        zoomf = ttk.Frame(toolbar)
        zoomf.pack(side="right")
        ttk.Button(zoomf, text="−", width=3, command=lambda: self._zoom(1 / ZOOM_STEP)).pack(side="left")
        ttk.Button(zoomf, text="Fit", command=self._fit).pack(side="left", padx=4)
        ttk.Button(zoomf, text="+", width=3, command=lambda: self._zoom(ZOOM_STEP)).pack(side="left")

        main = ttk.Frame(self)
        main.pack(fill="both", expand=True)

        left = ttk.Frame(main, width=210)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        pane = ttk.PanedWindow(left, orient="vertical")
        pane.pack(fill="both", expand=True)

        # --- Samples (top pane): scrollable custom rows, one per sample, each with
        # three readiness dots (CCK/CHR/SNAP). A plain Listbox can't draw colored
        # dots, hence the hand-rolled Canvas-in-a-Frame-in-a-Canvas scroll idiom.
        samples_outer = ttk.Frame(pane)
        ttk.Label(samples_outer, text="Samples", font=("", 10, "bold")).pack(
            anchor="w", padx=8, pady=(8, 2))
        samples_wrap = ttk.Frame(samples_outer)
        samples_wrap.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.sample_canvas = tk.Canvas(samples_wrap, highlightthickness=0, bg=ROW_BG)
        samples_scroll = tk.Scrollbar(samples_wrap, orient="vertical", command=self.sample_canvas.yview,
                                       **SCROLLBAR_COLORS)
        self.sample_canvas.configure(yscrollcommand=samples_scroll.set)
        self.sample_canvas.pack(side="left", fill="both", expand=True)
        samples_scroll.pack(side="right", fill="y")
        self.sample_rows_frame = tk.Frame(self.sample_canvas, bg=ROW_BG)
        self._sample_rows_window = self.sample_canvas.create_window(
            (0, 0), window=self.sample_rows_frame, anchor="nw")
        self.sample_rows_frame.bind(
            "<Configure>", lambda e: self.sample_canvas.configure(scrollregion=self.sample_canvas.bbox("all")))
        self.sample_canvas.bind(
            "<Configure>", lambda e: self.sample_canvas.itemconfig(self._sample_rows_window, width=e.width))
        self._bind_wheel_scroll(self.sample_canvas, self.sample_canvas)
        pane.add(samples_outer, weight=2)

        # --- Queue (bottom pane): pending work, Start/Stop, and reorder controls.
        # Same scrollable-custom-rows idiom as Samples above (not a plain Listbox)
        # so the two panels look and feel like one consistent design.
        queue_outer = ttk.Frame(pane)
        queue_header = ttk.Frame(queue_outer)
        queue_header.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(queue_header, text="Queue", font=("", 10, "bold")).pack(side="left")
        self.queue_toggle_btn = ttk.Button(queue_header, text="Inactive", width=8,
                                            state="disabled", command=self._toggle_queue_running)
        self.queue_toggle_btn.pack(side="right")

        queue_wrap = ttk.Frame(queue_outer)
        queue_wrap.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.queue_canvas = tk.Canvas(queue_wrap, highlightthickness=0, bg=ROW_BG)
        queue_scroll = tk.Scrollbar(queue_wrap, orient="vertical", command=self.queue_canvas.yview,
                                     **SCROLLBAR_COLORS)
        self.queue_canvas.configure(yscrollcommand=queue_scroll.set)
        self.queue_canvas.pack(side="left", fill="both", expand=True)
        queue_scroll.pack(side="right", fill="y")
        self.queue_rows_frame = tk.Frame(self.queue_canvas, bg=ROW_BG)
        self._queue_rows_window = self.queue_canvas.create_window(
            (0, 0), window=self.queue_rows_frame, anchor="nw")
        self.queue_rows_frame.bind(
            "<Configure>", lambda e: self.queue_canvas.configure(scrollregion=self.queue_canvas.bbox("all")))
        self.queue_canvas.bind(
            "<Configure>", lambda e: self.queue_canvas.itemconfig(self._queue_rows_window, width=e.width))
        self._bind_wheel_scroll(self.queue_canvas, self.queue_canvas)

        # 2x2 matrix, symbols only: rows are up/down, columns are one-step/all-the-
        # way. Grid (not pack) so all four cells always divide the space evenly
        # and stay the same size regardless of sidebar width.
        queue_btns = ttk.Frame(queue_outer)
        queue_btns.pack(fill="x", padx=8, pady=(0, 8))
        queue_btns.columnconfigure(0, weight=1)
        queue_btns.columnconfigure(1, weight=1)
        ttk.Button(queue_btns, text="▲", command=self._queue_move_up).grid(
            row=0, column=0, sticky="ew", padx=(0, 2), pady=(0, 2))
        ttk.Button(queue_btns, text="▲▲", command=self._queue_send_to_top).grid(
            row=0, column=1, sticky="ew", padx=(2, 0), pady=(0, 2))
        ttk.Button(queue_btns, text="▼", command=self._queue_move_down).grid(
            row=1, column=0, sticky="ew", padx=(0, 2))
        ttk.Button(queue_btns, text="▼▼", command=self._queue_send_to_bottom).grid(
            row=1, column=1, sticky="ew", padx=(2, 0))
        pane.add(queue_outer, weight=1)

        center = ttk.Frame(main)
        center.pack(side="left", fill="both", expand=True)

        tabs = ttk.Frame(center)
        tabs.pack(side="top", fill="x", pady=2)
        self.channel_tab_buttons: dict[str, ttk.Button] = {}
        for ch in CHANNELS:
            btn = ttk.Button(tabs, text=ch, command=lambda c=ch: self._on_channel_select(c))
            btn.pack(side="left", padx=4)
            self.channel_tab_buttons[ch] = btn
        ttk.Separator(tabs, orient="vertical").pack(side="left", fill="y", padx=4)
        self.composite_tab_btn = ttk.Button(tabs, text="Composite", command=self._on_composite_select)
        self.composite_tab_btn.pack(side="left", padx=4)

        canvas_frame = ttk.Frame(center)
        canvas_frame.pack(fill="both", expand=True, padx=4, pady=4)
        self.canvas = tk.Canvas(canvas_frame, bg="#1a1a1a", highlightthickness=0, cursor="hand2")
        self.canvas.pack(fill="both", expand=True)

        right = ttk.Frame(main, width=200)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        ttk.Label(right, text="", font=("", 11, "bold"), textvariable=self._var("filename_var")).pack(
            anchor="w", padx=10, pady=(10, 4), fill="x")
        ttk.Label(right, textvariable=self._var("counts_var"), justify="left").pack(
            anchor="w", padx=10, pady=(0, 10))
        instructions = (
            "Review: click toggles cell/not-cell; drag-select marks many at once.\n\n"
            "Draw: click to place vertices; double-click or Enter closes (needs "
            "≥3 points); Esc cancels.\n\n"
            "Delete: click a cell to remove it.\n\n"
            "Ctrl+Z undoes, Ctrl+Shift+Z redoes — any number of times, including "
            "whole drag-select batches as one step.\n\n"
            "Hold right mouse button to hide outlines/masks."
        )
        ttk.Label(right, text=instructions, wraplength=180, foreground="#666").pack(
            anchor="w", padx=10, pady=(0, 10))

    def _var(self, name: str) -> tk.StringVar:
        if not hasattr(self, name):
            setattr(self, name, tk.StringVar(value=""))
        return getattr(self, name)

    def _bind_canvas_events(self) -> None:
        self.canvas.bind("<Configure>", lambda e: self._redraw(full=False))
        self.canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        # Right mouse button = "hide outlines while held". Bound to both Button-2
        # and Button-3: Tk's virtual-button numbering for the secondary click is
        # reversed on macOS Aqua (right-click reports Button-2, not Button-3, the
        # opposite of X11/Windows) — binding both means this works as "the right
        # button" regardless of platform, rather than hardcoding one number.
        self.canvas.bind("<ButtonPress-2>", self._hide_outlines)
        self.canvas.bind("<ButtonRelease-2>", self._show_outlines)
        self.canvas.bind("<ButtonPress-3>", self._hide_outlines)
        self.canvas.bind("<ButtonRelease-3>", self._show_outlines)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Shift-MouseWheel>", self._on_shift_wheel)
        self.canvas.bind("<Control-MouseWheel>", self._on_ctrl_wheel)
        self.canvas.bind("<Button-4>", lambda e: self._pan(0, -PAN_STEP))   # Linux wheel up
        self.canvas.bind("<Button-5>", lambda e: self._pan(0, PAN_STEP))    # Linux wheel down

    def _bind_global_keys(self) -> None:
        top = self.winfo_toplevel()
        top.bind("<Escape>", lambda e: self._cancel_draw())
        top.bind("<Return>", lambda e: self._commit_draw() if self.mode == "draw" else None)
        top.bind("<KP_Enter>", lambda e: self._commit_draw() if self.mode == "draw" else None)
        top.bind("<Control-z>", lambda e: self._undo())
        # Redo: bind every variant a shifted "z" is reported as, since platforms
        # disagree on whether holding Shift changes the keysym case
        # (Control-Shift-z) or is folded into an uppercase keysym (Control-Z).
        top.bind("<Control-Shift-z>", lambda e: self._redo())
        top.bind("<Control-Shift-Z>", lambda e: self._redo())
        top.bind("<Control-Z>", lambda e: self._redo())

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    def _bind_wheel_scroll(self, widget, canvas) -> None:
        """Wheel events go to whatever widget is directly under the cursor, not to
        the scrollable canvas underneath it — without this, scrolling only works
        in the sliver of empty canvas below the last row, not over an actual row
        (label/dots), which is nearly the entire visible list. Shared by the
        Samples and Queue panels, each scrolling their own canvas."""
        widget.bind("<MouseWheel>", lambda e: canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        widget.bind("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
        widget.bind("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))

    def _populate_sample_rows(self) -> None:
        for child in self.sample_rows_frame.winfo_children():
            child.destroy()
        self._sample_rows = {}
        for prefix in sorted(self.samples.keys()):
            row = tk.Frame(self.sample_rows_frame, bg=ROW_BG)
            row.pack(fill="x")
            row.bind("<Button-1>", lambda e, p=prefix: self._on_sample_row_click(p, None))
            self._bind_wheel_scroll(row, self.sample_canvas)

            name_label = tk.Label(row, text=prefix, anchor="w", bg=ROW_BG)
            name_label.pack(side="left", fill="x", expand=True, padx=(6, 2), pady=3)
            name_label.bind("<Button-1>", lambda e, p=prefix: self._on_sample_row_click(p, None))
            self._bind_wheel_scroll(name_label, self.sample_canvas)

            dots = {}
            for ch in CHANNELS:
                dot = tk.Canvas(row, width=10, height=10, highlightthickness=0, bg=ROW_BG)
                oval = dot.create_oval(1, 1, 9, 9, outline="")
                dot.pack(side="left", padx=2)
                dot.bind("<Button-1>", lambda e, p=prefix, c=ch: self._on_sample_row_click(p, c))
                self._bind_wheel_scroll(dot, self.sample_canvas)
                dots[ch] = (dot, oval)

            self._sample_rows[prefix] = {"row": row, "name_label": name_label, "dots": dots}
        self._refresh_sample_dots()
        self._update_sample_row_selection()

    def _on_sample_row_click(self, prefix: str, channel: str | None) -> None:
        channels = self.samples.get(prefix, {})
        if channel is None or channel not in channels:
            channel = next((c for c in (self.current_channel, *CHANNELS) if c in channels), None)
        if channel:
            self._select_image(prefix, channel)

    def _update_sample_row_selection(self) -> None:
        for prefix, row in self._sample_rows.items():
            bg = ROW_BG_SELECTED if prefix == self.current_prefix else ROW_BG
            row["row"].configure(bg=bg)
            row["name_label"].configure(bg=bg)
            for dot, _oval in row["dots"].values():
                dot.configure(bg=bg)

    def _channel_status(self, prefix: str, channel: str) -> str:
        """"not_ready" | "processing" | "ready" — drives a sidebar dot's color."""
        filename = self.samples.get(prefix, {}).get(channel)
        if filename is None:
            return "not_ready"
        if self.queue.is_processing(filename):
            return "processing"
        info = self.manifest.data["images"].get(filename)
        if info and info.get("status") == "done":
            return "ready"
        return "not_ready"

    def _refresh_sample_dots(self) -> None:
        for prefix, row in self._sample_rows.items():
            for ch in CHANNELS:
                color = DOT_COLORS[self._channel_status(prefix, ch)]
                dot, oval = row["dots"][ch]
                dot.itemconfig(oval, fill=color)

    def _select_initial_image(self) -> None:
        last = self.ui_state.get("last_image")
        if last:
            for prefix, channels in self.samples.items():
                for ch, fn in channels.items():
                    if fn == last:
                        self._select_image(prefix, ch)
                        return
        if self.samples:
            prefix = sorted(self.samples.keys())[0]
            for ch in CHANNELS:
                if ch in self.samples[prefix]:
                    self._select_image(prefix, ch)
                    return

    def _on_channel_select(self, channel: str) -> None:
        if self.current_prefix is None or channel not in self.samples.get(self.current_prefix, {}):
            return
        self._select_image(self.current_prefix, channel)

    def _current_path(self) -> Path:
        return self.folder / self.current_filename

    def _apply_viewport(self, same_sample: bool, display_array) -> None:
        """Preserve the current pan/zoom when switching channels within the same
        sample (SNAP/CCK/CHR/Composite are simultaneous channels of one
        acquisition — the region you're looking at on CCK is still the same
        physical region on CHR) — only reset to fit when landing on a genuinely
        different sample's image, where the prior position wouldn't mean anything.
        `imaging.crop_and_scale` already letterboxes a viewport that runs past an
        image's bounds, so carrying `origin`/`scale` over unchanged is safe even in
        the (unexpected) case that a sample's channels aren't all the same size.
        """
        if same_sample:
            return
        self.scale = self._fit_scale_for(display_array) * self.ui_state.get("zoom_multiplier", 1.0)
        self._center_on(display_array)

    def _select_image(self, prefix: str, channel: str) -> None:
        filename = self.samples.get(prefix, {}).get(channel)
        if filename is None:
            return

        self._flush_current(sync=False)
        if self.current_filename is not None:
            self._save_ui_state()

        same_sample = prefix == self.current_prefix
        self.current_prefix, self.current_channel, self.current_filename = prefix, channel, filename
        self.current_cells = self.manifest.load_cells(filename)
        self.viewing_composite = False
        self.draw_points = []
        self.mode_var.set("review")
        self.mode = "review"
        self.canvas.configure(cursor="hand2")

        self.image_cache.invalidate()  # let the previous image's array be GC'd before loading the next
        self.statusbar.set_message(f"Loading {filename}...")
        self.update_idletasks()
        display_array = self.image_cache.get(self._current_path())

        self._apply_viewport(same_sample, display_array)

        self.ui_state["last_image"] = filename
        self._update_sample_row_selection()
        self._update_channel_tabs()
        self._update_mode_controls()
        self._update_swatches()
        self._update_render_mode_button()
        self._update_counts()
        self._redraw(full=True)
        self.statusbar.set_message(f"{filename} — {len(self.current_cells)} cell(s)")

    def _composite_available(self, prefix: str) -> bool:
        """All three channels processed for this sample — Composite needs kept
        cells from all of them (CCK+CHR to overlay, SNAP for the population count)."""
        channels = self.samples.get(prefix, {})
        return all(ch in channels for ch in CHANNELS) and \
            all(self._channel_status(prefix, ch) == "ready" for ch in CHANNELS)

    def _update_channel_tabs(self) -> None:
        available = self.samples.get(self.current_prefix, {})
        for ch, btn in self.channel_tab_buttons.items():
            btn.configure(state="normal" if ch in available else "disabled")
            btn.state(["pressed"] if (ch == self.current_channel and not self.viewing_composite)
                      else ["!pressed"])
        composite_ok = self.current_prefix is not None and self._composite_available(self.current_prefix)
        self.composite_tab_btn.configure(state="normal" if composite_ok else "disabled")
        self.composite_tab_btn.state(["pressed"] if self.viewing_composite else ["!pressed"])

    def _update_mode_controls(self) -> None:
        """Composite is view-only — no toggling/drawing/deleting on a derived
        overlay, editing always happens on one real channel."""
        widget_state = "disabled" if self.viewing_composite else "normal"
        for rb in self.mode_radio_buttons:
            rb.configure(state=widget_state)

    def _update_swatches(self) -> None:
        for ch, btn in self.color_buttons.items():
            btn.configure(bg=self.ui_state["channel_colors"].get(ch, "#ffffff"))

    def _update_render_mode_button(self) -> None:
        self.render_mode_btn.configure(text=f"Mode: {self.ui_state['render_mode'].title()}")

    def _update_counts(self) -> None:
        kept = sum(1 for c in self.current_cells if c["status"] == "kept")
        total = len(self.current_cells)
        edited = sum(1 for c in self.current_cells if c.get("edited"))
        self.filename_var.set(self.current_filename or "")
        self.counts_var.set(f"{kept} kept\n{total - kept} filtered\n{total} total\n{edited} edited")

    # ------------------------------------------------------------------ #
    # Composite tab: CCK+CHR overlay with coexpressing cells outlined
    # ------------------------------------------------------------------ #
    def _on_composite_select(self) -> None:
        if self.current_prefix is None or not self._composite_available(self.current_prefix):
            return
        self._select_composite(self.current_prefix)

    def _select_composite(self, prefix: str) -> None:
        channels = self.samples.get(prefix, {})
        snap_filename = channels.get("SNAP")
        if snap_filename is None:
            return

        self._flush_current(sync=False)
        if self.current_filename is not None:
            self._save_ui_state()

        same_sample = prefix == self.current_prefix
        self.current_prefix = prefix
        self.current_channel = "Composite"
        # Background art is SNAP's own image (the general cell-population channel)
        # — pointing current_filename at it means every bit of existing viewport
        # math (fit/zoom/pan/image_cache) needs no composite-specific handling.
        self.current_filename = snap_filename
        self.viewing_composite = True
        self.current_cells = []  # unused: composite is view-only, no hit-testing
        self.draw_points = []
        self.mode_var.set("review")
        self.mode = "review"
        self.canvas.configure(cursor="hand2")

        self.composite_cck_cells = [c for c in self.manifest.load_cells(channels["CCK"])
                                     if c["status"] == "kept"]
        self.composite_chr_cells = [c for c in self.manifest.load_cells(channels["CHR"])
                                     if c["status"] == "kept"]
        self.composite_result = coexpression.compute_coexpression(
            self.composite_cck_cells, self.composite_chr_cells)
        snap_cells = self.manifest.load_cells(snap_filename)
        self.composite_snap_kept = sum(1 for c in snap_cells if c["status"] == "kept")

        self.image_cache.invalidate()
        self.statusbar.set_message(f"Loading composite for {prefix}...")
        self.update_idletasks()
        display_array = self.image_cache.get(self._current_path())

        self._apply_viewport(same_sample, display_array)

        self.ui_state["last_image"] = snap_filename
        self._update_sample_row_selection()
        self._update_channel_tabs()
        self._update_mode_controls()
        self._update_swatches()
        self._update_render_mode_button()
        self._update_composite_counts()
        self._redraw(full=True)
        self.statusbar.set_message(f"Composite — {prefix}")

    def _update_composite_counts(self) -> None:
        n_pairs = len(self.composite_result.pairs) if self.composite_result else 0
        rate = (n_pairs / self.composite_snap_kept * 100) if self.composite_snap_kept else 0.0
        self.filename_var.set(f"{self.current_prefix} — Composite")
        self.counts_var.set(
            f"{n_pairs} coexpressing pair(s)\n"
            f"{self.composite_snap_kept} SNAP kept (population)\n"
            f"{rate:.1f}% coexpression rate"
        )

    # ------------------------------------------------------------------ #
    # Viewport / zoom / pan
    # ------------------------------------------------------------------ #
    def _canvas_size(self) -> tuple[int, int]:
        self.canvas.update_idletasks()
        return max(self.canvas.winfo_width(), 100), max(self.canvas.winfo_height(), 100)

    def _fit_scale_for(self, display_array) -> float:
        img_h, img_w = display_array.shape
        vw, vh = self._canvas_size()
        return vw / img_w if img_h >= img_w else vh / img_h

    def _center_on(self, display_array) -> None:
        img_h, img_w = display_array.shape
        vw, vh = self._canvas_size()
        cx, cy = img_w / 2, img_h / 2
        self.origin = (cx - (vw / self.scale) / 2, cy - (vh / self.scale) / 2)

    def _viewport_rect(self) -> tuple[float, float, float, float]:
        vw, vh = self._canvas_size()
        x0, y0 = self.origin
        return (x0, y0, x0 + vw / self.scale, y0 + vh / self.scale)

    def _screen_to_image(self, sx: float, sy: float) -> tuple[float, float]:
        x0, y0 = self.origin
        return (x0 + sx / self.scale, y0 + sy / self.scale)

    def _fit(self) -> None:
        if self.current_filename is None:
            return
        display_array = self.image_cache.get(self._current_path())
        self.scale = self._fit_scale_for(display_array)
        self.ui_state["zoom_multiplier"] = 1.0
        self._center_on(display_array)
        self._redraw(full=True)

    def _zoom(self, factor: float, anchor_screen: tuple[float, float] | None = None) -> None:
        if self.current_filename is None:
            return
        vw, vh = self._canvas_size()
        ax, ay = anchor_screen if anchor_screen is not None else (vw / 2, vh / 2)
        old_scale = self.scale
        new_scale = max(MIN_SCALE, min(MAX_SCALE, old_scale * factor))
        if abs(new_scale - old_scale) < 1e-9:
            return
        x0, y0 = self.origin
        img_x, img_y = x0 + ax / old_scale, y0 + ay / old_scale
        self.scale = new_scale
        self.origin = (img_x - ax / new_scale, img_y - ay / new_scale)
        display_array = self.image_cache.get(self._current_path())
        self.ui_state["zoom_multiplier"] = new_scale / self._fit_scale_for(display_array)
        self._redraw(full=False)

    def _pan(self, dx_screen: float, dy_screen: float) -> None:
        if self.current_filename is None:
            return
        x0, y0 = self.origin
        self.origin = (x0 + dx_screen / self.scale, y0 + dy_screen / self.scale)
        self._redraw(full=False)

    def _on_wheel(self, event) -> None:
        self._pan(0, -PAN_STEP if event.delta > 0 else PAN_STEP)

    def _on_shift_wheel(self, event) -> None:
        self._pan(-PAN_STEP if event.delta > 0 else PAN_STEP, 0)

    def _on_ctrl_wheel(self, event) -> None:
        factor = ZOOM_STEP if event.delta > 0 else 1 / ZOOM_STEP
        self._zoom(factor, anchor_screen=(event.x, event.y))

    def _hide_outlines(self, event=None) -> None:
        self._outlines_hidden = True
        self._redraw(full=True)

    def _show_outlines(self, event=None) -> None:
        self._outlines_hidden = False
        self._redraw(full=True)

    # ------------------------------------------------------------------ #
    # Redraw pipeline
    # ------------------------------------------------------------------ #
    def _blit(self, pil_image) -> None:
        self._photo = ImageTk.PhotoImage(pil_image)
        if self._canvas_image_id is None:
            self._canvas_image_id = self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        else:
            self.canvas.itemconfig(self._canvas_image_id, image=self._photo)

    def _redraw(self, full: bool) -> None:
        if self.current_filename is None:
            return
        vw, vh = self._canvas_size()
        rect = self._viewport_rect()
        display_array = self.image_cache.get(self._current_path())
        bg = imaging.crop_and_scale(display_array, rect, (vw, vh))

        if full:
            if self._overlay_timer_id is not None:
                self.after_cancel(self._overlay_timer_id)
                self._overlay_timer_id = None
            self._render_and_blit(bg, rect)
        else:
            self._blit(bg)  # instant background feedback during a fast gesture
            if self._overlay_timer_id is not None:
                self.after_cancel(self._overlay_timer_id)
            self._overlay_timer_id = self.after(
                OVERLAY_DEBOUNCE_MS, lambda: self._render_and_blit(bg, rect))

    def _render_and_blit(self, bg, rect) -> None:
        self._overlay_timer_id = None
        if self._outlines_hidden:
            self._blit(bg)
            return
        vw, vh = self._canvas_size()
        if self.viewing_composite:
            self._blit(self._render_composite_overlay(bg, rect, vw, vh))
            return
        overlay = rendering.render_overlay(
            self.current_cells, rect, self.scale, (vw, vh),
            self.ui_state["render_mode"], self.ui_state["channel_colors"][self.current_channel])
        layers = [overlay]
        if self.mode == "draw" and self.draw_points:
            layers.append(rendering.render_in_progress_polygon(self.draw_points, rect, self.scale, (vw, vh)))
        self._blit(rendering.composite(bg, *layers))

    def _render_composite_overlay(self, bg, rect, vw: int, vh: int):
        """CCK + CHR both `mode="mask"` and translucent, so real overlap reads as
        a blended color for free via alpha compositing — no separate blend logic
        needed. A third outline-only layer, in a fixed non-channel color, marks
        exactly the cells found coexpressing."""
        cck_color = self.ui_state["channel_colors"]["CCK"]
        chr_color = self.ui_state["channel_colors"]["CHR"]
        cck_layer = rendering.render_overlay(
            self.composite_cck_cells, rect, self.scale, (vw, vh), "mask", cck_color)
        chr_layer = rendering.render_overlay(
            self.composite_chr_cells, rect, self.scale, (vw, vh), "mask", chr_color)

        highlight_cells = (
            [c for c in self.composite_cck_cells if c["id"] in self.composite_result.cck_ids]
            + [c for c in self.composite_chr_cells if c["id"] in self.composite_result.chr_ids]
        ) if self.composite_result else []
        highlight_layer = rendering.render_overlay(
            highlight_cells, rect, self.scale, (vw, vh), "outline", COEXPRESS_HIGHLIGHT_COLOR)

        return rendering.composite(bg, cck_layer, chr_layer, highlight_layer)

    # ------------------------------------------------------------------ #
    # Hit-testing
    # ------------------------------------------------------------------ #
    def _hit_test(self, img_x: float, img_y: float) -> dict | None:
        candidates = []
        pad = 2.0 / self.scale
        for cell in self.current_cells:
            bx0, by0, bx1, by1 = geometry.bbox_of(cell["polygons"])
            if bx0 - pad <= img_x <= bx1 + pad and by0 - pad <= img_y <= by1 + pad:
                if geometry.point_in_any_ring(img_x, img_y, cell["polygons"]):
                    candidates.append(cell)
        if candidates:
            return min(candidates, key=lambda c: c["area"])

        tol = 10.0 / self.scale
        best, best_d = None, tol
        for cell in self.current_cells:
            dx, dy = cell["centroid"][0] - img_x, cell["centroid"][1] - img_y
            d = (dx * dx + dy * dy) ** 0.5
            if d < best_d:
                best, best_d = cell, d
        return best

    # ------------------------------------------------------------------ #
    # Mouse: press / drag / release, mode-dependent
    # ------------------------------------------------------------------ #
    def _on_canvas_press(self, event) -> None:
        if self.current_filename is None:
            return
        self._press_pos = (event.x, event.y)
        self._dragging = False
        self._drag_rect_id = None

    def _on_canvas_drag(self, event) -> None:
        if self._press_pos is None or self.viewing_composite:
            return
        sx, sy = self._press_pos
        if not self._dragging:
            if abs(event.x - sx) < DRAG_THRESHOLD and abs(event.y - sy) < DRAG_THRESHOLD:
                return
            if self.mode != "review":
                return  # drag-select only applies in Review mode
            self._dragging = True
        if self._drag_rect_id is None:
            self._drag_rect_id = self.canvas.create_rectangle(
                sx, sy, event.x, event.y, outline="#ffffff", dash=(4, 2), width=2)
        else:
            self.canvas.coords(self._drag_rect_id, sx, sy, event.x, event.y)

    def _on_canvas_release(self, event) -> None:
        if self._press_pos is None:
            return
        sx, sy = self._press_pos
        was_dragging = self._dragging
        if self._drag_rect_id is not None:
            self.canvas.delete(self._drag_rect_id)
        self._drag_rect_id = None
        self._press_pos = None
        self._dragging = False

        if self.viewing_composite:
            return  # Composite is view-only, regardless of the mode selector's value

        if self.mode == "review":
            if was_dragging:
                x0, y0 = self._screen_to_image(sx, sy)
                x1, y1 = self._screen_to_image(event.x, event.y)
                self._select_rectangle((x0, y0, x1, y1))
            else:
                img_x, img_y = self._screen_to_image(event.x, event.y)
                cell = self._hit_test(img_x, img_y)
                if cell is not None:
                    self._toggle_cell(cell)

        elif self.mode == "draw" and not was_dragging:
            self._on_draw_click(event.x, event.y)

        elif self.mode == "delete" and not was_dragging:
            img_x, img_y = self._screen_to_image(event.x, event.y)
            cell = self._hit_test(img_x, img_y)
            if cell is not None:
                self.current_cells.remove(cell)
                self._push_undo(_RemoveAction(cell))
                self._redraw(full=True)
                self._update_counts()

    # ------------------------------------------------------------------ #
    # Review mode actions
    # ------------------------------------------------------------------ #
    def _toggle_cell(self, cell: dict) -> None:
        before = (cell["status"], cell.get("edited", False))
        cell["status"] = "filtered" if cell["status"] == "kept" else "kept"
        cell["edited"] = True
        after = (cell["status"], cell["edited"])
        self._push_undo(_ModifyAction([(cell, before, after)]))
        self._redraw(full=True)
        self._update_counts()

    def _select_rectangle(self, img_rect: tuple[float, float, float, float]) -> None:
        x0, y0, x1, y1 = img_rect
        left, right = sorted((x0, x1))
        top, bottom = sorted((y0, y1))
        selected = [c for c in self.current_cells
                    if left <= c["centroid"][0] <= right and top <= c["centroid"][1] <= bottom]
        if not selected:
            return
        mark_as_cells = messagebox.askyesnocancel(
            "Mark detections",
            f"{len(selected)} detection(s) selected.\n\n"
            "Mark all as cells (Yes) or not cells (No)? Cancel to leave them unchanged.")
        if mark_as_cells is None:
            return
        new_status = "kept" if mark_as_cells else "filtered"
        changes = []
        for c in selected:
            before = (c["status"], c.get("edited", False))
            c["status"] = new_status
            c["edited"] = True
            changes.append((c, before, (c["status"], c["edited"])))
        # One action for the whole batch, so one Ctrl+Z undoes every cell the
        # rectangle touched, not just the last one.
        self._push_undo(_ModifyAction(changes))
        self._redraw(full=True)
        self._update_counts()

    # ------------------------------------------------------------------ #
    # Draw mode actions
    # ------------------------------------------------------------------ #
    def _on_draw_click(self, sx: int, sy: int) -> None:
        now = time.monotonic()
        is_double = (
            self._last_draw_click_time is not None
            and now - self._last_draw_click_time < DOUBLE_CLICK_SECONDS
            and self._last_draw_click_screen is not None
            and abs(sx - self._last_draw_click_screen[0]) < DOUBLE_CLICK_SCREEN_PX
            and abs(sy - self._last_draw_click_screen[1]) < DOUBLE_CLICK_SCREEN_PX
        )
        # Own timing-based double-click detection instead of Tk's <Double-Button-1>:
        # Tk fires a plain ButtonPress/Release for the second click of a pair *in
        # addition to* the double-click event, not instead of it, which would add a
        # spurious near-duplicate vertex right before closing if bound naively.
        if is_double:
            self._last_draw_click_time = None
            self._last_draw_click_screen = None
            self._commit_draw()
            return
        self._last_draw_click_time = now
        self._last_draw_click_screen = (sx, sy)
        img_x, img_y = self._screen_to_image(sx, sy)
        self.draw_points.append((img_x, img_y))
        self._redraw(full=True)

    def _commit_draw(self) -> None:
        if len(self.draw_points) < 3:
            if self.draw_points:
                messagebox.showinfo("Draw cell", "Need at least 3 points to close a cell outline.")
            self.draw_points = []
            self._redraw(full=True)
            return
        ring = [[round(x, 2), round(y, 2)] for x, y in self.draw_points]
        area, centroid = geometry.shoelace_area_centroid(ring)
        new_id = max((c["id"] for c in self.current_cells), default=0) + 1
        new_cell = {
            "id": new_id, "polygons": [ring], "centroid": centroid, "area": area,
            "status": "kept", "color": None, "source": "human", "edited": True,
        }
        self.current_cells.append(new_cell)
        self.draw_points = []
        self._push_undo(_AddAction(new_cell))
        self._redraw(full=True)
        self._update_counts()

    def _cancel_draw(self, event=None) -> None:
        if self.draw_points:
            self.draw_points = []
            self._redraw(full=True)

    # ------------------------------------------------------------------ #
    # Undo/redo
    # ------------------------------------------------------------------ #
    def _push_undo(self, action) -> None:
        """Record a completed edit. Any fresh edit invalidates redo history for
        this image, matching standard undo/redo semantics."""
        if self.current_filename is None:
            return
        self._undo_stacks.setdefault(self.current_filename, []).append(action)
        self._redo_stacks[self.current_filename] = []
        self._mark_dirty()

    def _undo(self, event=None) -> None:
        stack = self._undo_stacks.get(self.current_filename)
        if not stack:
            return
        action = stack.pop()
        action.undo(self.current_cells)
        self._redo_stacks.setdefault(self.current_filename, []).append(action)
        self._mark_dirty()
        self._redraw(full=True)
        self._update_counts()

    def _redo(self, event=None) -> None:
        stack = self._redo_stacks.get(self.current_filename)
        if not stack:
            return
        action = stack.pop()
        action.redo(self.current_cells)
        self._undo_stacks.setdefault(self.current_filename, []).append(action)
        self._mark_dirty()
        self._redraw(full=True)
        self._update_counts()

    # ------------------------------------------------------------------ #
    # Mode / render-mode / color
    # ------------------------------------------------------------------ #
    def _on_mode_change(self) -> None:
        self.mode = self.mode_var.get()
        self.draw_points = []
        cursors = {"review": "hand2", "draw": "tcross", "delete": "X_cursor"}
        self.canvas.configure(cursor=cursors.get(self.mode, "arrow"))
        self._redraw(full=True)

    def _toggle_render_mode(self) -> None:
        self.ui_state["render_mode"] = "mask" if self.ui_state["render_mode"] == "outline" else "outline"
        self._save_ui_state()
        self._update_render_mode_button()
        self._redraw(full=True)

    def _pick_color(self, channel: str) -> None:
        initial = self.ui_state["channel_colors"].get(channel, "#ffffff")
        _, hex_color = colorchooser.askcolor(color=initial, title=f"{channel} color", parent=self)
        if hex_color is None:
            return
        self.ui_state["channel_colors"][channel] = hex_color
        self._save_ui_state()
        self._update_swatches()
        if channel == self.current_channel or (self.viewing_composite and channel in ("CCK", "CHR")):
            self._redraw(full=True)

    # ------------------------------------------------------------------ #
    # Persistence: state.json (light, immediate) and cell data (debounced)
    # ------------------------------------------------------------------ #
    def _save_ui_state(self) -> None:
        state.save_folder_state(self.folder, self.ui_state)

    def _mark_dirty(self) -> None:
        self._dirty_filename = self.current_filename
        self._dirty_since = time.monotonic()

    def _schedule_flush_check(self) -> None:
        self._flush_timer_id = self.after(FLUSH_CHECK_INTERVAL_MS, self._flush_check)

    def _flush_check(self) -> None:
        if self._dirty_filename is not None and time.monotonic() - self._dirty_since >= FLUSH_IDLE_SECONDS:
            self._flush_current(sync=False)
        self._schedule_flush_check()

    def _flush_current(self, sync: bool) -> None:
        """Write out the currently-dirty image's cells.

        Async path takes a shallow snapshot of the cell list and writes it on a
        background thread (measured ~223ms on the densest real image — enough to
        stutter the UI if done on the main thread). A concurrent edit to a cell
        dict already handed to the snapshot (in-place mutation, not list
        add/remove) can race with the in-flight json.dumps; CPython's GIL keeps
        that from corrupting the write, and any such field lands correctly on the
        *next* flush regardless — an acceptable tradeoff for a single-user desktop
        tool versus deep-copying ~17MB+ on every debounce tick.
        """
        if self._dirty_filename is None:
            return
        filename = self._dirty_filename
        self._dirty_filename = None
        self._dirty_since = None
        if sync:
            self.manifest.save_cells(filename, self.current_cells)
        else:
            snapshot = list(self.current_cells)
            threading.Thread(target=self.manifest.save_cells, args=(filename, snapshot), daemon=True).start()

    # ------------------------------------------------------------------ #
    # Queue panel + live readiness dots (polls the background worker thread's
    # ProcessingQueue/Manifest — never touched directly from that thread, only read)
    # ------------------------------------------------------------------ #
    def _schedule_queue_poll(self) -> None:
        self._queue_poll_id = self.after(QUEUE_POLL_MS, self._poll_queue)

    def _poll_queue(self) -> None:
        self._refresh_sample_dots()
        self._refresh_queue_panel()
        self._check_newly_done()
        self._schedule_queue_poll()

    def _refresh_queue_panel(self) -> None:
        snap = self.queue.snapshot()
        if not snap.items:
            # Nothing queued or in flight — Start/Stop has nothing to act on.
            # Showing "Stop" here (the old behavior) implied something was
            # running that a click could halt, which wasn't true.
            self.queue_toggle_btn.configure(text="Inactive", state="disabled")
        else:
            self.queue_toggle_btn.configure(text=("Stop" if snap.running else "Start"), state="normal")

        signature = [(it.filename, it.status) for it in snap.items]
        if signature == self._queue_display_signature:
            # Nothing actually changed since the last poll — leave the rows
            # completely untouched. Rebuilding unconditionally on every 500ms
            # tick (the old behavior) reset the scroll position to the top and
            # clobbered selection mid-gesture, breaking both scrolling and
            # Shift-click range-select.
            return

        scroll_top = self.queue_canvas.yview()[0]
        self._queue_display_items = snap.items
        self._queue_display_signature = signature
        # Drop selection for anything no longer in the queue (completed, or
        # claimed by the worker and finished) rather than let it accumulate.
        self._queue_selected &= {it.filename for it in snap.items}
        self._rebuild_queue_rows()
        self.queue_canvas.yview_moveto(scroll_top)

    def _rebuild_queue_rows(self) -> None:
        for child in self.queue_rows_frame.winfo_children():
            child.destroy()
        self._queue_row_widgets = {}
        for item in self._queue_display_items:
            row = tk.Frame(self.queue_rows_frame, bg=ROW_BG, highlightthickness=2,
                            highlightbackground=ROW_BG, highlightcolor=ROW_BG)
            row.pack(fill="x")
            label = tk.Label(row, text=f"{item.sf.prefix} · {item.sf.channel}", anchor="w", bg=ROW_BG)
            label.pack(side="left", fill="x", expand=True, padx=(6, 4), pady=3)
            for widget in (row, label):
                widget.bind("<Button-1>", lambda e, fn=item.filename: self._on_queue_row_click(fn, e))
                self._bind_wheel_scroll(widget, self.queue_canvas)
            self._queue_row_widgets[item.filename] = {"row": row, "label": label}
        self._refresh_queue_row_styles()

    def _refresh_queue_row_styles(self) -> None:
        for item in self._queue_display_items:
            widgets = self._queue_row_widgets.get(item.filename)
            if widgets is None:
                continue
            bg = QUEUE_PROCESSING_BG if item.status == "processing" else ROW_BG
            border = QUEUE_SELECTED_BORDER if item.filename in self._queue_selected else bg
            widgets["row"].configure(bg=bg, highlightbackground=border, highlightcolor=border)
            widgets["label"].configure(bg=bg)

    def _on_queue_row_click(self, filename: str, event) -> None:
        # Standard multi-select paradigm, hand-rolled since these are plain
        # Frames/Labels, not a native Listbox: plain click selects only this row;
        # Ctrl-click toggles it in/out of the selection; Shift-click selects the
        # contiguous range from the last-clicked row to this one. Tk's modifier
        # bitmask (Shift=0x0001, Control=0x0004) is consistent across platforms.
        shift = bool(event.state & 0x0001)
        ctrl = bool(event.state & 0x0004)
        order = [item.filename for item in self._queue_display_items]

        if shift and self._queue_last_clicked in order and filename in order:
            i0, i1 = order.index(self._queue_last_clicked), order.index(filename)
            lo, hi = sorted((i0, i1))
            self._queue_selected = set(order[lo:hi + 1])
        elif ctrl:
            if filename in self._queue_selected:
                self._queue_selected.discard(filename)
            else:
                self._queue_selected.add(filename)
            self._queue_last_clicked = filename
        else:
            self._queue_selected = {filename}
            self._queue_last_clicked = filename

        self._refresh_queue_row_styles()

    def _check_newly_done(self) -> None:
        # Single atomic snapshot call before iterating — the worker thread only
        # ever does single-key dict assignments (`self.data["images"][fn] = ...`),
        # and CPython's GIL keeps a `dict(...)` copy from tearing across that, same
        # reasoning as the debounced-save snapshot in `_flush_current`. Iterating
        # the *live* dict directly here would risk "dictionary changed size during
        # iteration" if a new image finishes mid-poll.
        images = dict(self.manifest.data["images"])
        done_now = {fn for fn, info in images.items() if info.get("status") == "done"}
        newly_done = done_now - self._known_done
        self._known_done = done_now
        if self.current_filename in newly_done:
            self.current_cells = self.manifest.load_cells(self.current_filename)
            self._update_counts()
            self._redraw(full=True)

    def _selected_queue_filenames(self) -> set[str]:
        return set(self._queue_selected)

    def _toggle_queue_running(self) -> None:
        if self.queue.is_running:
            self.queue.stop()
        else:
            self.queue.start()
        self._refresh_queue_panel()

    def _queue_move_up(self) -> None:
        filenames = self._selected_queue_filenames()
        if filenames:
            self.queue.move_up(filenames)
            self._refresh_queue_panel()

    def _queue_move_down(self) -> None:
        filenames = self._selected_queue_filenames()
        if filenames:
            self.queue.move_down(filenames)
            self._refresh_queue_panel()

    def _queue_send_to_top(self) -> None:
        filenames = self._selected_queue_filenames()
        if filenames:
            self.queue.send_to_front(filenames)
            self._refresh_queue_panel()

    def _queue_send_to_bottom(self) -> None:
        filenames = self._selected_queue_filenames()
        if filenames:
            self.queue.send_to_back(filenames)
            self._refresh_queue_panel()

    # ------------------------------------------------------------------ #
    # Export (File > Export Data...)
    # ------------------------------------------------------------------ #
    def export_data(self) -> None:
        if not self.samples:
            messagebox.showinfo("Export Data", "No samples in this folder yet.", parent=self)
            return

        dialog = _ExportDialog(self, self.samples, self._channel_status)
        if dialog.result is None:
            return
        prefixes, include_summary, include_cells = dialog.result

        path_str = filedialog.asksaveasfilename(
            title="Export Data",
            initialfile=f"{self.folder.name}_export.xlsx",
            defaultextension=".xlsx",
            filetypes=[("Excel Workbook", "*.xlsx")],
            parent=self,
        )
        if not path_str:
            return
        path = Path(path_str)

        summary_rows, cell_rows = export.build_export_rows(
            prefixes, self.samples, self.manifest, self._channel_status)
        try:
            export.export_xlsx(path, summary_rows if include_summary else None,
                                cell_rows if include_cells else None)
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc), parent=self)
            return

        messagebox.showinfo("Export complete", f"Wrote:\n{path.name}", parent=self)
        self.statusbar.set_message(f"Exported {len(prefixes)} sample(s) to {path.name}")

    def close(self) -> None:
        """Call before tearing this panel down (folder switch or app exit) — flushes
        synchronously so a quit can't drop the last edit."""
        if self._flush_timer_id is not None:
            self.after_cancel(self._flush_timer_id)
            self._flush_timer_id = None
        if self._overlay_timer_id is not None:
            self.after_cancel(self._overlay_timer_id)
            self._overlay_timer_id = None
        if self._queue_poll_id is not None:
            self.after_cancel(self._queue_poll_id)
            self._queue_poll_id = None
        if self.current_filename is not None:
            self._save_ui_state()
        self._flush_current(sync=True)
