# A&A Integration Report

## 1. Итог

**Partially implemented.** Код integration flow реализован в существующей архитектуре CRM и покрыт автоматическими тестами. Обязательная живая приёмка полного sandbox flow **заблокирована авторизацией**: указанный шлюз возвращает HTTP 401 / Authorization error. Нельзя заявлять production readiness или успешные реальные booking/cancel.

Контракт: [официальная документация Roomlink](https://roomlink.ru/documentation_api), опубликованный там [API 4.0.1](https://disk.360.yandex.ru/i/DSHUMf72OZqdfQ). Используется XML2 form POST `RequestName=…&XML=…`, UTF-8 и multi-rate `IsMultiRate=1`. В примерах PDF есть опечатки XML; реализация опирается на таблицы параметров и использует XML builder.

## 2. Изменённые файлы

| Файл | Изменение и назначение |
|---|---|
| `integrations/adapters.py` | Lazy registry, adapter capabilities, точное tenant/supplier/provider/environment routing. Mock запрещён при отключённом mock mode. |
| `integrations/hotelbook.py` | Capabilities существующего HB; его HTTP-контракт сохранён. |
| `integrations/models.py` | Общая модель SupplierOperationLease; старое Python-имя оставлено совместимым alias. |
| `integrations/hotelbook_lock.py` | Совместимый импорт общего lease. |
| `integrations/scheduled_tasks.py` | Provider-neutral polling с backoff и подавлением дубликатов jobs. Старое cron-имя совместимо. |
| `integrations/job_handlers.py` | Общий status sync, короткая транзакция сохранения, общий incident flow. |
| `suppliers/job_handlers.py` | Проверка подключения установленным адаптером, фильтрация tenant и окружения. |
| `services/job_handlers.py` | Опрос credentials подходящего окружения, supplier/provider/external_key dedup, pin provider в snapshot; устранён cross-tenant priority lookup. |
| `services/revalidation.py` | Общая обработка booking namespace и сохранение свежего reservation под row lock после preflight I/O. |
| `services/views.py` | Общие booking options/room assignments, provider cancellation и блокировка ручной подмены статуса API-услуги; A&A city suggestions. HB-only content endpoints остаются отдельными. |
| `booking/preflight.py` | Capability-based blocking; A&A не требует паспорт, остальные адаптеры сохраняют прежнее требование. |
| `booking/job_handlers.py` | Общий locator recovery, обработка отказа и pending, защита сохранённого snapshot от устаревшего inquiry. |
| `booking/views.py` | Общая выдача supplier confirmation для выбранного adapter. |
| `config/settings/base.py`, `.env.example` | AANDA_ENVIRONMENT и явно настраиваемый production URL без придуманного default; credentials хранятся отдельно в encrypted field. |
| `pyproject.toml`, `uv.lock` | Фиксированная dependency `defusedxml` для безопасного XML parsing. |

## 3. Новые файлы

| Файл | Назначение |
|---|---|
| `integrations/aanda.py` | Credentials, city cache, search, normalization, pricing, signed 30-second baseline, multi-room OrderRequest, recovery/retrieve/cancel и явные unsupported operations. |
| `integrations/aanda_xml.py` | Один HTTP POST без redirect/retry, connect/read timeouts 10/60 s, bounded response, XML parser, централизованные mappings и allowlisted IntegrationLog. |
| `integrations/supplier_lock.py` | Общий durable lease на OrderService, записанный до внешнего I/O. |
| `integrations/migrations/0006_supplier_operation_lease.py` | State-only обобщение модели lease с сохранением физической таблицы. |
| `integrations/management/commands/configure_aanda.py` | Интерактивный ввод credentials без пароля в argv/history. |
| `integrations/management/commands/aanda_sandbox_check.py` | Живые read-only credentials/search/pricing; нет booking/cancel. |
| `integrations/tests/test_aanda.py` | XML transport fixtures и полный CRM pipeline. |
| `integrations/tests/test_aanda_safety.py` | Таймауты, concurrency, stale snapshots, TTL, security, multi-provider/multi-room. |
| `docs/aanda/local-check.md` | Команды и последовательность самостоятельной проверки. |
| `docs/aanda/integration-report.md` | Этот отчёт. |

## 4. Architecture changes

Обобщены registry, dedup, credential routing, preflight blocking, room assignment, API lifecycle guards, durable operation lease, recovery locator, supplier confirmation и polling. Supplier dictionaries/IDs остаются внутри соответствующего provider namespace/cache. OrderService, ServiceOffer, BookingWorkflow, pricing/markups, IntegrationLog и IntegrationIncident используются существующие.

Жизненный цикл: SearchSession → supplier priority → supplier offer → OrderService → preflight/Pricing → подписанный detailed snapshot → один OrderRequest → OrderInfo → разрешённый cancel. После истечения TTL повторяется Pricing с полным сравнением условий. Свежая валидная подпись позволяет worker не делать второй Pricing.

Реальные отличия price/currency, штрафа/deadline/info, rate/payment/meal, availability и room composition не игнорируются. Критически исчезнувшие hotel/product/rate блокируют booking. Один заказ содержит комнаты одного A&A hotel, currency и payment mode. Комбинации ограничены 4096 на hotel: при превышении требуется уточнить поиск, неправильный заказ не создаётся.

Hotelbook-specific части, оставленные намеренно: HB gateway protocol/auth, dictionaries/test hotels, certification, item status normalization и HB hotel content/certificate. Физическое историческое имя таблицы lease сохранено только ради совместимости развёртывания; A&A использует общую модель, а не HB-specific класс.

## 5. Реализованные A&A методы

«Реализовано» в таблице означает код + fixtures/integration tests, **не успешный живой sandbox вызов**.

| Operation | A&A request | Status |
|---|---|---|
| Credentials check | CurrencyListRequest | Реализовано; live AUTH_ERROR |
| Location dictionaries | CountryListRequest / CityListRequest | Реализовано, supplier cache 24 h |
| Search | HotelSearchRequest, IsMultiRate=1 | Реализовано |
| Pricing | HotelPricingRequest, IsMultiRate=1 | Реализовано |
| Fare rules | Данные Search/Pricing | Реальные penalty/deadline/info/payment; без выдуманных refundable flags |
| Booking | OrderRequest | Реализовано, durable ReferenceNumber |
| Multi-room | OrderRequest / AccommodationList | Реализовано; отдельное размещение NumberOfRooms=1 для каждой комнаты |
| OrderRequestMulti | Альтернативный протокол | Не вызывается: выбран документированный AccommodationList |
| Retrieve / recovery | OrderInfoRequest, Id либо ReferenceNumber | Реализовано |
| Cancel | Свежий OrderInfoRequest → OrderRequest с ToBeCancelled=1, ToBeChanged=1 → OrderInfoRequest | Реализовано; проверяются статус, AllowCancel, фактический/возможный штраф |
| Separate refund | Не документирован | NOT_SUPPORTED; fake refund отсутствует |
| Ticket issue | Не относится к hotel API | NOT_SUPPORTED |
| Children / ages | Поля возраста не найдены в прочитанном XML2 contract | NOT_SUPPORTED; возраст не теряется и взрослый тариф не подставляется |

## 6. Error mapping

| Supplier code / событие | CRM code |
|---|---|
| 1, 20; HTTP 401/403 | AUTH_ERROR |
| 2, 3, 4, 10, 12 | SEARCH_CRITERIA_INVALID |
| 6, 9, 49 | RATE_UNAVAILABLE |
| 7 | CANCELLATION_FAILED |
| 8 | BOOKING_NOT_FOUND |
| 5, 11, 13–19, 21, 50 | INVALID_REQUEST |
| Неизвестный numeric error | PROVIDER_ERROR + отдельный raw code |
| Timeout read-like operation | TIMEOUT, retry_safe=True |
| Connection failure / 5xx read-like | SUPPLIER_UNAVAILABLE, retry_safe=True |
| Empty/malformed/unexpected XML | INVALID_RESPONSE; mutation становится unknown |
| Неоднозначный booking timeout/reset/5xx/redirect/invalid response | BOOKING_UNKNOWN, retry_safe=False |
| Неоднозначная cancel mutation | CANCELLATION_UNKNOWN, retry_safe=False |
| Price mismatch перед mutation | PRICE_CHANGED |
| Реальные изменения terms после Pricing | terms_changed=True / подтверждение RATE_CHANGED; worker блокирует неподтверждённые изменения |
| Изменённый supplier/даты | AVAILABILITY_CONFLICT |
| Refused/cancelled creation result | BOOKING_REJECTED |

Транспорт не делает автоматических повторов. Финансовые jobs не retryable. После ambiguous booking повтор OrderRequest запрещён; inquiry использует уже сохранённый ReferenceNumber. После ambiguous cancel разрешён inquiry, но не повторная mutation. Известный отказ отмены можно повторить только новым действием после свежего OrderInfo.

## 7. Status mapping

| A&A raw code | Нормализованный adapter status / CRM |
|---|---|
| 11, 18, 20, 21, 47; неизвестный status | pending → service approval, workflow item unknown |
| 25 | failed → service/item failed, incident |
| 35, 65 | booked, только если размещения также подтверждены |
| 71 | cancelled, только если размещения также отменены |

Multi-rate Availability: `1 → available`, `0 → on_request`, прочее → unknown с блокировкой booking при Pricing. Legacy `2/4` не трактуются как multi-rate availability. Raw codes сохраняются; успешный OrderResponse/Success Id сам по себе не означает подтверждение.

## 8. Tests

| Команда / набор | Passed | Failed | Skipped |
|---|---:|---:|---:|
| `.venv/bin/pytest integrations booking services suppliers --disable-warnings` (финальная версия) | 253 | 0 | 0 |
| `CORS_ALLOW_ALL_ORIGINS=true .venv/bin/pytest --disable-warnings` (финальная версия) | 730 | 1 | 0 |
| Новые A&A tests в этих прогонах: `test_aanda.py` + `test_aanda_safety.py` | 79 | 0 | 0 |
| HB regression subset в этих прогонах: hotelbook + certification + env | 142 | 0 | 0 |

`.venv/bin/python manage.py check` — без ошибок; `.venv/bin/python manage.py makemigrations --check --dry-run` — No changes detected; `git diff --check` — без ошибок. Ruff по всем изменённым/новым Python files — All checks passed. Оба pytest-прогона имеют одно предупреждение, без пропусков и отключения tests.

Полный первый запуск без изменения локального env: 679 passed / 22 failed. Из них 21 CORS failure из-за локального CORS allowlist, 1 avatar failure. CORS_ALLOW_ALL_ORIGINS=true задаётся только для тестового процесса. В изолированной копии исходного `e4fedf8` тот же avatar test воспроизведён: 23 passed / 1 failed для CORS + avatar. Assertions существующих tests не менялись.

Repository-wide Ruff: 15 существующих findings; исходный `e4fedf8` даёт те же 15. Все изменённые и новые Python files проходят Ruff. Несвязанные CRM/document/TTBooking lint issues не исправлялись в этой задаче.

## 9. Sandbox test

| Шаг | Фактический результат |
|---|---|
| Authentication | FAIL: HTTP 401; отдельная безопасная диагностика XML дала error code 1 |
| Search | NOT RUN: нет успешной авторизации |
| Pricing | NOT RUN |
| Booking | NOT RUN |
| Retrieve | NOT RUN |
| Cancel | NOT RUN |
| Final supplier order status | Не получен; supplier order не создавался |

Использованы credentials, введённые пользователем через getpass. Их значение не приводится. BuyerId/UserId проверены на соответствие переданным идентификаторам. Проверка подключения не создаёт бронь. Проверка штатной read-only командой вернула AUTH_ERROR; дополнительный прямой form POST из примера PDF также дал HTTP 401 / code 1. Credential помечен failed. Реальные HBPRO booking/cancel не выполнялись.

## 10. Реальные полученные supplier IDs

A&A HotelCode, ProductCode, RateID, OrderId, ReferenceNumber и статусы из успешного живого flow **не получены**. ID `453/13450/1/999` в fixtures — тестовые данные контракта, не результаты sandbox.

Настроенный пользователем **CRM Supplier ID**: `7fead928-56fe-478e-b949-48cc653b8bd1`. Это не A&A OrderId.

## 11. Security verification

| Проверка | Результат |
|---|---|
| Password committed to git | NO; commit/push не выполнялся, фактический пароль отсутствует в изменённых файлах |
| Password in logs | NO; проверены реальные A&A IntegrationLog и fixtures |
| PII in IntegrationLog | NO; whitelist summaries, проверено tests |
| XXE allowed | NO; DTD/entities/external entities отвергаются |
| Automatic retry of ambiguous booking | NO; проверены timeout/reset/5xx/malformed/redirect и повтор worker |
| Cross-tenant credential access | NO; tenant/supplier/provider/environment isolation tests |
| Реальный credential в БД | Encrypted (`enc$1$`), проверено непосредственно в storage |

Пароли хранятся EncryptedTextField; GET credentials использует write_only secrets. В IntegrationLog пишутся только allowlisted summaries/technical IDs/status/numeric error; исходные XML, ошибки с echo, имена и паспорта не сохраняются. Поставщик и tenant проверяются в config/routing/retrieve. Sandbox endpoint фиксирован; production URL задаётся явно. DTD/entities/external entities запрещены defusedxml; redirects и автоматические mutation retries отсутствуют.

Reservation сохраняется до POST. Общий lease и row lock защищают concurrent workers. Stale preflight сохраняет свежий booking marker; stale inquiry не может затереть более поздний cancel marker. При расхождении OrderInfo Price с Pricing сохраняются реальный заказ и actual_price и открывается PRICE_CHANGED incident; автоматического второго booking или cancel нет.

## 12. Regression

142 tests в `integrations/tests/test_hotelbook.py` (95), `test_hbpro_certification.py` (38), `test_hotelbook_env.py` (9) прошли в финальных прогонах. Общий связанный набор: 253 passed / 0 failed.

Проверяются старые HB search/revalidation/booking/retrieve/cancel/certification/credential tests. Отдельные multi-provider tests проверяют одинаковые offers, отказ одного supplier, сохранение обоих результатов и отправку booking только выбранному adapter. Ни production credentials, ни HB-specific protocol не подменяются.

## 13. Database migrations

`integrations.0006_supplier_operation_lease`: SeparateDatabaseAndState, RenameModel и явный прежний db_table. SQL — no-op; существующие lease rows и физическое имя таблицы сохраняются. Обратное применение также не меняет таблицу. Дополнительной A&A booking/error таблицы нет.

Миграция проверена на новой pytest PostgreSQL DB. На локальной БД проверено применение `0006 → rollback 0005 → 0006`, все шаги OK. Ранее ожидавшая применения `0005_shared_cache` также применена перед `0006`. `sqlmigrate integrations 0006` подтверждает no-op; `makemigrations --check` не требует новых migrations.

## 14. Git diff summary

`27 files changed, 3272 insertions(+), 188 deletions(-)`. Полный patch предоставлен отдельно: `../aanda-integration.diff` (в корне workspace crm).

Ключевые изменения: XML2 adapter и безопасный transport; supplier-neutral routing/dedup/preflight/locking/polling; защита durable mutation state; regression/safety tests; интерактивный setup и read-only smoke command. Изменения находятся в рабочей копии; новый commit/push в рамках этой задачи не выполнялся.

## 15. Что осталось до production

- [ ] Исправить/подтвердить XML credentials и допуск тестового аккаунта; повторить read-only check.
- [ ] На разрешённом тестовом аккаунте выполнить реальные Search → Pricing → Booking → OrderInfo → Cancel → OrderInfo, записать технические IDs и финальный статус.
- [ ] Подтвердить тестовый режим указанного gateway; не делать production mutations ради smoke.
- [ ] Получить/настроить согласованный production endpoint и отдельные production credentials.
- [ ] Согласовать WhereToPay, ON_REQUEST policy, детские размещения, ограничения групп/квот и rate limits.
- [ ] Проверить живые суммы/commission, сроки в timezone отеля и cancel penalty; отдельно несколько комнат.
- [ ] Подтвердить cancel protocol и гостевой контекст на реальном аккаунте, затем сделать ограниченный production smoke по отдельному допуску.
- [ ] Разобрать исходный PostgreSQL avatar-test failure и 15 исходных Ruff findings отдельной задачей.

У API OrderRequest нет документированного expected-price constraint; Pricing снижает риск изменения цены перед booking, но не создаёт атомарную гарантию цены у поставщика. Обнаруженное расхождение OrderInfo поднимает incident и требует оператора. Полная живая приёмка до устранения AUTH_ERROR остаётся незавершённой.
