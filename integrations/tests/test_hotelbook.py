"""Gateway fixtures follow HB Pro OpenAPI 2026.09.23, not a mock-provider API."""

import io
import json
from copy import deepcopy
from decimal import Decimal
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest
from django.core.cache import cache
from django.core.management import call_command

from booking.models import BookingWorkflowItem
from crm.models import Person, PersonDocument
from integrations.adapters import AdapterContext, AdapterError, get_adapter
from integrations.hotelbook import TEST_HOTELS, HotelbookAdapter
from integrations.models import IntegrationLog
from services.models import OrderService, ServiceOffer
from suppliers.models import Supplier, SupplierCredential, SupplierMarkupRule

pytestmark = pytest.mark.django_db
SEARCH_ID = "019a04ef-24a4-7000-8000-000000000001"
ITEM_ID = "0a04ef24-24a4-4000-8000-000000000001"


def offer(hotel_id=1251539):
    return {
        "offerId": "opaque-rate-key",
        "hotelId": hotel_id,
        "checkIn": "2027-03-10",
        "checkOut": "2027-03-12",
        "price": {"amount": 120.25, "currency": "EUR"},
        "confirmationMode": "ONLINE",
        "meal": {"mealTypeId": 10, "description": "Breakfast"},
        "roomGroupName": "Double",
        "rooms": [
            {
                "hash": "opaque-room-hash",
                "providerRoomId": "vendor-room",
                "roomInfoId": "hb-room",
                "roomName": "Standard Double",
                "adults": 1,
                "children": [],
                "quantity": 1,
                "quota": 3,
                "beds": [{"name": "Double", "quantity": 1, "places": 2}],
            }
        ],
        "finePolicies": {
            "cancel": [{"from": "2027-03-08T12:00:00+01:00", "price": {"amount": 120.25, "currency": "EUR"}}],
            "change": [],
            "noShow": {"amount": 120.25, "currency": "EUR"},
        },
        "isCyrillicGuestNameAllowed": False,
        "isEndCustomerNeeded": False,
    }


