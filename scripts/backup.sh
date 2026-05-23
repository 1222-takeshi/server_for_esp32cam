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

if [ ! -f "$DB_PATH" ]; then
    echo "ERROR: database not found: $DB_PATH" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"

DEST="${BACKUP_DIR}/esp32cam_$(date +%Y%m%d).sqlite3"
sqlite3 "$DB_PATH" ".backup '$DEST'"
echo "Backup written: $DEST"

find "$BACKUP_DIR" -name 'esp32cam_*.sqlite3' -mtime "+${RETAIN_DAYS}" -delete
