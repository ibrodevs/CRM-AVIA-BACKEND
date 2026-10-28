"""XML fixtures follow Roomlink API 4.0.1; no network or real credentials."""

import json
import uuid
from copy import deepcopy
from decimal import Decimal
from urllib.parse import parse_qs
from xml.etree import ElementTree as ET

import pytest
from django.core.management import call_command

from integrations.aanda import AandaAdapter
from integrations.aanda_xml import child
from integrations.adapters import AdapterContext, AdapterError, adapter_for_service, get_adapter
from integrations.models import IntegrationLog
from services.models import OrderService, SearchSession, ServiceOffer
from suppliers.models import Supplier, SupplierCredential, SupplierMarkupRule

pytestmark = pytest.mark.django_db
CRITERIA = {
    "check_in": "2027-03-10",
    "check_out": "2027-03-12",
    "rooms": [{"adults": 1}],
    "aanda": {"hotel_codes": [453]},
}


class Gateway:
    def __init__(self):
        self.calls = []
        self.price = "6056"
        self.penalty = "100"
        self.deadline = "09.03.2027"
        self.currency = "RUR"
        self.availability = "1"
        self.status = "35"
        self.allow_cancel = "1"
        self.fail = ""
        self.bad = None
        self.raw_error = ""
        self.bookings = {}
        self.search_count = 1
        self.rate_id = "1"
        self.info = ""

    def __call__(self, url, body):
        payload = parse_qs(body.decode())
        name = payload["RequestName"][0]
        root = ET.fromstring(payload["XML"][0])
        self.calls.append((name, root))
        assert url == "https://go-api.aanda.ru/"
        if self.bad is not None:
            return 200, self.bad
        if self.raw_error:
            return (
                200,
                f'<Error><Type Code="{self.raw_error}" Description="echo password passport full guest"/></Error>'.encode(),
            )
        if self.fail == name and name != "OrderRequest":
            raise TimeoutError("private transport data must not escape")
        if name == "CurrencyListRequest":
            response = ET.Element("CurrencyListResponse")
            child(response, "Success")
            child(response, "Currency", Code="2", Name="RUR")
        elif name in ("HotelSearchRequest", "HotelPricingRequest"):
            pricing = name == "HotelPricingRequest"
            pax = root.get("NumberOfGuests")
            response = ET.Element("HotelPricingResponse" if pricing else "HotelSearchResponse")
            child(response, "Success")
            hotel = (
                response if pricing else child(child(response, "AvailableHotelsList"), "Hotel", Code="453")
            )
            hotel.attrib.update(
                Code="453", Name="Отель & Спа", CheckInTime="14:00", CheckOutTime="12:00", Timezone="UTC+3"
            )
            if pricing:
                hotel.attrib.update(StartDate="10.03.2027", EndDate="12.03.2027")
            child(hotel, "Currency", Code="2", Name=self.currency)
            child(hotel, "City", Code="18", Name="Самара")
            child(hotel, "Country", Code="9", Name="Россия")
            if not pricing:
                child(hotel, "Stars", Code="4", Name="4 звезды")
            listing = child(hotel, "HotelPricingList" if pricing else "HotelOfferList")
            for index in range(self.search_count):
                detail = child(
                    listing,
                    "HotelPricingDetail" if pricing else "HotelOfferDetail",
                    Code=str(13450 + index),
                    RoomName="Стандарт",
                )
                values = {
                    "RateID" if pricing else "ID": self.rate_id,
                    "RateDescription" if pricing else "Description": "Основной",
                    "RateName" if pricing else "Name": "Основной",
                    f"Price{pax}Pax" if pricing else "TotalPrice": self.price,
                    f"PenaltySize{pax}Pax" if pricing else "PenaltySize": self.penalty,
                    "DeadlineDate": self.deadline if pricing else self.deadline + " 12:00",
                    "PenaltyInfo": self.info,
                }
                if pricing:
                    values["DeadlineTime"] = "12:00"
                rate = child(child(detail, "Rates"), "Rate", **values)
                child(rate, "Availability", Code=self.availability, Rooms="8")
                child(rate, "Meal", Code="1", TypeCode="4", TypeName="Завтрак")
                child(child(rate, "WhereToPayList") if pricing else rate, "WhereToPay", Code="3")
                child(rate, "UnexpectedSecret", Password="never copy unknown fields")
        elif name == "OrderRequest":
            ref = root.get("ReferenceNumber")
            if root.get("Id"):
                self.status = "71"
            else:
                self.bookings[ref] = deepcopy(root)
            if self.fail == name:
                raise TimeoutError("ambiguous result")
            response = ET.Element("OrderResponse")
            child(response, "Success", Id="999")
        elif name == "OrderInfoRequest":
            ref = root.get("ReferenceNumber")
            source = self.bookings.get(ref)
            if source is None:
                return 200, b'<Error Code="8"/>'
            response = ET.Element("OrderInfoResponse", Id="999", ReferenceNumber=ref)
            child(response, "Success")
            child(response, "Status", Code=self.status)
            child(response, "Currency", Code="2", Name=self.currency)
            listing = child(response, "AccommodationList")
            for index, original in enumerate(source.findall("./AccommodationList/Accommodation")):
                node = child(
                    listing,
                    "Accommodation",
                    Id=str(index + 10),
                    ConfirmationNumber="CONFIRMED" if self.status == "35" else "",
                    Penalty="0",
                    PossiblePenaltySize="0",
                    DeadlineDate=self.deadline,
                )
                node.append(deepcopy(original.find("Hotel")))
                node.append(deepcopy(original.find("Product")))
                child(node, "Status", Code=self.status)
                child(node, "AllowCancel", Code=self.allow_cancel)
                child(node, "Persons", PassportNumber="private document", Email="guest@example.test")
        else:
            raise AssertionError(name)
        return 200, ET.tostring(response)

    def count(self, name):
        return sum(n == name for n, _ in self.calls)


