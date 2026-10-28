import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.db import close_old_connections, connection
from django.utils import timezone

from integrations.aanda import AandaAdapter
from integrations.aanda_xml import endpoint
from integrations.adapters import AdapterContext, AdapterError, get_adapter
from integrations.models import IntegrationIncident, SupplierOperationLease
from integrations.scheduled_tasks import queue_supplier_status_sync
from integrations.tests.test_aanda import (
    CRITERIA,
    jobs,
    post,
    ready_booking,
    search,
    start,
)
from integrations.tests.test_aanda import (
    aa_gateway as aa_gateway,
)
from integrations.tests.test_aanda import (
    aa_supplier as aa_supplier,
)
from integrations.tests.test_aanda import (
    ctx as ctx,
)
from integrations.tests.test_hotelbook import Gateway as HotelbookGateway
from integrations.tests.test_hotelbook import hb_supplier as hb_supplier
from services.models import OrderService, ServiceOffer

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("field", ["childrenAges", "children_ages", "children"])
def test_all_child_age_schemas_refused(ctx, aa_gateway, field):
    with pytest.raises(AdapterError) as error:
        search(ctx, {**CRITERIA, "rooms": [{"adults": 1, field: [4]}]})
    assert error.value.code == "NOT_SUPPORTED" and not aa_gateway.calls


def test_pricing_signature_ttl_and_tamper(ctx, aa_gateway):
    adapter = AandaAdapter()
    detailed = adapter.revalidate(ctx, search(ctx)[0])["snapshot"]
    assert adapter._booking_validation(ctx, detailed)["status"] == "valid"
    assert aa_gateway.count("HotelPricingRequest") == 1
    tampered = deepcopy(detailed)
    tampered["price"]["amount"] = "1"
    assert adapter._booking_validation(ctx, tampered)["status"] == "price_changed"
    aa_gateway.penalty = "101"
    adapter.revalidation_ttl = -1
    assert adapter._booking_validation(ctx, detailed)["terms_changed"] is True
    assert aa_gateway.count("HotelPricingRequest") == 3


@pytest.mark.parametrize("mutation_failure", ["timeout", "reset", "empty", "malformed", "5xx", "redirect"])
def test_all_ambiguous_book_results_are_durable_and_never_retry(
    admin_client, tenant, aa_supplier, aa_gateway, monkeypatch, mutation_failure
):
    workflow, service = ready_booking(admin_client, tenant, aa_supplier)
    boundary = aa_gateway

    def fail(url, body):
        status, data = boundary(url, body)
        if boundary.calls[-1][0] != "OrderRequest":
            return status, data
        if mutation_failure == "timeout":
            raise TimeoutError("not for logs")
        if mutation_failure == "reset":
            raise ConnectionResetError("not for logs")
        return {
            "empty": (200, b""),
            "malformed": (200, b"<broken"),
            "5xx": (503, b"private"),
            "redirect": (302, b"private"),
        }[mutation_failure]

    monkeypatch.setattr("integrations.aanda_xml.exchange", fail)
    result = start(admin_client, workflow)
    assert result["items"][0]["error_code"] == "BOOKING_UNKNOWN"
    assert result["items"][0]["status"] == "unknown"
    service.refresh_from_db()
    adapter = AandaAdapter()
    with pytest.raises(AdapterError) as error:
        adapter.book(
            AdapterContext(tenant_id=tenant.id, supplier_id=aa_supplier.id), {"service_id": str(service.id)}
        )
    assert error.value.code == "BOOKING_UNKNOWN"
    assert aa_gateway.count("OrderRequest") == 1
    assert IntegrationIncident.objects.filter(
        service=service, provider_adapter="aanda", error_code="BOOKING_UNKNOWN"
    ).exists()


