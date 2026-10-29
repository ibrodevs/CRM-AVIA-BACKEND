# Гостиничные post-sale действия

## Реализованный flow

Гостиницы (`hotel` / UI «Гостиница») открывают отдельный `HotelBookingActionsDrawer` из карточки услуги. Кнопки отмены и обмена гостиничной услуги также направлены в эту панель. Авиационная форма `NewReturnModal` не менялась; на границе returns-модуля гостиницы исключены из списка услуг. Backend отклоняет создание гостиничного авиационного AfterSaleCase с `HOTEL_ACTIONS_REQUIRED`.

В гостиничной панели доступны сведения о брони, supplier booking и confirmation references, отдельные номера с room ref и supplier item ID при наличии, распределённые гости, даты, питание, стоимость, статус и условия отмены. Один supplier item может соответствовать нескольким номерам. Неизвестные отдельные цены не заменяются полной стоимостью всей услуги. Нераспределённые гости показываются отдельно.

Список номеров поддерживает выбор отдельных карточек и «Выбрать все». Частичная API-отмена блокируется и во frontend, и в backend. Пустой или устаревший snapshot не превращается в выдуманный список номеров.

## Возможности адаптеров

Capabilities возвращаются backend, определяются через `ProviderAdapter.hotel_action_capabilities(service)` и переопределены HotelbookAdapter и AandaAdapter. GET панели читает snapshot и БД, без supplier dictionary/status/login запросов.

| Действие | Реализация |
| --- | --- |
| Обновить данные поставщика | API: существующий `retrieve_booking`, для API-брони HBPRO/A&A с сохранённым locator |
| Отмена всей брони | API: существующий `cancel`, с отдельным подтверждением и финансовым согласием |
| Частичная отмена номеров | Unsupported |
| Удалить отдельный номер | Unsupported |
| Отменить без штрафа | Unsupported без зафиксированного расчёта нулевого штрафа |
| Запросить отмену без штрафа | Manual request |
| Даты, категория, гости, питание | Manual request |
| Ранний заезд, поздний выезд, кровать, пожелания | Manual request |
| Связаться с поставщиком | Manual request, без автоматической отправки сообщений |
| Добавить номер | New booking: объяснение и переход в существующий подбор гостиницы |
| Проверить условия отмены | Local: отображение сохранённых условий; обновление выполняется отдельным status inquiry |
| Документы/ваучер | Local: существующий список/просмотр документов услуги |
| Сертификат на проживание | Существующий `hotel-certificate` для подтверждённой/отменённой HBPRO-брони и пользователя с правом генерации |

Для других/ручных поставщиков базовый адаптер не заявляет API-возможности: отмена обрабатывается ручным запросом, supplier refresh недоступен. При отсутствии конфигурации API не вызывается. Отмена также недоступна для уже отменённой брони, незавершённого предыдущего cancel и supplier booking, связанного сразу с несколькими CRM-услугами — требуется сверка всей брони.

## Операции и безопасность

Добавлены `HotelOperation` и `HotelOperationHistory` в aftersales. Хранятся tenant, order, service, supplier, тип операции, scope, room refs, execution mode, исходный/запрошенный/результирующий/финансовый snapshots, supplier response/reference, actor, timestamps, idempotency key, job и подтверждение.

Статусы операции отделены от статуса гостиничной брони: created, checking, awaiting_operator_confirmation, submitted_to_supplier, awaiting_supplier, completed, rejected, failed, unknown.

API отмена сначала создаёт `awaiting_operator_confirmation`. До явного подтверждения supplier cancel не вызывается. Проверяются текущая версия операции, неизменность сведений брони и отсутствие другой активной отмены. Подтверждение финансовых условий фиксирует пользователя, время и явное согласие с неизвестным точным штрафом.

Задача `hotel.operation` использует существующий worker и существующий adapter cancel/retrieve с booking lease. Автоматических повторов cancel нет: `max_attempts=1`, `retryable=False`; повторное подтверждение/доставка задачи не вызывает второй cancel. Supplier-specific markers и idempotency mechanisms не удалялись. Timeout/неопределённый результат переводит операцию в unknown; выполняется безопасный status inquiry. Если отмена не подтверждена, CRM не считает бронь отменённой. Дальнейшая сверка выполняется через refresh, без повторного cancel. Поздний timeout не перезаписывает уже подтверждённую параллельной сверкой отмену.

Подтверждённая отмена обновляет статус услуги и существующего booking workflow. Успешный inquiry неизвестной брони также восстанавливает workflow без повторного booking. Номера/услуга не удаляются новым flow.

Ручной запрос имеет описание и историю. Оператор может взять его в проверку, отметить ожидание поставщика, завершить или отклонить с комментарием. Эти действия не изменяют бронь или финансовые поля автоматически. API-операцию нельзя вручную пометить успешной через endpoint ручной обработки.

Права проверяются отдельно:

- просмотр: `orders.view` и доступ к заказу через существующий selector;
- ручное изменение: `orders.change` и разрешённое действие hotel/book;
- отмена: `orders.change`, `services.cancel` и hotel/cancel;
- финансовое подтверждение: дополнительно `finance.approve_payment`;
- документы: существующие `documents.view` / `documents.generate`.

