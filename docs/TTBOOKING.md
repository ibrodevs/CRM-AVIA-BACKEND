# TT Booking / stdCBS

Адаптер `ttbooking` использует существующий `ProviderAdapter`, `SupplierCredential`, `ServiceOffer`, `OrderService`, booking workflow и `IntegrationLog`. Hotelbook и mock сохраняют свои адаптеры. Справочники и токены stdCBS не создают отдельные универсальные CRM-модели.

Основание контракта: предоставленный документ **«stdCBSapii (1).doc»** — разделы U1/F, U2, U3/B, T-B и справочники. Документ найден в Downloads; его содержимое и примеры авторизации не копируются в репозиторий.

## Настройка

В карточке поставщика выберите **API → Авиа → TT Booking**. Укажите:

| Поле | Значение |
| --- | --- |
| API URL | `https://www.ttbooking.ru/api/scbs/json` либо HTTPS endpoint, выданный TT Booking |
| Agency, User | Идентификаторы выданного аккаунта |
| Password Key | Пароль/ключ; хранится зашифрованно, не возвращается API |
| Locale | `ru` |
| Environment | `sandbox` или `production` |
| Валюта поставщика | Валюта расчётов именно этого аккаунта TT Booking |

В F из документа нет поля валюты. Поэтому нормализованная цена использует валюту из credentials `currency`, если она задана, иначе первую валюту поставщика. Это не конвертация валют и не валюта, которую пользователь выбрал для показа поиска.

Credentials сохраняются в `SupplierCredential`; `password` и `password_key` — альтернативные имена секрета. Пустой Password Key при обновлении существующего окружения сохраняет предыдущий ключ. Для нового окружения нужен отдельный ключ. Настройки карточки хранят только URL/версию, без секретов.

Выбор TT Booking сохраняет адаптер и выбранное окружение для конкретного поставщика. Старые mock credentials не подменяют этот выбор. Ранее полученные предложения и брони сохраняют собственное окружение и привязку к аккаунту; изменение agency/user/URL запрещает использование прежних токенов с новым аккаунтом. Ротация только пароля сохраняет идентичность аккаунта.

Статус `connected` требует успешного SEARCHFR для текущей версии credentials и окружения. Историческое `active` после одной структурной проверки не считается подтверждённым подключением. HTTP 401/403 помечают использованный доступ как failed; секреты и hash в доказательство проверки не записываются.

`environment` — метаданные CRM, а не автоматическое переключение production URL на sandbox. Тестовый endpoint/аккаунт и разрешённый исходящий IP должен выдать TT Booking. Часы backend должны быть синхронизированы; подпись включает UTC timestamp и MD5 URL-encoded GET-параметров в порядке `agency`, `password`, `time`, `user`. Пароль в context не отправляется.

## Команды

| stdCBS | Метод / поведение |
| --- | --- |
| SEARCHFR | `search()` — one-way, round-trip, multi-city; ADULT/CHILD/INFANT; ECONOMY/BUSINESS/FIRST/PREMIUM; direct/e-ticket/preferred airlines |
| SELECTFR | `revalidate()` — одна группа, один flight на itinerary, только токены; новые цена, baggage/fare, срок и признак изменения цены |
| CREATEBR | `book()` — обновлённые токены SELECTFR, customer, passengers/passport и телефон первого пассажира |
| DISPLAYSTATUS | `retrieve_booking()` — чтение состояния поставщика |
| LOADBOOKING | `retrieve_booking(refresh=True)` — обновление из системы бронирования; также используется при inquiry неизвестного результата |
| PAYDEPOSIT | `issue()` — проверка статуса, срока и DISPLAYTICKETS перед выпиской |
| DISPLAYTICKETS | `display_tickets()` — каждый билет сохраняется отдельно; один пассажир может иметь несколько билетов |
| CANCELBOOKING | `cancel()` — актуальный статус перед отменой; подтверждение CANCELLED и void всех ранее известных выписанных билетов |

U3 использует структуру выбранных flightGroup → itinerary → flight из результата SELECTFR, как в документе. Segment tokens сохраняются в supplier snapshot/нормализованных сегментах; недокументированные поля не добавляются в U3.

Нормализованные данные сохраняют маршрут со всеми сегментами и участками, baggage details, terminals, equipment, booking/service class, fare basis, duration, connections, GDS, e-ticket и timeLimit. В `OrderService.external_id` сохраняется PNR, а `bookingFile.token`, reservation tokens, все locators, даты, raw status и исходный bookingFile — в `provider_snapshot.ttbooking_booking`. Финансовые операции требуют сохранённую CRM-услугу соответствующего tenant и supplier, не произвольный токен из другого контекста.

Пассажиры передаются с документом, выбранным для участника заказа, либо его активным документом. Тип пассажира вычисляется на дату первого вылета: взрослый с 12 лет, ребёнок с 2, младенец до 2. Учитывается `latinRegistration`. INTERNAL/FOREIGN применяются к документам РФ; паспорт иностранного гражданина — PASSPORT; свидетельство о рождении — BIRTHDAY_NOTIFICATION. Для неподдержанных типов документов и отсутствующего срока действия возвращается явная ошибка, данные не выдумываются. Телефон E.164 разбирается через phonenumbers; также поддерживаются явные countryCode/areaCode/phone. E-mail передаётся в документированном customer.

