# Локальная проверка A&A / Roomlink

Выполнять из `CRM-AVIA-BACKEND`. Пароль вводится только в интерактивном терминале через `getpass` и сохраняется через `EncryptedTextField`.

## Подготовка

```bash
.venv/bin/uv sync --group dev
.venv/bin/python manage.py migrate
.venv/bin/python manage.py configure_aanda
```

Команда автоматически выбирает существующего A&A supplier. Если его нет, создаёт активного поставщика гостиниц в однозначно определяемой организации. Короткий список выбора появляется только при неоднозначности.

Ввести только три значения: `BuyerID` (актуальный: `HMEL2024`), `Login / UserID` (актуальный: `hmel`) и пароль. UserID `HMEL2024` больше не использовать. Пароль получает `getpass`; если скрытый ввод недоступен, команда прекращает работу.

Команда создаёт или обновляет sandbox credential через `EncryptedTextField`, проверяет чтение адаптером и шифрование в БД, затем автоматически выполняет read-only `CurrencyListRequest`. Окружение на время команды принудительно `sandbox`, без записи в `.env`. При успехе credential получает статус `active`; при отказе авторизации — `failed`. Пароль и XML не выводятся.

Для этой рабочей копии поставщик: `7fead928-56fe-478e-b949-48cc653b8bd1`.

```bash
.venv/bin/python manage.py aanda_sandbox_check
```

Ожидаются `A&A AUTH: OK`, `Credentials: OK` и `Sandbox connection: OK`. Проверяются sandbox gateway, выбор credential и отсутствие пароля в новых IntegrationLog. При ошибке выводятся HTTP status, числовой A&A error code (если получен) и безопасный код описания. `AUTH_ERROR` означает, что шлюз отклонил доступ. Проверить реквизиты и разрешение XML API у Roomlink; не добавлять пароль в аргументы команды, историю shell или сообщения.

## Реальные Search и Pricing без финансовых операций

После успешной авторизации можно проверить поиск по **коду города A&A**, полученному из его справочника:

```bash
.venv/bin/python manage.py aanda_sandbox_check \
  --supplier 7fead928-56fe-478e-b949-48cc653b8bd1 \
  --city-code 18 --check-in 2027-03-10 --check-out 2027-03-12
```

Код `18` — пример из документации (Самара), не результат успешного живого поиска в этой работе. Заменить на актуальный supplier code и будущие даты. Альтернатива: `--hotel-code` вместо `--city-code`. Команда выводит только число предложений, технические ID, цену и результат Pricing. Она **не содержит booking/cancel**.

## Проверка через существующий API CRM

Используйте обычный backend с той же БД, в которой выполнен setup. `hbpro_ui_demo` из предыдущей проверки использует отдельную БД и подменяет HB transport; созданного здесь A&A supplier в нём нет. Обычный backend можно запустить на свободном порту:

```bash
.venv/bin/python manage.py runserver 127.0.0.1:8001
```

Для интерфейса остановить прежний frontend dev process в его терминале и запустить из `CRM-Admin` с `BACKEND_URL=http://127.0.0.1:8001 npm run dev`. Это направит интерфейс в локальный backend с настроенными credentials.

1. У поставщика указать `service_kinds=["hotel"]`; активировать credential через существующую проверку подключения. Настроить `SupplierSearchPriority` для гостиниц, включив A&A и Hotelbook в нужном порядке.
2. Создать `POST /api/v1/service-searches/` с приведённым ниже телом. Для общего поиска можно передать название города в `location`: backend найдёт A&A city code в отдельном supplier cache. Цифровой Hotelbook ID не интерпретируется как A&A hotel code.
3. Дождаться `completed` или `partial`, получить `/api/v1/service-searches/{search_id}/offers/`. Офферы разных suppliers должны сохраняться отдельно даже при одинаковых гостинице/цене. Поле `provider_adapter` выбранного offer должно быть `aanda`.
4. Через `POST /api/v1/orders/{order_id}/services/` прикрепить `offer_id`. Для нескольких комнат передать `room_assignments`: ключ — ID участника заказа, значение — `room:0`, `room:1` и т. д. То же назначение доступно в `PUT /api/v1/services/{service_id}/passengers/` вместе с актуальной `version` и `participants`.
5. Создать workflow через `POST /api/v1/booking-workflows/`, затем выполнить `/preflight/`. Проверить отсутствие blocking errors; новые цена/условия требуют существующего подтверждения CRM. Для A&A-only workflow паспорт не требуется поставщиком; другие проверки профиля CRM сохраняются.
6. В `IntegrationLog` должны появиться `hotel_search` и `hotel_pricing`; в `provider_snapshot.aanda` — подробные условия и время revalidation. Пароль и гости в журнал не попадают.

```json
{
  "kind": "hotel",
  "criteria": {
    "check_in": "2027-03-10",
    "check_out": "2027-03-12",
    "rooms": [{"adults": 1, "quantity": 1}],
    "aanda": {"city_code": "18"}
  }
}
```

Для двух комнат: `rooms=[{"adults":1,"quantity":1},{"adults":2,"quantity":1}]`, три гостя с назначением по комнатам. Нельзя смешивать currencies/WhereToPay в одном заказе. Детские ages не поддержаны прочитанным XML2-контрактом: адаптер возвращает `NOT_SUPPORTED`, не превращая детей во взрослых.

## Booking / cancel

Это финансовые операции. Read-only команда выше их не выполняет. В рамках выданного ТЗ разрешён тестовый flow на указанном шлюзе, но в этой работе он заблокирован авторизацией. До исправления credentials и подтверждения тестового режима полный живой flow не считается проверенным.

После допуска тестового аккаунта использовать обычные CRM workflow `/start/`, `/status-inquiry/`, `/cancel/`. Проверить:

- После свежего preflight не появляется второй `HotelPricingRequest` в пределах 30 секунд.
- В заказе сохранены `OrderId`, CRM ReferenceNumber и реальные статусы; `11/18/20/21` остаются pending.
- Для отмены сначала есть свежий `order_info` и `AllowCancel=1`; штраф и deadline сохранены в cancellation quote. Не выбирать для автоматического smoke заказ со штрафом или неизвестным штрафом.
- После booking/cancel timeout не нажимать повторно финансовую операцию: выполнить inquiry по сохранённому ReferenceNumber/OrderId. Адаптер блокирует повтор неоднозначной mutation.
- `PRICE_CHANGED` после получения OrderInfo означает расхождение фактической суммы созданного заказа с Pricing. Заказ сохраняется, создаётся incident; нужна проверка оператора. Автоматической отмены нет.

## Автоматические проверки без внешнего API

```bash
.venv/bin/pytest integrations booking services suppliers --disable-warnings
CORS_ALLOW_ALL_ORIGINS=true .venv/bin/pytest --disable-warnings
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
```

Fixtures перехватывают HTTP: реальные booking/cancel не выполняются. В исходном коммите воспроизводится отдельное падение PostgreSQL-теста аватара (`accounts/tests/test_profile_settings.py::test_avatar_validates_bytes_and_is_private`); подробности и точные результаты — в `integration-report.md`. CORS-флаг в команде относится только к окружению тестового процесса, а не меняет настройки сервера.
