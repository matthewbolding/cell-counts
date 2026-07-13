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

1. **Sign in.** You'll be prompted for a username and password — this is the single
   shared login for the compute server; ask whoever set up the server for it.
2. **Pick a folder.** A folder picker opens — choose the folder containing your
   `{PREFIX}_{CCK,CHR,SNAP}.tif` images (e.g. `A1_SNAP.tif`, `A1_CCK.tif`, ...).
3. **Wait.** The app hashes every recognized image and skips anything already
   processed (tracked in a `cellcounts.json` file it creates in that folder — don't
   delete it, it's what makes re-opening the same folder fast). Anything new gets
   uploaded to the server and segmented.

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

## What you get

A `cellcounts.json` in the folder you picked, holding every recognized image's
processing status and every detected cell as a polygon outline. There is no
review/editing screen yet in this version — that (draw/delete cells, mask vs.
outline, color picker, coexpression view, export) is coming in a later update on top
of this same file, so nothing you do now needs to be redone once it lands.

## Troubleshooting

- **"Authentication failed"** — wrong username/password; re-launch the app to be
  prompted again.
- **"Could not reach server"** — the server may be down, or `research.matthewbolding.com`
  unreachable from your network; check with whoever runs the server.
- The app never writes your password to disk — you'll be asked again every launch.