@pytest.fixture
def aa_gateway(monkeypatch):
    gateway = Gateway()
    monkeypatch.setattr("integrations.aanda_xml.exchange", gateway)
    return gateway


@pytest.fixture
def aa_supplier(tenant):
    supplier = Supplier.objects.create(tenant=tenant, name="A&A", service_kinds=["hotel"])
    SupplierCredential.objects.create(
        tenant=tenant,
        supplier=supplier,
        provider_adapter="aanda",
        environment="sandbox",
        status="active",
        encrypted_secrets=json.dumps(
            {
                "buyer_id": uuid.uuid4().hex,
                "user_id": uuid.uuid4().hex,
                "password": uuid.uuid4().hex,
                "language": "RU",
            }
        ),
    )
    return supplier


@pytest.fixture
def ctx(tenant, aa_supplier):
    return AdapterContext(tenant_id=tenant.id, supplier_id=aa_supplier.id)


def search(ctx, criteria=None):
    return AandaAdapter().search(ctx, "hotel", criteria or CRITERIA)


def test_registry_and_credential_verification(aa_supplier, aa_gateway):
    from suppliers.job_handlers import verify_supplier_credentials

    assert isinstance(get_adapter("aanda"), AandaAdapter)
    result = verify_supplier_credentials(aa_supplier)
    assert result["status"] == "connected"
    assert aa_gateway.count("CurrencyListRequest") == 1


@pytest.mark.parametrize("missing", ["buyer_id", "user_id", "password"])
def test_missing_credentials(aa_supplier, aa_gateway, missing):
    credential = aa_supplier.credentials.get()
    config = json.loads(credential.encrypted_secrets)
    config.pop(missing)
    credential.encrypted_secrets = json.dumps(config)
    with pytest.raises(AdapterError, match="BuyerId"):
        AandaAdapter().verify_credentials(credential)
    assert aa_gateway.calls == []


