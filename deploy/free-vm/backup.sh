#!/bin/sh
# Nightly dump of the database (cron: 0 3 * * * /path/to/backup.sh). Keeps 7 days. Copy the folder off the VM too.
set -eu
DIR="${BACKUP_DIR:-$HOME/defuser-backups}"
mkdir -p "$DIR"
docker compose -f "$(dirname "$0")/docker-compose.yml" exec -T db pg_dump -U fd -Fc fd > "$DIR/fd-$(date +%F).dump"
find "$DIR" -name 'fd-*.dump' -mtime +7 -delete
