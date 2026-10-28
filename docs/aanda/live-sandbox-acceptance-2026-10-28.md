# A&A Live Sandbox Acceptance Report

Дата: 28 октября 2026. Последний реальный OrderInfo: **28.10.2026 14:40:20 +06**.

**Приёмка НЕ ЗАВЕРШЕНА.** Search, Pricing и отправка одного заказа подтверждены живыми ответами A&A. Поставщик сохраняет заказ в статусе **11 — «Запрос получен»**, normalized **pending**; CRM service — **approval**. **AllowCancel = Нет**. Подтверждение брони и фактическая аннуляция пока не проверены. Статус cancelled не выставлялся вручную.

## Environment

| Поле | Значение |
|---|---|
| Supplier | A&A / Roomlink, Travel Hub |
| Adapter | aanda |
| Environment | sandbox |
| Gateway | https://go-api.aanda.ru/ |
| AUTH | OK, CurrencyListRequest, HTTP 200 |
| Credentials | Существующий SupplierCredential 1c7ac67d-bd2d-4e1e-bed8-4fffc4734c2c, active; EncryptedTextField |
| Test account | До OrderRequest пользователь подтвердил, что A&A обозначили HMEL2024 / hmel как тестовый аккаунт |

Адрес gateway сам по себе не доказывает тестовый режим: официальная спецификация описывает его как общий XML2 endpoint. Проверены настройки sandbox, соответствующий credential, адаптер и точный HTTPS URL перед каждым внешним mutation request. Credentials не брались из .env или исходного кода.

## Search

| Поле | Результат |
|---|---|
| Status | OK, реальные CountryListRequest → CityListRequest → HotelSearchRequest |
| City | Самара, реальный City Code=18, Country Code=9 |
| Check-in / Check-out | 12.12.2026 / 14.12.2026 |
| Rooms / Adults / Children | 1 / 2 / 0 |
| Offers received | Самара: 803, из них 749 available; Москва: 8187 |
| Focused hotel search | 4 предложения отеля «Пастель» |
| UI search | completed, 803 предложения, provider run только aanda |

HotelCode получен из живого поиска; fixtures не использовались как доказательство API. Предложения сохранены стандартной функцией CRM `_persist_offer` в ServiceOffer с supplier, provider_adapter, external_key, itinerary, fare, price, availability и raw_snapshot. Для UI выполнено реально созданное интерфейсом задание services.search через штатный обработчик; глобальная очередь чужих заданий не запускалась.

Первый UI-поиск свободным текстом «Самара» загрузил 245 стран и завершился SEARCH_CRITERIA_INVALID из-за неоднозначного выбора города. Исправление: короткие подсказки со страной, отдельные country caches, передача конкретного city_code и supplier/provider selection. После выбора «Самара — A&A / Roomlink · Россия» повторный UI-поиск успешен. В локальный SupplierSearchPriority добавлен A&A; существующий Hotelbook сохранён. Выбранный A&A город не направляется в Hotelbook.

## Selected offer

| Поле | Значение |
|---|---|
| Hotel | Пастель |
| HotelCode | 71359 |
| Room | стандарт с двуспальной кроватью |
| ProductCode | 217912 |
| RateID | 1 |
| Meal | Code=0, питание не включено |
| Availability | Code=1, свободная продажа / available |
| Price / Currency | 5166 RUB |
| WhereToPay | Code=3, безналичная оплата |
| Standard check-in / check-out | 14:00 / 12:00 |

## Requested UI information

✅ в столбце UI означает визуальную проверку в локальной CRM или проверенную клиентскую форму; это не заявление об успешном финальном booking/cancel.