class Gateway:
    """A controllable HTTP boundary with real paths, auth and response envelopes."""

    def __init__(self):
        self.calls = []
        self.current = offer()
        self.pages = None
        self.item_status = "CONFIRMED"
        self.cancel_status = "CANCELED"
        self.items = [ITEM_ID]
        self.failure = None
        self.fail_once_auth = False
        self.order_has_items = True

    def open(self, request, timeout):
        url = urlparse(request.full_url)
        body = json.loads(request.data) if request.data else None
        self.calls.append((request.method, url.path, parse_qs(url.query), body, dict(request.header_items())))
        assert timeout > 0
        if url.path.endswith("/gateway/login"):
            assert body == {"login": "fixture-login", "password": "fixture-password"}
            response = {"token": "fixture-jwt"}
        else:
            assert request.get_header("Authorization") == "Bearer fixture-jwt"
            if self.fail_once_auth:
                self.fail_once_auth = False
                self.raise_error(request, 401, "ERR013#4")
            if self.failure and url.path.endswith(self.failure[0]):
                error = self.failure[1]
                if isinstance(error, Exception):
                    raise error
                self.raise_error(request, error[0], error[1])
            if url.path.endswith("/hotel/gateway/search"):
                assert request.method == "POST"
                assert body["checkIn"] == "2027-03-10" and body["checkOut"] == "2027-03-12"
                assert body["citizenshipId"] == 77
                assert body["rooms"] == [{"adults": 1, "quantity": 1}]
                response = {"searchId": SEARCH_ID, "state": "IN_PROCESS"}
            elif "/hotel/gateway/results/" in url.path:
                assert request.method == "GET"
                response = (
                    self.pages.pop(0)
                    if self.pages
                    else {"finished": True, "totalOffers": 1, "searchOffers": [deepcopy(self.current)]}
                )
            elif url.path.endswith("/search/details"):
                assert body == {"searchId": SEARCH_ID, "offerId": "opaque-rate-key"}
                response = deepcopy(self.current)
            elif "/dict/gateway/hotel/hotels/" in url.path:
                hotel_id = int(url.path.rsplit("/", 1)[1])
                response = {
                    "id": hotel_id,
                    "name": TEST_HOTELS[hotel_id],
                    "cityId": 42,
                    "details": {"address": "Fixture Street 1"},
                }
            elif url.path.endswith("/geo/countries"):
                response = [{"id": 77, "alpha2": "KG", "name": "Kyrgyzstan", "trash": False}]
            elif url.path.endswith("/geo/cities"):
                response = [{"id": 42, "name": "Fixture City", "trash": False}]
            elif url.path.endswith("/order/gateway/orders") and request.method == "POST":
                assert {"clientOrderId", "payForm", "contactInfo"} <= set(body)
                if self.current.get("isEndCustomerNeeded"):
                    assert body["customer"] == {"type": "PRIVATE"}
                assert body["payForm"] == "CASHLESS"
                response = {"orderId": 4321}
            elif url.path.endswith("/hotel/gateway/book"):
                assert body["orderId"] == 4321
                assert body["price"] == self.current["price"]["amount"] and body["currency"] == "EUR"
                assert body["rooms"] == [
                    {
                        "hash": "opaque-room-hash",
                        "guests": [
                            {
                                "firstName": "PETR",
                                "lastName": "IVANOV",
                                "gender": "M",
                                "citizenship": 77,
                                "isChild": False,
                            }
                        ],
                    }
                ]
                assert body["confirmationMode"] == "ONLINE"
                response = {"items": [self.booking_item(i) for i in self.items]}
            elif url.path.endswith("/order/gateway/orders/4321"):
                response = {
                    "orderId": 4321,
                    "items": [
                        {"itemId": i, "type": "hotel", "serviceType": "ONLINE", "state": self.item_status}
                        for i in self.items
                    ]
                    if self.order_has_items
                    else [],
                }
            elif url.path.endswith("/order/gateway/orders") and request.method == "GET":
                assert "clientOrderId" in parse_qs(url.query)
                response = [{"orderId": 4321}]
            elif url.path.endswith("/cancel"):
                assert request.method == "POST" and body == {}
                response = self.booking_item(url.path.split("/")[-2], self.cancel_status)
                self.item_status = self.cancel_status
            elif "/hotel/gateway/booking_items/" in url.path:
                response = self.booking_item(url.path.rsplit("/", 1)[1])
            else:
                raise AssertionError(f"Undocumented request: {request.method} {url.path}")
        return Response(response)

    def booking_item(self, item_id, status=None):
        return {
            "itemId": item_id,
            "orderId": 4321,
            "status": status or self.item_status,
            "isCancellationAllowed": True,
            "price": self.current["price"],
        }

    @staticmethod
    def raise_error(request, status, code):
        raise HTTPError(
            request.full_url,
            status,
            "fixture",
            {},
            io.BytesIO(
                json.dumps(
                    {
                        "title": "Error",
                        "description": "Do not expose secrets",
                        "details": {"errorCode": code, "errorId": "fixture-id"},
                    }
                ).encode()
            ),
        )


class Response:
    status = 201

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return json.dumps(self.payload).encode()


@pytest.fixture
def gateway(monkeypatch):
    cache.clear()
    gateway = Gateway()
    monkeypatch.setattr("integrations.hotelbook.build_opener", lambda *args: gateway)
    monkeypatch.setattr(HotelbookAdapter, "poll_interval", 0)
    return gateway


@pytest.fixture
def hb_supplier(tenant):
    supplier = Supplier.objects.create(tenant=tenant, name="Hotelbook", service_kinds=["hotel"])
    SupplierCredential.objects.create(
        tenant=tenant,
        supplier=supplier,
        provider_adapter="hotelbook",
        status="active",
        encrypted_secrets=json.dumps(
            {"login": "fixture-login", "password": "fixture-password", "default_citizenship": "KG"}
        ),
    )
    return supplier


def context(tenant, supplier):
    return AdapterContext(tenant_id=tenant.id, supplier_id=supplier.id)


def criteria(hotel_id=1251539):
    return {
        "location": str(hotel_id),
        "check_in": "2027-03-10",
        "check_out": "2027-03-12",
        "guests": 1,
        "rooms": 1,
    }