def test_search_normalizes_and_drops_unknown_xml(ctx, aa_gateway):
    offers = search(ctx)
    assert len(offers) == 1
    offer = offers[0]
    assert Decimal(offer["price"]["amount"]) == Decimal("6056")
    assert offer["price"]["currency"] == "RUB"
    assert offer["itinerary"]["property_name"] == "Отель & Спа"
    assert offer["aanda"]["rooms"][0]["deadline"]["timestamp"] == "2027-03-09T12:00:00+03:00"
    assert "UnexpectedSecret" not in json.dumps(offer)
    assert offer["aanda"]["rooms"][0]["where_to_pay"] == "3"
    assert search(ctx)[0]["external_key"] == offer["external_key"]
    aa_gateway.search_count = 0
    assert search(ctx) == []


@pytest.mark.parametrize(
    "code,expected", [("1", "available"), ("0", "on_request"), ("2", "unknown"), ("4", "unknown")]
)
def test_multirate_availability(ctx, aa_gateway, code, expected):
    aa_gateway.availability = code
    assert search(ctx)[0]["availability"] == expected


@pytest.mark.parametrize(
    "raw,code",
    [
        ("1", "AUTH_ERROR"),
        ("20", "AUTH_ERROR"),
        ("3", "SEARCH_CRITERIA_INVALID"),
        ("6", "RATE_UNAVAILABLE"),
        ("8", "BOOKING_NOT_FOUND"),
        ("7", "CANCELLATION_FAILED"),
        ("16", "INVALID_REQUEST"),
        ("99", "PROVIDER_ERROR"),
    ],
)
def test_errors_sanitized(ctx, aa_gateway, raw, code):
    aa_gateway.raw_error = raw
    with pytest.raises(AdapterError) as error:
        search(ctx)
    assert error.value.code == code
    assert error.value.raw == raw
    assert "echo" not in str(error.value)
    log = IntegrationLog.objects.latest("id")
    assert log.provider_error == {"code": raw}
    assert "passport" not in json.dumps(log.response_sanitized)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"<broken",
        b"<Other><Success/></Other>",
        b'<!DOCTYPE a [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><HotelSearchResponse><Success/>&xxe;</HotelSearchResponse>',
        b'<!DOCTYPE a [<!ENTITY x "ha">]><HotelSearchResponse><Success/>&x;</HotelSearchResponse>',
    ],
)
def test_bad_xml_blocked(ctx, aa_gateway, body):
    aa_gateway.bad = body
    with pytest.raises(AdapterError) as error:
        search(ctx)
    assert error.value.code == "INVALID_RESPONSE"
    assert error.value.retry_safe


def test_read_timeout(ctx, aa_gateway):
    aa_gateway.fail = "HotelSearchRequest"
    with pytest.raises(AdapterError) as error:
        search(ctx)
    assert error.value.code == "TIMEOUT" and error.value.retry_safe


def test_pricing_identical_and_changed(ctx, aa_gateway):
    adapter = AandaAdapter()
    baseline = search(ctx)[0]
    result = adapter.revalidate(ctx, baseline)
    assert result["status"] == "valid" and result["terms_changed"] is False
    assert result["snapshot"]["aanda"]["terms_source"] == "pricing"
    aa_gateway.price = "6100.50"
    result = adapter.revalidate(ctx, baseline)
    assert result["status"] == "price_changed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("penalty", "101"),
        ("deadline", "08.03.2027"),
        ("currency", "USD"),
        ("info", "Невозвратный"),
        ("availability", "0"),
    ],
)
def test_pricing_real_terms_changes(ctx, aa_gateway, field, value):
    baseline = AandaAdapter().revalidate(ctx, search(ctx)[0])["snapshot"]
    setattr(aa_gateway, field, value)
    assert AandaAdapter().revalidate(ctx, baseline)["terms_changed"] is True


def test_rate_disappeared(ctx, aa_gateway):
    baseline = search(ctx)[0]
    aa_gateway.rate_id = "99"
    with pytest.raises(AdapterError) as error:
        AandaAdapter().revalidate(ctx, baseline)
    assert error.value.code == "RATE_UNAVAILABLE"


