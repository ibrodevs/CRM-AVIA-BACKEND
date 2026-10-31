"""Documented supplier fixtures only; these tests never contact a live API."""

import io
import json
from urllib.error import HTTPError, URLError

import pytest
from django.core.cache import cache

from crm.models import Person
from integrations.adapters import AdapterContext, AdapterError, get_adapter
from integrations.iway import IwayAdapter
from integrations.models import IntegrationLog
from integrations.onelya import OnelyaAdapter
from integrations.ostrovok import OstrovokAdapter
from integrations.ttbooking import TTBookingAdapter
from orders.models import Order
from services.models import OrderService
from suppliers.models import Supplier, SupplierCredential

pytestmark = pytest.mark.django_db


@pytest.fixture
def supplier(tenant):
    cache.clear()
    return Supplier.objects.create(
        tenant=tenant, name="Contract fixture", service_kinds=["hotel", "transfer", "avia"]
    )


def context(supplier):
    return AdapterContext(tenant_id=supplier.tenant_id, supplier_id=supplier.pk)


def credential(supplier, provider, **config):
    row = SupplierCredential(
        tenant_id=supplier.tenant_id,
        supplier=supplier,
        provider_adapter=provider,
        environment="sandbox",
        status="active",
    )
    row.encrypted_secrets = json.dumps(config)
    row.save()
    return row


def service(supplier, snapshot):
    person = Person.objects.create(tenant_id=supplier.tenant_id, given_name="Fixture", surname="Guest")
    order = Order.objects.create(tenant_id=supplier.tenant_id, client_person=person, number="ORD-FIXTURE")
    return OrderService.objects.create(
        tenant_id=supplier.tenant_id,
        order=order,
        supplier=supplier,
        kind=snapshot.get("kind", "hotel"),
        title="Fixture",
        source="api",
        provider_snapshot=snapshot,
    )


@pytest.fixture
def transport(monkeypatch):
    class Gateway:
        def __init__(self):
            self.responses = []
            self.calls = []

        def open(self, request, timeout):
            self.calls.append(request)
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            body = io.BytesIO(json.dumps(result).encode())
            body.status = 200
            return body

    gateway = Gateway()
    monkeypatch.setattr("integrations.json_provider.build_opener", lambda *a: gateway)
    monkeypatch.setattr("integrations.ttbooking.build_opener", lambda *a: gateway)
    return gateway


@pytest.mark.parametrize(
    "key,kind", [("ostrovok", "hotel"), ("iway", "transfer"), ("onelya", "rail"), ("ttbooking", "avia")]
)
def test_adapter_registration(key, kind):
    assert kind in get_adapter(key).supported_kinds


def test_public_catalog_has_no_credentials(admin_client):
    response = admin_client.get("/api/v1/integration-providers/")
    assert response.status_code == 200
    rows = response.json()["results"]
    assert {r["key"] for r in rows} == {"hotelbook", "aanda", "ttbooking", "ostrovok", "iway", "onelya"}
    assert not any("default" in f for r in rows for f in r["fields"] if f["secret"])


def test_configuration_isolates_tenants_suppliers_and_environments(supplier, other_tenant):
    adapter = OstrovokAdapter()
    row = credential(supplier, "ostrovok", key_id="fixture-id", api_key="private-key")
    adapter._config(context(supplier), row)
    for ctx in [
        AdapterContext(tenant_id=other_tenant.id, supplier_id=supplier.id),
        AdapterContext(tenant_id=supplier.tenant_id, supplier_id=other_tenant.id),
    ]:
        with pytest.raises(AdapterError):
            adapter._config(ctx, row)
    row.environment = "production"
    with pytest.raises(AdapterError, match="Окружение"):
        adapter._config(context(supplier), row)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org",
        "https://key:password@example.org",
        "https://example.org?token=secret",
        "https://example.org/#token",
    ],
)
def test_credentials_reject_unsafe_urls(supplier, url):
    row = credential(supplier, "ostrovok", key_id="fixture-id", api_key="private-key", url=url)
    with pytest.raises(AdapterError):
        OstrovokAdapter()._config(context(supplier), row)


