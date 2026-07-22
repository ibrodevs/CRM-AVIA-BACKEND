#!/usr/bin/env bash
set -euo pipefail

BRANCH="${BRANCH:-main}"
VENV_PATH="${VENV_PATH:-$HOME/.virtualenvs/travelhub}"
SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-config.settings.pythonanywhere}"

cd "$(dirname "$0")/.."

git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

source "$VENV_PATH/bin/activate"
python -m pip install --upgrade pip
python -m pip install .

export DJANGO_SETTINGS_MODULE="$SETTINGS_MODULE"
python manage.py check
python manage.py migrate
python manage.py collectstatic --noinput

cat <<'MSG'

PythonAnywhere backend files are updated.
Next step: open the PythonAnywhere Web tab and press Reload for this web app.
Then run the frontend production smoke with real credentials:

  SMOKE_LOGIN='admin@example.com' SMOKE_PASSWORD='***' npm run smoke:production

MSG
