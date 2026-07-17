# Travel Hub CRM — Backend

Backend для CRM-системы туристической компании. Проект построен на Django, Django REST Framework и PostgreSQL.

## Быстрый запуск

### 1. Установите зависимости

Для управления зависимостями используется `uv`.

```bash
uv sync
```

### 2. Запустите PostgreSQL

Пример для macOS с Homebrew:

```bash
brew services start postgresql@16
createdb travelhub
```

### 3. Настройте переменные окружения

```bash
cp .env.example .env
```

При необходимости отредактируйте значения в `.env`.

### 4. Выполните миграции

```bash
uv run python manage.py migrate
```

### 5. Создайте администратора и организацию

```bash
uv run python manage.py bootstrap_tenant \
  --admin-email admin@travelhub.local \
  --admin-password 'Adm1n-Demo-2026!'
```

### 6. Запустите сервер

```bash
uv run python manage.py runserver
```

API будет доступно по адресу:

```text
http://127.0.0.1:8000/
```

## Фоновые задачи

В отдельном терминале запустите обработчик фоновых заданий:

```bash
uv run python manage.py run_jobs
```

Периодические проверки в режиме разработки можно запускать вручную:

```bash
uv run python manage.py run_scheduled_jobs
```

В production эту команду необходимо запускать через cron или Kubernetes CronJob каждую минуту.

## Демо-данные

После выполнения миграций можно создать тестовые данные:

```bash
uv run python manage.py seed_demo_data
```

Будут созданы:

* пользователи с разными ролями;
* клиенты;
* поставщики;
* заказы;
* связанные тестовые данные.

Доступные пользователи:

```text
admin@travelhub.local
operator@travelhub.local
accountant@travelhub.local
manager@travelhub.local
```

Пароль для всех пользователей:

```text
Demo-Pass-2026!
```

## Документация API

После запуска сервера доступны:

* Swagger UI — `http://127.0.0.1:8000/api/v1/docs/`
* OpenAPI 3.1 — `http://127.0.0.1:8000/api/v1/schema/`
* ReDoc — `http://127.0.0.1:8000/api/v1/redoc/`
* Django Admin — `http://127.0.0.1:8000/admin/`

Проверка состояния приложения:

* `/health/live/` — приложение запущено;
* `/health/ready/` — приложение готово принимать запросы.

## Тесты

Создайте отдельную тестовую базу данных:

```bash
createdb travelhub_test
```

Запуск всех тестов:

```bash
uv run pytest
```

Запуск тестов с отчётом о покрытии:

```bash
uv run pytest --cov
```

## Запуск через Docker

Создайте файл окружения:

```bash
cp .env.example .env
```

Обязательно заполните:

```text
DJANGO_SECRET_KEY
FIELD_ENCRYPTION_KEY
```

Затем запустите проект:

```bash
docker compose up --build
```

Docker Compose поднимает:

* Django API;
* PostgreSQL;
* обработчик фоновых задач;
* сервис периодических заданий.

## Структура проекта

| Приложение | Назначение                                                                                            |
| -------------------- | --------------------------------------------------------------------------------------------------------------- |
| `config`           | Настройки проекта, маршруты, ASGI и WSGI                                               |
| `common`           | Базовые модели, деньги, ошибки, аудит, фоновые задачи, health checks |
| `tenancy`          | Организации и изоляция данных между ними                                     |
| `accounts`         | Пользователи, роли, права доступа, JWT, сессии и 2FA                         |
| `crm`              | Клиенты и основная CRM-логика                                                             |
| `travel_policy`    | Политики и правила поездок                                                               |
| `orders`           | Заказы и управление их статусами                                                    |
| `services`         | Общая работа с туристическими услугами                                        |
| `avia`             | Авиабилеты                                                                                            |
| `rail`             | Железнодорожные билеты                                                                     |
| `hotels`           | Отели и размещение                                                                              |
| `groups_app`       | Групповые поездки                                                                               |
| `offers`           | Коммерческие предложения                                                                 |
| `suppliers`        | Поставщики                                                                                            |
| `booking`          | Бронирования                                                                                        |
| `finance`          | Платежи, счета и финансовые операции                                             |
| `documents`        | Документы и шаблоны                                                                            |
| `aftersales`       | Возвраты, обмены и сопровождение после продажи                          |
| `communications`   | История коммуникаций                                                                         |
| `notifications`    | Уведомления                                                                                          |
| `calendar_app`     | Календарь и события                                                                            |
| `workforce`        | Сотрудники и распределение работы                                                 |
| `integrations`     | Внешние интеграции                                                                             |
| `reports`          | Отчёты и аналитика                                                                              |
| `search`           | Глобальный поиск                                                                                 |

## Основные правила проекта

### Деньги

Все денежные значения хранятся через `Decimal`.

Валюта указывается по стандарту ISO 4217.

Пример ответа API:

```json
{
  "amount": "1720.00",
  "currency": "USD"
}
```

### Ошибки API

Все ошибки возвращаются в едином формате:

```json
{
  "error": {
    "code": "ERROR_CODE",
    "message": "Описание ошибки",
    "fields": {},
    "details": {},
    "request_id": "request-id"
  }
}
```

### Смена статусов

Статусы сущностей изменяются через отдельный endpoint:

```text
POST .../transition/
```

Для смены статуса не используется обычный `PATCH`.

Для защиты от параллельного редактирования применяется поле `version`.

### Идемпотентность

Опасные операции требуют заголовок:

```text
Idempotency-Key
```

Повторный запрос с тем же ключом, но другим телом вернёт:

```text
409 IDEMPOTENCY_CONFLICT
```

### Безопасность данных

* аудит работает в режиме append-only;
* чувствительные поля шифруются через `FIELD_ENCRYPTION_KEY`;
* персональные данные маскируются в ответах и логах;
* логи выводятся в JSON-формате;
* каждый запрос получает `request_id`;
* `request_id` передаётся через заголовок `X-Request-ID`.

### Обновления для frontend

Frontend получает обновления через:

```text
GET /api/v1/events/?cursor=
```

Поддерживаются:

* `ETag`;
* ответ `304 Not Modified`;
* хранение событий в течение 7 дней.

При использовании устаревшего курсора API возвращает:

```text
410 Gone
```

## Резервное копирование

Создание backup:

```bash
pg_dump -Fc travelhub > backup.dump
```

Восстановление:

```bash
pg_restore -d travelhub --clean backup.dump
```

В production резервные копии необходимо:

* создавать ежедневно;
* хранить в зашифрованном виде;
* регулярно проверять через тестовое восстановление.

## Production-рекомендации

* `run_jobs` должен работать как отдельный сервис;
* можно запускать несколько экземпляров `run_jobs`;
* конкурентная обработка защищена через `FOR UPDATE SKIP LOCKED`;
* `run_scheduled_jobs` должен запускаться каждую минуту;
* параллельный запуск периодических задач блокируется advisory lock;
* `/health/ready/` проверяет PostgreSQL и heartbeat обработчика фоновых задач;
* приложение должно выводить структурированные JSON-логи в `stdout`.
