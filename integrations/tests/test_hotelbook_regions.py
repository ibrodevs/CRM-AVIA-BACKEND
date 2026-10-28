"""HBPRO geography stays inside supplier dictionaries and cached offer metadata."""

# ruff: noqa: F811

import json
from copy import deepcopy
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from django.core.cache import cache
from django.core.cache.backends.db import DatabaseCache

from integrations.adapters import AdapterError
from integrations.hotelbook import HotelbookAdapter
from integrations.models import IntegrationLog
from integrations.tests.test_hotelbook import (  # noqa: F401
    Gateway,
    Response,
    context,
    criteria,
    hb_supplier,
    jobs,
    post,
    ready_booking,
    start,
)
from suppliers.models import Supplier, SupplierCredential

pytestmark = pytest.mark.django_db


class GeoGateway(Gateway):
    def __init__(self):
        super().__init__()
        self.region_pages = None
        self.countries = [
            {"id": 77, "name": "Kyrgyzstan", "alpha2": "KG", "trash": False},
            {"id": 88, "name": "Kazakhstan", "alpha2": "KZ", "trash": False},
        ]
        self.regions = [
            {"id": 7, "name": "Northern Region", "countryId": 77, "trash": False},
            {"id": 8, "name": "Southern Region", "countryId": 77, "trash": False},
            {"id": 9, "name": "Northern Region", "countryId": 88, "trash": False},
            {"id": 10, "name": "Archived Region", "countryId": 77, "trash": True},
        ]
        self.cities = [
            {"id": 42, "name": "Fixture City", "countryId": 77, "stateRegionId": 7, "trash": False},
            {"id": 43, "name": "Fixture City", "countryId": 77, "stateRegionId": 8, "trash": False},
            {"id": 44, "name": "Fixture City", "countryId": 88, "stateRegionId": 9, "trash": False},
            {"id": 45, "name": "Fixture City", "countryId": 77, "stateRegionId": 7, "trash": True},
        ]

    def open(self, request, timeout):
        url = urlparse(request.full_url)
        resource = url.path.rsplit("/", 1)[-1]
        if "/dict/gateway/geo/" not in url.path:
            return super().open(request, timeout)
        assert request.get_header("Authorization") == "Bearer fixture-jwt"
        assert request.method == "GET"
        self.calls.append((request.method, url.path, parse_qs(url.query), None, dict(request.header_items())))
        if resource == "state_regions" and self.region_pages:
            return Response(self.region_pages.pop(0))
        return Response(
            deepcopy(
                {"cities": self.cities, "state_regions": self.regions, "countries": self.countries}[resource]
            )
        )


@pytest.fixture
def gateway(monkeypatch):
    cache.clear()
    gateway = GeoGateway()
    monkeypatch.setattr("integrations.hotelbook.build_opener", lambda *args: gateway)
    monkeypatch.setattr(HotelbookAdapter, "poll_interval", 0)
    return gateway


def setup_adapter(tenant, supplier):
    adapter = HotelbookAdapter()
    ctx = context(tenant, supplier)
    credential, config = adapter._config(ctx)
    return adapter, ctx, credential, config


def paths(gateway):
    return [call[1] for call in gateway.calls]


def test_regions_use_dictionary_auth_pagination_logging_and_24h_ttl(tenant, hb_supplier, gateway):
    adapter, ctx, credential, config = setup_adapter(tenant, hb_supplier)
    with patch.object(cache, "set", wraps=cache.set) as store:
        assert adapter._dictionary(ctx, config, "geo/state_regions") == gateway.regions
    region_call = next(call for call in gateway.calls if call[1].endswith("/geo/state_regions"))
    assert region_call[1] == "/api/v1/ru/dict/gateway/geo/state_regions"
    assert region_call[2] == {"itemsPerPage": ["5000"], "page": ["1"]}
    writes = [call for call in store.call_args_list if "dict:geo/state_regions:" in call.args[0]]
    assert len(writes) == 1 and writes[0].args[2] == 86400
    assert (
        IntegrationLog.objects.filter(
            supplier=hb_supplier, operation="dictionary", url__contains="state_regions"
        ).count()
        == 1
    )
    before = len(gateway.calls)
    assert HotelbookAdapter()._dictionary(ctx, config, "geo/state_regions") == gateway.regions
    assert len(gateway.calls) == before


@pytest.mark.parametrize(
    "hints,expected",
    [
        ({"region": "Southern Region"}, 43),
        ({"region": "Northern Region", "country_code": "KG"}, 42),
        ({"country": "Kazakhstan"}, 44),
        ({"hotelbook": {"state_region_id": 7, "country_id": 77}}, 42),
    ],
)
def test_same_city_names_resolve_using_region_and_country(tenant, hb_supplier, gateway, hints, expected):
    adapter, ctx, _, config = setup_adapter(tenant, hb_supplier)
    assert adapter._resolve_city(ctx, config, hints, "Fixture City")["id"] == expected
    assert paths(gateway).count("/api/v1/ru/dict/gateway/geo/state_regions") == 1


