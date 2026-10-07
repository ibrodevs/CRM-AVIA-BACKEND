# Local live CRM flow: remaining Hotelbook test hotels

Verified 2026-10-07 using real HTTP CRM endpoints and the existing background
worker against HB Pro. Stay: 2026-10-21 through 2026-10-23, one room, one
synthetic adult. Only the four requested test IDs were searched.

The isolated local tenant is `hotelbook-live-local`. The existing encrypted
sandbox SupplierCredential selects Hotelbook; no production credential routing
was changed. No credentials or tokens are included in this report.

For each available hotel, the sequence was search -> results -> revalidate ->
new test Order with participant -> attach ServiceOffer -> preflight -> book ->
status inquiry -> cancel -> final status inquiry. These runs used the existing
CRM API and worker; the browser UI was not exercised again. Each booking was
cancelled and verified before proceeding to the next hotel.

## Results

| Hotel / HB hotel ID | Search | Book | Cancel | Final HB / CRM | HB order |
|---|---|---|---|---|---|
| Hilton Cologne / `1251542` | 6 offers | CONFIRMED | OK | CANCELED / cancelled | `2351520` |
| Haus Mooren / `1251543` | 2 offers | CONFIRMED | OK | CANCELED / cancelled | `2351528` |
| Palazzo Victoria / `1251563` | 4 offers | CONFIRMED | OK | CANCELED / cancelled | `2351530` |
| Мастер-отель Первомайская / `31687` | HTTP 400 ERR001#4 | Not attempted | Not needed | No booking | — |

Confirmed CRM services were `booked`; their workflows completed. After the final
inquiry all three services/workflows are `cancelled` and workflow items are
`compensated`. HB order/item IDs remained unchanged across booking and cancellation
in `provider_snapshot.hotelbook_booking`. Audit of all API-backed Hotelbook services
in the isolated tenant found no active or unresolved test reservations.

## Saved supplier data

Prices below are the HB supplier price, not CRM markup. Decimal amount/currency
match revalidated offers and OrderService.supplier_cost/currency. Meal descriptions
and cancellation rules remain in provider_snapshot.itinerary/fare. Every booked
offer below returned "Без питания".

| Hotel | Price | Cancellation deadline / fine |
|---|---|---|
| Hilton Cologne | 1024680 RUB | 2026-10-20T15:00:00Z: 1024680 RUB; Штраф при незаезде или аннуляции в день заезда и позднее - 100%. |
| Haus Mooren | 1035130 RUB | 2026-10-20T14:00:00Z: 1035080 RUB; Штраф при незаезде или аннуляции в день заезда и позднее - 100%. |
| Palazzo Victoria | 1683 RUB | 2026-10-20T15:00:00Z: 857 RUB; Штраф при незаезде или аннуляции в день заезда и позднее - 100%. |

HB details updated cancellation terms on the first revalidation (`terms_changed=true`);
prices remained valid. Preflight then succeeded without warnings. HB fine amounts
are preserved independently of the stay price: Haus Mooren fine is 1,035,080 RUB
versus price 1,035,130 RUB; Palazzo Victoria fine is 857 RUB versus price 1,683 RUB.
These are actual supplier values, not computed CRM penalties.

## Local audit references

### Hilton Cologne (`1251542`)

- CRM order: `b9f05442-f8a2-4bee-a84c-590cf119935b` (`ORD-000002`)
- Search: `fef2fabf-a332-42da-8253-16e1449454a3`; offer: `f3fe6e59-648e-4721-ab02-2d2c300b3be3`
- Service: `4f1a48b8-e913-4ce0-a620-21e681481bb2`
- Workflow: `64755e27-c78a-4944-8633-c981a29898e5`; workflow item: `dd54850e-9e76-466d-afab-61abdaaea8dc`
- HB order: `2351520`; HB item: `ea3ab8de-65b0-4f51-a5a9-9d2f2f16653f`

### Haus Mooren (`1251543`)

- CRM order: `58414ee0-5ccc-435f-a913-2e656b8c46d3` (`ORD-000003`)
- Search: `8dfaa2d8-df16-4ab6-afd4-dac27a9355e8`; offer: `6d46773f-78fc-4a0a-be56-659b9f75aab8`
- Service: `2e4b55cb-2e36-4376-bb79-a5c66ceaecbe`
- Workflow: `47e9c932-01c5-4855-8ee8-409a32141d3a`; workflow item: `0f594ceb-6c87-4c2d-b293-da46466b791f`
- HB order: `2351528`; HB item: `de61ecec-48b1-4474-873a-e1086781e36a`

### Palazzo Victoria (`1251563`)

- CRM order: `dd09706f-a211-4b24-b70c-ba72f2083dcf` (`ORD-000004`)
- Search: `3934b6c2-c0ae-43a2-9fd4-e4d65ffe81b5`; offer: `24c50f36-e610-4893-9305-ecd9835713a3`
- Service: `5c898731-04d0-4c9e-b172-c00ee585e844`
- Workflow: `91528ce4-91ca-4c8d-b6a7-54e8be7c3495`; workflow item: `3837972d-618b-4c53-b2ef-6db0e33af521`
- HB order: `2351530`; HB item: `e5aaf660-3934-4a03-a58c-f9209ee62ae0`

## Blocked test hotel 31687

HB search returned HTTP 400, `ERR001#4`: "Отели с указанными идентификаторами
не найдены". A second read-only search reproduced it. The live supplier dictionary
GET `/api/v1/ru/dict/gateway/hotel/hotels/31687` instead identifies
`TULIP INN AMSTERDAM ART`, with `trash=true`. Consequently this ID cannot currently
verify Мастер-отель Первомайская. No alternate hotel was substituted and no order
or booking was created for 31687. HB must supply/restore the correct active test
hotel ID before its full scenario can proceed.

Initial failed CRM search: `67364cbe-6ded-4c74-89bb-aad89e6293b2`.

The adapter previously classified this response as generic `PROVIDER_ERROR`. It
now maps search `ERR001#4` to existing `SEARCH_CRITERIA_INVALID`, category
`validation`, with a static safe message and no automatic retry. The regression
test exercises the CRM search worker and confirms failed provider run, zero offers,
and no OrderService/booking item or HB create-order/book calls. Other error mappings
remain unchanged.

Validation: 111 backend integration/booking/services/suppliers/orders/groups tests
passed. Sanitized integration logs were checked against configured login/password;
neither appeared. Final live CRM search verifies the new normalized error.
