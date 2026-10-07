# Hotelbook / HB Pro Expert

Интеграция основана на официальной [документации](https://api.hbpro.expert/docs/)
и [OpenAPI](https://api.hbpro.expert/docs/swagger.json), версия документа 2026.09.23,
проверено 07.10.2026. Живой прогон требует credentials HB Pro и разрешённого IP backend.
Контрактные тесты подменяют только HTTP boundary; это не подтверждение живой брони.

## Архитектура

Backend — Django/DRF, PostgreSQL, JWT/RBAC, tenant-scoped модели и PostgreSQL queue.
`common.apps` регистрирует обработчики заданий; `common.jobs` ставит их в очередь,
`run_jobs` выполняет под tenant context. Значимые команды используют существующую
идемпотентность, события outbox, ценовые снимки и IntegrationIncident.

Связанные приложения и ответственность:

| Область | Реализация |
|---|---|
| Организации, доступ, аудит, очередь | `tenancy`, `accounts`, `common` |
| Клиенты и участники поездки | `crm.Person`, `orders.OrderParticipant` |
| Доступ поставщика | `suppliers.SupplierCredential.encrypted_secrets` |
| Контракт и registry | `integrations.adapters.ProviderAdapter`, `get_adapter` |
| Общий поиск | `SearchSession → services.search → SearchProviderRun → ServiceOffer` |
| Цена и наценка | `services.pricing`, immutable `PriceSnapshot` |
| Добавление в заказ | `OrderServicesView`, `OrderService`, `ServicePassenger` |
| Бронирование | `BookingWorkflow → preflight → booking.run → BookingWorkflowItem` |
| Получение статуса, отмена | `booking.status_inquiry`, `booking.compensate` |
| Отельное размещение | Существующие `HotelStay`, `HotelRoom`, `HotelPlacement`; HB-справочники сюда не импортируются |
| Остальной контур | `avia`, `rail`, `groups_app`, `offers`, `finance`, `documents`, `aftersales`, `calendar_app`, `communications`, `notifications`, `workforce`, `reports`, глобальный `search` |

`HotelbookAdapter.key = hotelbook`, поддерживаемый kind — `hotel`.
Registry загружает адаптер при первом обращении. `iter_search` расширяет существующий
контракт потоковой выдачей; mock и другие list-based adapters продолжают работать.
Worker сохраняет предложения по мере поступления, UI опрашивает тот же SearchSession.
HB offers одинаковой цены не объединяются, если их supplier/offer IDs различаются.

Названия, адрес, даты, комнаты, питание и условия нормализуются в обычные
`itinerary`/`fare`/`price`. Цена HB — цена всего предложения, не цена за ночь.
Frontend использует текущий HotelsPage и общие CRM endpoints.

`searchId`, `offerId`, `hotelId`, hashes комнат, provider room IDs и исходные ответы
находятся внутри `raw_snapshot.hotelbook`, затем `provider_snapshot.hotelbook`.
`external_key` — стабильный хэш search/offer IDs, который помещается в поле CRM.
HB order/item IDs сохраняются внутри `provider_snapshot.hotelbook_booking`.
Workflow locator — 32-символьная ссылка CRM; это не HB item ID (UUID длиннее поля locator).
Ответы workflow содержат общие статусы и CRM locator; credentials наружу не возвращаются.

## Подключённые методы HB Pro

Базовый адрес: `https://api.hbpro.expert`, locale: `ru` или `en`.

| Назначение | Метод и путь |
|---|---|
| Авторизация | `POST /api/v1/{locale}/gateway/login` — `login`, `password`, ответ `token` |
| Поиск | `POST /api/v1/{locale}/hotel/gateway/search` — `checkIn`, `checkOut`, `rooms`, `hotels`/`cities`, `citizenshipId` |
| Варианты номеров/тарифов | `GET /api/v1/hotel/gateway/results/{searchId}?offset=…&limit=…` |
| Revalidate и условия | `POST /api/v1/{locale}/hotel/gateway/search/details` — `searchId`, `offerId` |
| Контейнер бронирования HB | `POST /api/v1/{locale}/order/gateway/orders` — `clientOrderId`, `payForm`, `contactInfo` |
| Бронирование | `POST /api/v1/{locale}/hotel/gateway/book` — search/offer/order IDs, `price`, `currency`, `confirmationMode`, `rooms[].hash`, `rooms[].guests` |
| Получение заказа | `GET /api/v1/{locale}/order/gateway/orders/{orderId}` |
| Сверка после неопределённого создания заказа | `GET /api/v1/{locale}/order/gateway/orders?clientOrderId=…&pagination=false` |
| Получение частей брони | `GET /api/v1/{locale}/hotel/gateway/booking_items/{itemId}` |
| Отмена каждой части | `POST /api/v1/{locale}/hotel/gateway/booking_items/{itemId}/cancel` — `{}` |
| Данные конкретного отеля | `GET /api/v1/{locale}/dict/gateway/hotel/hotels/{id}` |
| Гражданство, поиск города | `GET /api/v1/{locale}/dict/gateway/geo/countries`, `…/geo/cities` с пагинацией |

JWT передаётся как `Authorization: Bearer …`, кэшируется на 86340 секунд,
в кэше хранится зашифрованным. При отклонённом токене выполняется одна попытка обновления.
Для нескольких процессов рекомендуется общий защищённый Django cache.
Словари кэшируются только в контексте credentials поставщика, в общий справочник CRM не записываются.

Поиск опрашивается с интервалом 1 секунда, offset увеличивается на количество
полученных HB offers. После `finished=true` оставшиеся страницы дочитываются.
Предложения живут 4 часа от `queryTime`; HTTP timeout — 20 секунд, предел поиска — 90 секунд.
API не содержит параметра поиска `currency` или `meal_plan`: адаптер не отправляет
выдуманные параметры. Валюта и питание показываются по реальному ответу HB.
Настройка `client3Did` проходит неизменной из criteria.hotelbook в details/book.

## Настройка credentials только на backend

Создайте отдельного Supplier с `service_kinds=["hotel"]`.
В существующем приоритете поиска гостиниц поставьте этого supplier первым
(для изолированного теста — единственным). Его активные credentials должны
однозначно указывать на Hotelbook.

На сервере выполните `uv run python manage.py shell` и введите секреты через getpass:

```python
import json
from getpass import getpass
from suppliers.models import Supplier, SupplierCredential
from suppliers.job_handlers import verify_supplier_credentials

supplier = Supplier.objects.get(pk="SUPPLIER_UUID")
config = {
    "login": getpass("HB Pro login: "),
    "password": getpass("HB Pro password: "),
    "default_citizenship": "KG",
    "locale": "ru",
    "pay_form": "CASHLESS",
}
credential, _ = SupplierCredential.objects.update_or_create(
    tenant=supplier.tenant,
    supplier=supplier,
    provider_adapter="hotelbook",
    environment="sandbox",
    defaults={"encrypted_secrets": json.dumps(config), "status": "inactive"},
)
print(verify_supplier_credentials(supplier)["status"])
del config
```

Секреты шифрует существующий EncryptedTextField через FIELD_ENCRYPTION_KEY.
Не помещайте значения в shell arguments, frontend, исходники, git или сообщения.
`check-connection` для Hotelbook выполняет реальную авторизацию, а не только проверку registry.
В настройках поставщика `contact_info` может задавать резервные контактные данные;
по умолчанию worker использует контактное лицо/клиента заказа. Если тариф требует
конечного покупателя, его данные нормализуются из клиента данного CRM-заказа
в HB `customer` типа PRIVATE/LEGAL.
Форма оплаты должна соответствовать договору HB. IP исходящих запросов должен быть разрешён HB.

## Один ручной сценарий: Casa Manzella

Запустите backend и worker: `uv run python manage.py run_jobs`.
У пользователя нужны права поиска, изменения заказа, бронирования и отмены гостиниц.
Участник заказа должен иметь реальные заполненные ФИО латиницей, дату рождения,
пол, гражданство ISO alpha-2 и документ, соответствующий требованиям общего preflight.
Для сценария ниже выберите одного взрослого, один номер и будущие даты.

1. На странице «Гостиницы» укажите `Casa Manzella` или `1251539`, даты, один номер,
   одного гостя и гражданство. При «Все» используется `default_citizenship` backend.
2. Нажмите «Найти». Варианты должны иметь supplier `hotelbook`, настоящее название
   номера, питание, цену HB и условия отмены. Дождитесь окончания поиска.
3. Откройте вариант и нажмите проверку наличия/цены. При изменении цены или условий
   ознакомьтесь с обновлённым тарифом.
4. Добавьте предложение в заказ с одним взрослым участником.
5. В заказе запустите общий wizard бронирования. Preflight использует credentials
   этого supplier; отсутствие доступности/авторизации блокирует старт.
   Изменения цены/условий требуют подтверждения.
6. Проверьте ответы workflow. Только HB `CONFIRMED` считается booked.
   При ожидающем подтверждении/неизвестном результате нажмите «Проверить».
7. Для получения актуальной брони используйте status inquiry. Для Hotelbook не нужна
   авиавыписка; после подтверждения переходите к завершению/финансам заказа.
8. Отмените гостиницу в карточке заказа либо через общий workflow cancel.
   Статус cancelled появляется только после `CANCELED` всех частей HB брони.
   При отложенной отмене выполните status inquiry через минуту.

Те же шаги доступны через существующий CRM API (`/api/v1/`):

| Шаг | Endpoint и body |
|---|---|
| Search | `POST service-searches/` — `{"kind":"hotel","criteria":{"location":"1251539","check_in":"2027-03-10","check_out":"2027-03-12","rooms":1,"guests":1,"citizenship":"KG"}}` |
| Варианты | `GET service-searches/{search_id}/offers/` |
| Проверка | `POST service-offers/{offer_id}/revalidate/` |
| Условия | `GET service-offers/{offer_id}/fare-rules/` |
| Добавление | `POST orders/{order_id}/services/` — `{"offer_id":"…","participants":["PARTICIPANT_UUID"]}` |
| Workflow | `POST booking-workflows/` — `{"order":"…","services":["SERVICE_UUID"]}` |
| Preflight | `POST booking-workflows/{id}/preflight/` |
| Book | `POST booking-workflows/{id}/start/` — `{"confirm":true}` после ознакомления с preflight |
| Статус CRM | `GET booking-workflows/{id}/status/` |
| Retrieve HB | `POST booking-workflows/{id}/status-inquiry/` — `{"item":"WORKFLOW_ITEM_UUID"}` |
| Cancel | `POST booking-workflows/{id}/cancel/` — `{"reason":"Тест интеграции"}` |

Для start/cancel используйте уникальный `Idempotency-Key`, CRM JWT — как для других
API действий. Задания асинхронные: между шагами дождитесь worker и проверьте статус.
Даты в примере нужно заменить, если HB не отдаёт доступность на них.
Новые номера нужно искать новым запросом, а не повторно бронировать один offer.

После успешного живого Casa Manzella повторите полный сценарий по отдельности:
Hilton Cologne `1251542`, Haus Mooren `1251543`, Palazzo Victoria `1251563`,
Мастер-отель Первомайская `31687`. Не объединяйте страны в один поиск:
HB запрещает hotels/cities из разных стран. Sandbox/test credentials ограничены этими пятью отелями.

## Ошибки и неопределённые результаты

Все ошибки проходят через AdapterError: auth/IP, timeout, availability, price/rate
change, missing offer и booking/cancel errors. В логи записываются только транспортные
метаданные; HB тексты ошибок, которые могут содержать входные данные, наружу не выводятся.

Перед create-order/book сохраняется долговечная отметка операции в provider_snapshot.
Timeout/5xx/нечитаемый ответ мутации означает unknown, а не безопасный повтор.
Status inquiry находит созданный заказ по clientOrderId и проверяет все item IDs.
Если HB заказ найден, но его hotel items отсутствуют, запись остаётся unknown;
необходимо сверить её с HB/куратором. Автоматическое повторное бронирование запрещено.
Повтор конкретного HB offer через другой OrderService также блокируется.
Отправленная отмена не дублируется, пока её результат не выяснен.

Смена статуса через общие service book/transition не имитирует HB-бронь.
Service cancel ставит existing booking.compensate в очередь для конкретной услуги.
Общая отмена заказа с ещё действующей HB-бронью требует сначала отменить её workflow.
Это предотвращает расхождение статусов CRM и поставщика.

## Проверки и ограничения

```bash
uv run pytest integrations/tests/test_hotelbook.py booking/tests services/tests suppliers/tests orders/tests groups_app/tests
uv run python manage.py check
uv run python manage.py makemigrations --check --dry-run
```

Frontend: `npm test`, `npm run build`.

Контрактные тесты проходят через реальные CRM endpoints, SearchSession, worker,
ServiceOffer, OrderService и booking workflow. Покрыты все пять отелей, пагинация,
актуализация цен и наценок, условия, ошибки/timeout, pending confirmation,
несколько частей брони/отмены, credentials и tenant isolation, запрет дублей.
Mock flow проверяется существующими regression tests. Новые миграции не требуются.

Живой сценарий не выполнен: в доступной локальной базе нет Hotelbook credentials.
Проверка доступности конкретных дат, ограничений счёта, IP whitelist и реальных
ответов HB остаётся обязательным шагом перед эксплуатацией.
Дополнительные операции HB (изменение брони, платежи HB, финансовые документы,
переписка и офлайн-бронирование) не входят в эту интеграцию.
