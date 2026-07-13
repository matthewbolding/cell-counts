# CUDA runtime lives inside the `torch` wheel itself (torch's pip package pulls the
# matching nvidia-cu*-* runtime libraries as ordinary dependencies), so the base
# image only needs a host driver compatible via nvidia-container-toolkit — it does
# NOT need to match a specific CUDA toolkit version itself. `pip install -r
# requirements.txt` alone resolves torch to a build matching the installed driver;
# no separate pinned-index-url step is needed. See SERVER.md for the host driver +
# nvidia-container-toolkit setup this depends on.
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runs as root: this container only ever runs on a private, single-purpose compute
# box behind Nginx Proxy Manager + Cloudflare, with server/data/ bind-mounted from
# the host for persistence — a fixed non-root UID inside the image fights that bind
# mount (Docker creates the host-side directory as root on first run, and a
# non-matching in-container UID then can't write to it without extra host-side
# chown steps this deploy has no sudo access to perform).

EXPOSE 8000

# --workers 1 is mandatory: see jobs.py / app.py docstrings.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
