#!/usr/bin/env python3
"""
cell_review.py — standalone GUI for reviewing cell detections.

This is meant to run on a plain Windows laptop with no GPU, no virtual
environment, and no admin rights — just a normal Python install.

What it does
------------
count_cells.py (run separately, on a machine with a GPU) writes a `review/`
folder full of small JSON files (one per image) plus plain background PNGs.
Each JSON lists every detection the model made as a polygon outline, tagged
"kept" (shown red — the model thinks it's a cell) or "filtered" (shown blue —
a brightness/locality filter voided it as noise). This program opens that
folder, draws the polygons on top of the background image, and lets you
click a shape to flip it between red and blue. Every click is saved to disk
immediately, so there's no separate "save" step and nothing to lose if the
window is closed.

One-time setup on the Windows machine (Command Prompt, no admin needed)
-------------------------------------------------------------------------
    1. Install Python from https://www.python.org/downloads/ if it isn't
       already there. On the installer's first screen, just click
       "Install Now" — that installs for your user only, no admin needed,
       and includes Tkinter (this program's GUI toolkit) by default.
    2. Install the one extra package this script needs:
           pip install --user pillow
       (No virtual environment needed — this puts it in your user
       site-packages.)

Running it
----------
    python cell_review.py                     # then pick the "review" folder
    python cell_review.py path\\to\\review       # skip the folder picker

Controls
--------
    Click a shape           toggle it between cell (red) and not-a-cell (blue)
    Left-drag a rectangle   select every detection whose centre falls inside,
                             then choose to mark them all as cells or not cells
    Hold right mouse button hide all outlines while held, to see the plain image
    Prev / Next buttons, or Left / Right arrow keys   change image
    Mouse wheel             scroll
    Ctrl + mouse wheel      zoom (or the -, Fit, + buttons)
    Fit button              portrait images fit to the window's width,
                             landscape images fit to the window's height
    Image list (right)      jump straight to an image
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageTk
except ImportError:
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror(
        "Cell Review",
        "This program needs the 'Pillow' package, which isn't installed yet.\n\n"
        "Open Command Prompt and run:\n\n    pip install --user pillow\n\n"
        "Then run this program again.",
    )
    sys.exit(1)

try:
    RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # older Pillow
    RESAMPLE = Image.LANCZOS

KEPT_COLOR = "#ff3b3b"      # red: model believes this is a cell
FILTERED_COLOR = "#33aaff"  # blue: a filter voided this detection
MIN_SCALE, MAX_SCALE = 0.02, 8.0
ZOOM_STEP = 1.25
DRAG_THRESHOLD = 4  # canvas px of movement before a left-button press becomes a drag-select


def _point_in_polygon(x: float, y: float, poly: list[list[float]]) -> bool:
    """Ray-casting point-in-polygon test; poly is a list of [x, y] pairs."""
    inside = False
    n = len(poly)
    xj, yj = poly[-1]
    for xi, yi in poly:
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        xj, yj = xi, yi
    return inside


def _point_in_any_ring(x: float, y: float, rings: list[list[list[float]]]) -> bool:
    """True if (x, y) falls inside any of a cell's boundary rings.

    A detection can be made of more than one disconnected pixel cluster (rare,
    but cellpose does occasionally give one label to two separate blobs), so a
    cell's shape is a list of independent rings rather than one polygon.
    """
    return any(_point_in_polygon(x, y, ring) for ring in rings)


def _looks_like_review_file(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "cells" in data and "background" in data


class ReviewApp(tk.Tk):
    def __init__(self, data_dir: Path):
        super().__init__()
        self.title("Cell Review")
        self.geometry("1280x860")
        self.minsize(900, 600)

        self.data_dir = data_dir
        self.files: list[Path] = []
        self.idx = 0
        self.scale = 1.0
        self.current: dict | None = None
        self.base_image: Image.Image | None = None
        self.photo = None  # keep a reference so Tk doesn't garbage-collect it
        self.img_w = self.img_h = 1
        self._suppress_listbox_event = False
        self._drag_start = None    # canvas coords where the left button went down
        self._drag_rect_id = None  # id of the in-progress selection rectangle
        self._dragging = False     # True once the drag has moved past the click threshold

        self._build_menu()
        self._build_ui()

        # Caller (main()) has already confirmed data_dir has valid review files.
        self._set_folder(data_dir, warn_if_empty=False)

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Open Folder...", command=self._open_folder, accelerator="Ctrl+O")
        filemenu.add_separator()
        filemenu.add_command(label="Quit", command=self.destroy)
        menubar.add_cascade(label="File", menu=filemenu)
        self.config(menu=menubar)
        self.bind("<Control-o>", lambda e: self._open_folder())

    def _build_ui(self) -> None:
        main = ttk.Frame(self)
        main.pack(fill="both", expand=True)

        # ---- left: canvas + nav bar ----
        left = ttk.Frame(main)
        left.pack(side="left", fill="both", expand=True)

        canvas_frame = ttk.Frame(left)
        canvas_frame.pack(fill="both", expand=True, padx=8, pady=8)
        canvas_frame.rowconfigure(0, weight=1)
        canvas_frame.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(canvas_frame, bg="#1a1a1a", highlightthickness=0, cursor="hand2")
        vbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        hbar = ttk.Scrollbar(canvas_frame, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")

        self.canvas.bind("<ButtonPress-1>", self._on_left_press)
        self.canvas.bind("<B1-Motion>", self._on_left_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_left_release)
        self.canvas.bind("<ButtonPress-3>", self._hide_outlines)
        self.canvas.bind("<ButtonRelease-3>", self._show_outlines)
        self.canvas.bind("<MouseWheel>", self._on_wheel)                 # Windows/Mac
        self.canvas.bind("<Shift-MouseWheel>", self._on_shift_wheel)
        self.canvas.bind("<Control-MouseWheel>", self._on_ctrl_wheel)
        self.canvas.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))   # Linux
        self.canvas.bind("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))

        nav = ttk.Frame(left)
        nav.pack(fill="x", padx=8, pady=(0, 8))
        self.prev_btn = ttk.Button(nav, text="◀ Prev", command=self._prev)
        self.prev_btn.pack(side="left")
        self.next_btn = ttk.Button(nav, text="Next ▶", command=self._next)
        self.next_btn.pack(side="left", padx=(6, 0))
        self.pos_label = ttk.Label(nav, text="")
        self.pos_label.pack(side="left", padx=12)

        zoomf = ttk.Frame(nav)
        zoomf.pack(side="right")
        ttk.Button(zoomf, text="−", width=3, command=lambda: self._zoom(1 / ZOOM_STEP)).pack(side="left")
        ttk.Button(zoomf, text="Fit", command=self._fit).pack(side="left", padx=4)
        ttk.Button(zoomf, text="+", width=3, command=lambda: self._zoom(ZOOM_STEP)).pack(side="left")

        self.bind("<Left>", lambda e: self._prev())
        self.bind("<Right>", lambda e: self._next())
        self.bind("<plus>", lambda e: self._zoom(ZOOM_STEP))
        self.bind("<KP_Add>", lambda e: self._zoom(ZOOM_STEP))
        self.bind("<minus>", lambda e: self._zoom(1 / ZOOM_STEP))
        self.bind("<KP_Subtract>", lambda e: self._zoom(1 / ZOOM_STEP))
        self.bind("<0>", lambda e: self._fit())

        # ---- right: sidebar ----
        right = ttk.Frame(main, width=290)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        pad = dict(padx=12, pady=(10, 0))
        self.filename_label = ttk.Label(right, text="", font=("", 11, "bold"), wraplength=260)
        self.filename_label.pack(anchor="w", **pad)

        counts = ttk.Frame(right)
        counts.pack(fill="x", padx=12, pady=(12, 4))
        self.red_var = tk.StringVar(value="0")
        self.blue_var = tk.StringVar(value="0")
        self.total_var = tk.StringVar(value="0")
        self.edited_var = tk.StringVar(value="0")
        self._count_row(counts, KEPT_COLOR, "Cells", self.red_var)
        self._count_row(counts, FILTERED_COLOR, "Not cells", self.blue_var)
        ttk.Separator(counts).pack(fill="x", pady=6)
        self._plain_row(counts, "Total detections", self.total_var)
        self._plain_row(counts, "Manually corrected", self.edited_var)

        instructions = (
            "Click a shape to switch it between a cell (red) and not a cell "
            "(blue). Click it again to switch it back. All changes save "
            "as they are made.\n\n"
            "Drag a rectangle to select several detections at once and mark "
            "them all as cells or not cells.\n\n"
            "Hold the right mouse button to hide the outlines and see the "
            "plain image."
        )
        ttk.Label(right, text=instructions, wraplength=260, foreground="#666").pack(
            anchor="w", padx=12, pady=(12, 0))

        listf = ttk.Frame(right)
        listf.pack(fill="both", expand=True, padx=12, pady=(14, 12))
        ttk.Label(listf, text="Images", font=("", 10, "bold")).pack(anchor="w")
        lbframe = ttk.Frame(listf)
        lbframe.pack(fill="both", expand=True, pady=(4, 0))
        self.listbox = tk.Listbox(lbframe, exportselection=False)
        lb_scroll = ttk.Scrollbar(lbframe, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=lb_scroll.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        lb_scroll.pack(side="right", fill="y")
        self.listbox.bind("<<ListboxSelect>>", self._on_listbox_select)

    def _count_row(self, parent, color, label, var) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        tk.Canvas(row, width=14, height=14, highlightthickness=1,
                 highlightbackground="#888", bg=color).pack(side="left")
        ttk.Label(row, text=" " + label).pack(side="left")
        ttk.Label(row, textvariable=var, font=("", 10, "bold")).pack(side="right")

    def _plain_row(self, parent, label, var) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=label, foreground="#666").pack(side="left")
        ttk.Label(row, textvariable=var).pack(side="right")

    # ------------------------------------------------------------------ #
    # Folder / file handling
    # ------------------------------------------------------------------ #
    def _open_folder(self) -> None:
        chosen = filedialog.askdirectory(
            title="Select the 'review' folder with the cell data",
            initialdir=str(self.data_dir) if self.data_dir else None)
        if chosen:
            self._set_folder(Path(chosen), warn_if_empty=True)

    def _set_folder(self, data_dir: Path, warn_if_empty: bool) -> bool:
        files = sorted(p for p in data_dir.glob("*.json") if _looks_like_review_file(p))
        if not files:
            if warn_if_empty:
                messagebox.showerror("Cell Review", f"No review data found in:\n{data_dir}")
            return False
        self.data_dir = data_dir
        self.files = files
        self.listbox.delete(0, "end")
        for p in self.files:
            self.listbox.insert("end", p.stem)
        self._load_image(0)
        return True

    # ------------------------------------------------------------------ #
    # Loading / rendering an image
    # ------------------------------------------------------------------ #
    def _load_image(self, idx: int) -> None:
        idx = max(0, min(idx, len(self.files) - 1))
        self.idx = idx
        path = self.files[idx]
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_path"] = path

        bg_path = (path.parent / data["background"]).resolve()
        self.base_image = Image.open(bg_path).convert("RGB")
        self.img_w, self.img_h = self.base_image.size

        for cell in data["cells"]:
            xs = [pt[0] for ring in cell["polygons"] for pt in ring]
            ys = [pt[1] for ring in cell["polygons"] for pt in ring]
            cell["_bbox"] = (min(xs), min(ys), max(xs), max(ys))
            cell["_items"] = []

        self.current = data

        self._suppress_listbox_event = True
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(idx)
        self.listbox.see(idx)
        self._suppress_listbox_event = False

        self.filename_label.configure(text=data["image"])
        self.pos_label.configure(text=f"{idx + 1} / {len(self.files)}")
        self.prev_btn.configure(state="normal" if idx > 0 else "disabled")
        self.next_btn.configure(state="normal" if idx < len(self.files) - 1 else "disabled")
        self.title(f"Cell Review — {path.stem}")

        self._fit()
        self._update_counts()

    def _render(self) -> None:
        self.canvas.delete("all")
        w = max(1, round(self.img_w * self.scale))
        h = max(1, round(self.img_h * self.scale))
        resized = self.base_image.resize((w, h), RESAMPLE) if self.scale != 1.0 else self.base_image
        self.photo = ImageTk.PhotoImage(resized)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo, tags="bg")
        self.canvas.configure(scrollregion=(0, 0, w, h))

        line_w = max(1, min(4, round(1.5 * self.scale)))
        for cell in self.current["cells"]:
            edited = cell["status"] != cell["original_status"]
            color = KEPT_COLOR if cell["status"] == "kept" else FILTERED_COLOR
            items = []
            for ring in cell["polygons"]:
                pts = []
                for x, y in ring:
                    pts.append(x * self.scale)
                    pts.append(y * self.scale)
                items.append(self.canvas.create_polygon(
                    *pts, outline=color, fill="", width=line_w + (1 if edited else 0),
                    tags="outline"))
            cell["_items"] = items

    def _fit(self) -> None:
        """Fit the image to the window: span its width if portrait, its height if
        landscape (square counts as portrait), scrolling to see the rest."""
        self.update_idletasks()
        vw = max(self.canvas.winfo_width(), 100)
        vh = max(self.canvas.winfo_height(), 100)
        if self.img_h >= self.img_w:
            scale = vw / self.img_w
        else:
            scale = vh / self.img_h
        self.scale = max(min(scale, MAX_SCALE), MIN_SCALE)
        self._render()

    # ------------------------------------------------------------------ #
    # Zoom / pan
    # ------------------------------------------------------------------ #
    def _zoom(self, factor: float, anchor: tuple[float, float] | None = None) -> None:
        if self.current is None:
            return
        new_scale = max(min(self.scale * factor, MAX_SCALE), MIN_SCALE)
        if abs(new_scale - self.scale) < 1e-9:
            return
        if anchor is None:
            anchor = (self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2)
        cx = self.canvas.canvasx(anchor[0])
        cy = self.canvas.canvasy(anchor[1])
        img_x, img_y = cx / self.scale, cy / self.scale

        self.scale = new_scale
        self._render()

        new_w = max(self.img_w * self.scale, 1)
        new_h = max(self.img_h * self.scale, 1)
        target_cx = img_x * self.scale
        target_cy = img_y * self.scale
        fx = max(0.0, min(1.0, (target_cx - anchor[0]) / new_w))
        fy = max(0.0, min(1.0, (target_cy - anchor[1]) / new_h))
        self.canvas.xview_moveto(fx)
        self.canvas.yview_moveto(fy)

    def _on_wheel(self, event) -> None:
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def _on_shift_wheel(self, event) -> None:
        self.canvas.xview_scroll(-1 if event.delta > 0 else 1, "units")

    def _on_ctrl_wheel(self, event) -> None:
        factor = ZOOM_STEP if event.delta > 0 else 1 / ZOOM_STEP
        self._zoom(factor, anchor=(event.x, event.y))

    # ------------------------------------------------------------------ #
    # Clicking / toggling cells / rectangle drag-select / hiding outlines
    # ------------------------------------------------------------------ #
    def _on_left_press(self, event) -> None:
        if self.current is None:
            return
        self._drag_start = (self.canvas.canvasx(event.x), self.canvas.canvasy(event.y))
        self._drag_rect_id = None
        self._dragging = False

    def _on_left_drag(self, event) -> None:
        if self.current is None or self._drag_start is None:
            return
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        sx, sy = self._drag_start
        if not self._dragging:
            if abs(cx - sx) < DRAG_THRESHOLD and abs(cy - sy) < DRAG_THRESHOLD:
                return
            self._dragging = True
        if self._drag_rect_id is None:
            self._drag_rect_id = self.canvas.create_rectangle(
                sx, sy, cx, cy, outline="#ffffff", dash=(4, 2), width=2)
        else:
            self.canvas.coords(self._drag_rect_id, sx, sy, cx, cy)

    def _on_left_release(self, event) -> None:
        if self.current is None or self._drag_start is None:
            return
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        sx, sy = self._drag_start
        was_dragging = self._dragging

        if self._drag_rect_id is not None:
            self.canvas.delete(self._drag_rect_id)
        self._drag_rect_id = None
        self._drag_start = None
        self._dragging = False

        if was_dragging:
            self._select_rectangle(sx, sy, cx, cy)
        else:
            ix, iy = cx / self.scale, cy / self.scale
            cell = self._hit_test(ix, iy)
            if cell is not None:
                self._toggle(cell)

    def _select_rectangle(self, x0: float, y0: float, x1: float, y1: float) -> None:
        """Prompt to bulk-mark every detection whose centroid falls in the
        image-space rectangle spanned by the two (canvas-space) corners."""
        left, right = sorted((x0 / self.scale, x1 / self.scale))
        top, bottom = sorted((y0 / self.scale, y1 / self.scale))
        selected = [
            cell for cell in self.current["cells"]
            if left <= cell["centroid"][0] <= right and top <= cell["centroid"][1] <= bottom
        ]
        if not selected:
            return
        mark_as_cells = messagebox.askyesnocancel(
            "Mark detections",
            f"{len(selected)} detection(s) selected.\n\n"
            "Mark all as cells (Yes) or not cells (No)? Cancel to leave them unchanged.")
        if mark_as_cells is None:
            return
        new_status = "kept" if mark_as_cells else "filtered"
        for cell in selected:
            cell["status"] = new_status
        self._save_current()
        self._render()
        self._update_counts()

    def _hide_outlines(self, event=None) -> None:
        self.canvas.itemconfigure("outline", state="hidden")

    def _show_outlines(self, event=None) -> None:
        self.canvas.itemconfigure("outline", state="normal")

    def _hit_test(self, x: float, y: float) -> dict | None:
        candidates = []
        pad = 2.0
        for cell in self.current["cells"]:
            minx, miny, maxx, maxy = cell["_bbox"]
            if minx - pad <= x <= maxx + pad and miny - pad <= y <= maxy + pad:
                if _point_in_any_ring(x, y, cell["polygons"]):
                    candidates.append(cell)
        if candidates:
            return min(candidates, key=lambda c: c["area"])

        # Nothing directly under the click: fall back to the nearest centroid,
        # forgiving for very small cells or an imprecise click near an edge.
        tol = 10.0 / self.scale
        best, best_d = None, tol
        for cell in self.current["cells"]:
            dx = cell["centroid"][0] - x
            dy = cell["centroid"][1] - y
            d = (dx * dx + dy * dy) ** 0.5
            if d < best_d:
                best, best_d = cell, d
        return best

    def _toggle(self, cell: dict) -> None:
        cell["status"] = "filtered" if cell["status"] == "kept" else "kept"
        edited = cell["status"] != cell["original_status"]
        color = KEPT_COLOR if cell["status"] == "kept" else FILTERED_COLOR
        line_w = max(1, min(4, round(1.5 * self.scale))) + (1 if edited else 0)
        for item in cell["_items"]:
            self.canvas.itemconfig(item, outline=color, width=line_w)
        self._save_current()
        self._update_counts()

    def _update_counts(self) -> None:
        cells = self.current["cells"]
        n_kept = sum(1 for c in cells if c["status"] == "kept")
        n_filtered = len(cells) - n_kept
        n_edited = sum(1 for c in cells if c["status"] != c["original_status"])
        self.red_var.set(str(n_kept))
        self.blue_var.set(str(n_filtered))
        self.total_var.set(str(len(cells)))
        self.edited_var.set(str(n_edited))

    def _save_current(self) -> None:
        data = self.current
        out = {
            "schema_version": data["schema_version"],
            "image": data["image"],
            "width": data["width"],
            "height": data["height"],
            "background": data["background"],
            "cells": [{k: v for k, v in c.items() if not k.startswith("_")} for c in data["cells"]],
        }
        data["_path"].write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    def _prev(self) -> None:
        if self.idx > 0:
            self._load_image(self.idx - 1)

    def _next(self) -> None:
        if self.idx < len(self.files) - 1:
            self._load_image(self.idx + 1)

    def _on_listbox_select(self, event) -> None:
        if self._suppress_listbox_event:
            return
        sel = self.listbox.curselection()
        if sel and sel[0] != self.idx:
            self._load_image(sel[0])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data", nargs="?", type=Path, default=None,
                    help="folder with the review/*.json files (e.g. results/review); "
                         "if omitted, a folder picker opens")
    args = ap.parse_args(argv)

    data_dir = args.data
    if data_dir is None:
        picker = tk.Tk()
        picker.withdraw()
        chosen = filedialog.askdirectory(title="Select the 'review' folder with the cell data")
        picker.destroy()
        if not chosen:
            return 0
        data_dir = Path(chosen)

    if not data_dir.is_dir():
        print(f"Not a folder: {data_dir}", file=sys.stderr)
        return 1

    if not any(_looks_like_review_file(p) for p in data_dir.glob("*.json")):
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Cell Review", f"No review data found in:\n{data_dir}")
        root.destroy()
        return 1

    app = ReviewApp(data_dir)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
