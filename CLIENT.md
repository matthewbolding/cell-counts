# CLIENT.md — reviewer install & operation

The client is a small desktop app (Tkinter — ships with a normal Python install, no
extra GUI toolkit needed). It talks to the compute server described in
[SERVER.md](SERVER.md); you don't need a GPU, admin rights, or a virtual environment
to run it.

## One-time setup

1. Install Python from <https://www.python.org/downloads/> if it isn't already on
   your machine. On the installer's first screen, click "Install Now" — that
   installs for your user only (no admin needed) and includes Tkinter by default.
2. Install the packages this app needs (no virtual environment needed — this puts
   them in your user site-packages):

   - **Windows**: `py -m pip install --user -r client/requirements.txt`
   - **macOS**: `python3 -m pip install --user -r client/requirements.txt`

## Running it

```
python client/app.py
```

1. **Sign in.** One dialog asks for the server address (e.g.
   `https://research.yourdomain.com`), username, and password — ask whoever set up
   the server for these. The server address you enter is remembered and pre-filled
   next time. Check **Remember me on this computer** to skip retyping the
   username/password too — that's saved to `~/.cellcounts/credentials.json` on
   your machine only (never part of the repo), readable only by your own account.
   Leave it unchecked, or uncheck it and sign in again, to forget a
   previously-remembered login. There's also an
   **Open the same folder as last time** checkbox (grayed out until you've opened
   at least one folder) that skips the folder picker below entirely and jumps
   straight back into whichever folder you had open last — handy when you're in
   and out of the same folder repeatedly.
2. **Pick a folder.** Unless you checked the box above, a folder picker opens —
   choose the folder containing your `{PREFIX}_{CCK,CHR,SNAP}.tif` images (e.g.
   `A1_SNAP.tif`, `A1_CCK.tif`, ...).
3. **The review screen opens right away** — you don't have to wait for processing
   to finish before you can look around. First, a quick check-in with the server
   for anything left in progress from last time (in case it actually finished
   while the app was closed — this is fast, just a status check, not a re-hash of
   anything). Then, in the background, the app hashes every recognized image and
   skips anything already processed (tracked in a `cellcounts.json` file it
   creates in that folder — don't delete it, it's what makes re-opening the same
   folder fast); anything new or changed gets uploaded first, then segmented.
   Once a file finishes uploading it's on the server for good — closing the app
   doesn't stop or lose it, segmentation keeps running there regardless, and
   reopening the folder later just checks in on results instead of uploading
   anything twice. See "Reviewing" below for the readiness dots that show you
   what's done, in progress, or still pending.

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
  channels. Zoomed into a region on CCK and flip to CHR (or Composite)? You'll see
  that same region — pan/zoom carries over across tabs *within a sample*, since
  they're simultaneous channels of the same physical field of view. It only resets
  to fit-to-window when you pick a different sample. Each sample row has three
  small readiness dots, one per channel, in
  SNAP/CCK/CHR order (matching the tab order above the image):
  - **red** — not ready (not yet processed, or that channel doesn't exist for this
    sample).
  - **blue** — currently uploading/being segmented.
  - **green** — done; click the dot (or the sample name) to jump straight to it.
  You can open a not-yet-processed channel any time — you'll see the plain image
  with no outlines yet, and the cells will pop in automatically once that channel
  finishes, if you're still looking at it. With a lot of samples the list scrolls
  (mouse wheel or the scrollbar) the same way the Queue list below it does.
- **Composite tab** — a fifth tab, next to SNAP/CCK/CHR, that lights up once all
  three channels for that sample are done. Shows CCK and CHR overlaid on the SNAP
  image at once (translucent, so real overlap between the two reads as a blended
  color), with every cell counted as coexpressing outlined in white. The right
  panel shows the actual numbers: coexpressing pairs, SNAP kept count (the
  population these are measured against), and the coexpression rate. This tab is
  view-only — Review/Draw/Delete are disabled while it's open, since editing
  always happens on one real channel, never on this derived view.
