#!/usr/bin/env bash
# Restores a backup into the running database, replacing what is there:
#   ./restore.sh /var/backups/callpulse/callpulse-20261024-023000.sql.gz
# Stops the app first so nothing writes while it restores, then starts it again.
set -euo pipefail
FILE="${1:?usage: ./restore.sh backup.sql.gz}"
[ -f "$FILE" ] || { echo "no such file: $FILE" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
read -r -p "This REPLACES the database with $FILE. Type 'restore' to continue: " ok
[ "$ok" = restore ] || { echo cancelled; exit 1; }
C="docker compose --env-file .env -f docker-compose.prod.yml"
$C stop api worker beat realtime
$C exec -T postgres psql -U callcenter -d postgres -c "DROP DATABASE IF EXISTS callcenter WITH (FORCE)" -c "CREATE DATABASE callcenter OWNER callcenter"
$C exec -T postgres psql -U callcenter -d callcenter -c "CREATE EXTENSION IF NOT EXISTS vector" 
gunzip -c "$FILE" | $C exec -T postgres psql -U callcenter -d callcenter -v ON_ERROR_STOP=1 -q
$C up -d
echo "restored from $FILE"
