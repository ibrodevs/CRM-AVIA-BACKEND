# Отчёт о реализации backend Travel Hub CRM

Дата: 16.07.2026. Основание: `../BACKEND-TZ-DJANGO-REST.md` v1.0.
Стек: Python 3.13, Django 5.2, DRF, PostgreSQL 16, без Redis/Celery.

## Статус: все 6 этапов ТЗ реализованы, 128 автотестов проходят

| Этап | Состав | Статус |
|---|---|---|
| 1. Platform/Core | config, common (деньги/ошибки/идемпотентность/audit/outbox), tenancy, accounts (JWT+сессии, 2FA TOTP, RBAC в БД), PostgreSQL job runner, health, OpenAPI 3.1, Docker | ✅ |
| 2. CRM и заказы | persons/documents (шифрование+маска)/loyalty/clients, companies/contracts/agreements/settlement, fee-templates, travel policy + `/check/`, suppliers (+credentials/markup/priorities), orders (счётчик номеров, статусная машина, участники, маршрут, overview, история, задачи, duplicate), trips/calendar, глобальный поиск | ✅ |
| 3. Услуги и коммерция | mock provider adapter (единый интерфейс, sanitized логи, моделирование ошибок), SearchSession/ProviderRun/Offer (202+polling, дедуп, события search.*), pricing (FeeRule+markup → immutable PriceSnapshot, ROUND_HALF_UP), OrderService lifecycle, extras, avia/rail/hotels модели с ограничениями §30, КП (версии, approve, PDF), service cards (public token, идемпотентный respond) | ✅ |
| 4. Operations | BookingWorkflow: preflight (§10 полный чек-лист), saga брони, issue с защитой ambiguous timeout (unknown → блокировка → status inquiry, без дублей билетов), compensating cancellation; группы (блоки, матрица, mass-actions per-item, roster import CSV/XLSX → preview same/changed/new/missing/conflict → apply c merge history, экспорт с транслитом и CSV-injection защитой); документы (magic bytes, SHA-256, версии-исправления с причиной, шаблоны, sign stub, receipt import/confirm); чаты (internal/client/supplier, read state, mentions, webhook dedup+signature) | ✅ |
| 5. Finance/Post-sale | двойной ledger (сбалансированность проверяется), obligations, payments (+confirm идемпотентный, allocations ≤ платежа), refund по формуле §14.4 со snapshot, депозит-резерв, сверка (CSV импорт, auto/manual match), aftersales cases (state machine §16, версионируемые quote, согласие клиента аннулируется при новой версии, execute требует финансовой операции) | ✅ |
| 6. Management | notifications (rules → события → персональные записи, deadline-check без дублей), dashboard (role-aware агрегаты), SLA queue, смены (одна открытая, immutable closing report, подтверждение расхождений), мотивация, integration incidents (assign/retry/snooze/switch/resolve/reopen/escalate; retry unknown заблокирован), `/meta/`, seed_demo_data | ✅ |

## Запуск

См. `README.md`. Кратко: `uv sync` → `manage.py migrate` → `manage.py bootstrap_tenant ...` → `manage.py seed_demo_data` → `manage.py runserver` + `manage.py run_jobs`.
Демо-пользователи: `admin|operator|accountant|manager@travelhub.local` / `Demo-Pass-2026!`.

## Ключевые инварианты (сквозные)

- Ошибки: единый контракт `{"error": {code, message, fields, details, request_id}}`; 409 для version/idempotency/transition конфликтов, 422 для бизнес-отказов.
- Деньги: Decimal + ISO 4217, `{"amount": "...", "currency": "..."}`, ROUND_HALF_UP.
- Статусы: только командные endpoint-ы `/transition/`; PATCH статуса возвращает `FIELD_NOT_PATCHABLE`.
- `Idempotency-Key` обязателен для платежей/выписки/возвратов/отправки; повтор с другим телом → `IDEMPOTENCY_CONFLICT`.
- Optimistic locking через `version` + `select_for_update`.
- Audit append-only; паспорта/счета/секреты шифруются (Fernet) и маскируются; секреты не возвращаются после сохранения.
- События: outbox в одной транзакции с данными; `/events/?cursor=` c ETag/304 и 410 по retention.
- Tenant isolation во всех queryset (контекст + фильтры); объектная область оператора (видит только свои заказы).
- Ограничения §30 реализованы в PostgreSQL (partial unique constraints: номер заказа, документы, билеты, места, блоки, открытая смена, webhook, idempotency и т.д.).

## Явные ограничения текущего этапа (по ТЗ §1.1, §29)

1. **Все внешние интеграции — mock/sandbox адаптер** (`integrations/adapters.py`): GDS/авиа/ЖД/отели, платёжный шлюз, мессенджеры (Telegram/WhatsApp/MAX), email, OCR квитанций, антивирус-скан файлов, ЭЦП. Интерфейс адаптера зафиксирован; реальные провайдеры подключаются реализацией `ProviderAdapter` без изменения API.
2. **Файлы** — локальная ФС в dev; для production требуется S3-compatible storage (настройка `django-storages`, pre-signed URL). Интерфейс версий/скачивания не изменится.
3. **PDF КП** — минимальный встроенный рендерер (латиница/транслит); production-вёрстка — подключение weasyprint тем же интерфейсом `render_proposal_pdf`.
4. **Поиск** — icontains/индексы; PostgreSQL FTS + pg_trgm подготовлены архитектурно, но не включены.
5. **2FA** — TOTP реализован; enforcement для admin/finance ролей в production включается политикой.
6. **OpenAPI** — генерируется (239 операций), но часть APIView без typed serializers даёт generic-схемы (⚠ warnings drf-spectacular). Контракт стабилен, типизацию схем можно доуточнить.
7. **Rate limiting** — DRF throttling (login/search/public/export); nginx-уровень — на деплое.
8. **Не реализовано из «желательных»**: WebSocket (по ТЗ и не требуется — polling), ChangeCase форс-мажора (§12.3, модельная основа есть в trips/conflicts), ApiClient внешних потребителей CRM (§21.2), экспорт XLSX больших отчётов (§14.4 — экспорт CSV реализован в группах/сверке).

## Вопросы владельцу продукта (блокируют только production, ТЗ §29)

Провайдеры GDS/платежей/мессенджеров, валюты и налоги, ЭЦП, retention, точная матрица object-level прав («оператор видит все заказы или только свои» — сейчас: только свои + переназначенные).

## Тесты

`uv run pytest` — 128 тестов: auth/2FA/сессии/brute force, RBAC-матрица и tenant isolation, статусные машины (заказ/услуга/КП/группа/aftersale), идемпотентность, job runner (locking/retry/dead/stale/outbox), поиск+pricing, booking saga + ambiguous issue, roster reconcile, файловая безопасность, ledger-баланс, формула возврата, дедлайны без дублей, смены, webhook dedup.
