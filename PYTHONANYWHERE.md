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
python scripts/setup_pythonanywhere_ocr.py
python manage.py check --deploy
python manage.py check_receipt_ocr
python manage.py migrate
python manage.py collectstatic --noinput
```

### OCR квитанций без Docker

PythonAnywhere запускает backend напрямую из virtualenv, поэтому Dockerfile для OCR не используется.
Скрипт `setup_pythonanywhere_ocr.py`:

- находит системный `/usr/bin/tesseract`;
- устанавливает русскую и английскую модели в `.runtime/tessdata` внутри проекта;
- проверяет, что версия Tesseract не ниже 4;
- использует установленный через `pip` пакет `pypdfium2` для преобразования страниц PDF в изображения, поэтому `apt install poppler-utils` не требуется.

Проверить production-окружение можно в Bash-консоли PythonAnywhere:

```bash
cd ~/CRM-AVIA-BACKEND
source ~/.virtualenvs/travelhub/bin/activate
which tesseract
tesseract --version
python manage.py check_receipt_ocr
```

Ожидаемый результат содержит `"ready": true`, языки `eng` и `rus`, а также renderer
`pypdfium2`. Каталог `.runtime/` не хранится в Git и сохраняется между обычными обновлениями проекта.

Если на старом system image доступен Tesseract ниже версии 4, сначала обновите system image в
настройках аккаунта PythonAnywhere, затем заново создайте virtualenv и повторите установку.

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

Скрипт делает `git pull`, обновляет пакет в virtualenv, устанавливает и проверяет OCR,
запускает `check`, `migrate` и `collectstatic`. После него всё равно нужно нажать **Reload**
на вкладке Web в PythonAnywhere.

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

## 6. Фоновая работа: очереди, уведомления, доставка

На обычном сервере фоновая часть работает двумя процессами: `run_jobs` живёт
постоянно, `run_scheduled_jobs` запускается по cron каждую минуту. **На
PythonAnywhere нет ни того, ни другого:** Bash-консоль закрывается вместе с
процессом, а планировщик даёт ограниченное число запусков.

Если фоновую часть не запускать, снаружи это выглядит как «ничего не
происходит»: события копятся необработанными, уведомления не создаются,
письма и сообщения остаются в очереди в состоянии `queued`.

Для этого есть команда, выполняющая **весь проход целиком за один вызов** и
завершающаяся:

```bash
python manage.py run_worker_pass
```

Она делает то же, что оба процесса вместе: разбирает outbox-события, выполняет
готовые фоновые задания и прогоняет периодические задачи (в том числе три
очереди доставки и проверку дедлайнов). Выберите один из способов ниже.

### Способ А. Always-on task (платный аккаунт) — рекомендуется

Вкладка **Tasks → Always-on tasks**. Команда:

```bash
cd /home/USERNAME/CRM-AVIA-BACKEND && /home/USERNAME/.virtualenvs/travelhub/bin/python manage.py run_worker_pass --loop --seconds 3300 --interval 30
```

Проход выполняется каждые 30 секунд в течение 55 минут, затем процесс
завершается и PythonAnywhere перезапускает его сам. Это ближе всего к обычному
воркеру: уведомления и письма уходят почти сразу.

### Способ Б. Scheduled task (почасовой, платный аккаунт)

Вкладка **Tasks → Scheduled tasks**, ежечасно:

```bash
cd /home/USERNAME/CRM-AVIA-BACKEND && /home/USERNAME/.virtualenvs/travelhub/bin/python manage.py run_worker_pass
```

Достаточно одной записи — команда закрывает всю фоновую работу. Задержка
доставки при этом до часа: письмо клиенту уйдёт не сразу.

### Способ В. Внешний cron дёргает HTTP (работает на бесплатном аккаунте)

Бесплатный аккаунт даёт один запуск в сутки — для доставки писем это
неприемлемо. Обходной путь: пусть внешний бесплатный сервис
(например `cron-job.org`) раз в несколько минут открывает защищённый адрес.

Задайте в `.env` длинный случайный токен:

```env
WORKER_TRIGGER_TOKEN=сгенерируйте-длинную-случайную-строку
```

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

После **Reload** настройте в cron-сервисе вызов раз в 5 минут:

```text
https://USERNAME.pythonanywhere.com/internal/worker-pass/?token=ВАШ_ТОКЕН
```

Токен можно передавать и заголовком `X-Worker-Token`, что безопаснее: адрес со
строкой запроса попадает в логи сервера.

Проверить вручную:

```bash
curl -s "https://USERNAME.pythonanywhere.com/internal/worker-pass/?token=ВАШ_ТОКЕН"
# {"status": "ok", "started_at": "...", "duration_ms": 412}
```

Свойства эндпоинта:

- **выключен по умолчанию** — пока `WORKER_TRIGGER_TOKEN` пуст, отвечает 404,
  так что случайно оставить его открытым нельзя;
- токен сравнивается в постоянное время, неверный даёт 403;
- одновременные вызовы не накладываются: второй получит `{"status": "busy"}`;
- проход выполняется внутри веб-запроса, поэтому держите интервал не чаще
  одного раза в 2–3 минуты и следите, чтобы проход укладывался в лимит
  времени запроса PythonAnywhere.

### Проверка, что фоновая часть действительно работает

```text
https://USERNAME.pythonanywhere.com/health/ready/
```

В ответе поле `checks.job_runner`: `ok` — задания обрабатываются, `stale` —
воркер не отрабатывает и задания зависли.

Состояние очередей доставки (нужна авторизация в CRM):

```text
/api/v1/notification-channels/     — какие каналы вообще настроены
/api/v1/notification-deliveries/   — что ушло, что нет и почему
```

Если записи подолгу остаются в состоянии `queued` — значит проход не
запускается, и надо возвращаться к этому разделу.

## Ограничение SQLite

SQLite подходит для демо и небольшой нагрузки. Не запускайте несколько параллельных job worker-процессов. При росте нагрузки перенесите базу на PostgreSQL, задав новый `DATABASE_URL`.
