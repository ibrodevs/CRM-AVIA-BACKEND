# HBPRO state regions

Geography is provider data, scoped by tenant, supplier credentials and their
fingerprint in the existing shared dictionary cache. No CRM geography tables,
identifiers or migrations are involved.

## Resolving a city

Existing `location: "City"` and `hotelbook.city_id` remain supported. Textual
hints can disambiguate same-name cities:

```json
{
  "location": "City",
  "region": "Region name",
  "country_code": "KG"
}
```

`country` also accepts the localized country name. A qualified location such as
`"City, Region, Country"`, `"City, Region"` or `"City, KG"` is supported. An exact
city name containing commas takes precedence. Numeric supplier hints belong
exclusively under `hotelbook.country_id` and `hotelbook.state_region_id`; top-level
numeric CRM IDs are not interpreted as HBPRO IDs. Citizenship is independent of
the destination country.

Active cities are joined to active regions by `stateRegionId`. A region with a
different `countryId` from the city or expected destination country is ignored.
A missing, archived or unknown region does not invalidate an otherwise unique
legacy city. Insufficient hints still return `LOCATION_REQUIRED`, rather than
arbitrarily selecting a same-name city.

## API calls and cache

The only new HBPRO endpoint is
`GET /api/v1/{locale}/dict/gateway/geo/state_regions` (existing pagination:
`itemsPerPage=5000&page=N`). It is loaded through `_dictionary()` only during city
resolution when matching cities expose `stateRegionId`, and only on a cache miss.
The existing 24-hour TTL, authorization, shared cache and sanitized IntegrationLog
apply. Textual country hints can use the existing `geo/countries` dictionary,
which follows the same cache rules. Hotel-ID and explicit-city-ID search paths do
not start geography dictionary requests.

Search offer `itinerary.city`, `itinerary.region`, and `itinerary.country` expose
only names available in this supplier's cache. Display enrichment never fetches
missing dictionaries; unavailable names stay absent (legacy `city` stays empty).
Indexes are built once per search. Names are retained in the provider snapshot
and preserved through revalidation, including after cache eviction.

There are no new geography requests in revalidate, createOrder, booking, status or
cancel. Dictionary cache misses do not force a new login while the token remains
valid. Credentials and tokens use the existing sanitization path, verified by
IntegrationLog regression tests.