@pytest.mark.parametrize(
    "location",
    ["Fixture City, Southern Region", "Fixture City, Northern Region, KG", "Fixture City, Kazakhstan"],
)
def test_qualified_location_text(tenant, hb_supplier, gateway, location):
    adapter, ctx, _, config = setup_adapter(tenant, hb_supplier)
    expected = 43 if "Southern" in location else 44 if "Kazakhstan" in location else 42
    assert adapter._resolve_city(ctx, config, {}, location)["id"] == expected


def test_ambiguity_remains_when_region_and_country_are_insufficient(tenant, hb_supplier, gateway):
    adapter, ctx, _, config = setup_adapter(tenant, hb_supplier)
    with pytest.raises(AdapterError, match="регион или страну") as error:
        adapter._resolve_city(ctx, config, {"region": "Northern Region"}, "Fixture City")
    assert error.value.code == "LOCATION_REQUIRED"


def test_trash_and_cross_country_regions_do_not_resolve_a_city(tenant, hb_supplier, gateway):
    adapter, ctx, _, config = setup_adapter(tenant, hb_supplier)
    gateway.cities.append({"id": 46, "name": "Fixture City", "countryId": 77, "stateRegionId": 10})
    with pytest.raises(AdapterError):
        adapter._resolve_city(ctx, config, {"region": "Archived Region"}, "Fixture City")
    assert adapter._city_region({"countryId": 77, "stateRegionId": 9}, gateway.regions) is None
    assert adapter._city_region(gateway.cities[0], gateway.regions, expected_country=88) is None
    assert adapter._city_region(gateway.cities[-1], gateway.regions) is None


@pytest.mark.parametrize("region_id", [None, 999, 10, 9])
def test_legacy_or_missing_region_preserves_unique_city_search(tenant, hb_supplier, gateway, region_id):
    adapter, ctx, _, config = setup_adapter(tenant, hb_supplier)
    gateway.cities = [{"id": 42, "name": "Fixture City", "countryId": 77}]
    if region_id is not None:
        gateway.cities[0]["stateRegionId"] = region_id
    assert adapter._resolve_city(ctx, config, {}, "Fixture City")["id"] == 42
    assert ("/api/v1/ru/dict/gateway/geo/state_regions" in paths(gateway)) == (region_id is not None)


def test_search_enriches_names_and_revalidation_preserves_them_without_dictionary_calls(
    tenant, hb_supplier, gateway
):
    adapter, ctx, _, _ = setup_adapter(tenant, hb_supplier)
    snapshot = adapter.search(
        ctx,
        "hotel",
        {**criteria(), "location": "Fixture City", "region": "Northern Region", "country_code": "KG"},
    )[0]
    expected = {"city": "Fixture City", "region": "Northern Region", "country": "Kyrgyzstan"}
    assert snapshot["hotelbook"]["location"] == expected
    assert all(snapshot["itinerary"][key] == value for key, value in expected.items())
    assert "stateRegionId" not in snapshot["itinerary"] and "region_id" not in snapshot["itinerary"]
    before = len(gateway.calls)
    adapter.search(
        ctx,
        "hotel",
        {**criteria(), "location": "Fixture City", "region": "Northern Region", "country_code": "KG"},
    )
    assert not any("/dict/" in call[1] or call[1].endswith("/login") for call in gateway.calls[before:])
    cache.clear()
    before = len(gateway.calls)
    result = adapter.revalidate(ctx, snapshot)
    assert result["snapshot"]["hotelbook"]["location"] == expected
    assert not any("/dict/" in call[1] for call in gateway.calls[before:])


def test_hotel_id_search_does_not_fetch_geo_dictionaries_to_enrich_display(tenant, hb_supplier, gateway):
    adapter, ctx, _, _ = setup_adapter(tenant, hb_supplier)
    snapshot = adapter.search(ctx, "hotel", criteria())[0]
    assert snapshot["itinerary"]["city"] == ""
    assert not any(call[1].endswith(("/geo/cities", "/geo/state_regions")) for call in gateway.calls)


def test_dictionary_isolated_by_supplier_credentials_and_not_shared_with_other_suppliers(
    tenant, hb_supplier, gateway
):
    adapter, ctx, _, config = setup_adapter(tenant, hb_supplier)
    adapter._dictionary(ctx, config, "geo/state_regions")
    other = Supplier.objects.create(tenant=tenant, name="Other HBPRO", service_kinds=["hotel"])
    SupplierCredential.objects.create(
        tenant=tenant,
        supplier=other,
        provider_adapter="hotelbook",
        status="active",
        encrypted_secrets=json.dumps(config),
    )
    gateway.regions = [{"id": 7, "name": "Different supplier region", "countryId": 77}]
    other_ctx = context(tenant, other)
    assert adapter._dictionary(other_ctx, config, "geo/state_regions") == gateway.regions
    assert adapter._dictionary(ctx, config, "geo/state_regions")[0]["name"] == "Northern Region"
    assert paths(gateway).count("/api/v1/ru/dict/gateway/geo/state_regions") == 2


