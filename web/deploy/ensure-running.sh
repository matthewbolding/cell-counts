#!/bin/sh
# Start any Cell Counts service that isn't running. Run from cron at boot and
# every few minutes (DEPLOY.md, "Keeping it running").
#
# Docker restarts the containers on boot by itself (restart: unless-stopped),
# but if one can't start — the NAS not mounted yet, or a stale NFS handle — it
# gives up on it for good. This keeps trying until everything is up, and does
# nothing while the NAS isn't there, so nothing starts writing to local disk.
#
# Stopping things on purpose (maintenance)? `touch .hold` here first, and
# remove it afterwards. Activity goes to the journal: journalctl -t cellcounts
set -eu
cd "$(dirname "$0")"

[ -e .hold ] && exit 0
exec 9>/tmp/cellcounts-ensure-running.lock
flock -n 9 || exit 0  # a previous run is still starting things

log() { logger -t cellcounts "$*"; }

nas=$(sed -n 's/^NAS_MOUNT=//p' .env | tail -n 1)
# `timeout`: an unreachable NAS on a hard NFS mount would otherwise hang here.
if ! timeout 20 test -f "${nas:-/mnt/cellcounts/app}/.cellcounts-volume"; then
    log "NAS not available at ${nas:-/mnt/cellcounts/app}; not starting services yet"
    exit 0
fi

running=$(docker compose ps --status running --services 2>/dev/null || true)
missing=""
for service in $(docker compose config --services | grep -vx migrate); do
    echo "$running" | grep -qx "$service" || missing="$missing $service"
done
[ -z "$missing" ] && exit 0

log "not running:$missing — starting"
if out=$(docker compose up -d 2>&1); then
    log "started:$missing"
else
    log "couldn't start everything: $(echo "$out" | grep -i error | tail -n 1)"
fi
