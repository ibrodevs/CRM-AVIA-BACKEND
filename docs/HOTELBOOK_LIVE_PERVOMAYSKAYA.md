# Local live test: Мастер-отель Первомайская 131687

Verified 2026-10-07, stay 2026-10-21 through 2026-10-23; one room, one synthetic adult.
Corrected sandbox allowlist ID from 31687 to 131687. Historical failed-ID audit
in HOTELBOOK_LIVE_TEST_HOTELS.md remains unchanged.

## Read-only diagnostic

Command run from backend:

```sh
uv run python manage.py hotelbook_live_test --settings=config.settings.dev --hotel-id 131687 --check-in 2026-10-21 --check-out 2026-10-23
```

- credentials_source=local_env; real login=OK.
- Search returned 35 offers for Мастер-Отель Первомайская, including explicitly labelled test rooms.
- First printed offer: 4047 RUB; other offers included 1964 RUB.
- First revalidate: valid, terms_changed=true (fines).
- Repeat revalidate: valid, terms_changed=false.
- Synthetic adult guest mapping: OK, one room; read-only command sent no booking.

## Full CRM API / worker flow

Local isolated tenant hotelbook-live-local and existing encrypted sandbox
SupplierCredential were used. Its credentials were refreshed exclusively from the
local .env; no production credentials or routing changed. The real CRM search
returned 35 offers; an ONLINE available offer was selected. Hotel name was checked
case-insensitively against Мастер-отель Первомайская.

Search -> revalidate -> new test Order with participant -> attach offer -> workflow
preflight (OK, no warnings) -> book (CONFIRMED) -> inquiry (booked) -> cancel -> final
inquiry (CANCELED). Each mutation used the existing CRM endpoints/background worker.

| Record | Value |
|---|---|
| CRM order | `5593f1b0-0739-4372-afe4-252a50282d49` (`ORD-000005`) |
| CRM service | `779a1a55-ba81-41de-8e5f-3e710eee0335` |
| Workflow | `abb35017-611b-4ada-9333-2489081c169d` |
| HB order | `2351605` |
| HB item | `b7850c23-377d-4d83-88d0-bf5d922e643c` |
| Selected price | 1333 RUB |
| Meal | Без питания |
| Cancellation | 2026-10-18T14:00:00+03:00: 682 RUB; Штраф при незаезде или аннуляции в день заезда и позднее - 100%. |
| Final HB | CANCELED |
| Final CRM | service/workflow cancelled; item compensated |

IDs are supplier-specific provider_snapshot.hotelbook_booking values. Audited
price/currency, meal and cancellation rules match the selected/revalidated offer
and OrderService. No active/unresolved test bookings remain in this local tenant.
Sanitized logs contain neither configured login nor password.

No API errors occurred. First details updated cancellation fines normally; the
second revalidation was stable. First read-only offer and selected ONLINE offer
are different tariffs, accounting for their different prices.

Validation: 112 backend tests passed; ruff and git diff --check passed. The
read-only diagnostic regression test now covers 131687, while full CRM fixture
coverage automatically includes the corrected ID. .env remains gitignored.
All changes remain local; no commit or push was made for this run.