| Информация | A&A API отдаёт | CRM сохраняет | CRM отображает |
|---|---|---|---|
| Standard check-in time | ✅ Hotel/@CheckInTime, HotelPricingResponse/@CheckInTime | ✅ aanda.rooms, itinerary.check_in_time | ✅ 14:00 |
| Standard check-out time | ✅ Hotel/@CheckOutTime, HotelPricingResponse/@CheckOutTime | ✅ aanda.rooms, itinerary.check_out_time | ✅ 12:00 |
| Cancellation policy | ✅ Rate/@DeadlineDate, PenaltyInfo; Pricing DeadlineDate/DeadlineTime | ✅ fare.cancellation_details, room deadline/info | ✅ Дата, местное время, валюта и штраф |
| Penalty | ✅ Search PenaltySize; Pricing PenaltySize2Pax; OrderInfo Accommodation/@Penalty и PossiblePenaltySize | ✅ Search/Pricing snapshot и aanda_booking.items | ✅ Условный штраф и отдельный текущий штраф |
| Important information | ✅ NewsList/News и OrderInfo CheckinNotice | ✅ allowlist news / checkin_notice, fare.information | ✅ Новость о ресепшене; непустые правила заселения |
| Room additional info | ✅ Pricing HotelPricingDetail/RoomAmenities/RoomAmenity, RateDescription, RoomName, RoomSize | ✅ aanda.rooms и fare.rooms[].information | ✅ Удобства номера, тип кровати в фактическом названии/списке, описание тарифа |
| Special requests | ✅ OrderRequest Accommodation/@AdditionalInfo | ✅ существующее special_requests, сохранение через revalidation | ✅ Пожелание в карточке; поле ввода доступно для A&A |

Специальное поле `ImportantInfo` не выдумывалось. Реальные названия: `NewsTitle`, `NewsInfo`, `NewsBegin`, `NewsEnd`, `PenaltyInfo`, `RateDescription`, `SupplierInfo`, `Note`, `CheckinNotice`.

Фактическая важная новость: ресепшен «Пастель» работает с 09:00 до 17:00; перед заселением требуется связаться с администрацией. API вернул 11 удобств номера, включая душ, фен, телевизор, двуспальную кровать, шкаф, чайник и микроволновую печь. `PenaltyInfo` выбранного тарифа пуст: отдельный текст штрафа не придумывался. Дополнительное питание с Meal Code=2 больше не показывается как включённое в цену. WhereToPay Code=3 не интерпретируется как доказанная предоплата: показываются условия тарифа и фактическое название способа оплаты.

Пожелания поддерживаются официальной спецификацией 4.0.1, §5.19, PDF страница 72 / печатная 71: `Accommodation/@AdditionalInfo`. Использована существующая цепочка CRM API → service provider snapshot → BookingWorkflow → AandaAdapter → XML. Пожелание сохранено после Pricing и фактически возвращено A&A в OrderInfo (`special_request_echoed=true`). Оно не попало в IntegrationLog.

## Pricing

| Сравнение | Search | Pricing |
|---|---|---|
| Price | 5166 | 5166 |
| Currency | RUB | RUB |
| Room | ProductCode 217912 | 217912 |
| Rate | 1 | 1 |
| Meal | Code=0 | Code=0 |
| Availability | available | available |
| Penalty | 2583 RUB | 2583 RUB |
| Deadline | 11.12.2026 12:00 UTC+4 | совпадает |
| WhereToPay | 3 | 3 |

Initial terms changed: **YES** — Pricing добавил ReservationLifetimeDate/Time (12.12.2026 12:00 UTC+4); также пришли удобства номера. Свежий Pricing принят как baseline, после чего повторный preflight перед единственным OrderRequest прошёл без warnings, price_changes и неподтверждённых изменений. Terms changed перед booking: **NO**.

## Booking

| Поле | Факт |
|---|---|
| OrderRequest | HTTP 200, A&A Success с Id; создание подтверждено, статус booked не подтверждён |
| A&A OrderId | 18919869 |
| ReferenceNumber | AA8410b1c35a720630144a1732859e01 |
| CRM order | ORD-000001 / e7b63731-0e88-437e-95c8-9e8c37ed1c04 |
| CRM service | e8a6e661-0217-40d5-8af9-85c8767a2c61 |
| BookingWorkflow | 72dcd852-8e39-435b-8dac-d95afe19a2c8 |
| Confirmation | Пусто — A&A ещё не предоставили номер подтверждения |
| Raw supplier status | 11 — Запрос получен |
| Normalized / CRM | pending / approval |
| Actual price | 5166 RUB |

