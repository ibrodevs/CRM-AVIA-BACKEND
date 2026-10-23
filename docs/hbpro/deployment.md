# HBPRO: обновление после сертификации

Production использует общий Django DatabaseCache в существующем PostgreSQL.
Redis в текущих Compose-конфигурациях отсутствует. Миграция
`integrations.0005_shared_cache` создаёт `crm_shared_cache`; web, jobs и scheduler
используют одну таблицу и прежние ключи, ограниченные credentials/tenant.
Токены хранятся зашифрованными, TTL 86340 секунд; словари/отели — 86400 секунд.
Одновременные login сериализуются блокировкой строки credentials.
Проверка подключения также использует действующий cached token.

`prepare` сохраняет поисковый snapshot без getSearchDetails. `book` вызывает
обычный preflight: один актуальный getSearchDetails и сохранение подписанного
snapshot. Worker использует его до 30 секунд; подпись связывает supplier,
credentials, поисковые данные, цену, условия и срок предложения. Изменение
snapshot или истечение TTL требует повторной проверки. При изменении цены или
условий worker блокирует бронирование до нового preflight/подтверждения.
В пределах 30 секунд остаётся обычное окно изменения данных поставщика;
createBooking передаёт проверенную цену, существующая обработка ошибок HBPRO сохранена.
Если очередь задержит worker, дополнительный getSearchDetails ожидаем и необходим.

Сохранённые item IDs используются для обычного polling. searchOrders нужен
только для восстановления orderId; OrderInfo — для восстановления item IDs.
Обе операции остаются в экспорте фактических логов, но больше не обязательны
для PASS. Исторические логи и запуски не меняются; summary пересчитывается.

## Deployment (вручную на сервере, после review и доставки изменений)

Не менять `.env`, credentials или HBPRO_ENVIRONMENT. Убедиться, что текущая
HBPRO среда — sandbox перед новым сертификационным запуском.
Все процессы должны использовать одинаковые DATABASE_URL, DJANGO_SECRET_KEY
и FIELD_ENCRYPTION_KEY. Миграция должна выполниться до перезапуска процессов.

```bash
cd /opt/travelhub-backend
./deploy/backup.sh
git rev-parse HEAD
# Доставить reviewed commit обычным процессом deployment.
docker compose -f docker-compose.prod.yml config -q
docker compose -f docker-compose.prod.yml build
# Дождаться завершения активных финансовых jobs перед остановкой worker.
docker compose -f docker-compose.prod.yml stop jobs scheduler
docker compose -f docker-compose.prod.yml run --rm web python manage.py migrate --noinput
docker compose -f docker-compose.prod.yml run --rm web python manage.py check
docker compose -f docker-compose.prod.yml up -d web jobs scheduler
docker compose -f docker-compose.prod.yml ps
curl -fsS http://127.0.0.1:8000/health/ready/
```

При rollback вернуть предыдущий image/commit; таблицу cache не удалять.
Обратная миграция намеренно сохраняет cache. Первый запуск новой версии может
выполнить один login, поскольку старый process-local cache недоступен.
DatabaseCache добавляет запросы к PostgreSQL; контролировать нагрузку и eviction.

## Один новый sandbox run (только вручную)

Параметры ниже заполнить действующими UUID и будущей датой. Команды финансовых
операций приведены для оператора; автоматически они не выполнялись.

```bash
python manage.py hbpro_certification prepare --tenant "$TENANT" --actor "$ACTOR" --supplier "$SUPPLIER" --scenario 1 --hotel-id 131687 --check-in "$CHECK_IN" --citizenship KG
# Сохранить новый run UUID из stdout; проверить snapshot и одного участника заказа.
python manage.py hbpro_certification book --tenant "$TENANT" --actor "$ACTOR" --run "$RUN" --order "$ORDER" --participants "$PARTICIPANT" --confirm-financial-actions
# Worker выполняет booking. Дождаться job, затем запросить статус.
python manage.py hbpro_certification refresh --tenant "$TENANT" --actor "$ACTOR" --run "$RUN"
# После завершения refresh и подтверждения брони:
python manage.py hbpro_certification cancel --tenant "$TENANT" --actor "$ACTOR" --run "$RUN" --confirm-financial-actions
# После завершения cancel job:
python manage.py hbpro_certification refresh --tenant "$TENANT" --actor "$ACTOR" --run "$RUN"
# После завершения final refresh:
python manage.py hbpro_certification export --tenant "$TENANT" --actor "$ACTOR" --run "$RUN" --output hbpro-new-clean-run.zip
```

Scenario 1: один номер, один взрослый, ON_REQUEST, три ночи. Старый scenario 2
сохранён для совместимости; повторять старые certification runs не требуется.
Словари могут загружаться при поиске/первом новом гражданстве, затем используются
из общего cache. ISO→HBPRO mapping из поиска сохраняется в snapshot; explicit
citizenship_id без проверенного ISO соответствия не приписывается гостям.
