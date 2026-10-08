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

# Validate interpolation and required variables before touching running services.
$COMPOSE config -q

# Build a fresh application image while keeping the database volume intact.
$COMPOSE build --pull

# Bring PostgreSQL up first and wait for its healthcheck.
$COMPOSE up -d db
for _ in $(seq 1 40); do
  if $COMPOSE exec -T db pg_isready -U "${POSTGRES_USER:-travelhub}" -d "${POSTGRES_DB:-travelhub}" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

if ! $COMPOSE exec -T db pg_isready -U "${POSTGRES_USER:-travelhub}" -d "${POSTGRES_DB:-travelhub}" >/dev/null 2>&1; then
  echo "ERROR: PostgreSQL did not become ready."
  $COMPOSE logs --tail=100 db
  exit 1
fi

# Apply schema changes and collect admin/static assets once before web/jobs start.
$COMPOSE run --rm web python manage.py migrate --noinput
$COMPOSE run --rm web python manage.py collectstatic --noinput

# Start/recreate the full stack.
$COMPOSE up -d --remove-orphans

APP_PORT="${APP_PORT:-8000}"
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${APP_PORT}/health/live/" >/dev/null 2>&1; then
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
