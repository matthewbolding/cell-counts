# Cell Counts web app — design

Cell Counts is one site at **cellcounts.matthewbolding.com**. Researchers sign in
from a browser; nothing is installed on their machines.

## Goals and constraints

- At most a handful of users (expected: 2). Every account is approved by an admin.
- Strong account security: email verification, admin approval, mandatory 2FA,
  robust password reset, Turnstile on every public form, rate limiting.
- The GPU box is never directly reachable from the internet.
- The GPU is shared: users take turns, rescans jump the line, everyone can see the
  queue.
- TIFFs are hosted server-side on the LAN NAS (NFS), with a per-user quota
  (default 150 GB, adjustable per user).
- Email notifications are opt-in per user; only security emails are on by default
  (and those can't be turned off).
- Projects are private to their owner, who can share one with another user as an
  editor or viewer.
- The review screen works like a desktop application: menu bar, side panels,
  status bar, keyboard shortcuts, and no page reloads.

## Deployment shape

```
browser ── HTTPS ── Cloudflare ── Tunnel ── cloudflared ─┐   (the GPU server, one compose project)
                                                         nginx ──── web (Django, gunicorn)
                                                           │          │
                                         X-Accel tiles ────┤       postgres  (local disk)
                                                           │          │
                                                           │    cpu-worker  (verify, tiles, exports, emails)
                                                           │    gpu-worker  (Cellpose; no published ports, no egress)
                                                           │          │
                                                           └──── /data  ← NFS mount of the NAS
```

- **No inbound ports at home.** `cloudflared` dials out to Cloudflare; nginx is the
  only thing it can reach.
- **Workers pull, nothing pushes to them.** Both workers poll the `processing_job`
  table. The GPU worker sits on an internal-only Docker network with Postgres and
  the NAS mount; its model weights are baked into the image at build time.
- **Postgres on local disk, files on the NAS.** Databases on network filesystems
  are slow and risk corruption. A nightly `pg_dump` lands on the NAS.
- **NAS safety check.** `/data/.cellcounts-volume` must exist. If the NFS mount
  is missing, the app refuses to write rather than filling the local disk through
  the empty mount point.

### Storage layout (`/data`)

| Path | Contents |
| --- | --- |
| `staging/<upload_id>.part` | Upload in progress; chunks are written in place at their offsets |
| `originals/<project_id>/<image_id>.tif` | Verified source TIFF |
| `tiles/<image_id>/<rev>/<level>/<col>_<row>.png` | 512 px grayscale tiles, level 0 = full resolution |
| `exports/<export_id>.xlsx` | Generated exports, deleted after 24 h |
| `backups/` | Database backups: the continuous WAL archive, nightly base backups (last 7) and dumps (last 14) |

## Accounts and security

- **django-allauth** handles login, request access (sign-up), email verification,
  password reset, TOTP 2FA, recovery codes and passkeys. Its built-in rate limits
  are tightened, and client IPs come from `CF-Connecting-IP`.
- **Request access → verify email → admin approves.** An unapproved account can't
  log in; it lands on a "waiting for approval" page. Admins can also invite someone
  directly, which sends a set-password link.
- **2FA is mandatory.** Middleware sends any signed-in user without an
  authenticator to the 2FA setup page; nothing else is reachable until it's done.
- **Password reset never skips 2FA.** A reset doesn't log you in, and signing in
  afterwards still asks for the code. If someone loses their authenticator and
  their recovery codes, an admin resets their 2FA from the user portal.
- **Links in emails don't act on GET.** Verify, invite and reset links open a page
  with a button; only the POST changes anything, so university link scanners
  can't burn the token.
- **Turnstile** guards sign-in, request access and the password-reset request.
- **Security emails are always on:** password changed or reset, 2FA changed,
  email changed, and sign-in from a new device.
- Session and CSRF cookies are `Secure` and `HttpOnly` where possible, with a
  strict CSP, `X-Frame-Options: DENY`, HSTS, Argon2 password hashing, and Django
  admin behind the same login and 2FA flow.

## Data model

- `accounts.User` uses email login, plus `approved_at`, `storage_quota_bytes`
  (default 150 GB) and `display_name`.
- `accounts.NotificationPreferences`: `batch_finished`, `job_failures`,
  `daily_summary`, all off by default.
- `accounts.KnownDevice` records past sign-ins (user, user-agent hash, IP prefix) so
  a sign-in from a new device triggers an email.
- `projects.Project` has an owner, a name, and timestamps.
- `projects.Membership` holds a collaborator's role on someone else's project
  (`editor` or `viewer`); the owner's role is implicit.
- `projects.Sample` is one prefix per project, e.g. `A3`.
- `projects.Image` is one channel file: filename, channel, sha256, size,
  `client_mtime`, width, height, `status` (`preparing`, `queued`, `segmenting`,
  `done`, `error`), `error`, `tiles_rev`, `tile_levels`, `cells_version`,
  `segmentation_params`.
- `projects.Cell` is one row per detection: `(image, cell_id)` unique, plus
  `polygons`, `centroid`, `area`, `status`, `filter_reason`, `source`, `edited`,
  `color`, `edited_by`, `edited_at`. The schema matches the `.cells.json` review
  files a folder may already contain, so imports are lossless.
- `projects.ImageLock` is a 60-second edit lease, renewed while an editor has the
  image open.
- `projects.ProjectUserState` holds per-user UI state: last image, render mode,
  channel colors, zoom, selection/draw style.
- `uploads.UploadSession` tracks one file in flight: chunk size, chunks received,
  sha256, and the image it replaces, if any.
- `processing.Job` has `kind` (`prepare`, `segment`, `rescan`, `export`, `purge`),
  `resource` (`cpu`, `gpu`), `priority` (0 = interactive, 10 = bulk), `status`,
  user, project, optional image/sweep/export, `position`, `params`, `result`,
  `error`, attempts, megapixels, and timestamps.
- `processing.QueueState` is a singleton holding the global GPU pause.
  `processing.UserQueueState` holds each user's "Stop Segmenting" flag and
  `last_served_at`.
- `processing.RescanSweep` is one rescan request (rect plus parameter grid).
  `processing.Export` is one export request.

## GPU scheduling

The GPU worker picks the next job like this:

1. Nothing starts if the GPU is paused globally (admin).
2. **Rescans first.** Any queued `rescan` job runs before bulk work, oldest first.
3. **Then take turns.** Among users with queued, unpaused `segment` jobs, the one
   served least recently goes next. Within that user, the lowest `position` wins,
   which is how "▲ / ▼ / ▲▲ / ▼▼" in the Queue panel reorders your own work.
4. A user's "Stop Segmenting" (Process menu) only skips that user's jobs.

The CPU worker runs `prepare` and `export` jobs oldest-first. `prepare` verifies
the sha256, reads dimensions, builds tiles, then queues segmentation, unless
the image's cells were imported from its folder's review files.

If a worker restarts, it re-queues whatever it was running once (`attempts`),
then marks it as failed.

Wait estimates use the median seconds-per-megapixel of the last 50 finished
segmentations.

## Notifications

All of these are off by default, chosen per user in Settings:

- **Batch finished:** sent when a user's last queued job in a project finishes,
  summarizing what completed and what failed since the previous batch email.
- **Failures:** sent right away when one of your jobs fails.
- **Daily summary:** sent once a day if anything of yours ran.

Security emails are not configurable.

## Upload flow (in the browser)

Scan, then hash, then upload:

1. **File → Add Images…** opens a folder picker (or accepts a dropped folder). The
   browser keeps `{PREFIX}_{CCK,CHR,SNAP}.tif` files and lists the rest as skipped.
2. `uploads/plan` with `(name, size, lastModified)` tells the client which files
   are unchanged by that check alone.
3. The rest are hashed with SHA-256 in a Web Worker (hash-wasm, streaming), then
   `uploads/init` either says *skip* (same content already there), *conflict*
   (same name, different content — the user confirms a replace, which discards
   that image's review edits), or returns an upload with the chunks already
   present.
4. 32 MB chunks are uploaded (`PUT`, below Cloudflare's 100 MB cap), then
   `complete`. Several chunks go up at once, in any order (each lands at its own
   offset and is recorded with one atomic update). The browser tunes how many to
   the connection: it adds one while that raises throughput by 10% or more, drops
   back when it doesn't, halves on a failed chunk, and never lets a chunk take
   much over 30 s at the measured speed (`laneTuner.ts`). Uploads resume after an
   interruption: re-adding the folder re-plans, and `init` reports the chunks
   already on the server.
5. Review work already in the folder comes along. A reviewed folder holds a
   `cellcounts.json` (each image's sha256, status and segmentation params) and a
   `<file>.cells.json` per image. Each image is matched to the nearest
   `cellcounts.json` at or above its folder (so a parent of several reviewed
   folders works). If that entry is finished (status `done`) for this exact
   file (the recorded sha256 matches), its `.cells.json` cells go up gzipped
   with `complete`, and the image skips segmentation. If the file is already on
   the server, the cells are imported into it instead (`import-cells`), unless
   someone has already reviewed it in the web app. Unreadable cells are
   dropped and counted rather than failing the whole import.

## Review screen

It's a single page per project:

- **Menu bar:** File (Open Project…, Add Images…, Export Data…, Share Project…),
  View (GPU Queue, Show Log), Process (Stop/Start Uploads, Stop/Start Segmenting),
  plus the account menu on the right.
- **Toolbar:** mode (Review / Draw / Delete / Rescan), Select (Lasso / Rectangle),
  Draw (Freehand / Polygon), Mode: Outline/Mask, channel colors, − / Fit / +.
- **Left:** Samples (rows with SNAP/CCK/CHR readiness dots) above Queue (breathing
  rows, Ctrl/Shift multi-select, ▲ ▲▲ ▼ ▼▼).
- **Center:** SNAP | CCK | CHR | Composite tabs over a dark canvas.
- **Right:** filename, counts, instructions, rescan controls.
- **Bottom:** status message and connection light (Idle / Connecting… / Connected /
  Processing… / Error).

The viewer is a hand-written canvas renderer in TypeScript: viewport origin and
scale, culling, an 8 px level-of-detail cutoff below which cells draw as dots,
and hit-testing, with image tiles drawn underneath. It doesn't use a mapping
library, so the look and interaction are entirely its own.

Edits save as small batched operations a second after the last change. Undo/redo
lives in the browser per image (Ctrl/⌘+Z, Ctrl/⌘+Shift+Z).

## JSON API

All endpoints live under `/api/` and use session auth. Unsafe methods need the
`X-CSRFToken` header. Errors return `{"error": "<code>", "detail": "<message>"}` with
a matching status code. IDs are UUIDs (cells use integer `id`s, unique per image).

### Projects and images

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/projects/<pid>/` | Bootstrap: project, your role, samples with their per-channel images, your UI state |
| GET | `/api/projects/<pid>/status` | Poll (2 s): image statuses/versions plus this project's queue rows |
| GET | `/api/projects/<pid>/samples` | The sample list again, refetched when a poll shows an image the page hasn't seen |
| PUT | `/api/projects/<pid>/ui-state` | Save UI state |
| GET | `/api/images/<iid>/cells` | `{version, cells}` |
| POST | `/api/images/<iid>/cells/ops` | `{base_version, ops}` → `{version}`; needs edit role; 409 `version_conflict` if stale, 423 if someone else holds the lease |
| POST | `/api/images/<iid>/lock` | Acquire/renew lease → `{held_by_me, holder}` |
| DELETE | `/api/images/<iid>/lock` | Release |
| POST | `/api/images/retry` | `{image_ids}` re-queues failed images |
| GET | `/api/samples/<sid>/composite` | Coexpression `{pairs, cck_ids, chr_ids, snap_kept, rate_pct}` |

Image object:

```json
{"id": "…", "filename": "A3_CCK.tif", "channel": "CCK", "status": "done",
 "error": null, "width": 2090, "height": 1690, "cells_version": 4,
 "tiles": {"rev": "a1b2c3d4", "levels": 4, "tile_size": 512}}
```

`tiles` is `null` until tiling finishes. Tile URLs follow the pattern
`/tiles/<iid>/<rev>/<level>/<col>_<row>.png`.

Cell ops (applied in order, all-or-nothing):

```json
{"op": "update", "changes": [{"id": 12, "status": "filtered", "edited": true}]}
{"op": "add", "cells": [{"id": 6027, "polygons": [[[x, y], …]], "centroid": [x, y],
                        "area": 211, "status": "kept", "source": "human", "edited": true}]}
{"op": "remove", "ids": [12, 13]}
```

### Uploads

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/api/projects/<pid>/uploads/plan` | `{files: [{name, size, last_modified}]}` → per-file `unchanged` or `needs_hash`, plus quota |
| POST | `/api/projects/<pid>/uploads/init` | `{filename, size, sha256, last_modified, replace}` → `skip` / `upload` (with `chunks_present`) / 409 `changed` / 413 `quota_exceeded` |
| PUT | `/api/uploads/<uid>/chunks/<n>` | Raw bytes |
| POST | `/api/uploads/<uid>/complete` | Optional JSON body (usually `Content-Encoding: gzip`) `{imported_cells: {cells, params}}` → `{image}` |
| DELETE | `/api/uploads/<uid>` | Abort |
| POST | `/api/images/<iid>/import-cells` | A folder's `.cells.json` review data for an image already uploaded: `{sha256, imported_cells}`; replaces the server's cells only if nobody has edited the image in the web app (`web_edited_at`), otherwise 409 `has_edits`; identical data is a no-op |

### Queue, rescans, exports

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/queue/` | GPU state, running job, per-user queue summary, your jobs with position and ETA |
| POST | `/api/queue/reorder` | `{job_ids}`, your own queued jobs, in the new order |
| POST | `/api/queue/pause` | `{paused}`, your own Stop/Start Segmenting |
| POST | `/api/queue/cancel` | `{job_ids}`; yours, or anyone's if admin |
| POST | `/api/admin/queue/pause` | `{paused}`, global GPU pause (admin) |
| POST | `/api/images/<iid>/rescans` | `{rect, diameters, cellprobs}` → `{sweep_id, combos}` |
| GET | `/api/rescans/<sid>` | Combos with status, and `cells` once done (full-image coordinates) |
| DELETE | `/api/rescans/<sid>` | Cancel what hasn't run |
| POST | `/api/projects/<pid>/exports` | `{prefixes, summary, cells}` → `{export_id}` |
| GET | `/api/exports/<eid>` | `{status, download_url}` |
| GET | `/exports/<eid>/download` | The workbook (requester only; served by nginx via X-Accel-Redirect) |

## Code layout

```
web/
  cellcounts/      settings, urls, wsgi
  accounts/        user model, allauth adapter/forms, 2FA + approval middleware, settings pages, admin portal
  projects/        projects, sharing, samples/images/cells, workspace page, project/cells API
  uploads/         chunked upload API and staging storage
  processing/      jobs, scheduler, workers, tiles, segmentation, rescan, export, coexpression, notifications, queue page
  core/            storage paths + NAS check, API helpers, Turnstile, Vite asset tag
  templates/ static/
  frontend/        TypeScript (Vite): workspace app, upload pipeline, queue page
  deploy/          Dockerfiles, compose, nginx, cloudflared, backups
```

## Milestones

1. Foundation: project scaffold, accounts and security, admin portal, settings.
2. Pipeline: uploads, storage, tiles, workers, scheduling, queue API/page, notifications.
3. Workspace: layout, samples/queue panels, tiled viewer, channel tabs, composite.
4. Editing: review/draw/delete, lasso/rectangle, undo/redo, autosave, edit locks.
5. Rescan, export, review-file import, sharing.
6. Deployment: images, compose, tunnel, NFS, backups, runbook.
