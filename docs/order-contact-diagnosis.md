# Контактное лицо заказа: диагностика и исправление

## Подтверждённая причина ORD-000017

Ответ production `GET /api/backend/orders/090c5e8f-5d4c-41c2-a7c9-3d38ba6d9c2c/overview` (HTTP 200), полученный через авторизованный Chrome:

- статус `new`, корпоративный заказ, компания АТГ;
- `contact_person: null`;
- `client_person: null`;
- единственный активный участник связан с CRM Person, но `is_contact: false`;
- `guest_snapshot: null`, `agreement_snapshot: null`.

Имя пассажира в шапке появлялось из выражения `participants.find(p => p.lead) || participants[0]`, а затем из `order.client`. Это не доказывало выбора контактного лица. Раздел «Клиенты» показывал «Контактное лицо: Не указано».

Модель Order не содержит столбца `contact_name`. Ошибка «Заполните имя контактного лица» возникала в `HotelbookAdapter.book`: проверялось `booking_request.contact_info.name`. Worker формировал `contact_info` только из `order.contact_person or order.client_person`, поэтому для ORD-000017 получался пустой словарь. Выбранного контакта в этом заказе фактически нет; назначать первого пассажира автоматически неправильно.

Свободное корпоративное бронирование создаёт заказ с компанией и добавляет гостей гостиницы обычными участниками. Гость номера и контакт заказа — разные роли.

## Flow

`OrderCard` → «Перейти к бронированию» / «Начать бронирование» → `BookingWizard.startBooking` → `bookingApi.create` → `preflight` → `start` → `WorkflowStartView` → background job `booking.run` → `run_booking` → `HotelbookAdapter.book` → HBPRO `createOrder.contactInfo`.

Изменение статуса заказа на «В работе» — отдельное действие; найденный текст ошибки относится к бронированию HBPRO.

## Исправление

Единый resolver контакта использует явно выбранный `contact_person`, одного активного участника `is_contact`, затем индивидуального заказчика `client_person`. Поддерживается выбранный участник со snapshot и старый строковый контракт, если он передан импортом. Первый пассажир и название компании не используются как контакт. Несколько контактных участников требуют явного выбора.

Имя берётся из реальных полей Person: нативные/Latin имя и фамилия, либо старые first_name/last_name/full_name. Пробельные строки невалидны; одного имени достаточно. Выбранный безымянный контакт не заменяется молча другим человеком.

API detail/overview возвращает `booking_contact`; frontend показывает тот же контакт и сохраняет его CRM Person ID. В окне бронирования можно выбрать существующего человека без выхода из заказа и без сброса услуг. PATCH использует текущую version и проверяет tenant/архивность Person. Создание заказа сохраняет выбранный contact_person.

Проверка выполняется в preflight, повторно перед постановкой в очередь и в worker. Требование контакта включено для HBPRO через capability адаптера. Финансовая проверка, пассажиры и права доступа сохранены. Сам адаптер передаёт в createOrder тот же нормализованный contactInfo, который проверил.

Тексты:

- нет контакта: «Укажите контактное лицо заказа.»;
- контакт выбран без имени: «У контактного лица не заполнено имя.».

## Проверки

- targeted backend: 20 passed;
- `pytest orders booking services finance avia rail integrations`: 345 passed;
- `python manage.py check`: ошибок нет;
- `python manage.py makemigrations --check --dry-run`: No changes detected;
- Ruff по изменённым Python-файлам: passed;
- frontend `npm test`: 414 passed;
- frontend `npm run build`: passed;
- `git diff --check`: passed в обоих репозиториях.

Есть одно предупреждение существующего теста cache key о совместимости с memcached. В relevant suite ошибок нет.

Regression tests: связанный Person без дублирующего имени; индивидуальный заказчик; явно отмеченный контакт старого заказа; отсутствие контакта при наличии пассажира; пустое/пробельное имя; Latin/Cyrillic; legacy и snapshot; удалённые и неоднозначные контакты; tenant isolation; согласованность PATCH/detail/overview; preflight/start/worker; корректный contactInfo в HBPRO; frontend create/PATCH с Person ID и version.

## Границы production-проверки

Production-заказ не изменялся, платное бронирование не запускалось. Данные подтверждены через API, а не прямой Django ORM. Локальная БД ORD-000017 не содержит. Сессия Chrome завершилась до сравнения с успешно забронированным production-заказом; такое сравнение не выполнено. Успешная передача contactInfo проверена автоматическим интеграционным тестом с тестовой БД и подменой внешнего gateway, без реальных операций у поставщика.

Код пока находится в локальной рабочей копии; push/deploy этого исправления не выполнены. Новых обязательных миграций нет. Новых методов HBPRO API не добавлено.

## Безопасная проверка

Read-only ORM-диагностика в backend environment (не требует изменения данных):

```sh
ORDER_NUMBER=ORD-000017 python manage.py shell < scripts/diagnose_order_contact.py
```

При одинаковых номерах у разных tenants добавить `ORDER_TENANT_ID`. Для известного успешного заказа добавить `ORDER_COMPARE_NUMBER`; иначе скрипт ищет заказ того же tenant с completed booking workflow. PostgreSQL-транзакция принудительно READ ONLY. Credentials, документы, email и телефоны не выводятся; для email/телефона — только признак наличия.

После deployment открыть ORD-000017: шапка и мастер должны показывать отсутствие контакта, preflight — точное сообщение. В мастере выбрать фактическое контактное лицо CRM: PATCH должен сохранить contact_person, detail/overview — вернуть его нормализованное имя. Услуги, тарифы и даты должны остаться прежними. Выполнить preflight без запуска оплачиваемого бронирования; остальные бизнес-проверки могут блокировать заказ независимо от контакта. Финальное бронирование выполняет ответственный пользователь.

## Изменённые файлы

Backend:

- orders/contacts.py
- orders/serializers.py
- orders/views.py
- orders/tests/test_order_contacts.py
- booking/contacts.py
- booking/preflight.py
- booking/views.py
- booking/job_handlers.py
- booking/tests/test_contact_preflight.py
- integrations/adapters.py
- integrations/hotelbook.py
- scripts/diagnose_order_contact.py
- docs/order-contact-diagnosis.md

Frontend:

- src/modules/orders/model/order-contact.js
- src/modules/orders/ui/OrderContactField.jsx
- src/modules/orders/ui/BookingWizard.jsx
- src/modules/orders/ui/OrderCard.jsx
- src/modules/orders/api/ordersApi.js
- src/legacy/compatibility/workspace-provider.jsx
- test/order-contact.test.mjs

Постороннее изменение .DS_Store существовало ранее и к исправлению не относится. Production HAR и персональные данные в репозиторий не добавлены.
