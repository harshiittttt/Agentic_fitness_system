#!/usr/bin/env bash
# Consistent nightly backup of every person's fitness.db using SQLite's online backup API (safe on a live DB).
# Usage: deploy/backup.sh [backup_dir]      cron: 15 3 * * * /path/to/repo/deploy/backup.sh /var/backups/fitness
# Keeps 14 days. Volumes are discovered by the compose project label; override with VOLUMES="vol1 vol2".
set -euo pipefail
DEST="${1:-./backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
KEEP_DAYS="${KEEP_DAYS:-14}"
mkdir -p "$DEST"

VOLUMES="${VOLUMES:-$(docker volume ls -q --filter label=fitness.user)}"
[ -n "$VOLUMES" ] || { echo "no volumes found (label fitness.user)"; exit 1; }

for vol in $VOLUMES; do
  out="$DEST/${vol}-${STAMP}.db"
  # a throwaway container mounts the volume and uses sqlite3's backup() (python stdlib), not a raw cp
  docker run --rm -v "$vol":/data -v "$(cd "$DEST" && pwd)":/out -e OUT="/out/$(basename "$out")" \
    python:3.11-slim python -c "
import os, sqlite3
src = sqlite3.connect('/data/fitness.db')
dst = sqlite3.connect(os.environ['OUT'])
src.backup(dst); dst.close(); src.close()
print('ok', os.environ['OUT'])
"
done
find "$DEST" -name '*.db' -mtime +"$KEEP_DAYS" -delete