Перед HTTP-вызовом guard проверил уже сохранённый в БД ReferenceNumber, состояние creating, tenant/supplier/adapter/environment и отсутствие предыдущего order_create для этого заказа. Выполнен повторный запуск того же обработчика: второй OrderRequest не появился. В CRM ровно одна связанная услуга и один workflow; в текущем acceptance отправлен ровно один запрос создания и получен один OrderId.

## OrderInfo

Повторные живые OrderInfoRequest: HTTP 200. Последняя сверка: **28.10.2026 14:40:20 +06**.

| Поле | Значение |
|---|---|
| Supplier status | 11 — Запрос получен |
| CRM normalized status | pending; service approval |
| AllowCancel | Нет (Code=2) |
| Current penalty | 0 RUB |
| PossiblePenaltySize | 2583 RUB |
| Deadline | 11.12.2026, 12:00 UTC+4 |
| Confirmation | Пусто |

В UI проверены supplier, статус ожидания, OrderId/reference, hotel, room, dates, стандартные часы, питание, цена, политика отмены, текущий штраф, важная новость, удобства и пожелание. Сохранение уже полученного confirmation при последующих пустых ответах покрыто regression test; реального confirmation этот заказ пока не получил.

## Cancellation

- Cancellation request: **NOT SENT / BLOCKED BY SUPPLIER STATE**.
- Последний OrderInfo: **OK**, но это не подтверждение аннуляции.
- Фактический последний supplier status: **11**, terminal status ещё не установлен.
- CRM status: **approval**, не cancelled.
- order_cancel: **0**. Отмена при AllowCancel=Нет не отправлялась.

Полный требуемый flow Booking → confirmed → Cancel → cancelled остаётся **НЕ ПРОЙДЕННЫМ**. Нельзя считать HTTP 200 подтверждением брони или объявлять отмену успешной без финального ответа поставщика.

## IntegrationLog

Search, Pricing, Booking, OrderInfo: **OK**. Cancellation log: **N/A**, запрос отмены не отправлен.

Проверены supplier/provider/environment/gateway, operation, HTTP result, duration, supplier status там, где он возвращён, technical IDs. Password leak: **NO**. Guest PII leak: **NO**. Полный auth XML не сохраняется. Проверка выполнялась в памяти по фактическому паролю и маркерам синтетических гостей; значения не выводились.

Безопасная детализация журналов и сравнения полей: [live-sandbox-evidence-2026-10-28.json](live-sandbox-evidence-2026-10-28.json).

## Tests

| Проверка | Результат |
|---|---|
| A&A tests | 92 passed / 0 failed |
| Hotelbook regression | 146 passed / 0 failed |
| Full backend suite | 743 passed / 1 failed |
| Frontend suite | 394 passed / 0 failed |
| Frontend production build | OK, в отдельной локальной build-копии |
| Django check | OK |
| makemigrations --check --dry-run | No changes detected |
| git diff --check, оба репозитория | OK |
| Ruff, файлы текущих изменений | OK |
| Ruff, весь backend | 15 нарушений вне изменённых файлов A&A flow |

Единственный full-suite failure: `accounts/tests/test_profile_settings.py::test_avatar_validates_bytes_and_is_private` — `django.db.utils.OperationalError: the connection is closed`. Такое же падение ранее зафиксировано в integration-report.md. Его не скрывали и не исправляли в рамках гостиничной интеграции. Также есть известный CacheKeyWarning у Hotelbook content cache.

Общий Ruff: нарушения в crm/company_finance_views.py, crm/person_document_ocr_view.py, receipt_* файлах документов и integrations/ttbooking.py; среди них два F821 в receipt_supplier_pdf_patch.py. Это отдельные от текущего flow проблемы.

## Changes made

Пути backend относительно CRM-AVIA-BACKEND; frontend — относительно CRM-Admin. Перечислены изменения этого acceptance, а не все ранее существовавшие незакоммиченные изменения проекта.