@pytest.mark.parametrize(
    "failure",
    [
        URLError("echo-secret"),
        ValueError("bad"),
        HTTPError("https://fixture.invalid", 503, "bad", {}, None),
        HTTPError("https://fixture.invalid", 302, "redirect", {}, None),
    ],
)
def test_mutation_transport_failure_is_unknown_and_logs_no_secrets(supplier, transport, failure):
    # ValueError exercises a malformed JSON transport response.
    transport.responses = [failure]
    adapter = OstrovokAdapter()
    with pytest.raises(AdapterError) as error:
        adapter._http(
            context(supplier),
            {"url": "https://fixture.invalid"},
            "finish",
            "POST",
            "/finish",
            {"password": "private-password", "first_name": "PRIVATE_NAME"},
            headers={"Authorization": "Bearer private-token"},
            mutation=True,
        )
    assert error.value.category == "booking_unknown"
    assert len(transport.calls) == 1
    logs = json.dumps(list(IntegrationLog.objects.values()), default=str)
    assert all(
        value not in logs for value in ("private-password", "PRIVATE_NAME", "private-token", "echo-secret")
    )


def test_signed_preflight_rejects_price_tampering_and_supplier_scope(supplier, monkeypatch):
    adapter = OstrovokAdapter()
    ctx = context(supplier)
    snapshot = {
        "provider_adapter": "ostrovok",
        "provider_context": {"supplier_id": str(supplier.id), "environment": "sandbox"},
        "price": {"amount": "10", "currency": "EUR"},
    }
    checked = adapter.validated(ctx, snapshot)
    assert adapter.booking_snapshot(ctx, checked) == checked
    checked["price"]["amount"] = "1"
    calls = []

    def revalidate(*args):
        calls.append(True)
        return {"price_changed": True}

    monkeypatch.setattr(adapter, "revalidate", revalidate)
    with pytest.raises(AdapterError, match="Условия изменились"):
        adapter.booking_snapshot(ctx, checked)
    assert calls == [True]
    checked["provider_context"]["supplier_id"] = "other"
    with pytest.raises(AdapterError):
        adapter.booking_snapshot(ctx, checked)
    assert calls == [True]


def test_durable_marker_blocks_repeat_after_timeout(supplier):
    adapter = OstrovokAdapter()
    snapshot = {"provider_adapter": "ostrovok"}
    row = service(supplier, snapshot)
    adapter.reserve(row, snapshot)
    calls = []

    def fail():
        calls.append(True)
        raise AdapterError("UNKNOWN", category="booking_unknown")

    with pytest.raises(AdapterError):
        adapter.send_once(context(supplier), row, snapshot, "finish_sent", fail)
    row.refresh_from_db()
    assert row.provider_snapshot["ostrovok_booking"]["finish_sent"] is True
    assert row.provider_snapshot["ostrovok_booking"]["state"] == "unknown"
    with pytest.raises(AdapterError):
        adapter.send_once(context(supplier), row, row.provider_snapshot, "finish_sent", fail)
    assert len(calls) == 1


def etg_rate():
    return {
        "hotels": [
            {
                "id": "hotel-fixture",
                "rates": [
                    {
                        "book_hash": "book-hash",
                        "match_hash": "match-hash",
                        "room_name": "Double",
                        "meal": "breakfast",
                        "payment_options": {
                            "payment_types": [
                                {
                                    "type": "deposit",
                                    "amount": "120.00",
                                    "currency_code": "EUR",
                                    "is_need_credit_card_data": False,
                                    "is_need_cvc": False,
                                    "cancellation_penalties": {
                                        "free_cancellation_before": "2027-01-01T00:00:00"
                                    },
                                }
                            ]
                        },
                    }
                ],
            }
        ]
    }