def jobs():
    call_command("run_jobs", "--once", "--worker-id", "hotelbook-test")


def post(client, path, body=None, key=None):
    extra = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
    response = client.post("/api/v1/" + path, body or {}, format="json", **extra)
    assert response.status_code in (200, 201, 202), response.content
    return response.json()


def ready_booking(admin_client, tenant, hb_supplier, hotel_id=1251539):
    person = Person.objects.create(
        tenant=tenant,
        surname="Иванов",
        given_name="Пётр",
        latin_surname="IVANOV",
        latin_given_name="PETR",
        birth_date="1990-01-01",
        gender="male",
        citizenship="KG",
    )
    PersonDocument.objects.create(
        tenant=tenant, person=person, type="foreign_passport", number="fixture-doc", expires_at="2033-01-01"
    )
    order = post(
        admin_client,
        "orders/",
        {
            "client_person": str(person.id),
            "planned_start": "2027-03-10",
            "planned_end": "2027-03-12",
            "participants": [{"person": str(person.id)}],
        },
    )
    search = post(admin_client, "service-searches/", {"kind": "hotel", "criteria": criteria(hotel_id)})
    jobs()
    offers = admin_client.get(f"/api/v1/service-searches/{search['search_id']}/offers/").json()
    assert offers["count"] == 1
    picked = offers["results"][0]
    assert picked["itinerary"]["property_name"] == TEST_HOTELS[hotel_id]
    assert picked["itinerary"]["room"] == "Standard Double"
    assert picked["fare"]["cancellation_rules"]
    assert "hotelbook" not in picked and "raw_snapshot" not in picked
    post(admin_client, f"service-offers/{picked['id']}/revalidate/")
    service = post(admin_client, f"orders/{order['id']}/services/", {"offer_id": picked["id"]})
    workflow = post(admin_client, "booking-workflows/", {"order": order["id"], "services": [service["id"]]})
    preflight = post(admin_client, f"booking-workflows/{workflow['id']}/preflight/")
    assert preflight["ok"] and not preflight["price_changes"]
    return workflow, service


def start(admin_client, workflow):
    post(admin_client, f"booking-workflows/{workflow['id']}/start/", key="start-hb")
    jobs()
    return admin_client.get(f"/api/v1/booking-workflows/{workflow['id']}/status/").json()


@pytest.mark.parametrize("hotel_id", list(TEST_HOTELS))
def test_full_crm_flow(admin_client, tenant, hb_supplier, gateway, hotel_id):
    gateway.current = offer(hotel_id)
    workflow, service = ready_booking(admin_client, tenant, hb_supplier, hotel_id)
    result = start(admin_client, workflow)
    assert result["status"] == "completed"
    item = result["items"][0]
    assert item["status"] == "booked" and len(item["locator"]) == 32
    adapter = get_adapter("hotelbook")
    assert adapter.retrieve_booking(context(tenant, hb_supplier), item["locator"])["status"] == "booked"
    post(admin_client, f"booking-workflows/{workflow['id']}/status-inquiry/", {"item": item["id"]})
    jobs()
    post(admin_client, f"booking-workflows/{workflow['id']}/cancel/", key="cancel-hb")
    jobs()
    stored = OrderService.objects.get(pk=service["id"])
    assert stored.status == "cancelled"
    assert stored.provider_snapshot["hotelbook_booking"]["items"][0]["itemId"] == ITEM_ID
    assert stored.supplier_cost == Decimal("120.25")
    post(admin_client, f"booking-workflows/{workflow['id']}/status-inquiry/", {"item": item["id"]})
    jobs()
    stored.refresh_from_db()
    assert stored.status == "cancelled"
    cancelled_state = admin_client.get(f"/api/v1/booking-workflows/{workflow['id']}/status/").json()
    assert cancelled_state["status"] == "cancelled"
    assert cancelled_state["items"][0]["status"] == "compensated"
    assert stored.provider_snapshot["hotelbook_booking"]["items"][0]["status"] == "CANCELED"
    paths = [r[1] for r in gateway.calls]
    assert paths.count("/api/v1/ru/hotel/gateway/book") == 1
    assert paths.count("/api/v1/ru/gateway/login") == 1
    logs = json.dumps(list(IntegrationLog.objects.values("request_sanitized", "response_sanitized")))
    assert "fixture-password" not in logs and "fixture-login" not in logs and "fixture-jwt" not in logs


