# HBPRO — implementation and verification report

2026-10-21. Работа выполнена в двух существующих репозиториях Travel Hub. Отдельное приложение не создавалось. Изменения не закоммичены и не опубликованы. Реальные брони/отмены не выполнялись, обращения в HBPRO от имени владельца не отправлялись, DNS/IP/firewall не менялись.

## 1. IMPLEMENTED

- Изучены оба файла архива: сертификационный PDF и Excel. Оригиналы сохранены побайтно без изменений в docs/hbpro. Аудит с исходной таблицей: HBPRO_CERTIFICATION_AUDIT.md.
- Sandbox дополнен отелем 1251201 из PDF; прежние пять отелей сохранены. Сертификация: 1 взрослый/1 номер/3 ночи/ON_REQUEST и 4 взрослых разных полов попарно/2 номера/2 ночи/ONLINE.
- Существующий Search → Results → Details → Create Order → Book → Search Orders → Order Info → Hotel Item Info → Cancel сохранён и расширен. Новые вызовы и query-параметры подтверждены официальным OpenAPI, загруженным 2026-10-21. API URL не меняется.
- Настоящие очищенные тела запросов/ответов, заголовки, URL, HTTP status, duration, environment, order/correlation/run ID, исходные ошибки и структурированные supplier IDs.
- Явные normalized/original statuses. ON_REQUEST не становится подтверждённым сразу; только CONFIRMED всех items переводит услугу в booked. ONLINE — confirmationMode предложения, не статус брони. Неизвестные и смешанные статусы остаются unresolved.
- Background sync через существующие cron/queue, backoff 1–15 минут, до 100 проверок, один активный job на услугу, terminal stop, incident после исчерпания. Ручная сверка сохранена.
- Межпроцессная lease для retrieve/cancel, долговечные запреты повторной мутации после unknown. Guest validation выполняется до create-order/book; multi-room остаётся одним book-запросом.
- Staged admin command, certification runs, JSON ZIP, автоматическая summary и заполненная копия Excel. Отсутствующие шаги не подделываются; summary проверяет hotel/nights/mode/rooms/adults/genders/IDs и pending → confirmed.
- Переключение credentials через HBPRO_ENVIRONMENT; импорт серверных env/secrets отдельной командой, свежая авторизация и latency при connection check.

## Этапы и проверка