def etg_search(adapter, supplier, transport):
    credential(
        supplier, "ostrovok", key_id="fixture-id", api_key="private-key", residency="kg", currency="EUR"
    )
    transport.responses = [{"status": "ok", "data": etg_rate()}]
    return adapter.search(
        context(supplier),
        "hotel",
        {
            "ostrovok": {"region_id": 100},
            "check_in": "2027-03-10",
            "check_out": "2027-03-12",
            "rooms": [{"adults": 1, "childrenAges": [8], "quantity": 1}],
        },
    )[0]


def test_etg_search_uses_supplier_region_and_exact_occupancy(supplier, transport):
    offer = etg_search(OstrovokAdapter(), supplier, transport)
    request = transport.calls[0]
    assert request.full_url.endswith("/search/serp/region/")
    body = json.loads(request.data)
    assert body["region_id"] == 100 and body["guests"] == [{"adults": 1, "children": [8]}]
    assert offer["price"] == {"amount": "120.00", "currency": "EUR"}
    assert "private-key" not in json.dumps(offer)


def test_etg_revalidation_hp_prebook_preserves_booking_fields(supplier, transport):
    adapter = OstrovokAdapter()
    offer = etg_search(adapter, supplier, transport)
    offer.update(main_guest="crm-participant", special_requests="Quiet room", booking_user_ip="192.0.2.1")
    transport.responses = [
        {"status": "ok", "data": {"name": "Fixture hotel", "address": "Address"}},
        {"status": "ok", "data": etg_rate()},
        {"status": "ok", "data": etg_rate()},
    ]
    result = adapter.revalidate(context(supplier), offer)
    assert result["price_changed"] is False and result["terms_changed"] is False
    assert result["snapshot"]["special_requests"] == "Quiet room"
    assert result["snapshot"]["main_guest"] == "crm-participant"
    assert [r.full_url.split("/v3/")[1] for r in transport.calls][-2:] == ["search/hp/", "hotel/prebook/"]


def test_etg_card_payment_not_silently_processed(supplier):
    data = etg_rate()
    data["hotels"][0]["rates"][0]["payment_options"]["payment_types"][0]["is_need_cvc"] = True
    assert (
        OstrovokAdapter()._offers(
            context(supplier),
            {"url": "https://api.worldota.net", "key_id": "fixture"},
            data,
            {"guests": [{"adults": 1}], "checkin": "2027-03-10", "checkout": "2027-03-12"},
        )
        == []
    )


def test_etg_book_sends_real_rooms_and_reconciles_before_cancel(supplier, transport):
    adapter = OstrovokAdapter()
    offer = etg_search(adapter, supplier, transport)
    offer["booking_user_ip"] = "192.0.2.1"
    offer = adapter.validated(context(supplier), offer)
    row = service(supplier, offer)
    payment = etg_rate()["hotels"][0]["rates"][0]["payment_options"]["payment_types"][0]
    transport.responses = [
        {"status": "ok", "data": {"order_id": 10, "payment_types": [payment]}},
        {"status": "ok", "data": {}},
    ]
    result = adapter.book(
        context(supplier),
        {
            "service_id": str(row.id),
            "contact_info": {"email": "fixture@example.invalid", "phone": "+996555000000"},
            "passengers": [
                {
                    "latin_given_name": "ADULT",
                    "latin_surname": "GUEST",
                    "birth_date": "1990-01-01",
                    "room_ref": "room:0",
                    "gender": "male",
                },
                {
                    "latin_given_name": "CHILD",
                    "latin_surname": "GUEST",
                    "birth_date": "2018-06-01",
                    "room_ref": "room:0",
                    "gender": "female",
                },
            ],
        },
    )
    assert result["status"] == "pending"
    sent = json.loads(transport.calls[-1].data)
    assert sent["rooms"][0]["guests"][1]["age"] == 8
    assert sent["partner"]["partner_order_id"] == result["locator"]
    before = len(transport.calls)
    with pytest.raises(AdapterError):
        adapter.book(context(supplier), {"service_id": str(row.id)})
    assert len(transport.calls) == before
    transport.responses = [
        {
            "status": "ok",
            "data": {
                "orders": [
                    {
                        "partner_data": {"order_id": result["locator"]},
                        "order_id": 10,
                        "status": "completed",
                        "is_cancellable": True,
                    }
                ]
            },
        }
    ]
    transport.responses.insert(
        0, {"status": "ok", "data": {"partner_order_id": result["locator"], "percent": 100}}
    )
    assert adapter.retrieve_booking(context(supplier), result["locator"])["status"] == "booked"