def test_children_not_silently_lost(ctx, aa_gateway):
    criteria = {**CRITERIA, "rooms": [{"adults": 1, "children_ages": [8]}]}
    with pytest.raises(AdapterError) as error:
        search(ctx, criteria)
    assert error.value.code == "NOT_SUPPORTED"
    assert aa_gateway.calls == []


def post(client, path, body=None, key=None):
    response = client.post(
        "/api/v1/" + path, body or {}, format="json", **({"HTTP_IDEMPOTENCY_KEY": key} if key else {})
    )
    assert response.status_code in (200, 201, 202), response.content
    return response.json()


def jobs():
    call_command("run_jobs", "--once", "--worker-id", "aanda-test")


def ready_booking(client, tenant, aa_supplier, *, criteria=None, preflight=True):
    from crm.models import Person, PersonDocument

    person = Person.objects.create(
        tenant=tenant,
        surname="Иванов",
        given_name="Пётр",
        latin_surname="O'NEIL & <Тест>",
        latin_given_name="PETR",
        birth_date="1990-01-01",
        gender="male",
        citizenship="KG",
    )
    PersonDocument.objects.create(
        tenant=tenant,
        person=person,
        type="foreign_passport",
        number=uuid.uuid4().hex,
        expires_at="2033-01-01",
    )
    order = post(
        client,
        "orders/",
        {
            "client_person": str(person.id),
            "planned_start": "2027-03-10",
            "planned_end": "2027-03-12",
            "participants": [{"person": str(person.id)}],
        },
    )
    session = post(client, "service-searches/", {"kind": "hotel", "criteria": criteria or CRITERIA})
    jobs()
    offer = ServiceOffer.objects.get(session_id=session["search_id"], provider_adapter="aanda")
    service = post(client, f"orders/{order['id']}/services/", {"offer_id": str(offer.id)})
    workflow = post(client, "booking-workflows/", {"order": order["id"], "services": [service["id"]]})
    if preflight:
        result = post(client, f"booking-workflows/{workflow['id']}/preflight/")
        assert result["ok"] and not result["price_changes"], result
    return workflow, OrderService.objects.get(pk=service["id"])


def start(client, workflow):
    post(client, f"booking-workflows/{workflow['id']}/start/", key=f"aa-{workflow['id']}")
    jobs()
    return client.get(f"/api/v1/booking-workflows/{workflow['id']}/status/").json()


def test_full_pipeline_and_fresh_preflight_no_second_pricing(admin_client, tenant, aa_supplier, aa_gateway):
    workflow, service = ready_booking(admin_client, tenant, aa_supplier)
    assert aa_gateway.count("HotelPricingRequest") == 1
    result = start(admin_client, workflow)
    assert result["status"] == "completed", result
    assert aa_gateway.count("HotelPricingRequest") == 1
    assert aa_gateway.count("OrderRequest") == 1
    submitted = next(r for n, r in aa_gateway.calls if n == "OrderRequest")
    assert submitted.find(".//Person").get("LastName") == "O'NEIL & <Тест>"
    service.refresh_from_db()
    assert service.status == "booked"
    assert service.provider_snapshot["aanda_booking"]["order_id"] == "999"
    assert "Persons" not in json.dumps(service.provider_snapshot)
    post(admin_client, f"services/{service.id}/cancel/", {"version": service.version})
    jobs()
    service.refresh_from_db()
    assert service.status == "cancelled"
    assert aa_gateway.count("OrderRequest") == 2
    adapter = AandaAdapter()
    ctx = AdapterContext(tenant_id=tenant.id, supplier_id=aa_supplier.id)
    assert adapter.cancel(ctx, service.external_id)["status"] == "cancelled"
    assert aa_gateway.count("OrderRequest") == 2
    credential = aa_supplier.credentials.get()
    secrets = json.loads(credential.encrypted_secrets)
    logs = json.dumps(list(IntegrationLog.objects.filter(provider_adapter="aanda").values()), default=str)
    for value in (*secrets.values(), "O'NEIL", "guest@example.test", "private document"):
        if len(value) > 3:
            assert value not in logs


