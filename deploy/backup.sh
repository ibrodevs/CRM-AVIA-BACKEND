#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

COMPOSE="docker compose -f docker-compose.prod.yml"
BACKUP_DIR="$ROOT_DIR/.runtime/backups"
mkdir -p "$BACKUP_DIR"

POSTGRES_DB_VALUE="${POSTGRES_DB:-travelhub}"
POSTGRES_USER_VALUE="${POSTGRES_USER:-travelhub}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="$BACKUP_DIR/travelhub-$STAMP.dump"

$COMPOSE exec -T db pg_dump \
  -U "$POSTGRES_USER_VALUE" \
  -d "$POSTGRES_DB_VALUE" \
  -Fc > "$TARGET"

if [[ ! -s "$TARGET" ]]; then
  echo "ERROR: backup file is empty: $TARGET"
  rm -f "$TARGET"
  exit 1
fi

# Keep local backups for 14 days. REG.RU infrastructure backups remain separate.
find "$BACKUP_DIR" -type f -name 'travelhub-*.dump' -mtime +14 -delete

echo "Backup created: $TARGET"