def test_iway_auth_cache_is_supplier_scoped(supplier, transport):
    adapter = IwayAdapter()
    row = credential(
        supplier,
        "iway",
        url="https://sandbox.iway.ru/transnextgen",
        login="fixture",
        password="private-password",
        user_id=10,
    )
    ctx = context(supplier)
    _, config = adapter._config(ctx)
    transport.responses = [{"result": {"token": "private-jwt"}}, {"result": []}, {"result": []}]
    adapter._call(ctx, row, config, "prices")
    adapter._call(ctx, row, config, "prices")
    assert len([r for r in transport.calls if "/auth/login" in r.full_url]) == 1
    assert all(
        r.get_header("Authorization") == "Bearer private-jwt"
        for r in transport.calls
        if "/prices" in r.full_url
    )
    second = Supplier.objects.create(tenant_id=supplier.tenant_id, name="Other iway")
    other = credential(
        second, "iway", url=config["url"], login="fixture", password="private-password", user_id=10
    )
    transport.responses = [{"result": {"token": "other-jwt"}}, {"result": []}]
    adapter._call(context(second), other, config, "prices")
    assert len([r for r in transport.calls if "/auth/login" in r.full_url]) == 2
    assert "private-jwt" not in json.dumps(list(IntegrationLog.objects.values()), default=str)


@pytest.mark.parametrize("value", [None, "address text", "91,30", "45,181", "nan,30"])
def test_iway_requires_real_coordinates(value):
    with pytest.raises(AdapterError):
        IwayAdapter._point(value)


@pytest.mark.parametrize(
    "raw,paid,expected",
    [
        (0, 0, "booked"),
        (1, 0, "booked"),
        (2, 0, "booked"),
        (2, 1, "issued"),
        (4, 5, "cancelled"),
        (5, 5, "cancelled"),
        (99, 1, "pending"),
    ],
)
def test_iway_status_is_not_http_success(supplier, raw, paid, expected):
    adapter = IwayAdapter()
    snapshot = {"provider_adapter": "iway", "iway_booking": {"locator": "fixture-locator"}}
    row = service(supplier, snapshot)
    assert (
        adapter._result(
            row, snapshot, {"order_id": 1, "transaction": 2, "status": raw, "payable_status": paid}
        )["status"]
        == expected
    )


def test_iway_lost_response_never_recreates_order(supplier, transport):
    snapshot = {
        "provider_adapter": "iway",
        "iway_booking": {"locator": "fixture-locator", "create_sent": True},
    }
    row = service(supplier, snapshot)
    with pytest.raises(AdapterError) as error:
        IwayAdapter()._retrieve(context(supplier), row, snapshot, snapshot["iway_booking"], None, {})
    assert error.value.category == "booking_unknown"
    assert not transport.calls


def test_ttbooking_async_envelope_and_hash_no_urlencoding(supplier, transport):
    row = credential(supplier, "ttbooking", agency="42", user="7", password_key="private-key", currency="RUB")
    adapter = TTBookingAdapter()
    config = adapter._parse_config(row)
    assert config["url"] == "https://dev.ttbooking.ru/api/scbs/json"
    transport.responses = [{"respond": {"token": "search-token"}}]
    adapter._wire(context(supplier), config, "SEARCHFLIGHTS", {"route": []})
    payload = json.loads(transport.calls[0].data)
    assert set(payload) == {"context", "parameters"}
    import hashlib

    c = payload["context"]
    assert (
        c["hash"]
        == hashlib.md5(f"agency=42&password=private-key&time={c['time']}&user=7".encode()).hexdigest()
    )
    assert adapter.verify_credentials(row)["verified"] is False