def test_shared_cache_reuses_regions_across_adapter_instances(tenant, hb_supplier, gateway, settings):
    settings.CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "crm_shared_cache"}
    }
    adapter, ctx, credential, config = setup_adapter(tenant, hb_supplier)
    adapter._dictionary(ctx, config, "geo/state_regions")
    independent = DatabaseCache("crm_shared_cache", {})
    key = adapter._cache_key(credential, config, "dict:geo/state_regions:{}")
    assert independent.get(key) == gateway.regions
    before = len(gateway.calls)
    assert HotelbookAdapter()._dictionary(ctx, config, "geo/state_regions") == gateway.regions
    assert len(gateway.calls) == before
    independent.clear()


def test_region_cache_expiry_fetches_only_dictionary_without_repeat_login(tenant, hb_supplier, gateway):
    adapter, ctx, credential, config = setup_adapter(tenant, hb_supplier)
    adapter._dictionary(ctx, config, "geo/state_regions")
    cache.delete(adapter._cache_key(credential, config, "dict:geo/state_regions:{}"))
    adapter._dictionary(ctx, config, "geo/state_regions")
    assert paths(gateway).count("/api/v1/ru/dict/gateway/geo/state_regions") == 2
    assert paths(gateway).count("/api/v1/ru/gateway/login") == 1


def test_new_dictionary_logs_are_sanitized_even_when_supplier_echoes_credentials(
    tenant, hb_supplier, gateway
):
    adapter, ctx, _, config = setup_adapter(tenant, hb_supplier)
    gateway.regions[0]["diagnostic"] = "fixture-login fixture-password fixture-jwt Bearer fixture-jwt"
    adapter._dictionary(ctx, config, "geo/state_regions")
    serialized = json.dumps(list(IntegrationLog.objects.values()), default=str)
    for secret in ("fixture-login", "fixture-password", "fixture-jwt"):
        assert secret not in serialized
    assert "[REDACTED]" in serialized


def test_booking_status_cancel_keep_no_dictionary_lookup_contract(admin_client, tenant, hb_supplier, gateway):
    workflow, service = ready_booking(admin_client, tenant, hb_supplier)
    gateway.calls.clear()
    cache.clear()
    result = start(admin_client, workflow)
    assert result["items"][0]["status"] == "booked"
    post(
        admin_client,
        f"booking-workflows/{workflow['id']}/status-inquiry/",
        {"item": result["items"][0]["id"]},
    )
    jobs()
    post(admin_client, f"booking-workflows/{workflow['id']}/cancel/", key="cancel-regions")
    jobs()
    assert not any("/dict/" in path for path in paths(gateway))


def test_state_region_pagination_is_cached_after_all_pages(tenant, hb_supplier, gateway):
    adapter, ctx, _, config = setup_adapter(tenant, hb_supplier)
    gateway.region_pages = [
        [{"id": i, "name": f"Region {i}", "countryId": 77} for i in range(5000)],
        [{"id": 5000, "name": "Last", "countryId": 77}],
    ]
    assert len(adapter._dictionary(ctx, config, "geo/state_regions")) == 5001
    calls = [call for call in gateway.calls if call[1].endswith("/geo/state_regions")]
    assert [call[2]["page"] for call in calls] == [["1"], ["2"]]
    before = len(gateway.calls)
    assert len(adapter._dictionary(ctx, config, "geo/state_regions")) == 5001
    assert len(gateway.calls) == before


def test_legacy_city_with_comma_in_its_name_keeps_exact_matching(tenant, hb_supplier, gateway):
    adapter, ctx, _, config = setup_adapter(tenant, hb_supplier)
    gateway.cities = [{"id": 42, "name": "City, District", "countryId": 77}]
    assert adapter._resolve_city(ctx, config, {}, "City, District")["id"] == 42
    assert not any(path.endswith("/geo/state_regions") for path in paths(gateway))


def test_display_metadata_cannot_use_another_supplier_cache(tenant, hb_supplier, gateway):
    adapter, ctx, credential, config = setup_adapter(tenant, hb_supplier)
    adapter._resolve_city(ctx, config, {"region": "Southern Region"}, "Fixture City")
    own = adapter._cached_geography(credential, config)
    assert adapter._cached_location(own, {"cityId": 42})["region"] == "Northern Region"
    other = Supplier.objects.create(tenant=tenant, name="Independent supplier", service_kinds=["hotel"])
    other_credential = SupplierCredential.objects.create(
        tenant=tenant,
        supplier=other,
        provider_adapter="hotelbook",
        status="active",
        encrypted_secrets=json.dumps(config),
    )
    foreign = adapter._cached_geography(other_credential, config)
    assert adapter._cached_location(foreign, {"cityId": 42}) == {}


def test_generic_numeric_geo_ids_are_not_treated_as_hbpro_ids(tenant, hb_supplier, gateway):
    adapter, ctx, _, config = setup_adapter(tenant, hb_supplier)
    with pytest.raises(AdapterError) as error:
        adapter._resolve_city(ctx, config, {"country_id": 88, "state_region_id": 9}, "Fixture City")
    assert error.value.code == "LOCATION_REQUIRED"
