#!/bin/sh
# Restore drill: rebuild the database from the newest base backup plus the
# archived WAL, in a throwaway container with no network, and compare it with
# the live one. Touches nothing live; the scratch container and volume are
# removed afterwards.
#
#   cd ~/cell-counts/web/deploy && sh restore-drill.sh
set -eu
cd "$(dirname "$0")"

env_value() { sed -n "s/^$1=//p" .env | tail -n 1; }
DB_USER=$(env_value POSTGRES_USER)
DB_NAME=$(env_value POSTGRES_DB)
NAS=$(env_value NAS_MOUNT)
BACKUPS=${NAS:-/mnt/cellcounts}/backups
IMAGE=postgres:17.11-trixie
NAME=cellcounts-restore-drill

base=$(ls -1d "$BACKUPS"/base/2* 2>/dev/null | sort | tail -n 1)
[ -n "$base" ] || { echo "No base backup in $BACKUPS/base yet: run 'docker compose run --rm backup --once'." >&2; exit 1; }
echo "Restoring $base, then the WAL archived since."

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; docker volume rm "$NAME" >/dev/null 2>&1 || true; }
cleanup
trap cleanup EXIT
docker volume create "$NAME" >/dev/null

# Same layout a real restore uses (DEPLOY.md section 4): the base backup
# unpacked, recovery.signal, and a restore_command reading the archive.
docker run --rm --network none --user postgres -v "$NAME":/var/lib/postgresql/data -v "$base":/base:ro "$IMAGE" sh -c '
    cd /var/lib/postgresql/data &&
    tar -xzf /base/base.tar.gz &&
    tar -xzf /base/pg_wal.tar.gz -C pg_wal &&
    touch recovery.signal &&
    echo "restore_command = '\''gzip -dc /backups/wal/%f.gz > %p'\''" >> postgresql.auto.conf'

docker run -d --name "$NAME" --network none -e POSTGRES_PASSWORD=unused \
    -v "$NAME":/var/lib/postgresql/data -v "$BACKUPS":/backups:ro "$IMAGE" >/dev/null

restored() { docker exec "$NAME" psql -qAt -U "$DB_USER" -d "$DB_NAME" -c "$1" 2>/dev/null; }
i=0
until [ "$(restored 'select pg_is_in_recovery()')" = "f" ]; do
    i=$((i + 1))
    if [ "$i" -gt 120 ]; then
        echo "Recovery didn't finish within 4 minutes:" >&2
        docker logs --tail 20 "$NAME" >&2
        exit 1
    fi
    sleep 2
done

COUNTS="select (select count(*) from accounts_user) || ' users, '
            || (select count(*) from projects_project) || ' projects, '
            || (select count(*) from projects_image) || ' images, '
            || (select count(*) from projects_cell) || ' cells'"
echo "Restored: $(restored "$COUNTS")"
echo "Live:     $(docker compose exec -T db psql -qAt -U "$DB_USER" -d "$DB_NAME" -c "$COUNTS")"
echo "Replayed up to: $(docker logs "$NAME" 2>&1 | sed -n 's/.*last completed transaction was at log time \(.*\)/\1/p' | tail -n 1)"