def test_preflight_price_change_uses_supplier_price_and_common_markups(
    admin_client, tenant, aa_supplier, aa_gateway
):
    workflow, service = ready_booking(admin_client, tenant, aa_supplier, preflight=False)
    aa_gateway.price = "6156"
    result = post(admin_client, f"booking-workflows/{workflow['id']}/preflight/")
    assert result["ok"] and result["price_changes"][0]["new"] == "6156.00"
    service.refresh_from_db()
    assert service.provider_snapshot["aanda"]["terms_source"] == "pricing"
    response = admin_client.post(
        f"/api/v1/booking-workflows/{workflow['id']}/start/",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY="aa-price-confirm",
    )
    assert response.status_code == 409
    assert aa_gateway.count("OrderRequest") == 0


def test_lease_busy_prevents_any_external_booking(admin_client, tenant, aa_supplier, aa_gateway):
    workflow, service = ready_booking(admin_client, tenant, aa_supplier)
    import uuid

    SupplierOperationLease.objects.create(
        service=service, owner=uuid.uuid4(), expires_at=timezone.now() + timedelta(minutes=1)
    )
    with pytest.raises(AdapterError) as error:
        AandaAdapter().book(
            AdapterContext(tenant_id=tenant.id, supplier_id=aa_supplier.id), {"service_id": str(service.id)}
        )
    assert error.value.code == "BOOKING_BUSY" and aa_gateway.count("OrderRequest") == 0


