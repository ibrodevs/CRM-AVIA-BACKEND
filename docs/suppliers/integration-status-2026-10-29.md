> Обновлено 30.10.2026: актуальные протоколы, live-проверки и следующие действия — [в новом отчёте](integration-status-2026-10-30.md). Статусы TTBooking/i’way ниже отражают состояние до ответов поддержки.

# Интеграции поставщиков CRM-AVIA — 29.10.2026

## Как читать готовность

Наличие адаптера и прохождение тестов не являются допуском к production. В этом изменении реальные бронирования, оплаты, выписки, отмены и сообщения поставщикам не выполнялись. Из исходного файла APIs/APIs.md пароли и ключи не копируются в документацию или исходники.

| Поставщик | Что реализовано | Что ещё не завершено |
|---|---|---|
| HBPRO / Hotelbook | Существующий полный гостиничный цикл, кеши/справочники, сверка, post-sale | Подтверждение production-допуска HBPRO; этот change не повторяет живую сертификацию |
| A&A / Roomlink | Существующий полный гостиничный цикл и post-sale | Живой цикл до confirmed и cancelled: последний отчёт 28.10 дошёл до pending, AllowCancel=Нет |
| TTBooking | Документированный async SEARCHFLIGHTS/SEARCHRESULT, SELECTFLIGHT/SELECTRESULT, SELECT_UPT, CREATEBOOKING/CREATERESULT, DISPLAYSTATUS, CANCELBOOKING; сохранение токенов и защита от повторной отправки | Бронь требует подтверждённой таблицы статусов. Автоматическая оплата/выписка заблокирована до подтверждения команд и схемы DISPLAYTICKETS; возвраты не реализованы |
| Ostrovok / ETG | ETG v3: локации, SERP/HP, prebook, form/finish/finish-status, order/info, cancel; cardless-тарифы, распределение по номерам, сохранение пожеланий | Живой цикл и certification; карточные платежи не поддерживаются; для полноценных названий всех результатов SERP нужна загрузка статического контента ETG. Карточка выбранного отеля загружается при revalidate с кешем 24 ч |
| i’way v4 | Авторизация с общим кешем, цены по координатам, revalidate, check-time, создание, статус, явное подтверждение оплаты, отмена без штрафа | Персональный sandbox и приёмка; детские кресла/допуслуги; автоматическая отмена со штрафом. Обратная поездка оформляется отдельно |
| Onelya / ИМ | Изолированный адаптер, Basic/POS, предварительный V1 TrainPricing и проверка справочного доступа | Актуальная документация недоступна: вход вернул «Доступ запрещён». Выбор вагонов/мест, резервирование, выписка, получение заказа и отмена заблокированы. Поиск по старому SDK не равен подтверждённой продаже |

## Что запросить и подготовить для каждого

### HBPRO

- Подтверждение прохождения сертификации и допуска нашей организации к production.
- Боевые API-реквизиты, разрешённые методы/лимиты и ограничения договора.
- Подтверждение финансовых условий/депозита и согласованный приёмочный заказ.
- Существующий кеш словарей 24 часа, shared cache и отсутствие повторных login не изменяются.

### A&A

- Подтверждение сценария, в котором тестовая бронь переходит из pending в подтверждённую.
- Тестовый отель/тариф/даты, для которых разрешена отмена; ожидаемые OrderInfo и AllowCancel.
- После полного цикла — production endpoint, реквизиты и договорные лимиты.
- Свежий отчёт: `docs/aanda/live-sandbox-acceptance-2026-10-28.md`; не использовать более старую AUTH_ERROR как текущее состояние.

### TTBooking — сообщение технической поддержке

> Просим подтвердить актуальный контракт stdCBS async для нашей agency/user: SEARCHFLIGHTS → SEARCHRESULT → SELECTFLIGHT → SELECTRESULT → CREATEBOOKING → CREATERESULT → DISPLAYSTATUS. Нужны полные обезличенные примеры успешных и ошибочных ответов, точное расположение и значения статусов (pending/booked/issued/cancelled/failed), локатор/PNR и time limit. Уточните, какая последовательность SELECTFOP / ISSUETICKETS / PAYDEPOSIT требуется по нашему договору; пришлите DISPLAYTICKETS с номерами билетов, перевозчиком, пассажирами, сегментами и документами. Нужны правила повторной отправки, поиск заказа при потере токена CREATEBOOKING, возвраты/аннуляции, sandbox-приёмка и условия production-допуска.