@pytest.mark.parametrize(
    "status,expected",
    [
        ("11", "pending"),
        ("18", "pending"),
        ("20", "pending"),
        ("21", "pending"),
        ("35", "booked"),
        ("65", "booked"),
        ("25", "failed"),
        ("71", "cancelled"),
        ("999", "pending"),
    ],
)
def test_supplier_status_not_automatically_booked(
    admin_client, tenant, aa_supplier, aa_gateway, status, expected
):
    workflow, service = ready_booking(admin_client, tenant, aa_supplier)
    aa_gateway.status = status
    result = start(admin_client, workflow)
    service.refresh_from_db()
    assert service.provider_snapshot["aanda_booking"]["normalized_status"] == expected
    if expected == "pending":
        assert result["items"][0]["status"] == "unknown"
        assert service.status == "approval"
    elif expected == "failed":
        assert service.status == "failed"
        assert result["items"][0]["status"] == "failed"


def test_timeout_never_rebooks_and_recovers_reference(admin_client, tenant, aa_supplier, aa_gateway):
    from booking.job_handlers import run_booking
    from common.models import BackgroundJob

    workflow, service = ready_booking(admin_client, tenant, aa_supplier)
    aa_gateway.fail = "OrderRequest"
    result = start(admin_client, workflow)
    assert result["items"][0]["status"] == "unknown"
    assert aa_gateway.count("OrderRequest") == 1
    service.refresh_from_db()
    assert service.provider_snapshot["aanda_booking"]["reference_number"]
    run_booking(BackgroundJob.objects.get(kind="booking.run", payload__workflow_id=workflow["id"]))
    assert aa_gateway.count("OrderRequest") == 1
    aa_gateway.fail = ""
    post(
        admin_client,
        f"booking-workflows/{workflow['id']}/status-inquiry/",
        {"item": result["items"][0]["id"]},
    )
    jobs()
    service.refresh_from_db()
    assert service.status == "booked"
    assert aa_gateway.count("OrderRequest") == 1
    info = [r for n, r in aa_gateway.calls if n == "OrderInfoRequest"][0]
    assert info.get("Id") == "" and info.get("ReferenceNumber")


def test_preflight_failure_blocks_aanda(admin_client, tenant, aa_supplier, aa_gateway):
    workflow, service = ready_booking(admin_client, tenant, aa_supplier, preflight=False)
    aa_gateway.fail = "HotelPricingRequest"
    result = post(admin_client, f"booking-workflows/{workflow['id']}/preflight/")
    assert not result["ok"]
    assert result["blocking_errors"][0]["code"] == "TIMEOUT"


def test_exact_credential_routing_and_tenant_isolation(
    admin_client, tenant, other_tenant, aa_supplier, aa_gateway
):
    workflow, service = ready_booking(admin_client, tenant, aa_supplier)
    SupplierCredential.objects.create(
        tenant=tenant, supplier=aa_supplier, provider_adapter="mock", status="active", encrypted_secrets="{}"
    )
    assert adapter_for_service(service).key == "aanda"
    with pytest.raises(AdapterError):
        AandaAdapter().retrieve_booking(
            AdapterContext(tenant_id=other_tenant.id, supplier_id=aa_supplier.id), "999"
        )
    assert aa_gateway.count("OrderInfoRequest") == 0


