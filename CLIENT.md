# CLIENT.md — reviewer install & operation

The client is a small desktop app (Tkinter — ships with a normal Python install, no
extra GUI toolkit needed). It talks to the compute server described in
[SERVER.md](SERVER.md); you don't need a GPU, admin rights, or a virtual environment
to run it.

## One-time setup

1. Install Python from <https://www.python.org/downloads/> if it isn't already on
   your machine. On the installer's first screen, click "Install Now" — that
   installs for your user only (no admin needed) and includes Tkinter by default.
2. Install the one extra package this app needs:

   ```
   pip install --user -r client/requirements.txt
   ```

   (No virtual environment needed — this puts it in your user site-packages.)

## Running it

```
python client/app.py
```

1. **Sign in.** One dialog asks for both the username and password — this is the
   single shared login for the compute server; ask whoever set up the server for
   it. Check **Remember me on this computer** to skip retyping it next time — it's
   saved to `~/.cellcounts/credentials.json` on your machine only (never part of
   the repo), readable only by your own account. Leave it unchecked, or uncheck it
   and sign in again, to forget a previously-remembered login.
2. **Pick a folder.** A folder picker opens — choose the folder containing your
   `{PREFIX}_{CCK,CHR,SNAP}.tif` images (e.g. `A1_SNAP.tif`, `A1_CCK.tif`, ...).
3. **The review screen opens right away** — you don't have to wait for processing
   to finish before you can look around. In the background, the app hashes every
   recognized image and skips anything already processed (tracked in a
   `cellcounts.json` file it creates in that folder — don't delete it, it's what
   makes re-opening the same folder fast); anything new or changed gets uploaded to
   the server and segmented while you browse. See "Reviewing" below for the
   readiness dots that show you what's done, in progress, or still pending.

The bottom of the window has two things to watch:

- **Status bar** (left) — narrates exactly what's happening: hashing, which file is
  uploading and how many chunks are done, waiting on the server, or a finished
  summary.
- **Connection light** (right) — the server connection state:
  - **gray "Idle"** — nothing needs to talk to the server (e.g. everything in this
    folder is already processed).
  - **amber "Connecting…"** — checking the server is reachable.
  - **green "Connected"** — reachable and healthy.
  - **blue "Processing…"** — a file is uploading or being segmented.
  - **red "Error"** — something failed; check the log pane above the status bar for
    details (a per-file error doesn't stop the rest of the batch — everything else
    still gets processed).

Files with a name that doesn't match `{PREFIX}_{CCK,CHR,SNAP}.tif` (wrong suffix,
extra file, etc.) are listed and skipped rather than silently ignored — check the
log pane if the count of processed images looks lower than expected.

## Reviewing

The review screen comes up as soon as you pick a folder (use **View > Show Log** to
check the processing log — e.g. to see why an upload failed — and **View > Show
Review** to come back).

- **Samples** (left, top) — every animal+sample prefix found in this folder (e.g.
  `A3`). Pick one, then a **CCK / CHR / SNAP** tab above the image to switch
  channels. Each sample row has three small readiness dots, one per channel, in
  SNAP/CCK/CHR order (matching the tab order above the image):
  - **red** — not ready (not yet processed, or that channel doesn't exist for this
    sample).
  - **blue** — currently uploading/being segmented.
  - **green** — done; click the dot (or the sample name) to jump straight to it.
  You can open a not-yet-processed channel any time — you'll see the plain image
  with no outlines yet, and the cells will pop in automatically once that channel
  finishes, if you're still looking at it. With a lot of samples the list scrolls
  (mouse wheel or the scrollbar) the same way the Queue list below it does.
- **Queue** (left, bottom) — the files still waiting to be uploaded/segmented, in
  the order they'll run. Click to select one; Ctrl-click to add/remove individual
  files from the selection; Shift-click to select a whole range — the standard
  paradigm. Four buttons reorder the selection: **▲** (up one), **▼** (down one),
  **▲▲** (send to top), **▼▼** (send to bottom). A multi-selection moves as a
  block — the items you selected keep their order relative to each other, they
  just all shift together (only affects files that haven't started yet — the one
  currently "(processing)" can't be reordered or reprioritized past). **Start/Stop**
  pauses and resumes the queue; Stop finishes whatever file is already in flight
  before pausing (there's no way to cancel a file mid-segmentation), so don't
  expect it to stop instantly. Your queue order and paused/running state are saved
  per folder and restored next time you open it, same as everything else below.
- **Mode** (top left: Review / Draw / Delete):
  - **Review** — click a cell to toggle it between kept and not-a-cell; drag a
    rectangle to select several at once and mark them all together.
  - **Draw** — click to place each corner of a new cell outline; double-click, or
    press Enter, to close it (needs at least 3 points); Esc cancels.
  - **Delete** — click a cell to remove it outright.
- **Undo/redo** — **Ctrl+Z** undoes, **Ctrl+Shift+Z** redoes, as many times as you
  like (toggles, deletes, and draws are all covered). A drag-select batch that
  marks several cells at once undoes/redoes as a single step, not cell by cell.
  History is kept per image, so switching channels and back doesn't lose it.
- **Mode: Outline / Mask** button — switches between drawing just the cell boundary
  and filling the whole cell with translucent color.
- **Colors** — one color per channel (SNAP/CCK/CHR), click a swatch to change it.
  Defaults are bright red (SNAP), bright green (CCK), and cyan (CHR); any color you
  pick is remembered per folder and used again next time you open it. Kept cells
  are drawn in their channel's color; cells marked "not a cell" are always a
  neutral gray regardless of channel, so a rejected detection reads the same
  everywhere.
- **View controls**: mouse wheel pans, Shift+wheel pans sideways, Ctrl+wheel zooms
  toward the cursor, or use the −/Fit/+ buttons. Hold the right mouse button to
  temporarily hide all outlines/masks and see the plain image.

Your zoom level, mask/outline choice, channel colors, and last-viewed image are
remembered per folder and restored the next time you open it (stored in
`~/.cellcounts/state.json` on your machine, separate from `cellcounts.json`).
Edits save automatically a moment after you make them — there's no separate save
step, and quitting the app flushes any pending edit first.

## What you get

A `cellcounts.json` in the folder you picked (image processing status), one
`<filename>.cells.json` per image (its detected cells), and a small
`cellcounts.queue.json` (queue order and paused/running state) — don't delete
these, they're what makes re-opening the same folder fast and what your review
edits and queue arrangement are saved into. Coexpression view/export and
crop-based rescan are coming in later updates on top of this same format, so
nothing you do now needs to be redone once they land.

## Troubleshooting

- **"Authentication failed"** — wrong username/password; re-launch the app to be
  prompted again.
- **"Could not reach server"** — the server may be down, or `research.matthewbolding.com`
  unreachable from your network; check with whoever runs the server.
- The app only writes your password to disk if you check "Remember me on this
  computer"; leave it unchecked and you'll be asked again every launch.