@pytest.mark.django_db(transaction=True)
def test_two_workers_send_one_order_request(admin_client, tenant, aa_supplier, aa_gateway, monkeypatch):
    from booking.job_handlers import _booking_passengers

    workflow, service = ready_booking(admin_client, tenant, aa_supplier)
    entered, release = threading.Event(), threading.Event()
    boundary = aa_gateway

    def slow(url, body):
        result = boundary(url, body)
        if boundary.calls[-1][0] == "OrderRequest":
            entered.set()
            assert release.wait(10)
        return result

    monkeypatch.setattr("integrations.aanda_xml.exchange", slow)
    booking_request = {"service_id": str(service.id), "passengers": _booking_passengers(service)}

    def worker():
        close_old_connections()
        try:
            return AandaAdapter().book(
                AdapterContext(tenant_id=tenant.id, supplier_id=aa_supplier.id), booking_request
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(worker)
        try:
            assert entered.wait(10)
            with pytest.raises(AdapterError) as error:
                worker()
            assert error.value.code == "BOOKING_BUSY"
        finally:
            release.set()
        assert pending.result(timeout=10)["status"] == "booked"
    assert aa_gateway.count("OrderRequest") == 1


def test_pending_automatic_poll_confirms_and_stops(admin_client, tenant, aa_supplier, aa_gateway):
    workflow, service = ready_booking(admin_client, tenant, aa_supplier)
    aa_gateway.status = "11"
    start(admin_client, workflow)
    assert queue_supplier_status_sync() == "queued 1"
    assert queue_supplier_status_sync() == "queued 0"
    aa_gateway.status = "35"
    jobs()
    service.refresh_from_db()
    assert service.status == "booked"
    assert queue_supplier_status_sync() == "queued 0"
    assert aa_gateway.count("OrderRequest") == 1


def test_refusal_during_status_inquiry_marks_failed(admin_client, tenant, aa_supplier, aa_gateway):
    workflow, service = ready_booking(admin_client, tenant, aa_supplier)
    aa_gateway.status = "11"
    result = start(admin_client, workflow)
    aa_gateway.status = "25"
    post(
        admin_client,
        f"booking-workflows/{workflow['id']}/status-inquiry/",
        {"item": result["items"][0]["id"]},
    )
    jobs()
    service.refresh_from_db()
    assert service.status == "failed"
    assert IntegrationIncident.objects.filter(service=service, error_code="BOOKING_REJECTED").exists()


def test_encryption_api_and_audit_do_not_expose_secrets(admin_client, aa_supplier):
    from common.models import AuditEvent
    from suppliers.views import CredentialSerializer

    credential = aa_supplier.credentials.get()
    config = json.loads(credential.encrypted_secrets)
    with connection.cursor() as cursor:
        cursor.execute("SELECT encrypted_secrets FROM suppliers_credential WHERE id=%s", [credential.id])
        stored = cursor.fetchone()[0]
    assert stored.startswith("enc$1$") and config["password"] not in stored
    assert "secrets" not in CredentialSerializer(credential).data
    assert config["password"] not in json.dumps(list(AuditEvent.objects.values()), default=str)


def test_production_endpoint_and_environment_guard(ctx, aa_gateway, settings):
    settings.AANDA_PRODUCTION_URL = ""
    with pytest.raises(AdapterError):
        endpoint("production")
    settings.AANDA_PRODUCTION_URL = "https://production.example.test/"
    assert endpoint("sandbox") == "https://go-api.aanda.ru/"
    snapshot = search(ctx)[0]
    settings.AANDA_ENVIRONMENT = "production"
    with pytest.raises(AdapterError) as error:
        AandaAdapter().revalidate(ctx, snapshot)
    assert error.value.code == "PROVIDER_NOT_CONFIGURED"


def test_city_dictionary_is_supplier_scoped_and_cached(ctx, aa_gateway, monkeypatch):
    cache.clear()
    boundary = aa_gateway
    calls = []

    def exchange(url, body):
        if b"RequestName=CountryListRequest" in body:
            calls.append("country")
            return 200, b'<CountryList><Success/><Country Code="9" Name="Russia"/></CountryList>'
        if b"RequestName=CityListRequest" in body:
            calls.append("city")
            return (
                200,
                '<CityList><Success/><Country Code="9"><City Code="18" Name="Самара"/></Country></CityList>'.encode(),
            )
        return boundary(url, body)

    monkeypatch.setattr("integrations.aanda_xml.exchange", exchange)
    criteria = {**CRITERIA, "aanda": {}, "location": "Самара"}
    assert search(ctx, criteria)
    assert search(ctx, criteria)
    assert calls == ["country", "city"]
    assert aa_gateway.calls[0][1].get("City") == "18"
    cache.clear()


@pytest.mark.parametrize("failing", [None, "aanda", "hotelbook"])
def test_multi_provider_search_partial_failure(
    admin_client, tenant, aa_supplier, aa_gateway, monkeypatch, failing
):
    from suppliers.models import Supplier, SupplierCredential

    supplier = Supplier.objects.create(tenant=tenant, name="Hotelbook", service_kinds=["hotel"])
    SupplierCredential.objects.create(
        tenant=tenant,
        supplier=supplier,
        provider_adapter="hotelbook",
        status="active",
        encrypted_secrets="{}",
    )
    snapshot = AandaAdapter().search(
        AdapterContext(tenant_id=tenant.id, supplier_id=aa_supplier.id), "hotel", CRITERIA
    )[0]

    def hotelbook_search(*args):
        if failing == "hotelbook":
            raise AdapterError("TIMEOUT", "test", category="timeout", retry_safe=True)
        return iter([{**snapshot, "provider_adapter": "hotelbook"}])

    monkeypatch.setattr(get_adapter("hotelbook"), "iter_search", hotelbook_search)
    if failing == "aanda":
        aa_gateway.fail = "HotelSearchRequest"
    session = post(admin_client, "service-searches/", {"kind": "hotel", "criteria": CRITERIA})
    jobs()
    offers = list(ServiceOffer.objects.filter(session_id=session["search_id"]))
    assert len(offers) == (2 if failing is None else 1)
    assert all(o.provider_adapter != failing for o in offers)
    result = admin_client.get(f"/api/v1/service-searches/{session['search_id']}/").json()
    assert result["status"] == ("completed" if failing is None else "partial")


def test_expired_preflight_repricing_blocks_changed_terms_before_post(
    admin_client, tenant, aa_supplier, aa_gateway
):
    workflow, service = ready_booking(admin_client, tenant, aa_supplier)
    aa_gateway.penalty = "200"
    with patch.object(AandaAdapter, "revalidation_ttl", -1):
        result = start(admin_client, workflow)
    assert result["items"][0]["error_code"] == "RATE_CHANGED"
    assert aa_gateway.count("OrderRequest") == 0


def test_multiroom_full_crm_flow_assigns_rooms_and_needs_no_passport(
    admin_client, tenant, aa_supplier, aa_gateway
):
    from crm.models import Person

    criteria = {**CRITERIA, "rooms": [{"adults": 1}, {"adults": 1}]}
    workflow, service = ready_booking(admin_client, tenant, aa_supplier, criteria=criteria, preflight=False)
    person = Person.objects.create(
        tenant=tenant,
        surname="Петров",
        given_name="Иван",
        latin_surname="PETROV",
        latin_given_name="IVAN",
        birth_date="1991-01-01",
        gender="male",
        citizenship="KG",
    )
    second = post(
        admin_client,
        f"orders/{service.order_id}/participants/",
        {"person": str(person.id), "role": "passenger"},
    )
    first_id = str(service.passengers.get().participant_id)
    response = admin_client.put(
        f"/api/v1/services/{service.id}/passengers/",
        {
            "version": service.version,
            "participants": [first_id, second["id"]],
            "room_assignments": {first_id: "room:0", second["id"]: "room:1"},
        },
        format="json",
    )
    assert response.status_code == 200, response.content
    preflight = post(admin_client, f"booking-workflows/{workflow['id']}/preflight/")
    assert preflight["ok"], preflight
    result = start(admin_client, workflow)
    assert result["status"] == "completed", result
    assert aa_gateway.count("OrderRequest") == 1
    submitted = next(root for name, root in aa_gateway.calls if name == "OrderRequest")
    assert len(submitted.findall("./AccommodationList/Accommodation")) == 2
    assert len(submitted.findall(".//Person")) == 2


@pytest.mark.parametrize("selected", ["aanda", "hotelbook"])
def test_selected_provider_books_only_that_provider(
    admin_client, tenant, aa_supplier, hb_supplier, aa_gateway, monkeypatch, selected
):
    hb_gateway = HotelbookGateway()
    monkeypatch.setattr("integrations.hotelbook.build_opener", lambda *args: hb_gateway)
    monkeypatch.setattr(get_adapter("hotelbook"), "poll_interval", 0)
    criteria = {**CRITERIA, "rooms": [{"adults": 1, "quantity": 1}], "hotelbook": {"hotels": [1251539]}}
    workflow, service = ready_booking(admin_client, tenant, aa_supplier, criteria=criteria, preflight=False)
    assert ServiceOffer.objects.count() == 2
    if selected == "hotelbook":
        person = service.order.client_person
        person.latin_surname = "IVANOV"
        person.save(update_fields=["latin_surname"])
        offer = ServiceOffer.objects.get(provider_adapter="hotelbook")
        row = post(admin_client, f"orders/{service.order_id}/services/", {"offer_id": str(offer.id)})
        workflow = post(
            admin_client, "booking-workflows/", {"order": str(service.order_id), "services": [row["id"]]}
        )
    result = post(admin_client, f"booking-workflows/{workflow['id']}/preflight/")
    assert result["ok"], result
    assert start(admin_client, workflow)["status"] == "completed"
    assert aa_gateway.count("OrderRequest") == (1 if selected == "aanda" else 0)
    assert sum(path.endswith("/hotel/gateway/book") for _, path, *_ in hb_gateway.calls) == (
        1 if selected == "hotelbook" else 0
    )


def test_unknown_order_price_never_defaults_to_zero(
    admin_client, tenant, aa_supplier, aa_gateway, monkeypatch
):
    workflow, service = ready_booking(admin_client, tenant, aa_supplier)
    start(admin_client, workflow)
    service.refresh_from_db()
    boundary = aa_gateway

    def no_penalty(url, body):
        from xml.etree import ElementTree as ET

        status, data = boundary(url, body)
        if boundary.calls[-1][0] == "OrderInfoRequest":
            root = ET.fromstring(data)
            for room in root.findall("./AccommodationList/Accommodation"):
                room.attrib.pop("PossiblePenaltySize")
            data = ET.tostring(root)
        return status, data

    monkeypatch.setattr("integrations.aanda_xml.exchange", no_penalty)
    with pytest.raises(AdapterError) as error:
        AandaAdapter().cancel(
            AdapterContext(tenant_id=tenant.id, supplier_id=aa_supplier.id), service.external_id
        )
    assert error.value.code == "CANCELLATION_FAILED" and aa_gateway.count("OrderRequest") == 1


def test_changed_order_price_preserves_booking_and_alerts_operator(
    admin_client, tenant, aa_supplier, aa_gateway, monkeypatch
):
    from xml.etree import ElementTree as ET

    workflow, service = ready_booking(admin_client, tenant, aa_supplier)
    boundary = aa_gateway

    def changed(url, body):
        status, data = boundary(url, body)
        if boundary.calls[-1][0] == "OrderInfoRequest":
            root = ET.fromstring(data)
            root.set("Price", "7000")
            data = ET.tostring(root)
        return status, data

    monkeypatch.setattr("integrations.aanda_xml.exchange", changed)
    assert start(admin_client, workflow)["status"] == "completed"
    service.refresh_from_db()
    assert service.provider_snapshot["aanda_booking"]["price_discrepancy"] is True
    assert service.provider_snapshot["aanda_booking"]["actual_price"] == {"amount": "7000", "currency": "RUB"}
    assert IntegrationIncident.objects.filter(service=service, error_code="PRICE_CHANGED").exists()
    assert aa_gateway.count("OrderRequest") == 1


def test_http_auth_error_keeps_only_numeric_supplier_code(ctx, aa_gateway, monkeypatch):
    from integrations.models import IntegrationLog

    monkeypatch.setattr(
        "integrations.aanda_xml.exchange",
        lambda *args: (401, b'<Error><Type Code="1" Description="password passport document"/></Error>'),
    )
    with pytest.raises(AdapterError) as error:
        search(ctx)
    assert error.value.code == "AUTH_ERROR"
    log = IntegrationLog.objects.latest("id")
    assert log.provider_error == {"code": "1"}
    assert "passport" not in json.dumps(log.response_sanitized)


def test_stale_preflight_cannot_erase_durable_booking_reservation(
    admin_client, tenant, aa_supplier, aa_gateway
):
    from services.revalidation import update_service

    workflow, service = ready_booking(admin_client, tenant, aa_supplier, preflight=False)
    ctx = AdapterContext(tenant_id=tenant.id, supplier_id=aa_supplier.id)
    result = AandaAdapter().revalidate(ctx, service.provider_snapshot)
    concurrent = deepcopy(service.provider_snapshot)
    concurrent["aanda_booking"] = {
        "locator": "AA-reserved",
        "reference_number": "AA-reserved",
        "state": "creating",
    }
    OrderService.objects.filter(pk=service.pk).update(provider_snapshot=concurrent)
    update_service(service, result)
    service.refresh_from_db()
    assert service.provider_snapshot["aanda_booking"] == concurrent["aanda_booking"]
    assert aa_gateway.count("OrderRequest") == 0


def test_stale_inquiry_cannot_erase_cancel_reservation(
    admin_client, tenant, aa_supplier, aa_gateway, monkeypatch
):
    from common.models import BackgroundJob

    workflow, service = ready_booking(admin_client, tenant, aa_supplier)
    result = start(admin_client, workflow)
    adapter = get_adapter("aanda")
    original = adapter.retrieve_booking

    def interleaved(ctx, locator):
        outcome = original(ctx, locator)
        latest = OrderService.objects.get(pk=service.pk)
        snapshot = deepcopy(latest.provider_snapshot)
        snapshot["aanda_booking"]["cancel_state"] = "sending"
        OrderService.objects.filter(pk=service.pk).update(provider_snapshot=snapshot)
        return outcome

    monkeypatch.setattr(adapter, "retrieve_booking", interleaved)
    post(
        admin_client,
        f"booking-workflows/{workflow['id']}/status-inquiry/",
        {"item": result["items"][0]["id"]},
    )
    jobs()
    service.refresh_from_db()
    assert service.provider_snapshot["aanda_booking"]["cancel_state"] == "sending"
    assert BackgroundJob.objects.get(kind="booking.status_inquiry").result["stale"] is True
    assert aa_gateway.count("OrderRequest") == 1
