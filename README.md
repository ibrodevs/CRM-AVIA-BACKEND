# Travel Hub CRM — Backend

```bash
# демо-данные (после migrate): 4 пользователя ролей, клиенты, поставщики, заказы
uv run python manage.py seed_demo_data
# логины: admin|operator|accountant|manager@travelhub.local / Demo-Pass-2026!
```

## Быстрый старт (dev)

```bash
# зависимости (нужен uv: https://docs.astral.sh/uv/)
uv sync

# PostgreSQL 16 (пример для macOS/Homebrew)
brew services start postgresql@16
createdb travelhub

# конфигурация
cp .env.example .env   # при необходимости отредактируйте

# миграции и стартовые данные
uv run python manage.py migrate
uv run python manage.py bootstrap_tenant \
    --admin-email admin@travelhub.local --admin-password 'Adm1n-Demo-2026!'

# web-сервер
uv run python manage.py runserver

# в отдельном терминале — воркер фоновых заданий
uv run python manage.py run_jobs

# периодические проверки (в dev можно вручную; в prod — cron каждую минуту)
uv run python manage.py run_scheduled_jobs
```

- Swagger UI: http://127.0.0.1:8000/api/v1/docs/
- OpenAPI 3.1: http://127.0.0.1:8000/api/v1/schema/
- ReDoc: http://127.0.0.1:8000/api/v1/redoc/
- Django admin: http://127.0.0.1:8000/admin/
- Health: `/health/live/`, `/health/ready/`

## Тесты

```bash
createdb travelhub_test   # один раз
uv run pytest             # весь набор
uv run pytest --cov       # с coverage
```

## Docker

```bash
cp .env.example .env      # заполнить DJANGO_SECRET_KEY и FIELD_ENCRYPTION_KEY
docker compose up --build # web + jobs + cron + PostgreSQL
```

## Архитектура

| Приложение                                                                                                                                                                                                                                                                                        | Ответственность                                                                                                                                                                             |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `config`                                                                                                                                                                                                                                                                                                  | settings (base/dev/prod/test), urls, ASGI/WSGI                                                                                                                                                             |
| `common`                                                                                                                                                                                                                                                                                                  | базовые модели, деньги (Decimal, ROUND_HALF_UP), контракт ошибок, request id, идемпотентность, audit (append-only), outbox, BackgroundJob + runner, health |
| `tenancy`                                                                                                                                                                                                                                                                                                 | Organization, tenant-контекст, изоляция queryset-ов                                                                                                                                      |
| `accounts`                                                                                                                                                                                                                                                                                                | пользователи, роли/permissions (RBAC в БД), JWT+сессии, 2FA (TOTP), preferences                                                                                                   |
| `crm`, `travel_policy`, `orders`, `services`, `avia`, `rail`, `hotels`, `groups_app`, `offers`, `suppliers`, `booking`, `finance`, `documents`, `aftersales`, `communications`, `notifications`, `calendar_app`, `workforce`, `integrations`, `reports`, `search` | этапы 2–6 ТЗ                                                                                                                                                                                       |

### Ключевые инварианты

- Деньги — только `Decimal` + ISO 4217; в ответах `{"amount": "1720.00", "currency": "USD"}`.
- Ошибки — единый контракт `{"error": {code, message, fields, details, request_id}}`.
- Команды смены статуса — `POST .../transition/`, не PATCH; optimistic locking через `version`.
- Опасные операции требуют `Idempotency-Key` (повтор с другим телом → `409 IDEMPOTENCY_CONFLICT`).
- Audit append-only; чувствительные поля шифруются (`FIELD_ENCRYPTION_KEY`) и маскируются.
- Обновления frontend получает через `GET /api/v1/events/?cursor=` (ETag/304, retention 7 дней → `410`).

## Backup / restore

```bash
pg_dump -Fc travelhub > backup.dump          # ежедневный backup (в prod — с шифрованием)
pg_restore -d travelhub --clean backup.dump  # регулярная restore-проверка обязательна
```

## Эксплуатация

- `run_jobs` — отдельный сервис; несколько экземпляров безопасны (`FOR UPDATE SKIP LOCKED`).
- `run_scheduled_jobs` — cron/K8s CronJob каждую минуту; advisory lock исключает параллельный запуск.
- `/health/ready/` проверяет PostgreSQL и свежесть heartbeat job runner-а.
- Логи — JSON в stdout с redaction PII; `request_id` сквозной (`X-Request-ID`).
