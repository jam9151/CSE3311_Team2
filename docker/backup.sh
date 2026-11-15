#!/bin/sh
# Copy the live SQLite database out of the running mediaflow-web container.
#
#   docker/backup.sh [destination folder]      default: ~/mediaflow-backups
#   KEEP=30 docker/backup.sh                   how many to keep, default 14
#
# Uses SQLite's own online backup, so it is safe while the app is serving
# requests, then gzips the copy and drops the oldest past KEEP. A cron line
# for every night at 03:15 is in docs/runbook.md.
set -eu

DEST="${1:-$HOME/mediaflow-backups}"
KEEP="${KEEP:-14}"
CONTAINER="${CONTAINER:-mediaflow-web}"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$DEST"

docker exec "$CONTAINER" python -c "
import sqlite3
source = sqlite3.connect('/data/db.sqlite3')
copy = sqlite3.connect('/data/backup.sqlite3')
source.backup(copy)
copy.close()
source.close()
"
docker cp "$CONTAINER:/data/backup.sqlite3" "$DEST/db-$STAMP.sqlite3"
docker exec "$CONTAINER" rm -f /data/backup.sqlite3
gzip -f "$DEST/db-$STAMP.sqlite3"

# Oldest first past KEEP, deleted.
ls -1t "$DEST"/db-*.sqlite3.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f

echo "Backed up to $DEST/db-$STAMP.sqlite3.gz"