Tenant, service/order scope и room refs проверяются на backend. Financial fields из frontend не принимаются как supplier payload. Сохранённые supplier responses очищены от секретов; сырой текст supplier exception с возможными credentials не пишется в историю. Изменения журналируются в истории операции и AuditEvent.

## Финансы

Supplier cost, client cost, service fee, penalty и refund представлены раздельно. Новые действия не перезаписывают supplier cost, не создают фиктивного возврата и не выполняют финансовые проводки. Отсутствующий penalty остаётся null. Cancellation policy не считается зафиксированной котировкой штрафа. Последний сообщённый A&A штраф отображается отдельно и не объявляется гарантированной суммой. Изменение клиентского документа не меняет supplier cost.

## Новые CRM endpoints

- `GET services/{service_id}/hotel-actions/`: summary, rooms, capabilities, permissions, operations/history.
- `POST services/{service_id}/hotel-actions/`: ручной запрос, refresh либо подготовка отмены; обязателен Idempotency-Key.
- `POST services/{service_id}/hotel-actions/{operation_id}/confirm/`: явное подтверждение API-отмены.
- `POST services/{service_id}/hotel-actions/{operation_id}/resolve/`: обработка только manual-операции с version и комментарием.

Новых HBPRO endpoints не добавлено. Используются существующие retrieve/status inquiry и cancel. Словарный cache, TTL, авторизация и certification booking flow не менялись.

## Миграция и развёртывание

Новая миграция: `aftersales/migrations/0002_hoteloperation_hoteloperationhistory_and_more.py`.

Она создаёт таблицы операций/истории, индекс tenant/service/status и уникальность service/idempotency_key. Данные существующих заказов и услуг не переписываются.

Перед включением UI применить `python manage.py migrate` и перезапустить backend и существующий job worker, чтобы зарегистрировался handler `hotel.operation`. На production миграция и реальные отмены в рамках этой работы не выполнялись. Развёртывание на production выполняется отдельно после применения миграции.

## Проверки

- Backend: `python -m pytest orders booking services aftersales finance avia rail integrations documents` — **582 passed** (включая 21 новый сценарий/параметр гостиничных действий).
- Frontend: `npm test` (в составе `npm run build`) — **421 passed**, ошибок нет.
- Frontend production build — passed; архитектурная проверка входит в tests и прошла.
- `python manage.py check` — ошибок нет.
- `python manage.py makemigrations --check --dry-run` — No changes detected; новая миграция уже включена в рабочую копию.
- Ruff по изменённому backend-коду и `git diff --check` в обоих репозиториях — passed.
- Есть одно предупреждение существующего integration test о совместимости cache key с memcached; тест проходит. Словарное кеширование этим изменением не затронуто.

Backend regression tests покрывают summary без дополнительных запросов к поставщику, несколько номеров в одном supplier item, room assignments, partial cancel/new booking rejection, confirmation/financial permissions, повторную отправку и доставку задачи, timeout/status reconciliation, manual history без изменения брони/сумм, tenant/order visibility, supplier-specific capabilities, sanitization, старый cancel marker, shared supplier booking, stale confirmation, восстановление unknown workflow и гонку позднего результата.

Frontend tests проверяют маршрутизацию hotel/ticket, выбор нескольких номеров, capabilities, неизвестный штраф, блокировку повторной отмены, права и фактические API payload/idempotency keys. Обновлён существующий regression test подписей «Гости»/«Пассажиры».

В Chrome на изолированном локальном стенде проверены отдельная панель, два номера, multi-select, partial cancel blocking, manual request/history, явное подтверждение, unknown result, повторная отмена disabled и отсутствие авиационных полей. На ширине 390px горизонтального переполнения не обнаружено. Стенд и его тестовые данные не добавлены в продукт/репозитории.

## Изменённые файлы

Backend:

- aftersales/models.py
- aftersales/views.py
- aftersales/migrations/0002_hoteloperation_hoteloperationhistory_and_more.py
- integrations/adapters.py
- integrations/hotel_actions.py
- integrations/hotelbook.py
- integrations/aanda.py
- services/hotel_actions/__init__.py
- services/hotel_actions/presentation.py
- services/hotel_actions/operations.py
- services/hotel_actions/views.py
- services/job_handlers.py
- services/urls.py
- services/tests/test_hotel_actions.py
- docs/hotel-post-sale-actions.md

Frontend:

- src/modules/services/hotels/actions/HotelBookingActionsDrawer.jsx
- src/modules/services/hotels/actions/HotelBookingSummary.jsx
- src/modules/services/hotels/actions/HotelRoomsSelector.jsx
- src/modules/services/hotels/actions/HotelActionMenu.jsx
- src/modules/services/hotels/actions/HotelCancellationPanel.jsx
- src/modules/services/hotels/actions/HotelManualRequestPanel.jsx
- src/modules/services/hotels/actions/hotelActionsApi.js
- src/modules/services/hotels/actions/hotelCapabilities.js
- src/modules/services/index.js
- src/modules/orders/ui/OrderCard.jsx
- src/modules/returns/ui/ReturnsPage.jsx
- src/styles/components.css
- test/hotel-actions.test.mjs
- test/order-card-unified-services.test.mjs

Постороннее существующее изменение .DS_Store к задаче не относится.
