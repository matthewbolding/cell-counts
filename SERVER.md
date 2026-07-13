# SERVER.md — compute server install & operation

The server is a FastAPI app that loads one Cellpose-SAM model on GPU at startup and
serves it over a small chunked-upload + job-polling HTTP API (see `server/app.py`
for the full endpoint list). It's meant to run in Docker on a Linux box with an
NVIDIA GPU.

## 1. Install the NVIDIA driver (one-time, manual — needs sudo)

```bash
ssh <user>@<gpu-host>
ubuntu-drivers devices          # see what it recommends for your GPU
sudo ubuntu-drivers autoinstall # or: sudo apt install nvidia-driver-<version>
sudo reboot
```

After the reboot:

```bash
nvidia-smi   # must show your GPU and a driver version — stop here and fix if not
```

## 2. Install the NVIDIA container toolkit (one-time, manual — needs sudo)

Docker must already be installed on `<gpu-host>`, but it needs the toolkit to pass
the GPU through to a container:

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update
sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

**Checkpoint before touching this repo's image** — isolate driver/toolkit problems
from application problems:

```bash
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

This must print the same GPU info as the bare-metal `nvidia-smi` above. If it
doesn't, the driver/toolkit setup needs fixing before going any further.

> Confirm `docker compose version` reports Compose **v2** (bundled with recent
> Docker Engine). The legacy Python `docker-compose` v1 tool silently ignores the
> `deploy.resources.reservations.devices` GPU-reservation syntax this repo's
> `docker-compose.yml` uses, and needs the older `runtime: nvidia` key instead.

## 3. Configure credentials

```bash
cd ~/cell-counts/server   # or wherever this repo is checked out on the box
cp .env.example .env
```

Edit `.env` and set a real `CELLCOUNTS_USER` / `CELLCOUNTS_PASS` — this is the
single username/password the client prompts for on launch. `.env` is gitignored;
never commit it.

## 4. Build and run

```bash
docker compose up -d --build
docker compose logs -f     # watch startup; wait for "Model loaded."
```

Verify locally on the box, before involving Nginx Proxy Manager or Cloudflare at all:

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok","gpu":true,"model_loaded":true}
```

## 5. Wire up Nginx Proxy Manager + Cloudflare

The DNS record for `research.matthewbolding.com` already exists and is proxied
through Cloudflare (free tier). In Nginx Proxy Manager, add/update the proxy host:

- **Domain**: `research.matthewbolding.com`
- **Forward to**: `<gpu-host's address reachable from the NPM host>` — port
  **8000**. (NPM runs on a separate host from `<gpu-host>`; use whichever address
  that host can actually reach it by — LAN IP or a VPN/Tailscale address — pick
  based on your network layout.)
- **Scheme**: `http` (NPM/Cloudflare terminate TLS in front of this)
- Force SSL, HTTP/2 support: on.
- Cloudflare: orange-cloud (proxied) the DNS record so the free-tier proxy sits in
  front of NPM.

**Cloudflare free-tier constraints this API is already designed around** — no
further NPM/Cloudflare-side configuration is needed for these, they're just why the
protocol looks the way it does:
- **100MB max request body.** TIFFs run up to ~280MB, so the client splits uploads
  into 32MB chunks (`server/uploads.py`) — each chunk is its own request.
- **~100s idle timeout on proxied requests.** Segmentation can take minutes, so the
  client never waits on one long request for a result — it uploads, gets a
  `job_id`, and polls `/jobs/{job_id}` with backoff (`server/jobs.py`) until it's
  done.

Once NPM is pointed at port 8000, repeat the `curl /health` check through
`https://research.matthewbolding.com/health` to confirm the whole proxy chain works
before trusting it with a real upload.

## Operating notes

- `docker compose logs -f` to tail; `docker compose restart` to bounce the server
  (in-flight jobs will error out and the client will re-upload on its next run —
  job status is persisted in SQLite, so a poll never just 404s into the void).
- `--workers 1` in the Dockerfile's `CMD` is mandatory, not a tuning knob: a second
  uvicorn worker process would load a second Cellpose model and likely exhaust the
  3080 Ti's VRAM. Don't change it.
- Uploaded files are staged/reassembled under `server/data/` (gitignored, bind-mounted
  into the container) and deleted once their segmentation job finishes; abandoned
  uploads older than 24h are swept automatically.
- Job history lives in `server/data/jobs.sqlite3`.