- `status_map` — объект «фактический код поставщика → нормализованный статус»; не заполнять предположениями.
- Нужны API agency ID, user ID и Password Key из личного кабинета. Логин и пароль веб-консоли не являются автоматически этими API-реквизитами.
- `check-connection` проверяет конфигурацию и возвращает `configured`; успешная конфигурация не выдаётся за живое подключение.
- Живая read-only проверка 29.10: вход в консоль и получение API-реквизитов успешны. SEARCHFLIGHTS FRU → IST на дату +30 дней вернул штатное MESSAGE «Не найдено ни одного подходящего под заданные параметры варианта», без токена. Нужен подтверждённый поставщиком тестовый маршрут/даты с выдачей и доступным инвентарём. Это не AUTH_ERROR и не подтверждение полного поискового цикла.
- Успешный поиск/получение токена не подтверждает возможность выписки.
- Legacy SEARCHFR/SELECTFR оставлены для совместимости поиска; legacy-бронирование заблокировано.

### Ostrovok / ETG

- Партнёрские sandbox API key ID/key и подтверждение доступа B2B API v3.
- Разрешённые payment types, валюта, страна резидентства гостей, депозит/кредитный лимит. Карточные тарифы с CVC или card data не выбираются.
- Требования ETG к статическому контенту/dump и частоте обновления; согласовать отдельную задачу синхронизации каталога до production.
- Реальный IP пользователя оформления с доверенной конфигурацией reverse proxy. Сейчас используется REMOTE_ADDR; произвольный X-Forwarded-For не считается доверенным.
- Тестовые отели/даты, сценарии price change, pending, отказа, успешной брони и отмены, затем certification/production-допуск.
- Первый revalidate выбранного отеля может запросить `hotel/info`; далее используется supplier/tenant/environment/credential-scoped кеш 24 ч. Массовых запросов контента на каждый SERP нет; некешированные результаты пока показывают supplier hotel ID.

### i’way

- Персональные sandbox login/password/user_id и точный API_URL v4; публичные demo-реквизиты из инструкции в продукт не включены.
- Подтверждение включения internal_number и правил сверки по нему при потере ответа создания: документированный trips принимает transaction/order_id, но не internal_number. Без transaction повторное создание запрещено, нужна сверка у поставщика.
- Валюта, финансовые условия и разрешение orders/approve (это подтверждение оплаты, а не проверка соединения).
- Координаты точек, адреса и местное время подачи; номер рейса/поезда при подаче в аэропорт/на вокзал, время отправления при трансфере к рейсу/поезду.
- Контракт детских кресел/обязательных дополнительных услуг и точного штрафа для автоматизации этих сценариев.
- Приёмка search → check-time → create → trips → approve → cancel; затем боевые реквизиты/endpoint и лимиты.
- CRM не отправляет клиентские/административные ваучеры и письма автоматически: send_params выключены.

### Onelya / Инновационная мобильность

> Предоставленный доступ к ApiDocs возвращает «Доступ запрещён». Просим открыть актуальную документацию либо прислать текущий Swagger/OpenAPI: справочник станций, TrainPricing/CarPricing, схема вагонов/мест, Reservation, Confirm/выписка, получение заказа, отмена/возврат. Нужны персональные sandbox API login/password, POS, валюта и договорные права, точные статусы/ошибки, правила идемпотентности и сверки при таймаутах, тестовые маршруты и процедура сертификации.

Старый сторонний onelya_sdk 1.5.1 использован только для предварительного read-only контракта. По нему не реализуются финансовые мутации. Station IDs/коды нужны вместо произвольных названий городов. Минимальная цена TrainPricing помечена предварительной и не может пройти revalidate для продажи.

## Настройка CRM