| Файл | Что исправлено и зачем |
|---|---|
| integrations/aanda.py | Реальные часы, NewsList, room amenities/size, payment/meal information; честное отображение optional meal; AdditionalInfo для пожеланий и их сохранение через revalidation; OrderInfo notes/notices/status name; сохранение confirmation; country-level caches и частичные country-labelled подсказки без ложного полного индекса |
| integrations/aanda_xml.py | Allowlisted технические City/CountryCode/Hotels в IntegrationLog без auth XML |
| services/job_handlers.py | Tenant/supplier/provider isolation при явном выборе города поставщика; исключён fallback в другой адаптер |
| services/views.py | Подсказки с city_code/country_code/supplier_id; безопасные hotel_details в сервисном API |
| services/hotel_presentation.py | Allowlist клиентских hotel/booking details без credential contents и гостевых данных |
| services/revalidation.py | После обновления offer обновляется persisted expires_at |
| orders/views.py | hotel_details в существующем order overview для карточки заказа |
| integrations/tests/test_aanda_acceptance.py | Регрессии часов/новостей/optional meals, пожеланий через API/workflow/XML, expiry, confirmation, city ambiguity и routing isolation |
| src/modules/services/hotels/HotelsPage.jsx | Передача выбранного A&A city code и scoped provider selection |
| src/modules/services/hotels/search-criteria.js | Проверка актуальности выбранной подсказки; provider-specific критерии без credential data |
| src/modules/services/hotels/presentation.js | Читаемая структурированная штрафная политика с местной timezone и подписью «Номер 1» |
| src/modules/services/hotels/aanda-details.js | Клиентское представление фактических A&A условий, статуса, IDs и notices |
| src/modules/services/hotels/AandaBookingDetails.jsx | Детали гостиницы в существующем стиле карточки |
| src/modules/services/index.js | Публичный export компонента с соблюдением архитектурных границ |
| src/modules/orders/ui/OrderCard.jsx | Отображение безопасных hotel_details |
| test/aanda-details.test.mjs | UI presentation и supplier namespace regression |
| test/hotel-presentation.test.mjs | Читаемые A&A deadline/penalty без технического room:0 |
| docs/aanda/live-sandbox-* | Этот отчёт и безопасные факты приёмки |

Локальная конфигурация: A&A включён в hotel search priority, Hotelbook сохранён. Пользователь установил пароль уже существующему локальному оператору через changepassword; роли и права не расширялись. Вход выполнен существующим логином hbpro-local-operator@localhost. Credentials A&A не менялись. Commit/push не выполнялись.

## Video readiness

**Готовы ли мы записать полное видео для A&A: NO.**

- [x] Search — реальный API и результаты UI.
- [x] Standard check-in/check-out — реальные часы сохранены и отображены.
- [x] Penalty policy — Search/Pricing/OrderInfo, валюта и timezone.
- [x] Important information — NewsList и notices.
- [x] Room additional information — реальные удобства и описание тарифа.
- [x] Special requests — документированный AdditionalInfo, реальная передача и echo.
- [x] Pricing — реальный revalidation, одинаковая цена, свежий preflight.
- [x] Booking request — один реальный OrderRequest и сохранённый OrderId.
- [ ] Order confirmation/status — pending, подтверждения отеля нет.
- [ ] Cancellation — поставщик пока запрещает.
- [ ] Final cancelled status — не получен.

Следующий шаг: A&A должны обработать **существующий заказ 18919869** и предоставить состояние, допускающее аннуляцию, либо аннулировать его на своей стороне. Затем повторить read-only OrderInfo; при AllowCancel=Да выполнить штатный cancel того же заказа и подтвердить конечный supplier status. Новый OrderRequest создавать не нужно. Для непрерывного поиска через UI необходим запущенный штатный worker; в этой приёмке исполнены только конкретные UI search jobs, без запуска чужой очереди.

Подготовленный текст для A&A (не отправлен):

> По тестовому аккаунту HMEL2024 / hmel создан один заказ 18919869, ReferenceNumber AA8410b1c35a720630144a1732859e01. Повторные OrderInfo возвращают статус 11 «Запрос получен», AllowCancel=2. Просим обработать этот существующий тестовый заказ и уточнить порядок штатной аннуляции для завершения интеграционного теста. Дополнительных броней не создаём.

Источники контракта: [официальная страница Roomlink](https://roomlink.ru/documentation_api), [API 4.0.1](https://disk.360.yandex.ru/i/DSHUMf72OZqdfQ). Доказательства live flow — реальные IntegrationLog, ServiceOffer, OrderService и BookingWorkflow в локальной БД; тестовые XML fixtures служили только для регрессий.
