#!/bin/sh
# Nightly Postgres backups onto the NAS, run by the `backup` service.
#
#   backup.sh          loop forever: back up once a day at BACKUP_HOUR (local TZ)
#   backup.sh --once   back up right now and exit (manual backups, restore drills)
#
# Postgres itself sends every finished WAL file to backups/wal/ as it goes
# (archive-wal.sh, continuous). Each night this adds:
#   - a base backup (backups/base/<stamp>/): restoring = newest base backup plus
#     the WAL archived since, replayed up to any moment (DEPLOY.md section 4);
#     the last BASE_BACKUP_KEEP are kept, and WAL older than the oldest of them
#     is pruned;
#   - a pg_dump (backups/cellcounts-<stamp>.dump): a self-contained copy that
#     pg_restore reads on its own; the last BACKUP_KEEP are kept.
# Only review data lives in the database; the TIFFs and tiles are files on the
# NAS already, and researchers keep their own originals.
set -eu

BACKUP_DIR=/data/backups
BASE_DIR=$BACKUP_DIR/base
WAL_DIR=$BACKUP_DIR/wal
KEEP="${BACKUP_KEEP:-14}"
BASE_KEEP="${BASE_BACKUP_KEEP:-7}"
HOUR="${BACKUP_HOUR:-3}"

# (Explicit checks below: `set -e` doesn't apply inside a function called from `||`.)

dump() {
    mkdir -p "$BACKUP_DIR"
    stamp=$(date +%Y%m%d-%H%M%S)
    target="$BACKUP_DIR/cellcounts-$stamp.dump"
    # Write to a temporary name and rename, so a half-written dump never looks
    # like a good one.
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

base_backup() {
    mkdir -p "$BASE_DIR"
    stamp=$(date +%Y%m%d-%H%M%S)
    target="$BASE_DIR/$stamp"
    # base.tar.gz + pg_wal.tar.gz (the WAL to make it consistent on its own).
    if ! pg_basebackup -h db -U "$POSTGRES_USER" -D "$target.partial" -Ft -z -X stream --checkpoint=fast; then
        rm -rf "$target.partial"
        return 1
    fi
    mv "$target.partial" "$target" || return 1
    echo "backup: base backup $target ($(du -sh "$target" | cut -f1))"

    ls -1d "$BASE_DIR"/2* | sort | head -n -"$BASE_KEEP" | while read -r old; do
        rm -rf "$old" && echo "backup: pruned $old"
    done
    # WAL from before the oldest kept base backup can no longer be replayed onto
    # anything: drop it.
    oldest=$(ls -1d "$BASE_DIR"/2* | sort | head -n 1)
    start=$(tar -xzOf "$oldest/base.tar.gz" backup_label | sed -n 's/^START WAL LOCATION: .*(file \([0-9A-F]*\))$/\1/p')
    if [ -n "$start" ] && [ -d "$WAL_DIR" ]; then
        pg_archivecleanup -x .gz "$WAL_DIR" "$start" || return 1
    fi
}

check_archiving() {
    status=$(psql -h db -U "$POSTGRES_USER" -d "$POSTGRES_DB" -qAt -c "
        select case when last_failed_time > coalesce(last_archived_time, '-infinity')
                    then 'FAILING since ' || last_failed_time || ' (' || last_failed_wal || ')'
                    else 'ok, last ' || coalesce(last_archived_wal || ' at ' || last_archived_time, 'none yet') end
        from pg_stat_archiver") || return 1
    case "$status" in
        ok*) echo "backup: WAL archiving $status" ;;
        *) echo "backup: WAL archiving $status — see 'docker compose logs db'" >&2; return 1 ;;
    esac
}

run_all() {
    failed=0
    base_backup || { echo "backup: base backup FAILED" >&2; failed=1; }
    dump || { echo "backup: dump FAILED" >&2; failed=1; }
    check_archiving || failed=1
    return "$failed"
}

if [ ! -f /data/.cellcounts-volume ]; then
    echo "backup: /data is not the Cell Counts volume (.cellcounts-volume missing) — is the NAS mounted?" >&2
    exit 1
fi

if [ "${1:-}" = "--once" ]; then
    run_all
    exit $?
fi

while true; do
    now=$(date +%s)
    next=$(date -d "today ${HOUR}:00" +%s)
    [ "$next" -le "$now" ] && next=$(date -d "tomorrow ${HOUR}:00" +%s)
    echo "backup: next run at $(date -d "@$next")"
    sleep $((next - now))
    run_all || true
done