def test_ttbooking_unknown_status_stays_pending(supplier):
    snapshot = {"provider_adapter": "ttbooking", "ttbooking_booking": {"locator": "fixture-locator"}}
    row = service(supplier, snapshot)
    result = TTBookingAdapter()._tt_result(
        row, snapshot, {"token": "order-token", "status": "not-known"}, {"status_map": {"1": "booked"}}
    )
    assert result["status"] == "pending" and "tickets" not in result


@pytest.mark.parametrize(
    "method,args",
    [("book", ({},)), ("revalidate", ({},)), ("retrieve_booking", ("locator",)), ("cancel", ("locator",))],
)
def test_onelya_sale_is_blocked_without_current_docs(method, args):
    with pytest.raises(AdapterError) as error:
        getattr(OnelyaAdapter(), method)(AdapterContext(tenant_id="fixture"), *args)
    assert error.value.code == "SUPPLIER_DOCUMENTATION_REQUIRED"


def iway_ready(supplier):
    credential(
        supplier,
        "iway",
        url="https://sandbox.iway.ru/transnextgen",
        login="fixture",
        password="private-password",
        user_id=10,
    )
    adapter = IwayAdapter()
    snapshot = adapter._normalize(
        context(supplier),
        {
            "price_id": 100,
            "price": "20",
            "currency": "EUR",
            "car_class": {"capacity": 3, "title": "Comfort"},
            "start_place": {"type": 1},
            "finish_place": {"type": 1},
        },
        {
            "start_place_point": "42.8,74.6",
            "finish_place_point": "43.0,74.5",
            "pickup_time": "2027-03-10 12:00",
            "origin_address": "Address A",
            "destination_address": "Address B",
        },
    )
    row = service(supplier, adapter.validated(context(supplier), snapshot))
    request = {
        "service_id": str(row.id),
        "contact_info": {"name": "Fixture", "phone": "+996555000000", "email": "test@example.invalid"},
        "passengers": [{"given_name": "Fixture", "surname": "Guest", "birth_date": "1990-01-01"}],
    }
    return adapter, row, request


def test_iway_booking_and_payment_have_independent_confirmed_status(supplier, transport):
    adapter, row, request = iway_ready(supplier)
    transport.responses = [
        {"result": {"token": "private-jwt"}},
        {"result": [{"allow_booking": True, "possible_pickup_time": True, "similar_orders": []}]},
        {"result": [{"order_id": 7, "transaction": 8, "status": 2, "payable_status": 0}]},
    ]
    result = adapter.book(context(supplier), request)
    assert result["status"] == "booked"
    trip = json.loads(transport.calls[-1].data)["trips"][0]
    assert trip["internal_number"] == result["locator"]
    assert trip["passengers_number"] == 1
    assert not any(trip["send_params"].values())
    transport.responses = [
        {"result": {"orders": [{"order_id": 7, "transaction": 8, "status": 2, "payable_status": 0}]}},
        {"result": [{"order_id": 7, "transaction": 8, "status": 2, "payable_status": 1}]},
    ]
    assert adapter.issue(context(supplier), {"locator": result["locator"]})["status"] == "issued"
    assert "/orders/approve/8" in transport.calls[-1].full_url
    assert len([r for r in transport.calls if "/auth/login" in r.full_url]) == 1


@pytest.mark.parametrize(
    "check",
    [
        {"allow_booking": False},
        {"allow_booking": True, "possible_pickup_time": False},
        {"allow_booking": True, "similar_orders": [123]},
    ],
)
def test_iway_checks_time_and_possible_duplicate_before_create(supplier, transport, check):
    adapter, row, request = iway_ready(supplier)
    transport.responses = [{"result": {"token": "private-jwt"}}, {"result": [check]}]
    with pytest.raises(AdapterError):
        adapter.book(context(supplier), request)
    assert not any(r.full_url.split("?")[0].endswith("/orders") for r in transport.calls)
    row.refresh_from_db()
    assert not row.provider_snapshot.get("iway_booking")