## CRM и повторные операции

Бронь/выписка выполняются через существующий booking workflow, включая preflight и подтверждение изменения цены. Ручная смена статуса API-услуги TT Booking не подменяет ответ поставщика. Отмена из карточки авиауслуги запускает provider job и показывает успех только после сохранённого подтверждения отмены. `/services/<id>/provider-status/` запускает inquiry с LOADBOOKING; интерфейс ожидает завершения job.

Состояние PAYED уже означает выполненную оплату. Без подтверждённого TT Booking режима выписки такого заказа повторный PAYDEPOSIT не отправляется; основной путь — NEW → PAYDEPOSIT → COMPLETE/ISSUING с подтверждёнными билетами.

Перед mutation в БД атомарно сохраняется состояние `sending`. Повтор CREATEBR для той же услуги использует уже сохранённый результат либо блокируется. Повторная выписка проверяет билеты и сохранённое состояние операции. Состояния `sending`/`unknown` блокируют повтор PAYDEPOSIT. Чтение статуса во время выполняющейся операции не освобождает этот барьер. После перезапуска worker незавершённая операция переходит в unknown и требует inquiry.

Timeout, HTTP 5xx и нечитаемый ответ после mutation означают неизвестный результат. Автоматического повторения mutation нет. Для CREATEBR без полученного bookingFile.token документ не предусматривает поиск по клиентскому ключу: нужна сверка в TT Booking; повторная бронь блокируется. Для выписки/отмены inquiry использует сохранённый токен, LOADBOOKING и билеты. Неполный void не переводит всю услугу в cancelled.

`IntegrationLog` хранит uppercase названия реальных команд, supplier, adapter, correlation, HTTP status, duration, result, error_code, retries и данные из строгого allowlist. Пароли, hash, credentials, токены, паспортные номера, контакты, ФИО, даты рождения, свободный текст ошибок и документы в логи не попадают. Неизвестные provider statuses остаются в supplier snapshot.

## Ограничения документации / вопросы TT Booking

1. **Flight rules:** §3.8 описывает `readFlightRules` с устаревшими login/password/salePoint и без stdCBS `context.command`. Команда в основном списке отсутствует. Нужно подтвердить её современное имя, параметры и доступ аккаунта. Сейчас `fare_rules()` честно возвращает `raw_available=false` и пишет лог недоступной возможности; нормализация документированных remarkGroups покрыта mock-тестом. Пароль в context не отправляется. ATPCO remarks не интерпретируются как точные условия возврата/обмена.
2. **Documents:** формат содержимого bookingFile.documents не описан. Данные сохраняются целиком, но неподтверждённая схема ссылок/файлов не используется для создания маршрут-квитанции.
3. **Номер билета:** в документе `airTicket.number` указан без расчётного кода авиакомпании. Сохраняются именно полученный номер и carrier; PNR не становится номером билета. Нужен контракт полного номера/расчётного префикса, если аккаунт выдаёт их отдельно.
4. **Ошибки:** документ не даёт полного каталога error/messages. HTTP и известные состояния нормализованы; произвольный текст не используется для угадывания AUTH/ATPCO/других условий. Нужны реальные обезличенные error fixtures.
5. **Бессрочные/ID-документы и младенец с отдельным местом:** требуется подтверждённая политика аккаунта. Не выдумывается срок документа или недокументированная категория/флаг stdCBS.
6. **Уже оплаченная бронь PAYED:** нужно подтвердить режим ISSUETICKETS/FOP для уже проведённой оплаты. Повторный PAYDEPOSIT автоматически не выполняется.
7. **Потерянный CREATEBR token:** нужен способ сверки/поиска операции у поставщика. Недокументированный lookup по client_request_id не реализован.

## Проверка

Все локальные TT Booking tests запрещают настоящие сетевые вызовы через autouse fixture и используют mock transport. Полный API/DB сценарий находится в `test_local_api_end_to_end`: supplier → credentials → connection → SEARCHFR → SELECTFR/preflight → order → CREATEBR → DISPLAYSTATUS/LOADBOOKING → PAYDEPOSIT → DISPLAYTICKETS → CANCELBOOKING/void. После этапов проверяются ORM-статусы, билеты и PII-free IntegrationLog.

```sh
uv sync --dev
uv run pytest integrations/tests/test_ttbooking.py -v
uv run pytest integrations/tests services/tests suppliers/tests booking/tests -v
uv run python manage.py check --settings=config.settings.test
uv run python manage.py makemigrations --check --dry-run --settings=config.settings.test
```

Используется PostgreSQL, как в README проекта. Модели/миграции не меняются. Отдельный GitHub workflow проверяет TT Booking и связанные backend tests с mock provider; frontend workflow запускает tests/build без TT credentials. Production CREATEBR/PAYDEPOSIT/CANCELBOOKING при разработке не выполняются. Перед боевым включением нужны выданные TT Booking sandbox credentials и проверка команд, форматов и доступных capabilities на этом аккаунте.
