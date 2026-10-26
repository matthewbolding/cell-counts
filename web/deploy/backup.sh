#!/bin/sh
# Nightly Postgres backup onto the NAS, run by the `backup` service.
#
#   backup.sh          loop forever: dump once a day at BACKUP_HOUR (local TZ)
#   backup.sh --once   dump right now and exit (manual backups, restore drills)
#
# Dumps are pg_dump custom format (-Fc), restored with pg_restore (DEPLOY.md).
# Only review data lives in the database; the TIFFs and tiles are files on the
# NAS already, and researchers keep their own originals.
set -eu

BACKUP_DIR=/data/backups
KEEP="${BACKUP_KEEP:-14}"
HOUR="${BACKUP_HOUR:-3}"

dump() {
    mkdir -p "$BACKUP_DIR"
    stamp=$(date +%Y%m%d-%H%M%S)
    target="$BACKUP_DIR/cellcounts-$stamp.dump"
    # Write to a temporary name and rename, so a half-written dump never looks
    # like a good one.
    # (Explicit checks: `set -e` doesn't apply inside a function called from `||`.)
    if ! pg_dump -Fc -h db -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f "$target.partial"; then
        rm -f "$target.partial"
        return 1
    fi
    mv "$target.partial" "$target" || return 1
    echo "backup: wrote $target ($(du -h "$target" | cut -f1))"
    # Keep the newest $KEEP dumps.
    ls -1t "$BACKUP_DIR"/cellcounts-*.dump 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
        rm -f "$old" && echo "backup: pruned $old"
    done
    rm -f "$BACKUP_DIR"/*.partial
}

if [ ! -f /data/.cellcounts-volume ]; then
    echo "backup: /data is not the Cell Counts volume (.cellcounts-volume missing) — is the NAS mounted?" >&2
    exit 1
fi

if [ "${1:-}" = "--once" ]; then
    dump
    exit 0
fi

while true; do
    now=$(date +%s)
    next=$(date -d "today ${HOUR}:00" +%s)
    [ "$next" -le "$now" ] && next=$(date -d "tomorrow ${HOUR}:00" +%s)
    echo "backup: next dump at $(date -d "@$next")"
    sleep $((next - now))
    dump || echo "backup: dump FAILED" >&2
done
