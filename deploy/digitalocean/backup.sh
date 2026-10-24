#!/usr/bin/env bash
# Nightly database backup: a compressed pg_dump kept for 14 days in /var/backups/callpulse.
# To also keep a copy off the server, set BACKUP_RCLONE_REMOTE (e.g. "spaces:my-bucket/callpulse")
# and configure rclone for it; the file is copied there after it is written.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DIR=/var/backups/callpulse
mkdir -p "$DIR"
FILE="$DIR/callpulse-$(date +%Y%m%d-%H%M%S).sql.gz"
cd "$HERE"
docker compose --env-file .env -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U callcenter -d callcenter --no-owner | gzip > "$FILE.partial"
mv "$FILE.partial" "$FILE"
# A backup that is almost empty means pg_dump failed silently.
[ "$(stat -c %s "$FILE")" -gt 2000 ] || { echo "backup suspiciously small: $FILE" >&2; exit 1; }
find "$DIR" -name 'callpulse-*.sql.gz' -mtime +14 -delete
if [ -n "${BACKUP_RCLONE_REMOTE:-}" ] && command -v rclone >/dev/null; then
  rclone copy "$FILE" "$BACKUP_RCLONE_REMOTE"
fi
echo "backup ok: $FILE ($(du -h "$FILE" | cut -f1))"
