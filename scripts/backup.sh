#!/usr/bin/env bash
# Daily hot backup of the ESP32-CAM SQLite database.
# Safe to run while the server is live; uses SQLite's online backup API.
#
# Usage:
#   DB_PATH=/app/data/esp32cam.sqlite3 BACKUP_DIR=/app/backups RETAIN_DAYS=7 ./scripts/backup.sh
#
# Environment variables (all optional, defaults shown below):
#   DB_PATH        Path to the SQLite database file.         Default: ./esp32cam.sqlite3
#   BACKUP_DIR     Directory for backup files.               Default: ./backups
#   RETAIN_DAYS    Number of daily backups to keep.          Default: 7

set -euo pipefail

DB_PATH="${DB_PATH:-./esp32cam.sqlite3}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETAIN_DAYS="${RETAIN_DAYS:-7}"

# Validate that RETAIN_DAYS is a positive integer to prevent find -mtime misuse.
if ! [[ "$RETAIN_DAYS" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: RETAIN_DAYS must be a positive integer, got: '$RETAIN_DAYS'" >&2
    exit 1
fi

# Reject path values that contain characters unsafe for SQLite dot-command parsing.
if [[ "$BACKUP_DIR" =~ [\'\"[:space:]\;] ]]; then
    echo "ERROR: BACKUP_DIR contains unsafe characters: $BACKUP_DIR" >&2
    exit 1
fi

if [ ! -f "$DB_PATH" ]; then
    echo "ERROR: database not found: $DB_PATH" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"

DEST="${BACKUP_DIR}/esp32cam_$(date +%Y%m%d).sqlite3"
umask 077
TMP=$(mktemp "${BACKUP_DIR}/.esp32cam_$(date +%Y%m%d).XXXXXX")
trap 'rm -f "$TMP" "$TMP-wal" "$TMP-shm"' EXIT
# Publish only a complete, checked SQLite backup; do not replace yesterday's
# usable copy if .backup fails partway through.
sqlite3 "$DB_PATH" "$(printf '.backup %s' "$TMP")"
if [ "$(sqlite3 "$TMP" 'PRAGMA journal_mode=DELETE; PRAGMA quick_check;')" != "$(printf 'delete\nok')" ]; then
    echo "ERROR: backup integrity check failed" >&2
    exit 1
fi
chmod 600 "$TMP"
rm -f "$TMP-wal" "$TMP-shm"
mv -f "$TMP" "$DEST"
trap - EXIT
echo "Backup written: $DEST"

find "$BACKUP_DIR" -name 'esp32cam_*.sqlite3' -mtime "+${RETAIN_DAYS}" -delete
