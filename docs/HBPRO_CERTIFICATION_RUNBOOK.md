# Hotelbook / HBPRO certification

Implementation date: 2026-10-21. Source requirements are preserved in `docs/hbpro/`.
Use the existing Travel Hub backend, PostgreSQL queue, scheduler and hotel UI. No separate application is required.

## Configuration and deployment

The endpoint is always `https://api.hbpro.expert`. Do not change it when enabling production.

```
HBPRO_ENVIRONMENT=sandbox
HBPRO_LOGIN=<server secret>
HBPRO_PASSWORD=<server secret>
HBPRO_LOCALE=ru
HBPRO_DEFAULT_CITIZENSHIP=KG
HBPRO_PAY_FORM=CASHLESS
```

`HBPRO_ENVIRONMENT` selects the matching active encrypted SupplierCredential. Env credentials are deliberately not consumed implicitly in production. Import them explicitly on the backend (never as command-line secret values):

```
python manage.py hbpro_credentials --tenant TENANT_UUID --supplier SUPPLIER_UUID
```

Alternatively `--prompt` reads login/password without echo. It verifies a fresh real login before reporting success; it never books. The existing `check-connection` endpoint now includes per-credential environment, verification time, latency and sanitized error. Dev env fallback remains sandbox-only and cannot bypass a failed DB credential. Import each environment separately under its environment setting.

Deploy using the existing deployment script/Compose: `manage.py migrate`, web, `run_jobs` worker and `run_scheduled_jobs` cron. Migrations 0003/0004 add telemetry, certification runs and a cross-worker operation lease. No existing supplier records or features are removed.

All HB requests originate from the backend services; browser requests never contact HBPRO. Place web/jobs/cron on the same server with a reserved external egress IP, or route them through the same static NAT gateway. Incoming DNS/ingress IP is not necessarily the outbound IP. For multiple hosts, use a shared external egress/NAT address and shared protected Django cache. Infrastructure provisioning/firewall changes are outside this code change and have not been performed.

## Run the two scenarios after owner approval

Use a CRM actor UUID with `integrations.manage`, order access, booking and cancellation permissions. CLI also enforces normal booking preflight and tenant isolation. Create/select actual CRM order participants with complete guest data and contact person. The commands below are examples; no live financial actions were executed during development.

First scenario: test hotel 1251201, 1 room, 1 adult, 3 nights, ON_REQUEST. Second: same hotel, 2 rooms, 2 adults per room with different genders, 2 nights, ONLINE. Booking uses ONE create-order and ONE hotel-book call for all rooms. Each returned item and supplier room identifier remains in supplier-specific structured data.

```
python manage.py hbpro_certification prepare --tenant TENANT_UUID --actor USER_UUID --supplier SUPPLIER_UUID --scenario 1 --check-in YYYY-MM-DD
python manage.py hbpro_certification prepare --tenant TENANT_UUID --actor USER_UUID --supplier SUPPLIER_UUID --scenario 2 --check-in YYYY-MM-DD
```

Prepare performs read-only search/results/details, exercises `limit=1` and `offset`, selects the required confirmation mode, and prints a run UUID. Use future dates with availability. It sends no create-order, booking or cancellation. Failed/empty prepares retain their request evidence and are exported as partial, never successful.

After reviewing the selected rate and cancellation penalties, explicitly authorize each financial action by running:

```
python manage.py hbpro_certification book --tenant TENANT_UUID --actor USER_UUID --run RUN_1 --order ORDER_UUID --participants ADULT_UUID --confirm-financial-actions
python manage.py hbpro_certification book --tenant TENANT_UUID --actor USER_UUID --run RUN_2 --order ORDER_UUID --participants MALE_1 FEMALE_1 MALE_2 FEMALE_2 --confirm-financial-actions
```

Participant IDs are OrderParticipant UUIDs, not Person UUIDs. Scenario 2 list order assigns the first two to room 1 and the last two to room 2. Guest count, room occupancy, gender, names, citizenship and birth dates are validated before the supplier booking. Primary guest and optional room assignment/special requests are also supported in the hotel UI/API. Free remarks are sent only when HBPRO explicitly permits them.

Worker executes the existing booking workflow. Never repeat `book` after an unknown supplier result. The run reserves its service before queueing; adapter mutation guards additionally protect the concrete offer. A failed/unknown run is inspected through logs/status rather than blindly booking again.

```
python manage.py hbpro_certification refresh --tenant TENANT_UUID --actor USER_UUID --run RUN_UUID
```

Refresh records official `searchOrders` with `orderCreatedDate[from]`, `orderCreatedDate[to]` and clientOrderId, then queues OrderInfo and every HotelOnlineItemInfo. Wait for worker completion between actions. This explicit refresh step is required for certification evidence even if background sync already confirmed the service.

ON_REQUEST is not confirmed on submission: CRM service is `approval`, workflow item `unknown`, normalized status `pending_supplier_confirmation`. Original statuses are preserved. Only all items `CONFIRMED` yield `confirmed` / CRM `booked`. `ONLINE` is an offer confirmation mode, not an actual booking status. Unknown or mixed item states stay unresolved. All `CANCELED` is required for cancellation completion.