def test_iway_create_timeout_preserves_locator_and_blocks_repeat(supplier, transport):
    adapter, row, request = iway_ready(supplier)
    transport.responses = [
        {"result": {"token": "private-jwt"}},
        {"result": [{"allow_booking": True}]},
        URLError("timeout"),
    ]
    with pytest.raises(AdapterError) as error:
        adapter.book(context(supplier), request)
    assert error.value.category == "booking_unknown"
    row.refresh_from_db()
    assert row.provider_snapshot["iway_booking"]["locator"]
    assert row.provider_snapshot["iway_booking"]["create_sent"]
    before = len(transport.calls)
    with pytest.raises(AdapterError):
        adapter.book(context(supplier), request)
    assert len(transport.calls) == before


def test_iway_birthday_boundary_requires_child_seat(supplier, transport):
    adapter, row, request = iway_ready(supplier)
    request["passengers"][0]["birth_date"] = "2015-03-11"
    with pytest.raises(AdapterError) as error:
        adapter.book(context(supplier), request)
    assert error.value.code == "CHILD_SEAT_REQUIRED"
    assert not transport.calls


def test_iway_expired_token_is_evicted_without_replaying_mutation(supplier, transport):
    adapter = IwayAdapter()
    row = credential(
        supplier,
        "iway",
        url="https://sandbox.iway.ru/transnextgen",
        login="fixture",
        password="fixture",
        user_id=10,
    )
    ctx = context(supplier)
    _, config = adapter._config(ctx)
    cache.set(adapter._token_key(ctx, row, config), "stale-token")
    transport.responses = [HTTPError(config["url"], 401, "expired", {}, None)]
    with pytest.raises(AdapterError):
        adapter._call(ctx, row, config, "orders", "POST", {}, mutation=True)
    assert len(transport.calls) == 1
    assert cache.get(adapter._token_key(ctx, row, config)) is None


def test_ttbooking_documented_empty_search_message_is_not_invalid_response(supplier, transport):
    credential(
        supplier,
        "ttbooking",
        agency="fixture-agency",
        user="fixture-user",
        password_key="fixture-key",
        currency="RUB",
    )
    transport.responses = [
        {
            "respond": {
                "token": "",
                "messages": {"message": [{"type": "MESSAGE", "code": "", "message": "No matching variants"}]},
            }
        }
    ]
    assert (
        TTBookingAdapter().search(
            context(supplier), "avia", {"origin": "FRU", "destination": "IST", "date": "2027-03-10"}
        )
        == []
    )
    assert len(transport.calls) == 1


def test_ttbooking_error_message_is_not_empty_search():
    with pytest.raises(AdapterError) as error:
        TTBookingAdapter()._respond(
            {"respond": {"messages": {"message": [{"type": "ERROR", "message": "private echo"}]}}}
        )
    assert error.value.code == "TTBOOKING_REJECTED"
    assert "private echo" not in str(error.value)


def test_etg_content_cache_reuses_and_is_supplier_scoped(supplier, transport):
    adapter = OstrovokAdapter()
    config = {"url": "https://api.worldota.net", "key_id": "fixture", "api_key": "private-key"}
    transport.responses = [
        {"status": "ok", "data": {"name": "Real hotel", "address": "Address", "private": "do not cache"}}
    ]
    ctx = context(supplier)
    assert adapter._hotel_content(ctx, config, "hotel-id", fetch=True)["name"] == "Real hotel"
    assert adapter._hotel_content(ctx, config, "hotel-id", fetch=True)["name"] == "Real hotel"
    assert len(transport.calls) == 1
    second = Supplier.objects.create(tenant_id=supplier.tenant_id, name="Other ETG")
    assert adapter._hotel_content(context(second), config, "hotel-id") == {}


def test_ttbooking_confirmed_statuses_cannot_be_overridden_by_config(supplier):
    snapshot = {"provider_adapter": "ttbooking", "ttbooking_booking": {"locator": "fixture-locator"}}
    row = service(supplier, snapshot)
    result = TTBookingAdapter()._tt_result(
        row, snapshot, {"bookingFile": {"status": "NEW"}}, {"status_map": {"NEW": "issued"}}
    )
    assert result["status"] == "booked"