def test_cancel_forbidden_and_unknown_not_repeated(admin_client, tenant, aa_supplier, aa_gateway):
    workflow, service = ready_booking(admin_client, tenant, aa_supplier)
    start(admin_client, workflow)
    service.refresh_from_db()
    adapter, ctx = AandaAdapter(), AdapterContext(tenant_id=tenant.id, supplier_id=aa_supplier.id)
    aa_gateway.allow_cancel = "2"
    with pytest.raises(AdapterError) as error:
        adapter.cancel(ctx, service.external_id)
    assert error.value.code == "CANCELLATION_FAILED"
    assert aa_gateway.count("OrderRequest") == 1
    aa_gateway.allow_cancel, aa_gateway.fail = "1", "OrderRequest"
    with pytest.raises(AdapterError) as error:
        adapter.cancel(ctx, service.external_id)
    assert error.value.code == "CANCELLATION_UNKNOWN" and not error.value.retry_safe
    aa_gateway.status = "35"
    with pytest.raises(AdapterError) as repeated:
        adapter.cancel(ctx, service.external_id)
    assert repeated.value.code == "CANCELLATION_UNKNOWN" and aa_gateway.count("OrderRequest") == 2
    aa_gateway.status = "71"
    assert adapter.cancel(ctx, service.external_id)["status"] == "cancelled"
    assert aa_gateway.count("OrderRequest") == 2


def test_markup_uses_common_layer(admin_client, tenant, aa_supplier, aa_gateway):
    SupplierMarkupRule.objects.create(
        tenant=tenant, supplier=aa_supplier, service_kind="hotel", amount_type="percent", amount_value=10
    )
    session = post(admin_client, "service-searches/", {"kind": "hotel", "criteria": CRITERIA})
    jobs()
    offer = ServiceOffer.objects.get(session_id=session["search_id"])
    assert offer.price_amount == Decimal("6661.60")
    assert Decimal(offer.raw_snapshot["price"]["amount"]) == Decimal("6056")


def test_supplier_dedup_preserves_both_providers(tenant, admin_user, aa_supplier, aa_gateway, ctx):
    from services.job_handlers import _persist_offer

    snapshot = search(ctx)[0]
    session = SearchSession.objects.create(tenant=tenant, user=admin_user, kind="hotel", criteria=CRITERIA)
    other_supplier = Supplier.objects.create(tenant=tenant, name="Hotelbook", service_kinds=["hotel"])
    seen = set()
    assert _persist_offer(session, aa_supplier, "aanda", snapshot, seen)
    assert not _persist_offer(session, aa_supplier, "aanda", snapshot, seen)
    assert _persist_offer(
        session, other_supplier, "hotelbook", {**snapshot, "provider_adapter": "hotelbook"}, seen
    )
    assert session.offers.count() == 2


def test_multiroom_search_pricing_and_escaped_booking(ctx, aa_gateway):
    criteria = {**CRITERIA, "rooms": [{"adults": 1}, {"adults": 2}]}
    snapshot = search(ctx, criteria)[0]
    assert snapshot["price"]["amount"] == "12112"
    snapshot = AandaAdapter().revalidate(ctx, snapshot)["snapshot"]
    guests = [
        {"surname": "Иванов", "given_name": "Иван", "room_ref": "room:0"},
        {"surname": "Smith", "given_name": "John", "room_ref": "room:1"},
        {"surname": "O'Neil & <>", "given_name": "Анна", "room_ref": "room:1"},
    ]
    adapter = AandaAdapter()
    groups = adapter._guests(snapshot, guests)
    root = adapter._order_xml(snapshot, groups, "AA-reference")
    roundtrip = ET.fromstring(ET.tostring(root))
    assert len(roundtrip.findall("./AccommodationList/Accommodation")) == 2
    assert roundtrip.findall(".//Person")[-1].get("LastName") == "O'Neil & <>"
    assert [r.get("NumberOfGuests") for n, r in aa_gateway.calls if n == "HotelSearchRequest"] == ["1", "2"]
    with pytest.raises(AdapterError, match="room_ref"):
        adapter._guests(snapshot, [{**p, "room_ref": "room:0"} for p in guests])


def test_refund_explicitly_unsupported(ctx):
    with pytest.raises(AdapterError) as error:
        AandaAdapter().refund_quote(ctx, {})
    assert error.value.code == "NOT_SUPPORTED"
