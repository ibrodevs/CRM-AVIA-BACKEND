# Local live CRM flow: Casa Manzella

Verified 2026-10-07 against the real HB Pro test API. Only hotel `1251539`
was searched/booked. Stay: 2026-10-21 through 2026-10-23, one room, one
synthetic adult. No production hotel or payment endpoint was used.

## Setup and results

The isolated local organization is `hotelbook-live-local`, supplier `Hotelbook`.
Its hotel search priority selects this supplier alone. Backend `.env` credentials
were imported into the existing encrypted `SupplierCredential` (`hotelbook`,
`sandbox`) and verified via real login. No secrets are committed or exposed to UI.
Existing local migrations were applied before testing; no new migration was needed.

The existing hotel picker in the UI returned four HB offers. The price and
cancellation conditions were revalidated through the UI, then the offer was
attached to local test order `ORD-000001` through the existing OrderServices API.
The resulting service has `source=api`, adapter `hotelbook`, one ServicePassenger,
and vendor data in `provider_snapshot.hotelbook`.

The existing UI booking wizard ran preflight successfully and queued `booking.run`.
The worker created the HB order and booked the test room. A CRM status inquiry
retrieved the order and booking item from HB. Cancellation was submitted through
the existing workflow cancel endpoint/worker, followed by a second CRM inquiry.

| Record | ID / result |
|---|---|
| CRM order | `3ed7680f-6ffa-42bf-a1f3-f9cbd744b502` (`ORD-000001`) |
| CRM service | `7f637ae7-f0b4-433e-8de2-1a7fe8cd8f0c` |
| Successful workflow | `e117a192-ee29-46cf-a191-6550713d60a8` |
| HB order | `2351491` |
| HB item | `6fd551b0-b870-46eb-9a91-68420d9b18f4` |
| Confirmed state | HB `CONFIRMED`; CRM service `booked`; workflow `completed` |
| Final state after inquiry | HB `CANCELED`; CRM service/workflow `cancelled`; item `compensated` |

HB IDs and the original booking/retrieval responses remain in
`provider_snapshot.hotelbook_booking`; the existing CRM workflow locator stays a
32-character CRM reference. HB create-order/book/cancel returned HTTP 201;
order/item retrieval returned HTTP 200.

## Bugs found and fixed

1. Free selection attachment ignored `_backendOfferId`, creating a manual copy
   instead of an API-backed service. It now preserves all existing offer ID aliases,
   revalidates offers before attachment, and requires review if price/terms change.
   The incorrect initial local manual copy was archived/cancelled; it was not booked.
2. HB `customer.name` is a company field and must not be sent for `PRIVATE` customers.
   The initial create-order request returned `422 ERR028#4` with this validation error.
   HB order lookup by that client reference confirmed that no order existed before
   the rejected local marker was cleared for an explicit new workflow. The failed
   workflow and integration logs remain for audit. Unknown mutations were not retried.
3. Inquiry rejected an already compensated hotel item, preventing verification after
   successful cancellation. Hotel inquiries now accept compensated items; other
   service kinds retain the existing status restrictions and permission checks.
4. Hotel descriptions "Без питания" were incorrectly displayed as included breakfast;
   and free-selection totals were labelled USD regardless of offer currency.
   Breakfast now requires explicit meal data, actual meal descriptions are shown,
   and monetary totals preserve each currency separately.

Regression coverage checks private/legal customers, book/cancel/post-cancel inquiry,
API offer attachment, the manual service path, mixed currencies and no-meal offers.
Full backend integration/regression suite: 110 passed; frontend suite: 355 passed.

No credential routing or environment fallback was added to production. For a fresh
manual run use this isolated local tenant, a new offer/order and the ordinary UI.
Do not reopen or rebook the cancelled offer. No email, chat message or proposal was
sent to any person during this test.
