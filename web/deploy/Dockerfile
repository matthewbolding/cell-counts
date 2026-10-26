# syntax=docker/dockerfile:1.7
#
# Image for `web` (gunicorn), the one-shot `migrate` release step, and
# `cpu-worker` (tiles, hash verification, exports, emails). Build context is
# web/ (see docker-compose.yml); the GPU worker has its own Dockerfile.gpu
# because torch + CUDA libraries + Cellpose weights are ~5 GB that nothing else
# needs.
#
#   docker build -f deploy/Dockerfile -t cellcounts-web .        (from web/)

ARG PYTHON_IMAGE=python:3.13.17-slim-trixie
ARG NODE_IMAGE=node:22.23.4-trixie-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.5

# --------------------------------------------------------------------------- #
# Frontend: Vite writes straight into ../static/dist (see vite.config.ts).
# --------------------------------------------------------------------------- #
FROM ${NODE_IMAGE} AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM ${UV_IMAGE} AS uv

# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
FROM ${PYTHON_IMAGE} AS app

# Container UID/GID. Files the app writes to the NAS end up owned by these
# (unless the NFS export squashes them) — see DEPLOY.md, "NAS mount".
ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY . .
COPY --from=frontend /src/static/dist /app/static/dist

# collectstatic needs settings to import in production mode, which insists on
# real secrets. These placeholders exist only for this one command (they're
# visible in `docker history`, which is fine: none of them is a secret, and
# none of them reaches the running container).
RUN DJANGO_SECRET_KEY=build-only-not-secret \
    POSTGRES_PASSWORD=build-only \
    RESEND_API_KEY=build-only \
    TURNSTILE_SITE_KEY=build-only \
    TURNSTILE_SECRET_KEY=build-only \
    python manage.py collectstatic --noinput --verbosity 0

# GID 100 ("users", Unraid's default share group) already exists in Debian, so
# only create the group when it's missing.
# /srv/static is where the `migrate` service publishes static files for nginx;
# creating it here, owned by the app user, means a fresh named volume mounted
# there inherits that ownership.
RUN (getent group "${APP_GID}" || groupadd --gid "${APP_GID}" app) \
 && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home --home-dir /tmp --shell /usr/sbin/nologin app \
 && mkdir -p /srv/static && chown "${APP_UID}:${APP_GID}" /srv/static \
 && chmod +x /app/deploy/release.sh /app/deploy/healthcheck.py

USER ${APP_UID}:${APP_GID}
EXPOSE 8000

CMD ["gunicorn", "--config", "/app/deploy/gunicorn.conf.py", "cellcounts.wsgi:application"]
