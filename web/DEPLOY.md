# Deploying the Cell Counts web app

Everything runs as one Docker Compose project on the GPU server, reachable only through a Cloudflare
Tunnel at **https://cellcounts.matthewbolding.com**. See [DESIGN.md](DESIGN.md) for why it's shaped this way.

```
browser ── Cloudflare ── tunnel ── cloudflared ── nginx ── web (Django)
                                                  │          │
                                   tiles/exports ─┘   postgres (local disk)
                                   from the NAS       cpu-worker  (tiles, exports, email)
                                                      gpu-worker  (Cellpose; no internet, no ports)
                                                      backup      (nightly pg_dump to the NAS)
```

No container publishes a port on the host. Files live on the NAS, mounted at `/mnt/cellcounts` on the host and at
`/data` in the containers. The database lives on the server's own disk.

The server needs Docker with Compose v2, an NVIDIA GPU with the NVIDIA container toolkit (the GPU image ships
torch's CUDA 13 wheels, so the driver must support CUDA 13), and `nfs-common`. In the commands below, `<nas>` is
the NAS's address and `<server>` is the GPU server's.

---

## 1. One-time setup (yours: these need sudo or a web dashboard)

### 1a. A share on the NAS

On the Unraid box, create a share named `cellcounts` and export it over NFS to the GPU server (`<server>`)
**read-write**. Unraid maps every NFS client to `nobody:users` (UID 99, GID 100). The app runs as that same
UID/GID (`APP_UID=99`, `APP_GID=100` in `.env`), so file ownership is consistent whether or not squashing applies.

### 1b. Mount it on the server

```sh
sudo mkdir -p /mnt/cellcounts
echo '<nas>:/mnt/user/cellcounts /mnt/cellcounts nfs4 rw,hard,noatime,_netdev,nofail,x-systemd.automount,x-systemd.mount-timeout=30 0 0' \
  | sudo tee -a /etc/fstab
sudo systemctl daemon-reload
sudo mount /mnt/cellcounts
touch /mnt/cellcounts/.cellcounts-volume
```

**About `.cellcounts-volume`:** the app refuses to read or write files unless this marker is present. If the NFS
mount ever fails to come up after a reboot, `/mnt/cellcounts` is just an empty folder on the local disk. Without
the marker check, uploads would quietly fill the server's disk instead of the NAS. With it, uploads fail with a
clear "storage unavailable" error and `/healthz` reports the problem. Create the marker once, on the NAS itself
(the command above does that), and never on the local disk.

### 1c. Cloudflare Tunnel

In the Cloudflare dashboard: **Zero Trust → Networks → Tunnels → Create a tunnel** (type *Cloudflared*), named
`cellcounts`.

1. Copy the tunnel token. It goes in `tunnel.env` (step 2).
2. Add a **public hostname**: `cellcounts.matthewbolding.com`, service type **HTTP**, URL **`nginx:80`**.
   cloudflared runs inside the compose project, so it reaches nginx by its service name.
3. Make sure **SSL/TLS** for matthewbolding.com is *Full* and **Always Use HTTPS** is on.

Cloudflare Access is deliberately not used. The app's own sign-in has Turnstile, rate limits and mandatory 2FA.

cloudflared is set to connect over HTTP/2 instead of its default QUIC (`--protocol http2` in
`docker-compose.yml`): through this tunnel, QUIC held uploads to about a third of HTTP/2's speed.

*Optional:* in **Security → WAF → Rate limiting rules**, add a rule for `/accounts/login/`,
`/accounts/signup/` and `/accounts/password/reset/` (for example, 20 requests per minute per IP, then block for
10 minutes). The app already rate-limits these routes; this stops floods before they reach the server.

### 1d. Turnstile

**Turnstile → Add widget**: hostname `cellcounts.matthewbolding.com`, mode *Managed*. Copy the site key and the
secret key into `.env`.

### 1e. Resend

The matthewbolding.com domain is already verified in Resend. Create an **API key** with *Sending access* restricted
to that domain, and put it in `.env`. Mail is sent from `Cell Counts <cellcounts@matthewbolding.com>`; change
`CELLCOUNTS_FROM_EMAIL` if you'd rather use a different address on the verified domain.

---

## 2. First deploy

```sh
ssh <you>@<server>
cd ~/cell-counts && git fetch && git checkout <the branch or tag to deploy>
cd web/deploy
cp .env.example .env && cp tunnel.env.example tunnel.env
chmod 600 .env tunnel.env
# Fill both in. Generate secrets with:
#   python3 -c "import secrets; print(secrets.token_urlsafe(50))"
docker compose build          # the GPU image downloads torch + Cellpose weights: several GB, once
docker compose up -d
docker compose ps             # everything "running"/"healthy"; `migrate` exited 0
```

Create the first administrator:

```sh
docker compose run --rm web python manage.py createsuperuser
```

Then sign in at https://cellcounts.matthewbolding.com. You'll be sent straight to two-factor setup; scan the QR
code with an authenticator app and **save the recovery codes**. After that, **Manage Users…** in the account menu
is where you approve access requests and invite people.

### Sharing the GPU

The GPU worker keeps Cellpose loaded on the server's 12 GB GPU. Other GPU workloads on the server use the same
card, so the largest images can occasionally fail with "The GPU ran out of memory on this image."; **Retry**
queues them again. To keep segmentation off the GPU for a while, **Pause GPU** on the GPU Queue page stops new
segmentation for everyone without stopping uploads, review or export.

---

## 3. Updating

```sh
cd ~/cell-counts && git pull
cd web/deploy
docker compose build
docker compose up -d
```

`migrate` runs migrations and publishes static files before `web` starts. Both workers finish the job they're
running before stopping (up to 5 minutes for CPU, 10 for GPU). A job that's cut off anyway is retried once when the
worker comes back. Uploads in progress resume on their own; the browser retries chunks until the server answers
again.

---

## 4. Backups and restore

Everything lands in `/mnt/cellcounts/backups/` on the NAS:

| What | Written | Kept |
|------|---------|------|
| `wal/` | Continuously: Postgres sends every finished WAL file, and starts a new one at least once a minute while anything changes | Back to the oldest base backup |
| `base/<stamp>/` | Nightly at 03:00: a full base backup | Last 7 |
| `cellcounts-<stamp>.dump` | Nightly at 03:00: a `pg_dump` | Last 14 |

So at most about a minute of review edits is ever not on the NAS, and the database can be put back as it was at any
moment of the last week. The database holds what can't be recreated: accounts and every review edit. The TIFFs are
on the NAS, and researchers keep their own originals; tiles can always be rebuilt.

- **Take a backup now:** `docker compose run --rm backup --once` (base backup and dump, then a check that WAL
  archiving is healthy).
- **Check archiving:** each night's log ends with `backup: WAL archiving ok, last …` (`docker compose logs backup`).
  If the NAS is away, Postgres keeps the WAL on the server's disk and catches up when it's back; the nightly check
  reports `FAILING` meanwhile.
- **Practise a restore:** `sh restore-drill.sh` rebuilds the database from the newest base backup plus the archived
  WAL in a throwaway container with no network, and compares it with the live one. Nothing live is touched.
- **Off-site:** the NAS sits next to the server, so it doesn't protect against a fire or theft. If that matters, copy
  `/mnt/cellcounts/backups` elsewhere regularly (e.g. `rclone sync` to Cloudflare R2 or Backblaze B2 from cron).

**Restore to the latest moment, or to an earlier one.** These are the steps `restore-drill.sh` runs, applied to the
real database volume:

```sh
cd ~/cell-counts/web/deploy
docker compose stop web cpu-worker gpu-worker backup db

# Keep the current database aside first, in case you want it back.
docker run --rm -v cellcounts_pgdata:/from:ro -v /mnt/cellcounts/backups:/to postgres:17.11-trixie \
    tar -czf "/to/pgdata-before-restore-$(date +%Y%m%d-%H%M%S).tar.gz" -C /from .

# Replace it with the newest base backup (or choose an older backups/base/<stamp>).
BASE=$(ls -1d /mnt/cellcounts/backups/base/2* | sort | tail -n 1)
docker run --rm -v cellcounts_pgdata:/data -v "$BASE":/base:ro postgres:17.11-trixie sh -c '
    find /data -mindepth 1 -delete &&
    tar -xzf /base/base.tar.gz -C /data && tar -xzf /base/pg_wal.tar.gz -C /data/pg_wal &&
    touch /data/recovery.signal &&
    echo "restore_command = '"'"'gzip -dc /backups/wal/%f.gz > %p'"'"'" >> /data/postgresql.auto.conf &&
    chown -R postgres:postgres /data'

# Starting db replays the archived WAL to the end, then opens for writes.
docker compose up -d db
docker compose logs db | grep -E "redo done|last completed transaction|ready to accept"
docker compose up -d
```

To stop at an earlier moment instead (say, just before a bad delete), add these two lines to `postgresql.auto.conf`
in the same step as `restore_command`:

```
recovery_target_time = '2026-10-26 14:30:00 America/Chicago'
recovery_target_action = 'promote'
```

**Restore a nightly dump** instead (simpler, but only to 03:00 that night):

```sh
cd ~/cell-counts/web/deploy
docker compose stop web cpu-worker gpu-worker
docker compose exec -T db pg_restore --clean --if-exists --no-owner \
    -U "$(grep ^POSTGRES_USER .env | cut -d= -f2)" -d "$(grep ^POSTGRES_DB .env | cut -d= -f2)" \
    < /mnt/cellcounts/backups/cellcounts-YYYYMMDD-HHMMSS.dump
docker compose start web cpu-worker gpu-worker
```

---

## 5. Health and logs

| What | How |
| --- | --- |
| Overall | `docker compose ps` (all *healthy*; `migrate` *exited (0)*) |
| Database + NAS | `https://cellcounts.matthewbolding.com/healthz`, which returns 503 if either is unavailable |
| Logs | `docker compose logs -f web cpu-worker gpu-worker` |
| GPU | `nvidia-smi`, or **View → GPU Queue** in the app |
| Disk | `df -h /mnt/cellcounts`; per-user usage on **Manage Users…** |

---

## 6. Security checklist

- [ ] `docker compose ps` shows **no published ports**, and `sudo ss -tlnp` on the server shows nothing new
      listening.
- [ ] `.env` and `tunnel.env` are `chmod 600` and not in git (both are in `.gitignore`).
- [ ] Visiting the server directly (not via the tunnel) can't reach the app. nginx also refuses any request that
      lacks Cloudflare's `CF-Connecting-IP` header.
- [ ] The GPU worker is only on the internal `backend` network: no internet access, and nothing can connect to it.
- [ ] Every account, including yours, has 2FA (**Manage Users…** shows a *2FA* column).
- [ ] Host patching: `unattended-upgrades` on; update Docker images with `docker compose pull && docker compose
      build --pull` occasionally.
- [ ] Rotating secrets:
  - A new `DJANGO_SECRET_KEY` signs everyone out.
  - The Resend key and the Turnstile keys can be swapped any time.
  - A new tunnel token comes from the Cloudflare dashboard.
  - To change `POSTGRES_PASSWORD`, run `ALTER USER` in the database first.
  - Run `docker compose up -d` after any change.

`manage.py check --deploy` is clean. Two of its checks are silenced on purpose in `settings.py`: HSTS is not
extended to all of matthewbolding.com's subdomains, and the domain is not on the HSTS preload list. Those are
decisions about the whole domain, not this app.