@pytest.mark.parametrize("hotel_id", list(TEST_HOTELS)[1:])
def test_remaining_test_hotels(tenant, hb_supplier, gateway, hotel_id):
    gateway.current = offer(hotel_id)
    adapter = get_adapter("hotelbook")
    offers = adapter.search(context(tenant, hb_supplier), "hotel", criteria(hotel_id))
    assert offers[0]["itinerary"]["property_name"] == TEST_HOTELS[hotel_id]
    assert offers[0]["hotelbook"]["hotel_id"] == hotel_id
    assert adapter.revalidate(context(tenant, hb_supplier), offers[0])["status"] == "valid"
    assert adapter.fare_rules(context(tenant, hb_supplier), offers[0])["cancel"]


def test_search_drains_finished_pages(tenant, hb_supplier, gateway):
    adapter = HotelbookAdapter()
    adapter.page_size = 1
    second = offer()
    second["offerId"] = "second-rate"
    gateway.pages = [
        {"finished": False, "totalOffers": 0, "searchOffers": []},
        {"finished": True, "totalOffers": 2, "searchOffers": [offer()]},
        {"finished": True, "totalOffers": 2, "searchOffers": [second]},
    ]
    offers = adapter.search(context(tenant, hb_supplier), "hotel", criteria())
    assert len(offers) == 2
    assert [c[2]["offset"] for c in gateway.calls if "/results/" in c[1]] == [["0"], ["0"], ["1"]]


@pytest.mark.parametrize(
    "code,status,expected",
    [
        ("ERR013#1", 401, "AUTH_ERROR"),
        ("ERR013#5", 403, "AUTH_ERROR"),
        ("ERR018#1", 400, "AVAILABILITY_CONFLICT"),
        ("ERR003#5", 500, "AVAILABILITY_CONFLICT"),
        ("ERR006#2", 400, "PRICE_CHANGED"),
        ("ERR006#9", 400, "RATE_CHANGED"),
        ("ERRH003#1", 400, "RATE_UNAVAILABLE"),
        ("ERR004#1", 400, "PROVIDER_ERROR"),
    ],
)
def test_provider_errors_normalized(tenant, hb_supplier, gateway, code, status, expected):
    gateway.failure = ("/search/details", (status, code))
    adapter = get_adapter("hotelbook")
    snapshot = adapter.search(context(tenant, hb_supplier), "hotel", criteria())[0]
    with pytest.raises(AdapterError) as exc:
        adapter.revalidate(context(tenant, hb_supplier), snapshot)
    assert exc.value.code == expected
    assert "Do not expose" not in str(exc.value)


def test_rejected_token_refreshes_once(tenant, hb_supplier, gateway):
    gateway.fail_once_auth = True
    get_adapter("hotelbook").search(context(tenant, hb_supplier), "hotel", criteria())
    assert sum(c[1].endswith("/gateway/login") for c in gateway.calls) == 2


def test_preflight_uses_real_adapter_and_blocks_unavailable(admin_client, tenant, hb_supplier, gateway):
    workflow, _ = ready_booking(admin_client, tenant, hb_supplier)
    gateway.failure = ("/search/details", (400, "ERR018#1"))
    result = post(admin_client, f"booking-workflows/{workflow['id']}/preflight/")
    assert not result["ok"] and result["blocking_errors"][0]["code"] == "AVAILABILITY_CONFLICT"


