# Cell Counts web app

Cell Counts in the browser: researchers sign in at cellcounts.matthewbolding.com, add a folder of
`{PREFIX}_{CCK,CHR,SNAP}.tif` images to a project, and review, rescan and export cells there.

- [DESIGN.md](DESIGN.md) — architecture, security model, data model, scheduling, JSON API
- [DEPLOY.md](DEPLOY.md) — production setup on the GPU server, updates, backups, security checklist

## Layout

| Path | What |
| --- | --- |
| `cellcounts/` | Django settings and URLs |
| `accounts/` | users, sign-in rules (approval, mandatory 2FA, Turnstile), security emails, admin portal |
| `projects/` | projects, sharing, samples/images/cells, review-screen API, tiles |
| `uploads/` | chunked, resumable upload API |
| `processing/` | job queue and GPU scheduling, workers, tiling, Cellpose, rescans, exports, notifications |
| `core/` | NAS storage paths, JSON API helpers, Turnstile, Vite asset tag |
| `frontend/` | TypeScript: the review screen (`src/workspace`) and the GPU Queue page (`src/queue`) |
| `deploy/` | Dockerfiles, compose, nginx, backup and release scripts |

## Local development

Needs [uv](https://docs.astral.sh/uv/), Node 22 and Docker.

```sh
cd web
docker compose -f docker-compose.dev.yml up -d        # Postgres on localhost:54329
uv sync
mkdir -p .data && touch .data/.cellcounts-volume      # the dev "NAS"
uv run --env-file dev.env python manage.py migrate
uv run --env-file dev.env python manage.py createsuperuser
uv run --env-file dev.env python manage.py seed_demo --owner you@example.com --samples 3
(cd frontend && npm ci && npm run build)              # or `npm run watch` while editing

uv run --env-file dev.env python manage.py runserver
uv run --env-file dev.env python manage.py run_worker --resource cpu   # second terminal
uv run --env-file dev.env python manage.py run_worker --resource gpu   # third terminal
```

`dev.env` turns on development mode: email is printed to the console, Turnstile uses Cloudflare's always-pass
test keys, the GPU worker uses a fake model (`CELLCOUNTS_FAKE_MODEL=1`) instead of Cellpose, and the 2FA code
`000000` is accepted (only ever with `DEBUG` on). You still have to set up 2FA on first sign-in, exactly as in
production.

## Tests

```sh
uv run pytest                      # backend (uses a test database in the dev Postgres)
uv run ruff check .
(cd frontend && npm test && npm run build)
```

Running several test sessions at once? Give each its own database: `CELLCOUNTS_TEST_DB=test_cc_mine uv run pytest`.
