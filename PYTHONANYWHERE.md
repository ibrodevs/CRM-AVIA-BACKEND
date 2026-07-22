# Deploy на PythonAnywhere (SQLite)

Эта конфигурация предназначена для бесплатного аккаунта PythonAnywhere и использует SQLite.

## 1. Клонирование и окружение

```bash
cd ~
git clone https://github.com/ibrodevs/CRM-AVIA-BACKEND.git
cd CRM-AVIA-BACKEND
python3.12 -m venv ~/.virtualenvs/travelhub
source ~/.virtualenvs/travelhub/bin/activate
pip install --upgrade pip
pip install .
```

## 2. Переменные окружения

```bash
cp .env.example .env
nano .env
```

Минимальная production-конфигурация:

```env
DJANGO_SETTINGS_MODULE=config.settings.pythonanywhere
DJANGO_SECRET_KEY=replace-with-a-long-random-secret
FIELD_ENCRYPTION_KEY=replace-with-a-valid-fernet-key
DJANGO_ALLOWED_HOSTS=USERNAME.pythonanywhere.com
CSRF_TRUSTED_ORIGINS=https://USERNAME.pythonanywhere.com
DATABASE_URL=sqlite:////home/USERNAME/CRM-AVIA-BACKEND/db.sqlite3
SQLITE_TIMEOUT=20
SECURE_SSL_REDIRECT=False
ALLOW_MOCK_ADAPTER=False
BUSINESS_TIMEZONE=Asia/Bishkek
LOG_LEVEL=INFO
```

Генерация ключей:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 3. Проверка и база

```bash
export DJANGO_SETTINGS_MODULE=config.settings.pythonanywhere
python manage.py check --deploy
python manage.py migrate
python manage.py collectstatic --noinput
```

Создание администратора и организации:

```bash
python manage.py bootstrap_tenant \
  --admin-email admin@example.com \
  --admin-password 'replace-with-a-strong-password'
```

Для повторного production-обновления уже настроенного проекта можно выполнить:

```bash
bash scripts/pythonanywhere_deploy.sh
```

Скрипт делает `git pull`, обновляет пакет в virtualenv, запускает `check`, `migrate` и `collectstatic`. После него всё равно нужно нажать **Reload** на вкладке Web в PythonAnywhere.

## 4. Web App

Создайте Manual configuration Web App с Python 3.12.

Virtualenv:

```text
/home/USERNAME/.virtualenvs/travelhub
```

Source code:

```text
/home/USERNAME/CRM-AVIA-BACKEND
```

WSGI-файл PythonAnywhere:

```python
import os
import sys

project_home = "/home/USERNAME/CRM-AVIA-BACKEND"
if project_home not in sys.path:
    sys.path.insert(0, project_home)

os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.pythonanywhere"

from config.wsgi_pythonanywhere import application
```

Static files:

```text
URL: /static/
Directory: /home/USERNAME/CRM-AVIA-BACKEND/staticfiles
```

Media files:

```text
URL: /media/
Directory: /home/USERNAME/CRM-AVIA-BACKEND/media
```

## 5. Проверка

После Reload проверьте:

```text
https://USERNAME.pythonanywhere.com/health/live/
https://USERNAME.pythonanywhere.com/health/ready/
https://USERNAME.pythonanywhere.com/api/v1/docs/
https://USERNAME.pythonanywhere.com/admin/
```

## Ограничение SQLite

SQLite подходит для демо и небольшой нагрузки. Не запускайте несколько параллельных job worker-процессов. При росте нагрузки перенесите базу на PostgreSQL, задав новый `DATABASE_URL`.