def test_price_change_persisted_and_confirmed_with_markup(admin_client, tenant, hb_supplier, gateway):
    SupplierMarkupRule.objects.create(
        tenant=tenant, supplier=hb_supplier, service_kind="hotel", amount_type="percent", amount_value=10
    )
    workflow, service = ready_booking(admin_client, tenant, hb_supplier)
    gateway.current["price"]["amount"] = 150.00
    result = post(admin_client, f"booking-workflows/{workflow['id']}/preflight/")
    assert result["price_changes"][0]["new"] == "165.00"
    stored = OrderService.objects.get(pk=service["id"])
    assert stored.supplier_cost == Decimal("150")
    response = admin_client.post(
        f"/api/v1/booking-workflows/{workflow['id']}/start/",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY="no-confirm",
    )
    assert response.status_code == 409
    post(admin_client, f"booking-workflows/{workflow['id']}/start/", {"confirm": True}, key="confirmed")
    jobs()
    assert OrderService.objects.get(pk=service["id"]).status == "booked"


def test_offer_revalidate_updates_attach_snapshot(admin_client, tenant, hb_supplier, gateway):
    search = post(admin_client, "service-searches/", {"kind": "hotel", "criteria": criteria()})
    jobs()
    picked = ServiceOffer.objects.get(session_id=search["search_id"])
    gateway.current["price"]["amount"] = 175.50
    result = post(admin_client, f"service-offers/{picked.id}/revalidate/")
    assert result["revalidation"]["status"] == "price_changed"
    picked.refresh_from_db()
    assert picked.price_amount == Decimal("175.50") and picked.raw_snapshot["price"]["amount"] == "175.5"


def test_pending_confirmation_does_not_mark_booked(admin_client, tenant, hb_supplier, gateway):
    workflow, service = ready_booking(admin_client, tenant, hb_supplier)
    gateway.item_status = "CONFIRM_IN_PROGRESS"
    result = start(admin_client, workflow)
    item = result["items"][0]
    assert item["status"] == "unknown" and OrderService.objects.get(pk=service["id"]).status == "approval"
    gateway.item_status = "CONFIRMED"
    post(admin_client, f"booking-workflows/{workflow['id']}/status-inquiry/", {"item": item["id"]})
    jobs()
    assert OrderService.objects.get(pk=service["id"]).status == "booked"


def test_booking_timeout_requires_inquiry_and_never_rebooks(admin_client, tenant, hb_supplier, gateway):
    workflow, service = ready_booking(admin_client, tenant, hb_supplier)
    gateway.failure = ("/hotel/gateway/book", TimeoutError())
    item = start(admin_client, workflow)["items"][0]
    assert item["status"] == "unknown" and item["error_code"] == "BOOKING_UNKNOWN"
    gateway.failure = None
    post(admin_client, f"booking-workflows/{workflow['id']}/status-inquiry/", {"item": item["id"]})
    jobs()
    assert OrderService.objects.get(pk=service["id"]).status == "booked"
    assert sum(c[1].endswith("/hotel/gateway/book") for c in gateway.calls) == 1


def test_multi_item_cancel_and_pending_confirmation(admin_client, tenant, hb_supplier, gateway):
    workflow, service = ready_booking(admin_client, tenant, hb_supplier)
    gateway.items.append("0a04ef24-24a4-4000-8000-000000000002")
    item = start(admin_client, workflow)["items"][0]
    gateway.cancel_status = "CONFIRM_IN_PROGRESS"
    post(admin_client, f"booking-workflows/{workflow['id']}/cancel/", key="pending-cancel")
    jobs()
    assert OrderService.objects.get(pk=service["id"]).status == "booked"
    gateway.item_status = "CANCELED"
    post(admin_client, f"booking-workflows/{workflow['id']}/status-inquiry/", {"item": item["id"]})
    jobs()
    assert OrderService.objects.get(pk=service["id"]).status == "cancelled"
    assert sum(c[1].endswith("/cancel") for c in gateway.calls) == 2


def test_credentials_tenant_isolation(tenant, other_tenant, hb_supplier, gateway):
    with pytest.raises(AdapterError, match="Нет активных"):
        get_adapter("hotelbook").search(context(other_tenant, hb_supplier), "hotel", criteria())
    assert not gateway.calls


def test_test_hotel_restriction(tenant, hb_supplier, gateway):
    with pytest.raises(AdapterError) as exc:
        get_adapter("hotelbook").search(
            context(tenant, hb_supplier), "hotel", {**criteria(), "hotelbook": {"hotels": [9999]}}
        )
    assert exc.value.code == "TEST_HOTEL_REQUIRED"
    assert not gateway.calls


