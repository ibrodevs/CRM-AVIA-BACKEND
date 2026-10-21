# STEP 1 — Audit (2026-10-21)

Sources: supplied PDF (2 pages), unchanged Excel template, official OpenAPI downloaded 2026-10-21. Both repositories initially clean; no AGENTS.md found. API URL remains https://api.hbpro.expert as confirmed by owner; old PDF hostname does not override that instruction.

| Функция | Уже реализована | Частично | Нет | Где находится | Что нужно изменить |
|---|---|---|---|---|---|
| Search/hotel/city/citizenship | | да | | integrations/hotelbook.py | Add certification hotel 1251201; input validation |
| Results pagination/details/reprice | да | | | integrations/hotelbook.py | Keep; capture sanitized bodies |
| Create order/book | да | | | integrations/hotelbook.py, booking/job_handlers.py | Keep durable mutation guards; validate contact |
| ONLINE/ON_REQUEST | | да | | _booking_result, status_inquiry | Explicit mapping + automatic sync |
| Multi-room/guests | | да | | _guests, ServicePassenger.room_ref | Actual two-room tests, room/item structure |
| Order info/item info/cancel | да | | | retrieve_booking/cancel | Date-based searchOrders; cancellation lock |
| Credentials/connection | | да | | SupplierCredential, suppliers/job_handlers.py | Environment/import command; fresh verification, latency |
| Logs/incidents | | да | | IntegrationLog, integrations/views.py | Bodies, headers, IDs, filters, tabs |
| Error mapping | | да | | _error | Validation/unavailable/cancellation, sanitized original error |
| Background sync | | | да | common.jobs/scheduled | Bounded read-only polling with locks/backoff |
| Hotel side panels | да | | | HotelsPage/StackPanel | Preserve existing drawer; display supplier conditions |
| Certification/export | | | да | — | Staged sandbox command, JSON archive, Excel copy |
| Tests/security | | да | | integrations/tests/test_hotelbook*.py | Extend fixtures, permissions, masking |

PDF scenario 1: hotel 1251201, one room/one adult/3 nights, ON_REQUEST; supplier manually confirms 09:00–18:00 Moscow within 30 minutes; then cancel. Scenario 2: same hotel, two rooms/two adults of different genders per room/2 nights, ONLINE, one booking call; then cancel. JSON accepted. Names: <scenario>.<letter>.<method>.json. B17 contains sample Order ID 414209, not evidence. Original template must remain unchanged.

No real reservations, cancellations, supplier messages, IP/DNS/firewall changes executed during development.
