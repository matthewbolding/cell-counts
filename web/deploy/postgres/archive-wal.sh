#!/bin/sh
# Postgres archive_command: copy one finished WAL file (or a .history/.backup
# file) onto the NAS, gzipped.
#
#   archive_command = 'sh /etc/postgresql/archive-wal.sh %p %f'
#
# Postgres retries a failed archive until it works and keeps the WAL locally
# meanwhile, so the NAS being away for a while loses nothing. A forced switch
# (archive_timeout) leaves most of a 16 MB segment zero-filled, which gzip
# shrinks to almost nothing.
set -eu

src=$1
dir=/backups/wal
dest=$dir/$2.gz

mkdir -p "$dir"
if [ -f "$dest" ]; then
    # Archived before (Postgres retries after a crash): fine only if it's the
    # same file; never overwrite one that differs.
    gzip -dc "$dest" | cmp -s - "$src"
    exit $?
fi
gzip -c "$src" > "$dest.tmp"
sync "$dest.tmp"
mv "$dest.tmp" "$dest"
