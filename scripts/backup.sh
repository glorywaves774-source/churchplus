#!/usr/bin/env bash
# Off-site PostgreSQL backup for ChurchPlus.
#   scripts/backup.sh "$DATABASE_URL" backups/
# Writes backups/churchplus-YYYYMMDD-HHMM.dump (custom format) and keeps the newest 8.
# Store the folder somewhere encrypted (e.g. a private bucket with encryption on).
set -euo pipefail
URL="${1:?database URL}"; OUT="${2:-backups}"
mkdir -p "$OUT"
FILE="$OUT/churchplus-$(date -u +%Y%m%d-%H%M).dump"
pg_dump --format=custom --no-owner --no-privileges "$URL" > "$FILE"
pg_restore --list "$FILE" > /dev/null          # proves the file is readable
ls -1t "$OUT"/churchplus-*.dump | tail -n +9 | xargs -r rm --
echo "Backup written: $FILE ($(du -h "$FILE" | cut -f1))"
