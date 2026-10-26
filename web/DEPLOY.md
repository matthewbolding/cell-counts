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

### Sharing the GPU with what's already on the server

The old single-user server (`server-cellcounts-server-1`) keeps its own Cellpose model loaded on the same 12 GB
GPU, and other GPU workloads on the server may too. Two Cellpose processes on one card can run out of memory on the largest
images. While the researcher is still using the desktop app, either:

- run the web app without segmentation for now: `docker compose up -d --scale gpu-worker=0`. Uploads, tiles,
  review and export all work; images wait in the queue until a GPU worker runs. Or
- leave both running and accept that a very large image may fail with "The GPU ran out of memory on this image."
  **Retry** queues it again.

Once everyone has moved over, retire the old server (section 6) and run the GPU worker full time.

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

`backup` writes a `pg_dump` to `/mnt/cellcounts/backups/` every night at 03:00 and keeps the last 14. The database
holds what can't be recreated: accounts and every review edit. The TIFFs are on the NAS, and researchers keep
their own originals; tiles can always be rebuilt.

- **Take one now:** `docker compose run --rm backup --once`
- **Off-site copy:** the NAS's RAID protects against a dead disk, not a dead NAS, a fire, or a bad delete. Copy
  `/mnt/cellcounts/backups` somewhere else regularly; the dumps are small. For example, run an `rclone sync` to
  Cloudflare R2 or Backblaze B2 from a cron job.

**Restore** (practice this once before you need it):

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

## 6. Retiring the old server

When the researcher has moved over:

1. On the server: `cd ~/cell-counts/server && docker compose down`.
2. Remove the old server's reverse-proxy route and its DNS record in
   Cloudflare.
3. Desktop folders can be brought into the web app as they are. Adding a folder that has a `cellcounts.json`
   uploads the images and keeps the review work stored next to them.

---

## 7. Security checklist

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