HBPRO manually confirms scenario 1 within 30 minutes during 09:00–18:00 Moscow time according to the supplied PDF. Wait for this supplier action and refresh. The worker automatically polls pending/unknown/cancelling reservations via the existing cron. There is one active sync job per service, backoff from 1 minute to 15 minutes, maximum 100 polls; terminal states stop polling. Exhaustion creates an incident; manual refresh remains available. A 10-minute committed lease prevents simultaneous manual/background retrieve/cancel operations. Cancellation markers commit before external I/O; ambiguous outcomes cannot trigger a blind resend.

After confirmation, and after separately authorizing cancellation:

```
python manage.py hbpro_certification cancel --tenant TENANT_UUID --actor USER_UUID --run RUN_UUID --confirm-financial-actions
```

If cancellation is pending, keep refreshing until each item is canceled. Each room/item cancellation is recorded separately. `cancel_requested_ids` prevents duplicate mutations. Do not manually clear unknown-operation guards without reconciling with HBPRO.

## Evidence export

```
python manage.py hbpro_certification export --tenant TENANT_UUID --actor USER_UUID --run RUN_1 --run RUN_2 --output /secure/path/hbpro-certification.zip
```

One run per scenario is accepted. Existing files are never overwritten; archive permissions are 0600. Export does not send anything externally. It includes:

```
hbpro-certification/
  scenario-1/
    1.a.Search.json
    1.b.getSearchResults.json
    1.b.getSearchResults.002.json
    1.c.getSearchDetails.json
    1.d.createOrder.json
    1.e.createBooking.json
    1.f.searchOrders.json
    1.f.OrderInfo.json
    1.g.HotelOnlineItemInfo.json
    1.h.cancellation.json
  scenario-2/...
  summary.json
  certification-filled.xlsx
```

Every actual call is retained, including pagination, retries, authentication (masked) and dictionary calls under `extra`. Missing steps are listed in summary and never synthesized. Each JSON contains operation, timestamp, actual URL/query, HTTP method/status, sanitized headers/request/response, duration, correlation, order ID and structured provider IDs.

Excel is a copy of the supplied template with original layout/merged cells. Supplier sample order 414209 and placeholder filenames are cleared. The copy is filled from evidence, and the complete filename manifest is included in summary.json (long Excel lists show the first three names and refer to the manifest). Scenario 1 cancellation is present in JSON/summary even though its original Excel section has no cancellation row. Review all values and filenames before sending. No real evidence package has been generated from live financial actions in this development session.

## UI and API

Existing side panels display supplier room/rate conditions, dates and returned check-in/out times, beds, meals, price/currency, cancellation fines, information, confirmation mode. Room allocation is explicit for multi-room offers. Existing suppliers' local hotel workflow is retained.

Supplier tabs retain existing functionality and add dedicated API logs/errors. Expand a log to inspect sanitized requests/responses/headers/IDs and provider error. Filter logs by supplier, operation, HTTP status, date range, CRM order ID, result and correlation/certification run.

- `GET /api/v1/integration-operations/`: additive telemetry and filters `supplier`, `operation`, `http_status`, `date_from`, `date_to`, `order_id`, `result`, `correlation_id`, `certification_run_id`.
- `GET /api/v1/integration-incidents/`: technical incident journal now requires `integrations.manage`.
- `GET /api/v1/hbpro-certification-runs/?supplier=UUID`: last 100 tenant-scoped summaries, requires `integrations.manage`.
- `GET /api/v1/hbpro-certification-runs/{uuid}/export/`: authorized tenant-scoped ZIP download, no-store, audited.
- `POST /api/v1/orders/{uuid}/services/` and `PUT /api/v1/services/{uuid}/passengers/`: optional HB-only `room_assignments` (`{participant_uuid: "room:0"}`), `main_guest` participant UUID and `special_requests` (max 2000, only if permitted). Existing bodies remain supported. Allocations lock after supplier booking submission.
- Workflow item responses add `supplier_confirmation` with normalized/original status and update time. No raw provider snapshot or credentials exposed.
- New errors distinguish `INVALID_REQUEST`, `SUPPLIER_UNAVAILABLE`, `BOOKING_REJECTED`, `CANCELLATION_FAILED`, `OFFER_EXPIRED`; existing auth/price/rate/unknown handling retained. Original sanitized errors are available only in authorized technical logs.

## MANUAL STEPS FOR OWNER

1. Determine production server's static outbound IP (and reserve/configure static NAT in infrastructure if needed).
2. Send it to HBPRO.
3. Give HBPRO the test URL and test login/password.
4. After approving financial actions, run both real certification scenarios above and wait for supplier confirmation.
5. Review exported JSON, summary and filled Excel copy.
6. Send the certification package to HBPRO; supplied PDF specifies support@hbpro.expert and subject «Сертификация - company name».
7. Wait for certification approval.
8. Obtain production credentials or confirmation that existing credentials were promoted.
9. Add them to server secrets/env and import/verify with `hbpro_credentials` under production configuration.
10. Switch deployed `HBPRO_ENVIRONMENT=production` for web/jobs/cron (same API URL) and restart through normal deployment.