| Этап | Изменённые файлы / области | Результат и проверка | Остаток |
|---|---|---|---|
| STEP 1 — Audit | docs/HBPRO_CERTIFICATION_AUDIT.md, docs/hbpro/* | Изучены все файлы ZIP; original byte comparison PASS; официальный OpenAPI проверен | Реальная доступность отеля проверяется при prepare |
| STEP 2 — Backend | integrations/hotelbook.py, adapters.py, models.py; services/views.py; booking/preflight.py; config/settings/base.py; suppliers/job_handlers.py; env/compose; hbpro_credentials.py | Проверка room/city/hotel/citizenship inputs, parser/errors, guests/contact, env routing; provider fixtures PASS | Production keys после сертификации |
| STEP 3 — Logging | integrations/hotelbook_observability.py, models.py, views.py, urls.py; migration 0003 | Тела/метаданные/IDs, redaction, tenant/RBAC и фильтры; tests PASS | Реальные request/response создадутся при запуске сценариев |
| STEP 4 — Status sync | integrations/job_handlers.py, scheduled_tasks.py, hotelbook_lock.py; booking/job_handlers.py, views.py; migration 0004 | Pending → confirmed, timeout/backoff, dedup, lease, exhaustion, cancellation tests PASS | Внешнее подтверждение HBPRO в scenario 1 |
| STEP 5 — Frontend | Все 8 файлов CRM-Admin ниже | Существующие drawers сохранены; guest-room mapping, supplier terms, logs/errors/filter tabs; 381 tests, architecture и build PASS | Тестовый URL/access владельца |
| STEP 6 — Certification tools | integrations/certification.py; management/commands/hbpro_certification.py; docs/HBPRO_CERTIFICATION_RUNBOOK.md | Полный staged scenario на HTTP fixtures, export/Excel/manifest/RBAC PASS | Реальные сценарии только после подтверждения владельца |
| STEP 7 — Tests | integrations/tests/test_hotelbook.py, test_hbpro_certification.py; frontend test/hotel-guests.test.mjs | 95 HBPRO tests; 158 affected backend tests; 381 frontend tests PASS | Внешняя сертификация |
| STEP 8 — Final verification | Этот отчёт, docs/HOTELBOOK.md | 600 backend tests PASS с 1 явным baseline deselect; system/migrations/diff/changed-lint PASS; Compose YAML PASS | Docker runtime недоступен; baseline avatar/lint issues |

## 2. CHANGED FILES

Полный список приведён ниже. Пути относительно указанных репозиториев.

### CRM-AVIA-BACKEND (31 файл)

```
.env.example
booking/job_handlers.py
booking/preflight.py
booking/views.py
config/settings/base.py
deploy/.env.production.example
docker-compose.yml
docs/HBPRO_CERTIFICATION_AUDIT.md
docs/HBPRO_CERTIFICATION_RUNBOOK.md
docs/HBPRO_IMPLEMENTATION_REPORT.md
docs/HOTELBOOK.md
docs/hbpro/certification-requirements.pdf
docs/hbpro/certification-template.xlsx
integrations/adapters.py
integrations/certification.py
integrations/hotelbook.py
integrations/hotelbook_lock.py
integrations/hotelbook_observability.py
integrations/job_handlers.py
integrations/management/commands/hbpro_certification.py
integrations/management/commands/hbpro_credentials.py
integrations/migrations/0003_hbpro_certification_telemetry.py
integrations/migrations/0004_hbpro_operation_lease.py
integrations/models.py
integrations/scheduled_tasks.py
integrations/tests/test_hbpro_certification.py
integrations/tests/test_hotelbook.py
integrations/urls.py
integrations/views.py
services/views.py
suppliers/job_handlers.py
```

### CRM-Admin (8 файлов)

```
src/modules/orders/ui/BookingWizard.jsx
src/modules/orders/ui/OrderCard.jsx
src/modules/services/hotels/HotelsPage.jsx
src/modules/services/hotels/guests.js
src/modules/suppliers/api/suppliersApi.js
src/modules/suppliers/ui/SupplierIntegrationJournal.jsx
src/modules/suppliers/ui/SuppliersPage.jsx
test/hotel-guests.test.mjs
```

## 3. DATABASE MIGRATIONS

- `0003_hbpro_certification_telemetry`: HotelbookCertificationRun; IntegrationLog environment/method/URL/headers/IDs/status/provider_error/order/certification_run. Старые логи сохраняются; новые поля имеют безопасные blank/null/default.
- `0004_hbpro_operation_lease`: HotelbookOperationLease с уникальностью по OrderService, owner и expiry; координация между процессами.

Миграции применены тестовым runner на PostgreSQL. `migrate --check` и `makemigrations --check --dry-run` прошли на тестовой БД. Рабочая БД не мигрировалась этой задачей; существующий deploy/Compose выполняет стандартный `manage.py migrate`.

## 4. API CHANGES

Все URL имеют префикс `/api/v1/`.

| API | Изменение |
|---|---|
| GET integration-operations/ | Добавлены environment, http_method, URL, headers, order_id, external_ids, supplier_status, provider_error, certification_run_id; фильтры operation/http_status/date_from/date_to/order_id/certification_run_id, прежние supplier/result/correlation сохранены |
| GET integration-incidents/ | Технический журнал требует integrations.manage; supplier UUID валидируется |
| GET hbpro-certification-runs/?supplier=UUID | До 100 последних tenant-scoped summaries; integrations.manage |
| GET hbpro-certification-runs/{uuid}/export/ | ZIP, tenant scope, integrations.manage, audit, Cache-Control:no-store |
| POST orders/{uuid}/services/ | Optional HB-only room_assignments, main_guest, special_requests; прежние запросы совместимы |
| PUT services/{uuid}/passengers/ | Те же HB options и возврат room_ref; изменение после supplier submission заблокировано |
| Booking workflow responses | Additive supplier_confirmation: normalized_status, supplier_statuses, updated_at |
| POST suppliers/{uuid}/check-connection/ | Свежая HB авторизация, environment/time/latency; проверяется выбранная HB environment |

Команды: `hbpro_credentials`, `hbpro_certification prepare|book|refresh|cancel|export`. Financial flags требуются только при последующем сознательном запуске book/cancel владельцем; ни один такой live-вызов здесь не выполнялся.

## 5. FRONTEND CHANGES

- Существующие Room/Pax/Group StackPanel остаются боковыми панелями, архитектура и дизайн не заменены.
- HB условия: confirmation mode, стандартное время заезда/выезда, дополнительная информация по комнатам; ранее показываемые питание/цены/валюты/штрафы/важная информация сохранены.
- Явное распределение гостей по фактическим комнатам тарифа, основной гость; групповой flow учитывает supplier room quantities. Другой состав требует нового поиска.
- Пожелания отображаются/отправляются только при isRemarkFreeFormAllowed, до 2000 символов.
- Supplier: отдельные вкладки «API-логи» и «Ошибки», фильтры и детали, connection environment/time/latency. Остальные вкладки сохранены.
- Кнопка «Обновить статус у поставщика» и видимое ожидание supplier confirmation.

## 6. TEST RESULTS

| Проверка | Результат |
|---|---|
| Полный backend с текущим локальным .env | 575 passed, 22 failed |
| Те же проблемные tests на чистом исходном HEAD | Те же 22 failures воспроизведены: 21 CORS при CORS_ALLOW_ALL_ORIGINS=False, 1 avatar DB connection closed |
| Полный backend с CORS_ALLOW_ALL_ORIGINS=True только для процесса тестов | 596 passed, 1 failed (тот же baseline avatar) |
| Финальный весь backend, тестовый CORS, явный deselect baseline avatar | 600 passed, 1 deselected; добавлены ещё 4 предметных теста |
| Финальная регрессия integrations/booking/services/suppliers/orders/groups_app | 158 passed |
| Последний HBPRO-only после изменений exporter/parser | 95 passed |
| Frontend npm test (также prebuild) | 381 passed, 0 failed |
| npm run build | PASS: compile, встроенная lint/type validation, static generation |
| npm run check:architecture | PASS: 1600 imports, 64 documented legacy edges, 2 retained cycles |
| Ruff по всем изменённым Python-файлам | PASS |
| Ruff по всему backend | 15 существующих ошибок; те же 15 на чистом HEAD |
| Python compileall | PASS |
| Django check | PASS, 0 issues |
| migrate --check / makemigrations --check --dry-run | PASS на тестовой PostgreSQL, No changes detected |
| git diff --check (оба repo) | PASS |
| Compose | YAML/anchors/services/shared HBPRO_ENVIRONMENT PASS; Docker CLI/daemon отсутствуют, runtime не проверен |

Отдельные scripts lint/typecheck в frontend package.json не определены; использованы встроенные проверки production build. Автоматические tests не заменяют финансовые сценарии в реальном HBPRO. Production CORS не менялся ради тестов.

## 7. SECURITY CHECK

PASS: credentials остаются на backend в encrypted SupplierCredential/server env; read serializers не возвращают password. Authorization/JWT/login/password/API keys/contact/guest PII маскируются в журналах и экспортируемых JSON. Исходные ошибки сохраняются в очищенном виде с нормализованным кодом; произвольный provider errorCode не попадает текстом в пользовательскую ошибку. FreeRemark не логируется открыто.

PASS: журналы/export требуют integrations.manage; tests проверяют отказ оператору, tenant isolation и 404 чужого run. Certification CLI проверяет actor permission/tenant; создание брони использует обычный order-access/preflight и защищено от повторного запуска. Участники должны принадлежать текущему заказу.

PASS: timeout/retry/search/poll limits, committed operation guards, cross-worker lease, inputs/guests/room allocations validation. Terminal cancelled требует всех items CANCELED. Нет blind retry финансовых мутаций.

PASS: git diff и новые текстовые файлы проверены на значения секретов из локальных env; совпадений не обнаружено. PDF и XLSX побайтно совпадают с исходным архивом. Экспорт не отправляется внешним адресатам; файл CLI создаётся с правами 0600 и без перезаписи существующего пути.

## 8. HBPRO CERTIFICATION READINESS

Статусы относятся к коду/доступным проверкам. Реальное прохождение сертификации не заявляется.

| Требование задачи | Статус | Основание / остаток |
|---|---|---|
| 1. Audit | PASS | Исходная таблица и все материалы изучены |
| 2. Full booking flow | PASS | Реальные documented methods, staged HTTP-fixture flow |
| 3. ONLINE / ON_REQUEST | PASS | Исходный статус отдельно, pending → CONFIRMED tests |
| 4. Background sync | PASS | Queue/cron, limits, dedup/backoff/terminal stop/exhaustion |
| 5. API logs | PASS | Sanitized bodies/metadata/IDs/correlation/order/run |
| 6. Supplier logs/errors UI | PASS | Вкладки, фильтры, детали |
| 7. Errors | PASS | Auth/timeout/validation/unavailable/price/expired/rejected/cancel/unknown |
| 8. Room/rate drawers | PASS | Существующие панели, только возвращённые supplier условия |
| 9. Guests | PASS | Validation, gender/citizenship/DOB/main/room assignments |
| 10. Multi-room | PASS | Два двухместных номера в одном order/book; item IDs отдельно |
| 11. Certification mode | PASS | Sandbox-only staged admin command + run ID |
| 12. Certification export | PASS | Принятый PDF JSON format, имена по PDF, все страницы и повторы |
| 13. Summary / Excel | PASS | Evidence-derived summary, полная manifest, копия original template |
| 14. Environment | PASS | sandbox/production, один URL, encrypted import |
| 15. Connection check | PASS | Fresh login, selected environment, timestamp/latency/error |
| 16. Tests / other suppliers | PASS | 95 HBPRO, 158 affected, 600 общий без baseline avatar |
| 17. Security | PASS | Redaction/RBAC/tenant/IDOR/validation/limits tests |
| 18. Existing features | PASS | Старые suppliers/пять тестовых отелей/вкладки/flow сохранены |
| 19. Migrations | PASS | 0003/0004, applied/check на тестовой БД |
| 20. Final verification | PARTIAL | Baseline avatar + 15 baseline lint issues; Docker runtime недоступен |
| Реальные сценарии HBPRO и production approval | PARTIAL | Только владелец запускает после согласования; HBPRO подтверждает и выдаёт доступ |

## 9. MANUAL STEPS FOR OWNER

1. Узнать/обеспечить static outbound IP production server или общего NAT для backend web/jobs/cron.
2. Передать этот IP HBPRO.
3. Дать HBPRO тестовый URL/login/password.
4. После своего подтверждения запустить реальные certification scenarios по RUNBOOK; дождаться supplier confirmation ON_REQUEST.
5. Проверить экспортированные JSON/summary/Excel, реальные IDs и штрафы/отмену.
6. Отправить certification package HBPRO.
7. Дождаться подтверждения сертификации.
8. Получить production credentials либо подтверждение перевода существующих в боевые.
9. Добавить production credentials в server secrets/env и импортировать/проверить через hbpro_credentials в production environment.
10. Переключить HBPRO_ENVIRONMENT=production у backend web/jobs/cron и выполнить обычный deployment/restart. API URL остаётся прежним.

Команды, права и примеры находятся в HBPRO_CERTIFICATION_RUNBOOK.md. Стандартный deploy уже выполняет миграции; отдельной ручной разработки базового booking flow не требуется.

## 10. BLOCKERS

Кодовых блокеров Hotelbook, выявленных доступными проверками, нет. Реальная сертификация и production credentials зависят от владельца/HBPRO.

Проверяемые ограничения среды: Docker runtime не проверен из-за отсутствия Docker; полный общий CI остаётся с воспроизводимым baseline avatar failure и 15 существующими lint errors. Они находятся за пределами Hotelbook и отдельно зафиксированы, а не скрыты под общим PASS.