- **Queue** (left, bottom) — the files still waiting to be *uploaded*, in the
  order they'll go, styled the same as the Samples list above it. This is
  upload order, not segmentation order — a file drops off this list as soon as
  it's finished uploading, even though it may still take a while to actually
  segment on the server (watch the readiness dots for that). Click to select
  one; Ctrl-click to add/remove individual files from the selection;
  Shift-click to select a whole range — the standard paradigm. The row
  currently uploading is tinted amber rather than labeled — it also can't be
  reordered or reprioritized past. Four buttons reorder the selection: **▲** (up
  one), **▼** (down one), **▲▲** (send to top), **▼▼** (send to bottom). A
  multi-selection moves as a block — the items you selected keep their order
  relative to each other, they just all shift together. **Start/Stop** pauses
  and resumes uploading; Stop finishes whatever file is already mid-upload
  before pausing. It has no effect on files already uploaded — those keep
  segmenting on the server regardless. When there's nothing queued or
  uploading, the button reads **Inactive** and is grayed out — there's nothing
  for it to start or
  stop. Your queue order and paused/running state are saved per folder and
  restored next time you open it, same as everything else below.
- **Mode** (top left: Review / Draw / Delete / Rescan):
  - **Review** — click a cell to toggle it between kept and not-a-cell; drag a
    rectangle to select several at once and mark them all together.
  - **Draw** — click to place each corner of a new cell outline; double-click, or
    press Enter, to close it (needs at least 3 points); Esc cancels.
  - **Delete** — click a cell to remove it outright.
  - **Rescan** — got a spot where the model missed cells or merged two together?
    Drag a rectangle around just that region. A dialog asks which Cellpose
    parameters to try — comma-separated **Diameter** (px, `0` = auto) and **Cell
    probability threshold** values, e.g. `0, 15, 30` × `-2, 0, 2` tries all 9
    combinations. Each one is a small, independent upload — only the crop is
    sent, not the whole image, so this is quick even on a huge source file.
    When it's done, the screen switches to reviewing the results: **< Prev /
    Next >** cycles through each combination's proposed cells zoomed into that
    region; click a proposed cell to accept or un-accept it (accepted ones fill
    solid green, undecided ones stay outlined in yellow) — you can accept cells
    from more than one combination before merging. **Merge Accepted** adds
    everything you picked into the real image (tagged so you can tell they came
    from a rescan, with full Ctrl+Z undo support); **Discard Sweep** (or Esc)
    throws the whole thing away with no changes made. Unavailable on the
    Composite tab, same as Draw/Delete.
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

## Exporting

**File > Export Data...** opens a dialog before anything gets written:

- **Samples to export** — every sample in the folder, checked by default (Select
  All / Select None to bulk-toggle), each with the same red/blue/green readiness
  dots as the sidebar so you can see what has data before picking it. Uncheck
  anything you don't want — handy in a large folder where you only care about a
  few samples right now and exporting everything would mean waiting on (and
  scrolling through) a lot of data you don't need yet.
- **Include** — Summary sheet, Cells sheet, or both (at least one is required).

Export works regardless of processing state — a sample doesn't need all three
channels done to be included. Whatever's missing for a given sample shows up as
`###` in the Summary sheet (e.g. a `chr_kept` of `###` means CHR hasn't been
processed for that sample yet) rather than silently dropping the whole row. A
channel that *has* been processed but genuinely detected zero kept cells still
shows a real `0` — those are different, worth-knowing-apart facts. Coexpression
pairs/rate show `###` whenever they can't be computed yet (either CCK or CHR
missing, or a zero-cell SNAP population makes a rate undefined).

Output is one `.xlsx` workbook with your requested sheet(s) — "Summary" (one row
per sample) and/or "Cells" (one row per kept cell across every included
sample/channel with data, with a coexpressing yes/no/`###` column for CCK/CHR
rows). These are the same coexpression numbers shown in the Composite tab,
computed the same way, so the two never disagree.

## What you get

A `cellcounts.json` in the folder you picked (image processing status), one
`<filename>.cells.json` per image (its detected cells), and a small
`cellcounts.queue.json` (queue order and paused/running state) — don't delete
these, they're what makes re-opening the same folder fast and what your review
edits and queue arrangement are saved into.

## Troubleshooting

- **"Authentication failed"** — wrong username/password; re-launch the app to be
  prompted again.
- **"Could not reach server"** — double-check the server address you entered, or
  the server itself may be down/unreachable from your network; check with
  whoever runs it.
- The app only writes your password to disk if you check "Remember me on this
  computer"; leave it unchecked and you'll be asked again every launch.