def test_no_credentials_never_falls_back_to_mock(admin_client, tenant, hb_supplier, gateway):
    workflow, service = ready_booking(admin_client, tenant, hb_supplier)
    SupplierCredential.objects.filter(supplier=hb_supplier).update(status="failed")
    result = post(admin_client, f"booking-workflows/{workflow['id']}/preflight/")
    assert not result["ok"] and result["blocking_errors"][0]["code"] == "PROVIDER_NOT_CONFIGURED"
    assert BookingWorkflowItem.objects.get(workflow_id=workflow["id"]).status == "pending"


def test_failed_second_preflight_revokes_start(admin_client, tenant, hb_supplier, gateway):
    workflow, _ = ready_booking(admin_client, tenant, hb_supplier)
    gateway.failure = ("/search/details", TimeoutError())
    assert not post(admin_client, f"booking-workflows/{workflow['id']}/preflight/")["ok"]
    response = admin_client.post(
        f"/api/v1/booking-workflows/{workflow['id']}/start/",
        {"confirm": True},
        format="json",
        HTTP_IDEMPOTENCY_KEY="revoked",
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "PREFLIGHT_REQUIRED"


def test_tariff_changes_need_confirmation(admin_client, tenant, hb_supplier, gateway):
    workflow, _ = ready_booking(admin_client, tenant, hb_supplier)
    gateway.current["finePolicies"]["cancel"][0]["from"] = None
    result = post(admin_client, f"booking-workflows/{workflow['id']}/preflight/")
    assert result["warnings"][0]["code"] == "RATE_CHANGED"
    response = admin_client.post(
        f"/api/v1/booking-workflows/{workflow['id']}/start/", {}, format="json", HTTP_IDEMPOTENCY_KEY="terms"
    )
    assert response.status_code == 409


def test_book_price_race_is_not_sent(admin_client, tenant, hb_supplier, gateway):
    workflow, service = ready_booking(admin_client, tenant, hb_supplier)
    gateway.current["price"]["amount"] = 999
    result = start(admin_client, workflow)
    assert result["items"][0]["error_code"] == "PRICE_CHANGED"
    assert not any(c[1].endswith("/hotel/gateway/book") for c in gateway.calls)
    assert OrderService.objects.get(pk=service["id"]).status == "proposed"


def test_guest_count_mismatch_is_not_sent(admin_client, tenant, hb_supplier, gateway):
    workflow, service = ready_booking(admin_client, tenant, hb_supplier)
    OrderService.objects.get(pk=service["id"]).passengers.update(status="cancelled")
    result = start(admin_client, workflow)
    assert result["items"][0]["error_code"] == "GUEST_ROOM_MISMATCH"
    assert not any(c[1].endswith("/hotel/gateway/book") for c in gateway.calls)


def test_no_duplicate_booking_of_concrete_offer(admin_client, tenant, hb_supplier, gateway):
    workflow, _ = ready_booking(admin_client, tenant, hb_supplier)
    assert start(admin_client, workflow)["items"][0]["status"] == "booked"
    # A second CRM service referencing the same search offer is not a new hotel room.
    picked = ServiceOffer.objects.first()
    order = post(
        admin_client,
        "orders/",
        {
            "client_person": str(Person.objects.first().id),
            "participants": [{"person": str(Person.objects.first().id)}],
        },
    )
    service = post(admin_client, f"orders/{order['id']}/services/", {"offer_id": str(picked.id)})
    duplicate = post(admin_client, "booking-workflows/", {"order": order["id"], "services": [service["id"]]})
    assert post(admin_client, f"booking-workflows/{duplicate['id']}/preflight/")["ok"]
    post(admin_client, f"booking-workflows/{duplicate['id']}/start/", key="duplicate-start")
    jobs()
    item = BookingWorkflowItem.objects.get(workflow_id=duplicate["id"])
    assert item.error_code == "DUPLICATE_BOOKING"
    assert sum(c[1].endswith("/hotel/gateway/book") for c in gateway.calls) == 1


def test_service_cancel_runs_provider_workflow(admin_client, tenant, hb_supplier, gateway):
    workflow, service = ready_booking(admin_client, tenant, hb_supplier)
    start(admin_client, workflow)
    stored = OrderService.objects.get(pk=service["id"])
    post(admin_client, f"services/{stored.id}/cancel/", {"version": stored.version})
    jobs()
    stored.refresh_from_db()
    assert stored.status == "cancelled"
    assert any(c[1].endswith("/cancel") for c in gateway.calls)


def test_service_cannot_fake_a_provider_booking(admin_client, tenant, hb_supplier, gateway):
    _, service = ready_booking(admin_client, tenant, hb_supplier)
    stored = OrderService.objects.get(pk=service["id"])
    response = admin_client.post(
        f"/api/v1/services/{stored.id}/book/", {"version": stored.version}, format="json"
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "BOOKING_WORKFLOW_REQUIRED"


def test_booking_failure_normalized(admin_client, tenant, hb_supplier, gateway):
    workflow, _ = ready_booking(admin_client, tenant, hb_supplier)
    gateway.failure = ("/hotel/gateway/book", (400, "ERR004#1"))
    assert start(admin_client, workflow)["items"][0]["error_code"] == "BOOKING_ERROR"


def test_order_creation_timeout_recovers_by_client_reference(admin_client, tenant, hb_supplier, gateway):
    workflow, service = ready_booking(admin_client, tenant, hb_supplier)
    gateway.failure = ("/order/gateway/orders", TimeoutError())
    item = start(admin_client, workflow)["items"][0]
    assert item["status"] == "unknown"
    gateway.failure = None
    gateway.order_has_items = False
    post(admin_client, f"booking-workflows/{workflow['id']}/status-inquiry/", {"item": item["id"]})
    jobs()
    assert OrderService.objects.get(pk=service["id"]).status == "proposed"
    assert BookingWorkflowItem.objects.get(pk=item["id"]).status == "unknown"
    assert sum(c[1].endswith("/order/gateway/orders") and c[0] == "POST" for c in gateway.calls) == 1


def test_connection_check_authenticates(tenant, hb_supplier, gateway):
    from suppliers.job_handlers import verify_supplier_credentials

    assert verify_supplier_credentials(hb_supplier)["status"] == "connected"
    assert any(c[1].endswith("/gateway/login") for c in gateway.calls)


def test_invalid_login_does_not_report_connected(tenant, hb_supplier, gateway, monkeypatch):
    from suppliers.job_handlers import verify_supplier_credentials

    def reject(request, timeout):
        Gateway.raise_error(request, 401, "ERR013#1")

    monkeypatch.setattr(gateway, "open", reject)
    assert verify_supplier_credentials(hb_supplier)["status"] == "failed"
    assert SupplierCredential.objects.get(supplier=hb_supplier).status == "failed"


def test_raw_credentials_are_encrypted_in_database(tenant, hb_supplier):
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT encrypted_secrets FROM suppliers_credential WHERE supplier_id = %s", [hb_supplier.id]
        )
        stored = cursor.fetchone()[0]
    assert stored.startswith("enc$1$") and "fixture-password" not in stored


def test_expired_offer_requires_new_search(tenant, hb_supplier, gateway):
    adapter = get_adapter("hotelbook")
    picked = adapter.search(context(tenant, hb_supplier), "hotel", criteria())[0]
    picked["expires_at"] = "2020-01-01T00:00:00Z"
    with pytest.raises(AdapterError) as exc:
        adapter.revalidate(context(tenant, hb_supplier), picked)
    assert exc.value.code == "RATE_UNAVAILABLE"


def test_pending_cancel_does_not_repeat_mutation(admin_client, tenant, hb_supplier, gateway):
    workflow, _ = ready_booking(admin_client, tenant, hb_supplier)
    item = start(admin_client, workflow)["items"][0]
    gateway.cancel_status = "CONFIRM_IN_PROGRESS"
    adapter = get_adapter("hotelbook")
    ctx = context(tenant, hb_supplier)
    with pytest.raises(AdapterError, match="ожидается подтверждение"):
        adapter.cancel(ctx, item["locator"])
    with pytest.raises(AdapterError):
        adapter.cancel(ctx, item["locator"])
    assert sum(c[1].endswith("/cancel") for c in gateway.calls) == 1


def test_required_end_customer_comes_from_crm_order(admin_client, tenant, hb_supplier, gateway):
    gateway.current["isEndCustomerNeeded"] = True
    workflow, _ = ready_booking(admin_client, tenant, hb_supplier)
    assert start(admin_client, workflow)["items"][0]["status"] == "booked"
    assert (
        next(c[3] for c in gateway.calls if c[1].endswith("/order/gateway/orders") and c[0] == "POST")[
            "customer"
        ]["type"]
        == "PRIVATE"
    )


def test_missing_hotel_search_fails_without_offers_or_booking(admin_client, tenant, hb_supplier, gateway):
    # A missing/deleted hotel must fail safely even if its ID is sandbox-allowlisted.
    gateway.failure = ("/hotel/gateway/search", (400, "ERR001#4"))
    search = post(admin_client, "service-searches/", {"kind": "hotel", "criteria": criteria(131687)})
    jobs()
    status = admin_client.get(f"/api/v1/service-searches/{search['search_id']}/").json()
    assert status["status"] == "failed"
    run = status["provider_runs"][0]
    assert run["provider_adapter"] == "hotelbook"
    assert run["error_code"] == "SEARCH_CRITERIA_INVALID"
    assert not ServiceOffer.objects.filter(session_id=search["search_id"]).exists()
    assert not OrderService.objects.exists()
    assert not BookingWorkflowItem.objects.exists()
    assert not any(c[1].endswith(("/order/gateway/orders", "/hotel/gateway/book")) for c in gateway.calls)
    error = get_adapter("hotelbook")._error(400, {"details": {"errorCode": "ERR001#4"}}, "search")
    assert error.category == "validation" and not error.retry_safe
    assert "не найдены" in str(error)


def test_inactive_hotelbook_credentials_never_search_mock(admin_client, tenant, hb_supplier, gateway):
    SupplierCredential.objects.filter(supplier=hb_supplier).update(status="failed")
    search = post(admin_client, "service-searches/", {"kind": "hotel", "criteria": criteria()})
    jobs()
    status = admin_client.get(f"/api/v1/service-searches/{search['search_id']}/").json()
    assert status["status"] == "failed"
    assert status["provider_runs"][0]["provider_adapter"] == "hotelbook"
    assert status["provider_runs"][0]["error_code"] == "PROVIDER_NOT_CONFIGURED"
    assert ServiceOffer.objects.filter(session_id=search["search_id"]).count() == 0
    assert not gateway.calls


def test_unknown_booking_blocks_local_order_cancellation(
    admin_client, admin_user, tenant, hb_supplier, gateway
):
    from common.errors import ApiError
    from common.models import BackgroundJob
    from orders.job_handlers import cancel_order_job

    workflow, service = ready_booking(admin_client, tenant, hb_supplier)
    gateway.failure = ("/hotel/gateway/book", TimeoutError())
    assert start(admin_client, workflow)["items"][0]["status"] == "unknown"
    order = OrderService.objects.get(pk=service["id"]).order
    job = BackgroundJob.objects.create(
        tenant=tenant,
        kind="orders.cancel",
        payload={"order_id": str(order.id), "user_id": str(admin_user.id)},
    )
    with pytest.raises(ApiError) as exc:
        cancel_order_job(job)
    assert exc.value.code == "PROVIDER_CANCEL_REQUIRED"
    order.refresh_from_db()
    assert order.status != "cancelled"


def test_private_customer_omits_legal_company_fields():
    from types import SimpleNamespace

    from booking.job_handlers import _booking_customer

    person = SimpleNamespace(full_name="LOCAL HOTELBOOKTEST")
    assert _booking_customer(SimpleNamespace(client_person=person, client_company=None)) == {"type": "PRIVATE"}
    company = SimpleNamespace(legal_name="Test Company", tax_id="1234567890", legal_address="Test address")
    assert _booking_customer(SimpleNamespace(client_person=None, client_company=company)) == {
        "type": "LEGAL", "name": "Test Company", "inn": "1234567890", "address": "Test address"
    }