1. Развернуть backend/frontend совместно; запустить обычные workers и scheduler.
2. В «Поставщики → Добавить» выбрать реальный адаптер и подходящие виды услуг. Каталог доступен по `GET /api/v1/integration-providers/` с правами на поставщиков.
3. Ввести секреты через credentials API/UI. Они хранятся в зашифрованном SupplierCredential; общие настройки содержат только публичные данные.
4. Установить соответствующее окружение backend: HBPRO_ENVIRONMENT, AANDA_ENVIRONMENT, TTBOOKING_ENVIRONMENT, OSTROVOK_ENVIRONMENT, IWAY_ENVIRONMENT, ONELYA_ENVIRONMENT. По умолчанию sandbox. Production-доступ не выбирается автоматически по NODE_ENV frontend.
5. Выполнить безопасную проверку соединения/поиск; согласовать sandbox-приёмку с мутациями отдельно.
6. Не включать новые адаптеры в production-маршрутизацию до завершения перечисленных приёмок.

## Защита операций

- Общая supplier lease сериализует операции на услуге. Locator и флаг отправки сохраняются до внешнего изменения.
- Таймаут/5xx/redirect/невалидный JSON после мутации означают неизвестный результат; повтор не отправляется автоматически.
- Status inquiry — чтение; неизвестный supplier status остаётся pending.
- HTTP 200 без подтверждённого issued не создаёт билет. Оплата трансфера не создаёт AviaBooking.
- Цена/условия перед booking перепроверяются; короткий preflight подписывается HMAC и привязан к tenant/supplier/офферу/сроку.
- Credentials/Authorization/паспортные данные и raw ответы новых адаптеров не попадают в IntegrationLog. Записываются только операция, названия полей, HTTP status и безопасный код ошибки.
- Новые модели и миграции не добавляются.

## Источники контрактов

- ETG: https://docs.emergingtravel.com/ и официальный https://api.ratehawk.com/openapi.json ; примеры официальной Postman-коллекции docs-old.emergingtravel.com.
- TTBooking: авторизованная тестовая консоль https://dev.ttbooking.ru/module/avia-api и её клиентский код. Это не заменяет подтверждение полного ticketing-контракта поставщиком.
- i’way: предоставленная в APIs/APIs.md инструкция v4 (скачанный DOCX), не сторонняя документация TIBCO iWay и не старая v3-схема.
- Onelya: https://test.onelya.ru/ApiDocs/ (доступ не получен); предварительные read-only маршруты сверены со старым опубликованным SDK, актуальность требует поставщика.

## Проверки реализации

- Основной relevant backend suite: `orders booking services aftersales finance avia rail integrations documents` — 637 passed. Существующее предупреждение HBPRO CacheKeyWarning; ошибок нет.
- После уточнения обработки пустой выдачи TTBooking, запрета legacy-выписки и проверки кеша ETG повторный targeted suite — 63 passed. Эти тесты пересекаются с основным набором; числа не суммируются.
- Frontend: 428 tests passed, `npm run build` успешно.
- `python manage.py check`: ошибок нет.
- `python manage.py makemigrations --check --dry-run`: No changes detected.
- Ruff для новых адаптеров/каталога/тестов и проверка frontend architecture: успешно.
- Новые тесты используют изолированные HTTP-фикстуры контрактов, не mock-адаптеры в production-конфигурации. Живой read-only TTBooking тест описан отдельно выше.

## Основные изменённые файлы

Backend: `integrations/json_provider.py`, `integrations/ostrovok.py`, `integrations/iway.py`, `integrations/onelya.py`, `integrations/ttbooking_async.py`, `integrations/ttbooking.py`, `integrations/provider_catalog.py`, `integrations/adapters.py`, `integrations/urls.py`, `integrations/hotel_actions.py`, `booking/job_handlers.py`, `services/views.py`, `services/hotel_actions/presentation.py`, `suppliers/job_handlers.py`, `suppliers/views.py`, `config/settings/base.py`, `.env.example`.

Backend tests: `integrations/tests/test_json_suppliers.py`, `integrations/tests/test_ttbooking.py`, `booking/tests/test_supplier_results.py`.

Frontend: `src/modules/suppliers/model/provider-setup.js`, `src/modules/suppliers/ui/ProviderSetupFields.jsx`, `src/modules/suppliers/ui/SuppliersPage.jsx`, `src/modules/suppliers/api/suppliersApi.js`, `src/modules/services/hotels/search-criteria.js`, `src/modules/services/free-booking-search.js`, `src/modules/services/ui/ServicesPage.jsx`, `test/provider-setup.test.mjs`.
