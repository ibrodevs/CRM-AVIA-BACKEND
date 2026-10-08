#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

COMPOSE="docker compose -f docker-compose.prod.yml"

if [[ ! -f .env ]]; then
  echo "ERROR: .env not found in $ROOT_DIR"
  echo "Copy deploy/.env.production.example to .env and fill production values first."
  exit 1
fi

mkdir -p .runtime/postgres .runtime/media .runtime/static .runtime/backups

# Dockerfile pins appuser to UID/GID 1000. Make persistent application mounts
# writable by that user. Running as root can fix ownership automatically; a
# regular deploy user must itself own these directories as UID 1000.
if [[ "$(id -u)" == "0" ]]; then
  chown -R 1000:1000 .runtime/media .runtime/static
else
  for path in .runtime/media .runtime/static; do
    if [[ "$(stat -c '%u' "$path")" != "1000" ]]; then
      echo "ERROR: $path must be owned by UID 1000."
      echo "Run once: sudo chown -R 1000:1000 .runtime/media .runtime/static"
      exit 1
    fi
  done
fi
chmod 0755 .runtime/media .runtime/static

# Validate interpolation and required variables before touching running services.
$COMPOSE config -q

# Build a fresh application image while keeping database/media data intact.
$COMPOSE build --pull

# Bring PostgreSQL up first and wait for its healthcheck. Read database names
# from the container itself so custom .env values are respected.
$COMPOSE up -d db
for _ in $(seq 1 40); do
  if $COMPOSE exec -T db sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

if ! $COMPOSE exec -T db sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; then
  echo "ERROR: PostgreSQL did not become ready."
  $COMPOSE logs --tail=100 db
  exit 1
fi

# Apply schema changes and collect admin/static assets once before web/jobs start.
$COMPOSE run --rm web python manage.py migrate --noinput
$COMPOSE run --rm web python manage.py collectstatic --noinput

# Start/recreate the full stack.
$COMPOSE up -d --remove-orphans

APP_PORT_VALUE="$(awk -F= '/^APP_PORT=/{print $2}' .env | tail -n1 | tr -d '\r')"
APP_PORT_VALUE="${APP_PORT_VALUE:-8000}"
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${APP_PORT_VALUE}/health/live/" >/dev/null 2>&1; then
    echo "Deployment complete: backend healthcheck is OK."
    $COMPOSE ps
    exit 0
  fi
  sleep 2
done

echo "ERROR: backend healthcheck failed after deployment."
$COMPOSE ps
$COMPOSE logs --tail=150 web
exit 1
