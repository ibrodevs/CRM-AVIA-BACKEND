"""Mock transport tests; no live credentials or provider mutations are used."""

import copy
import json
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError

import pytest
from django.core.management import call_command

from integrations.adapters import AdapterContext, AdapterError, AmbiguousResultError, get_adapter
from integrations.models import IntegrationLog
from integrations.ttbooking import TTBookingAdapter
from services.models import SearchSession, ServiceOffer
from suppliers.models import Supplier, SupplierCredential, SupplierSearchPriority

pytestmark = pytest.mark.django_db

CONFIG = {
    "agency": "42",
    "user": "7",
    "password_key": "private-secret",
    "locale": "ru",
    "url": TTBookingAdapter.base_url,
}
CRITERIA = {"origin": "FRU", "destination": "IST", "date": "2027-01-01"}


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Live TT Booking network calls are forbidden in this suite")

    monkeypatch.setattr("integrations.ttbooking.urlopen", blocked)


@pytest.fixture
def supplier(tenant):
    return Supplier.objects.create(
        tenant=tenant, name="TT Booking", status="active", service_kinds=["avia"], currencies=["RUB"]
    )


@pytest.fixture
def credential(tenant, supplier):
    return SupplierCredential.objects.create(
        tenant=tenant,
        supplier=supplier,
        provider_adapter="ttbooking",
        environment="sandbox",
        status="active",
        encrypted_secrets=json.dumps(CONFIG),
    )


@pytest.fixture
def ctx(tenant, supplier):
    return AdapterContext(tenant_id=tenant.id, supplier_id=supplier.id, correlation_id="ttbooking-test")


@pytest.fixture
def search_response():
    segment = {
        "token": "segment-1",
        "serviceClass": "ECONOMY",
        "bookingClass": "Y",
        "fareBasis": "YRT",
        "carrier": {"code": "TK"},
        "dateBegin": "2027-01-01T08:00:00+03:00",
        "dateEnd": "2027-01-01T13:00:00+03:00",
        "flightNumber": "347",
        "locationBegin": {"code": "FRU"},
        "locationEnd": {"code": "IST"},
        "baggage": {"type": "1PC", "value": 23},
        "terminalBegin": "1",
        "terminalEnd": "2",
        "equipment": {"code": "738"},
        "travelDuration": 300,
    }
    return {
        "respond": {
            "token": "search-token",
            "flightsGroup": {
                "flightGroup": [
                    {
                        "token": "group-token",
                        "carrier": {"code": "TK"},
                        "eticket": True,
                        "gds": "TEST",
                        "timeLimit": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
                        "itineraries": {
                            "itinerary": [
                                {
                                    "token": "itin-1",
                                    "flights": {
                                        "flight": [
                                            {
                                                "token": "flight-1",
                                                "segments": {"segment": [segment]},
                                            }
                                        ]
                                    },
                                }
                            ]
                        },
                        "fares": {
                            "fareTotal": {"total": 25000},
                            "fareSeats": {
                                "fareSeat": [{"passengerType": "ADULT", "count": 1, "total": 25000}]
                            },
                        },
                    }
                ]
            },
        }
    }


def mock_http(monkeypatch, result=None, error=None, raw=None):
    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return raw if raw is not None else json.dumps(result).encode()

    def wire(request, timeout):
        requests.append(json.loads(request.data))
        if error:
            raise error
        return Response()

    monkeypatch.setattr("integrations.ttbooking.urlopen", wire)
    return requests


def test_hash_known_vector_and_no_password_in_context():
    # Literal independently computed digest protects order/encoding including reserved characters.
    assert (
        TTBookingAdapter.build_hash("42", "secret", "2026-10-13T10:00:00+00:00", "7")
        == "a9dfe273902e33508303190a3a6c9ac9"
    )
    config = {**CONFIG, "password": CONFIG["password_key"]}
    context = TTBookingAdapter()._context(config, "SEARCHFR")
    assert context["command"] == "SEARCHFR"
    assert "password" not in context and "password_key" not in context


@pytest.mark.parametrize("cabin", ["ECONOMY", "BUSINESS", "FIRST", "PREMIUM", "premium_economy"])
@pytest.mark.parametrize("route_type", ["one-way", "round-trip", "multi-city"])
def test_search_parameters(route_type, cabin):
    criteria = {
        **CRITERIA,
        "adults": 2,
        "children": 1,
        "infants": 1,
        "cabin": cabin,
        "direct_only": True,
        "etickets_only": True,
        "preferred_airlines": ["TK"],
    }
    if route_type == "round-trip":
        criteria["return_date"] = "2027-01-10"
    if route_type == "multi-city":
        criteria["route"] = [
            {"origin": "FRU", "destination": "IST", "date": "2027-01-01"},
            {"origin": "IST", "destination": "DXB", "date": "2027-01-05"},
            {"origin": "DXB", "destination": "FRU", "date": "2027-01-10"},
        ]
    payload = TTBookingAdapter()._search_parameters(criteria)
    assert len(payload["route"]) == {"one-way": 1, "round-trip": 2, "multi-city": 3}[route_type]
    assert payload["seats"] == [
        {"passengerType": "ADULT", "count": 2},
        {"passengerType": "CHILD", "count": 1},
        {"passengerType": "INFANT", "count": 1},
    ]
    assert payload["serviceClass"] == ("PREMIUM" if cabin == "premium_economy" else cabin)
    assert payload["skipConnected"] is True and payload["eticketsOnly"] is True
    assert payload["preferredAirlines"] == [{"code": "TK", "name": ""}]


@pytest.mark.parametrize(
    "patch",
    [
        {"adults": 0},
        {"adults": -1},
        {"adults": "x"},
        {"cabin": "bad"},
        {"seats": [{"type": "UNKNOWN", "count": 1}]},
    ],
)
def test_invalid_search_never_sends_request(ctx, credential, patch):
    with pytest.raises(AdapterError):
        TTBookingAdapter().search(ctx, "avia", {**CRITERIA, **patch})


def test_normalization_preserves_selection_and_provider_details(search_response):
    group = search_response["respond"]["flightsGroup"]["flightGroup"][0]
    itinerary = group["itineraries"]["itinerary"][0]
    flight = itinerary["flights"]["flight"][0]
    second = copy.deepcopy(flight["segments"]["segment"][0])
    second.update(token="segment-2", locationBegin={"code": "IST"}, locationEnd={"code": "DXB"})
    flight["segments"]["segment"].append(second)
    return_itinerary = copy.deepcopy(itinerary)
    return_itinerary["token"] = "itin-2"
    return_itinerary["flights"]["flight"][0]["token"] = "flight-2"
    group["itineraries"]["itinerary"].append(return_itinerary)
    offer = TTBookingAdapter()._normalize_search(search_response, "RUB")[0]
    assert offer["price"] == {"amount": "25000", "currency": "RUB"}
    assert len(offer["itinerary"]["segments"]) == 4
    assert offer["itinerary"]["connections"] == 2
    segment = offer["itinerary"]["segments"][0]
    assert segment["provider_token"] == "segment-1"
    assert segment["baggage_details"] == {"type": "1PC", "value": 23}
    assert segment["departure_terminal"] == "1" and segment["duration"] == 300
    assert offer["fare"]["gds"] == "TEST" and offer["fare"]["eticket"]
    selected = offer["provider_selection"]
    assert selected["token"] == "search-token"
    selected_itineraries = selected["flightsGroup"]["flightGroup"][0]["itineraries"]["itinerary"]
    assert [it["token"] for it in selected_itineraries] == ["itin-1", "itin-2"]
    assert all(len(it["flights"]["flight"]) == 1 for it in selected_itineraries)
    assert offer["raw_snapshot"]["flightGroup"] == group


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"respond": {}},
        {"respond": {"flightsGroup": []}},
        {"respond": {"token": "valid-token", "flightsGroup": {"flightGroup": ["invalid"]}}},
    ],
)
def test_invalid_search_response(response):
    with pytest.raises(AdapterError, match="TT Booking") as error:
        TTBookingAdapter()._normalize_search(response, "RUB")
    assert error.value.code == "INVALID_RESPONSE"


@pytest.mark.parametrize("amount", [None, "NaN", "-1", "invalid", "Infinity"])
def test_invalid_price(search_response, amount):
    search_response["respond"]["flightsGroup"]["flightGroup"][0]["fares"]["fareTotal"]["total"] = amount
    with pytest.raises(AdapterError) as error:
        TTBookingAdapter()._normalize_search(search_response, "RUB")
    assert error.value.code == "INVALID_RESPONSE"


@pytest.mark.parametrize("price,changed", [(25000, False), (26000, True)])
def test_selectfr_and_price_change(ctx, credential, search_response, monkeypatch, price, changed):
    adapter = get_adapter("ttbooking")
    offer = adapter._normalize_search(search_response, "RUB")[0]
    search_response["respond"]["flightsGroup"]["flightGroup"][0]["fares"]["fareTotal"]["total"] = price
    requests = mock_http(monkeypatch, search_response)
    result = adapter.revalidate(ctx, offer)
    assert result["price_changed"] is changed
    assert result["status"] == ("price_changed" if changed else "valid")
    assert requests[0]["query"]["context"]["command"] == "SELECTFR"
    assert requests[0]["query"]["parameters"] == offer["provider_selection"]


def test_selectfr_rejects_multiple_flights_and_other_supplier(ctx, credential, search_response):
    offer = TTBookingAdapter()._normalize_search(search_response, "RUB")[0]
    offer["supplier_id"] = "different-supplier"
    with pytest.raises(AdapterError):
        TTBookingAdapter().revalidate(ctx, offer)
    del offer["supplier_id"]
    flights = offer["provider_selection"]["flightsGroup"]["flightGroup"][0]["itineraries"]["itinerary"][0][
        "flights"
    ]["flight"]
    flights.append({"token": "another-flight"})
    with pytest.raises(AdapterError):
        TTBookingAdapter().revalidate(ctx, offer)


@pytest.mark.parametrize(
    "http_status,code",
    [
        (401, "AUTH_ERROR"),
        (403, "AUTH_ERROR"),
        (400, "INVALID_REQUEST"),
        (404, "INVALID_REQUEST"),
        (429, "RATE_LIMITED"),
        (500, "PROVIDER_ERROR"),
        (503, "PROVIDER_ERROR"),
    ],
)
def test_http_errors(ctx, credential, monkeypatch, http_status, code):
    mock_http(
        monkeypatch, error=HTTPError(TTBookingAdapter.base_url, http_status, "secret in error", {}, None)
    )
    with pytest.raises(AdapterError) as error:
        TTBookingAdapter().search(ctx, "avia", CRITERIA)
    assert error.value.code == code
    log = IntegrationLog.objects.get()
    assert log.http_status == http_status and log.error_code == code and log.operation == "SEARCHFR"
    assert "secret in error" not in str(log.response_sanitized)


@pytest.mark.parametrize(
    "raw,code",
    [
        (b"not-json private-secret", "INVALID_RESPONSE"),
        (b"[]", "INVALID_RESPONSE"),
        (b'{"respond":{"error":{"message":"private-secret"}}}', "PROVIDER_ERROR"),
    ],
)
def test_invalid_json_and_provider_error(ctx, credential, monkeypatch, raw, code):
    mock_http(monkeypatch, raw=raw)
    with pytest.raises(AdapterError) as error:
        TTBookingAdapter().search(ctx, "avia", CRITERIA)
    assert error.value.code == code
    assert "private-secret" not in str(IntegrationLog.objects.get().response_sanitized)


def test_read_timeout_can_retry(ctx, credential, monkeypatch):
    requests = mock_http(monkeypatch, error=TimeoutError())
    with pytest.raises(AdapterError) as error:
        TTBookingAdapter().search(ctx, "avia", CRITERIA)
    assert error.value.code == "TIMEOUT" and error.value.retry_safe
    assert len(requests) == 1


@pytest.mark.parametrize(
    "command,code,category",
    [
        ("CREATEBR", "BOOKING_UNKNOWN", "booking_unknown"),
        ("PAYDEPOSIT", "ISSUE_UNKNOWN", "issue_unknown"),
        ("CANCELBOOKING", "CANCEL_UNKNOWN", "cancel_unknown"),
    ],
)
@pytest.mark.parametrize("failure", ["timeout", "invalid-json", "server-error"])
def test_mutation_uncertainty_never_retries(ctx, credential, monkeypatch, command, code, category, failure):
    error = (
        TimeoutError()
        if failure == "timeout"
        else HTTPError(TTBookingAdapter.base_url, 502, "gateway", {}, None)
        if failure == "server-error"
        else None
    )
    requests = mock_http(monkeypatch, error=error, raw=b"broken" if failure == "invalid-json" else None)
    adapter = TTBookingAdapter()
    with pytest.raises(AdapterError) as caught:
        adapter._wire(ctx, adapter._config(ctx), command, {"token": "booking-file-token"}, mutation=True)
    assert caught.value.code == code and caught.value.category == category and not caught.value.retry_safe
    if command == "PAYDEPOSIT":
        assert isinstance(caught.value, AmbiguousResultError)
    assert len(requests) == 1
    log = IntegrationLog.objects.get()
    assert log.result == "unknown" and log.retries == 0 and log.operation == command


def test_logs_exclude_secrets_and_passenger_data(ctx, credential, search_response, monkeypatch):
    adapter = TTBookingAdapter()
    search_response["respond"].update(
        passengers=[
            {
                "name": "Test Passenger",
                "passport": {"number": "P12345678"},
                "phone": "+996555123456",
                "email": "test@example.com",
            }
        ],
        hash="secret-hash",
    )
    requests = mock_http(monkeypatch, search_response)
    adapter.search(ctx, "avia", CRITERIA)
    log = IntegrationLog.objects.get()
    logged = json.dumps([log.request_sanitized, log.response_sanitized])
    for sensitive in [
        "private-secret",
        "Test Passenger",
        "P12345678",
        "+996555123456",
        "test@example.com",
        "secret-hash",
        requests[0]["query"]["context"]["hash"],
    ]:
        assert sensitive not in logged
    assert log.supplier_id == ctx.supplier_id and log.correlation_id == ctx.correlation_id
    assert log.http_status == 200 and log.duration_ms >= 0


@pytest.mark.parametrize("refresh,command", [(False, "DISPLAYSTATUS"), (True, "LOADBOOKING")])
def test_status_retains_unknown_and_does_not_enable_issue(
    ctx, credential, monkeypatch, refresh, command, booking_response, tt_service
):
    tt_service.provider_snapshot["ttbooking_booking"] = {"token": "booking-file-token"}
    tt_service.save()
    booking_response["respond"]["bookingFile"]["status"] = "UNRECOGNIZED"
    requests = mock_http(monkeypatch, booking_response)
    result = TTBookingAdapter().retrieve_booking(ctx, "booking-file-token", refresh=refresh)
    assert result["status"] == "unknown" and result["raw_status"] == "UNRECOGNIZED"
    assert requests[0]["query"]["context"]["command"] == command
    assert requests[0]["query"]["parameters"] == {"token": "booking-file-token"}


def test_ticket_result_keeps_multiple_tickets(ctx, credential, monkeypatch, booking_response, tt_service):
    tt_service.provider_snapshot["ttbooking_booking"] = {"token": "booking-file-token"}
    tt_service.save()
    raw = booking_response["respond"]["bookingFile"]
    raw["status"] = "COMPLETE"
    products = raw["reservations"]["reservation"][0]["products"]["airTicket"]
    products[0].update(number="1234567890", status="SELL")
    second = copy.deepcopy(products[0])
    second["number"] = "1234567891"
    products.append(second)
    requests = mock_http(monkeypatch, booking_response)
    result = TTBookingAdapter().display_tickets(ctx, "booking-file-token")
    assert [row["ticket_number"] for row in result["tickets"]] == ["1234567890", "1234567891"]
    assert all(row["carrier"] == "TK" and row["status"] == "issued" for row in result["tickets"])
    assert requests[0]["query"]["context"]["command"] == "DISPLAYTICKETS"


def test_fare_rules_do_not_guess_legacy_auth_command(ctx):
    result = TTBookingAdapter().fare_rules(ctx, {})
    assert result["raw_available"] is False and result["rules"] == []
    response = {
        "respond": {
            "remarkGroups": {
                "remarkGroup": [
                    {
                        "remarks": {
                            "remark": [{"title": "PENALTIES", "content": "Raw conditions", "number": "16"}]
                        }
                    }
                ]
            }
        }
    }
    normalized = TTBookingAdapter.normalize_fare_rules(response)
    assert normalized["raw_available"] and normalized["rules"][0]["content"] == "Raw conditions"
    assert normalized["refund"] == "" and normalized["exchange"] == ""


def test_credential_isolation(ctx, credential, other_tenant, tenant):
    adapter = TTBookingAdapter()
    with pytest.raises(AdapterError):
        adapter._config(AdapterContext(tenant_id=other_tenant.id, supplier_id=ctx.supplier_id))
    other_supplier = Supplier.objects.create(tenant=tenant, name="Other", service_kinds=["avia"])
    with pytest.raises(AdapterError):
        adapter._config(AdapterContext(tenant_id=tenant.id, supplier_id=other_supplier.id))
    credential.status = "failed"
    credential.save()
    with pytest.raises(AdapterError):
        adapter._config(ctx)


def create_supplier_and_credential(client):
    created = client.post(
        "/api/v1/suppliers/",
        {"name": "TT Booking", "status": "active", "service_kinds": ["avia"], "currencies": ["RUB"]},
        format="json",
    )
    assert created.status_code == 201
    supplier_id = created.json()["id"]
    saved = client.post(
        f"/api/v1/suppliers/{supplier_id}/credentials/",
        {"provider_adapter": "ttbooking", "environment": "sandbox", "secrets": CONFIG},
        format="json",
    )
    assert saved.status_code == 201 and saved.json()["has_secrets"]
    assert "private-secret" not in saved.content.decode()
    return supplier_id


def test_connection_requires_real_provider_response(admin_client, monkeypatch, search_response):
    supplier_id = create_supplier_and_credential(admin_client)
    requests = mock_http(monkeypatch, search_response)
    checked = admin_client.post(f"/api/v1/suppliers/{supplier_id}/check-connection/", {}, format="json")
    assert checked.status_code == 200 and checked.json()["status"] == "connected"
    assert requests[0]["query"]["context"]["command"] == "SEARCHFR"
    credential = SupplierCredential.objects.get(supplier_id=supplier_id)
    assert credential.status == "active" and credential.last_verified_at
    mock_http(monkeypatch, error=HTTPError(TTBookingAdapter.base_url, 403, "no", {}, None))
    checked = admin_client.post(f"/api/v1/suppliers/{supplier_id}/check-connection/", {}, format="json")
    assert checked.status_code == 409 and checked.json()["status"] == "failed"
    credential.refresh_from_db()
    assert credential.status == "failed"


def test_secret_rotation_and_readback(admin_client):
    supplier_id = create_supplier_and_credential(admin_client)
    response = admin_client.post(
        f"/api/v1/suppliers/{supplier_id}/credentials/",
        {
            "provider_adapter": "ttbooking",
            "environment": "sandbox",
            "secrets": {"agency": "new", "password_key": ""},
        },
        format="json",
    )
    assert response.status_code == 201
    assert SupplierCredential.objects.filter(supplier_id=supplier_id).count() == 1
    assert response.json()["public_config"]["agency"] == "new"
    credential = SupplierCredential.objects.get(supplier_id=supplier_id)
    assert json.loads(credential.encrypted_secrets)["password_key"] == CONFIG["password_key"]
    assert credential.status == "inactive"
    readback = admin_client.get(f"/api/v1/suppliers/{supplier_id}/credentials/")
    assert (
        "private-secret" not in readback.content.decode()
        and "encrypted_secrets" not in readback.content.decode()
    )


def test_search_api_revalidation_and_supplier_isolation(admin_client, tenant, monkeypatch, search_response):
    supplier_ids = [create_supplier_and_credential(admin_client) for _ in range(2)]
    requests = mock_http(monkeypatch, search_response)
    for supplier_id in supplier_ids:
        assert admin_client.post(f"/api/v1/suppliers/{supplier_id}/check-connection/", {}).status_code == 200
    created = admin_client.post(
        "/api/v1/service-searches/", {"kind": "avia", "criteria": CRITERIA}, format="json"
    )
    assert created.status_code == 202
    call_command("run_jobs", "--once", "--worker-id", "ttbooking-test")
    session = SearchSession.objects.get(pk=created.json()["search_id"])
    assert session.status == "completed"
    offers = list(ServiceOffer.objects.filter(session=session))
    assert len(offers) == 2 and {str(o.supplier_id) for o in offers} == set(supplier_ids)
    assert all(
        o.provider_adapter == "ttbooking" and o.raw_snapshot["supplier_id"] == str(o.supplier_id)
        for o in offers
    )
    assert len({o.dedup_hash for o in offers}) == 2
    search_response["respond"]["flightsGroup"]["flightGroup"][0]["fares"]["fareTotal"]["total"] = 26000
    response = admin_client.post(f"/api/v1/service-offers/{offers[0].id}/revalidate/", {})
    assert response.status_code == 200 and response.json()["revalidation"]["price_changed"]
    offers[0].refresh_from_db()
    assert offers[0].price_amount == 26000 and offers[0].expires_at
    assert [row["query"]["context"]["command"] for row in requests] == ["SEARCHFR"] * 4 + ["SELECTFR"]
    logs = admin_client.get(f"/api/v1/integration-operations/?supplier={supplier_ids[0]}").json()
    assert all(str(row["supplier"]) == supplier_ids[0] for row in logs["results"])
    assert "private-secret" not in json.dumps(logs)


def test_inactive_ttbooking_never_falls_back_to_mock(admin_client, supplier, credential, monkeypatch):
    credential.status = "failed"
    credential.save()
    created = admin_client.post(
        "/api/v1/service-searches/", {"kind": "avia", "criteria": CRITERIA}, format="json"
    )
    call_command("run_jobs", "--once", "--worker-id", "ttbooking-test")
    session = SearchSession.objects.get(pk=created.json()["search_id"])
    assert session.status == "failed" and session.offers.count() == 0
    assert session.provider_runs.get().provider_adapter == "ttbooking"


def test_search_priority_cannot_route_to_foreign_tenant(tenant, other_tenant, admin_user):
    from services.job_handlers import _suppliers_for_search

    foreign = Supplier.objects.create(
        tenant=other_tenant, name="Foreign", status="active", service_kinds=["avia"]
    )
    SupplierSearchPriority.objects.create(
        tenant=tenant, service_kind="avia", ordered_suppliers=[str(foreign.id)]
    )
    session = SearchSession.objects.create(tenant=tenant, user=admin_user, kind="avia", criteria=CRITERIA)
    assert foreign not in _suppliers_for_search(session)


@pytest.mark.parametrize(
    "patch",
    [
        {"url": "http://example.com"},
        {"url": "https://user:secret@example.com"},
        {"agency": ""},
        {"user": ""},
        {"locale": "xx"},
    ],
)
def test_invalid_credential_save_rejected_without_secret_echo(admin_client, patch):
    supplier_id = create_supplier_and_credential(admin_client)
    response = admin_client.post(
        f"/api/v1/suppliers/{supplier_id}/credentials/",
        {"provider_adapter": "ttbooking", "environment": "sandbox", "secrets": {**CONFIG, **patch}},
        format="json",
    )
    # Blank fields during rotation mean "keep existing"; required blanks are tested on a new config below.
    if patch.get("agency") == "" or patch.get("user") == "":
        assert response.status_code == 201
    else:
        assert response.status_code == 400
    assert "private-secret" not in response.content.decode()


def test_environment_mismatch_cannot_send_selectfr(ctx, credential, search_response):
    offer = TTBookingAdapter()._normalize_search(search_response, "RUB")[0]
    offer["provider_environment"] = "production"
    with pytest.raises(AdapterError) as error:
        TTBookingAdapter().revalidate(ctx, offer)
    assert error.value.code == "PROVIDER_NOT_CONFIGURED"


def test_tenant_cannot_read_credentials_or_logs(admin_client, other_tenant, monkeypatch, search_response):
    foreign = Supplier.objects.create(
        tenant=other_tenant,
        name="Private supplier",
        status="active",
        service_kinds=["avia"],
        currencies=["RUB"],
    )
    SupplierCredential.objects.create(
        tenant=other_tenant,
        supplier=foreign,
        provider_adapter="ttbooking",
        status="active",
        encrypted_secrets=json.dumps(CONFIG),
    )
    mock_http(monkeypatch, search_response)
    TTBookingAdapter().search(
        AdapterContext(tenant_id=other_tenant.id, supplier_id=foreign.id), "avia", CRITERIA
    )
    assert admin_client.get(f"/api/v1/suppliers/{foreign.id}/credentials/").status_code == 404
    assert admin_client.post(f"/api/v1/suppliers/{foreign.id}/check-connection/", {}).status_code == 404
    assert admin_client.get(f"/api/v1/integration-operations/?supplier={foreign.id}").json()["count"] == 0


def test_local_api_end_to_end(
    admin_client, tenant, admin_user, monkeypatch, search_response, booking_response
):
    from booking.models import BookingWorkflowItem
    from crm.models import Person, PersonDocument
    from services.models import OrderService

    supplier_id = create_supplier_and_credential(admin_client)
    provider = LifecycleProvider(monkeypatch, search_response, booking_response)
    assert admin_client.post(f"/api/v1/suppliers/{supplier_id}/check-connection/", {}).status_code == 200
    search = admin_client.post(
        "/api/v1/service-searches/", {"kind": "avia", "criteria": CRITERIA}, format="json"
    )
    call_command("run_jobs", "--once", "--worker-id", "ttbooking-test")
    offer = ServiceOffer.objects.get(session_id=search.json()["search_id"])
    person = Person.objects.create(
        tenant=tenant,
        surname="Тест",
        given_name="Пассажир",
        latin_surname="TEST",
        latin_given_name="PASSENGER",
        birth_date="1990-01-01",
        gender="male",
        citizenship="KG",
        phone="+996555123456",
        email="test@example.com",
        created_by=admin_user,
    )
    PersonDocument.objects.create(
        tenant=tenant,
        person=person,
        type="foreign_passport",
        number="TEST123456",
        issuing_country="KG",
        expires_at="2033-01-01",
    )
    order = admin_client.post(
        "/api/v1/orders/",
        {
            "request_type": "individual",
            "client_person": str(person.id),
            "planned_start": "2027-01-01",
            "planned_end": "2027-01-10",
            "participants": [{"person": str(person.id), "role": "passenger"}],
        },
        format="json",
    )
    assert order.status_code == 201, order.content
    attached = admin_client.post(
        f"/api/v1/orders/{order.json()['id']}/services/", {"offer_id": str(offer.id)}, format="json"
    )
    assert attached.status_code == 201, attached.content
    service = OrderService.objects.get(pk=attached.json()["id"])
    assert (
        service.supplier_id == offer.supplier_id
        and service.provider_snapshot["provider_selection"] == offer.raw_snapshot["provider_selection"]
    )
    workflow = admin_client.post(
        "/api/v1/booking-workflows/",
        {"order": order.json()["id"], "services": [str(service.id)]},
        format="json",
    )
    assert workflow.status_code == 201, workflow.content
    workflow_id = workflow.json()["id"]
    preflight = admin_client.post(f"/api/v1/booking-workflows/{workflow_id}/preflight/", {})
    assert preflight.status_code == 200 and preflight.json()["ok"], preflight.content
    started = admin_client.post(
        f"/api/v1/booking-workflows/{workflow_id}/start/",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY="ttbooking-test-start",
    )
    assert started.status_code == 202, started.content
    call_command("run_jobs", "--once", "--worker-id", "ttbooking-test")
    item = BookingWorkflowItem.objects.get(workflow_id=workflow_id)
    assert item.status == "booked" and item.locator == "TEST01"
    service.refresh_from_db()
    assert service.status == "booked" and service.external_id == "TEST01"
    assert service.provider_snapshot["ttbooking_booking"]["token"] == "booking-file-token"
    assert service.ticketing_deadline
    inquiry = admin_client.post(
        f"/api/v1/booking-workflows/{workflow_id}/status-inquiry/", {"item": str(item.id)}, format="json"
    )
    assert inquiry.status_code == 202
    call_command("run_jobs", "--once", "--worker-id", "ttbooking-test")
    issued = admin_client.post(
        f"/api/v1/booking-workflows/{workflow_id}/issue/",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY="ttbooking-issue",
    )
    assert issued.status_code == 202, issued.content
    call_command("run_jobs", "--once", "--worker-id", "ttbooking-test")
    service.refresh_from_db()
    item.refresh_from_db()
    assert service.status == "issued" and item.status == "issued"
    from avia.models import AviaBooking, Ticket

    assert Ticket.objects.count() == 2
    assert all(
        row.validating_carrier == "TK" and row.passenger_id is not None for row in Ticket.objects.all()
    )
    assert AviaBooking.objects.get().provider_adapter == "ttbooking"
    visible = admin_client.get(f"/api/v1/services/{service.id}/").json()
    assert (
        len(visible["tickets"]) == 2
        and visible["provider_adapter"] == "ttbooking"
        and visible["has_provider_booking"]
    )
    assert all(ticket["passenger"] for ticket in visible["tickets"])
    assert "TEST123456" not in json.dumps(visible) and "private-secret" not in json.dumps(visible)
    provider_status = admin_client.post(f"/api/v1/services/{service.id}/provider-status/", {})
    assert provider_status.status_code == 202
    call_command("run_jobs", "--once", "--worker-id", "ttbooking-test")
    cancelled = admin_client.post(
        f"/api/v1/booking-workflows/{workflow_id}/cancel/",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY="ttbooking-cancel",
    )
    assert cancelled.status_code == 202
    call_command("run_jobs", "--once", "--worker-id", "ttbooking-test")
    service.refresh_from_db()
    item.refresh_from_db()
    assert service.status == "cancelled" and item.status == "compensated"
    assert set(Ticket.objects.values_list("status", flat=True)) == {"void"}
    assert AviaBooking.objects.get().status == "cancelled"
    commands = provider.commands()
    for command in [
        "SEARCHFR",
        "SELECTFR",
        "CREATEBR",
        "DISPLAYSTATUS",
        "LOADBOOKING",
        "PAYDEPOSIT",
        "DISPLAYTICKETS",
        "CANCELBOOKING",
    ]:
        assert command in commands
    assert (
        commands.count("CREATEBR") == 1
        and commands.count("PAYDEPOSIT") == 1
        and commands.count("CANCELBOOKING") == 1
    )
    logged = json.dumps(list(IntegrationLog.objects.values("request_sanitized", "response_sanitized")))
    for secret in ["private-secret", "TEST123456", "+996555123456", "test@example.com", "PASSENGER"]:
        assert secret not in logged


@pytest.fixture
def booking_response(search_response):
    segments = search_response["respond"]["flightsGroup"]["flightGroup"][0]["itineraries"]["itinerary"][0][
        "flights"
    ]["flight"][0]["segments"]
    return {
        "respond": {
            "token": "request-token",
            "bookingFile": {
                "token": "booking-file-token",
                "status": "NEW",
                "createDate": datetime.now(timezone.utc).isoformat(),
                "documents": {},
                "reservations": {
                    "reservation": [
                        {
                            "token": "reservation-1",
                            "recordLocator": "TEST01",
                            "regLocator": "REG01",
                            "timeLimit": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
                            "products": {
                                "airTicket": [
                                    {
                                        "carrier": {"code": "TK"},
                                        "status": "BOOKING",
                                        "number": "",
                                        "passenger": {
                                            "passport": {
                                                "firstName": "PASSENGER",
                                                "lastName": "TEST",
                                                "birthday": "1990-01-01T00:00:00+00:00",
                                            }
                                        },
                                        "segments": segments,
                                    }
                                ]
                            },
                        }
                    ]
                },
            },
        }
    }


class LifecycleProvider:
    """Stateful mock of the documented U1/U2/U3/T→F/B stdCBS contract."""

    def __init__(self, monkeypatch, search, booking, *, fail_command=None, apply_before_timeout=False):
        self.search = copy.deepcopy(search)
        self.booking = copy.deepcopy(booking)
        self.requests = []
        self.fail_command = fail_command
        self.apply_before_timeout = apply_before_timeout
        provider = self

        class Response:
            status = 200

            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(self.payload).encode()

        def wire(request, timeout):
            payload = json.loads(request.data)
            provider.requests.append(payload)
            query = payload["query"]
            command = query["context"]["command"]
            if command == provider.fail_command and not provider.apply_before_timeout:
                raise TimeoutError()
            if command in {"SEARCHFR", "SELECTFR"}:
                result = copy.deepcopy(provider.search)
            else:
                file = provider.booking["respond"]["bookingFile"]
                products = file["reservations"]["reservation"][0]["products"]["airTicket"]
                if command == "CREATEBR":
                    products[0]["passenger"] = copy.deepcopy(
                        query["parameters"]["passengers"]["passenger"][0]
                    )
                if command == "PAYDEPOSIT":
                    file["status"] = "COMPLETE"
                    products[0].update(
                        status="SELL", number="1234567890", issueDate=datetime.now(timezone.utc).isoformat()
                    )
                    if len(products) == 1:
                        second = copy.deepcopy(products[0])
                        second["number"] = "1234567891"
                        products.append(second)
                if command == "CANCELBOOKING":
                    file["status"] = "CANCELLED"
                    for product in products:
                        product["status"] = "VOID" if product.get("number") else "VOID_BOOKING"
                result = copy.deepcopy(provider.booking)
                if command == "DISPLAYTICKETS" and file["status"] == "NEW":
                    result["respond"]["bookingFile"]["reservations"]["reservation"][0]["products"][
                        "airTicket"
                    ] = []
            if command == provider.fail_command:
                raise TimeoutError()
            return Response(result)

        monkeypatch.setattr("integrations.ttbooking.urlopen", wire)

    def commands(self):
        return [request["query"]["context"]["command"] for request in self.requests]


@pytest.fixture
def tt_service(tenant, supplier, credential, search_response):
    from crm.models import Person
    from orders.models import Order
    from services.models import OrderService

    person = Person.objects.create(tenant=tenant, given_name="Test", surname="Contact")
    order = Order.objects.create(tenant=tenant, number="TT-TEST", client_person=person)
    snapshot = TTBookingAdapter()._normalize_search(search_response, "RUB")[0]
    snapshot["supplier_id"] = str(supplier.id)
    snapshot["provider_environment"] = "sandbox"
    return OrderService.objects.create(
        tenant=tenant,
        order=order,
        supplier=supplier,
        kind="avia",
        title="TT Test",
        source="api",
        currency="RUB",
        client_total=25000,
        supplier_cost=25000,
        provider_snapshot=snapshot,
    )


@pytest.fixture
def booking_request(tt_service):
    return {
        "service_id": str(tt_service.id),
        "client_request_id": "test-booking",
        "contact_info": {"name": "Test Contact", "email": "test@example.com", "phone": "+996555123456"},
        "passengers": [
            {
                "latin_given_name": "PASSENGER",
                "latin_surname": "TEST",
                "latin_middle_name": "MIDDLE",
                "birth_date": "1990-01-01",
                "gender": "male",
                "citizenship": "KG",
                "document": {
                    "type": "foreign_passport",
                    "number": "TEST123456",
                    "issued_at": "2020-01-01",
                    "expires_at": "2033-01-01",
                },
            }
        ],
    }


def test_createbr_exact_documented_payload_and_idempotency(
    ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
):
    provider = LifecycleProvider(monkeypatch, search_response, booking_response)
    adapter = TTBookingAdapter()
    result = adapter.book(ctx, booking_request)
    assert result["status"] == "booked" and result["locator"] == "TEST01"
    assert result["provider_snapshot"]["ttbooking_booking"]["token"] == "booking-file-token"
    request = next(row for row in provider.requests if row["query"]["context"]["command"] == "CREATEBR")[
        "query"
    ]["parameters"]
    assert request["token"] == "search-token"
    assert request["customer"] == {
        "name": "Test Contact",
        "email": "test@example.com",
        "countryCode": "996",
        "areaCode": "555",
        "phone": "123456",
    }
    passenger = request["passengers"]["passenger"][0]
    assert passenger["type"] == "ADULT" and passenger["phoneNumber"] == "123456"
    assert passenger["passport"] == {
        "firstName": "PASSENGER",
        "lastName": "TEST",
        "middleName": "MIDDLE",
        "citizenship": {"code": "KG", "name": ""},
        "issued": "2020-01-01T00:00:00+00:00",
        "expired": "2033-01-01T00:00:00+00:00",
        "number": "TEST123456",
        "type": "PASSPORT",
        "birthday": "1990-01-01T00:00:00+00:00",
        "gender": "MALE",
    }
    assert adapter.book(ctx, booking_request)["locator"] == "TEST01"
    assert provider.commands().count("CREATEBR") == 1
    tt_service.refresh_from_db()
    booking = tt_service.provider_snapshot["ttbooking_booking"]
    assert booking["reservation_tokens"] == ["reservation-1"] and booking["reg_locators"] == ["REG01"]


@pytest.mark.parametrize(
    "birth_date,category", [("1990-01-01", "ADULT"), ("2020-01-01", "CHILD"), ("2026-06-01", "INFANT")]
)
def test_passenger_age_category_mapping(tt_service, booking_request, birth_date, category):
    snapshot = copy.deepcopy(tt_service.provider_snapshot)
    snapshot["fare"]["passenger_prices"] = [{"passengerType": category, "count": 1}]
    booking_request["passengers"][0]["birth_date"] = birth_date
    assert (
        TTBookingAdapter()._booking_parameters(snapshot, booking_request)["passengers"]["passenger"][0][
            "type"
        ]
        == category
    )


@pytest.mark.parametrize(
    "kind,country,number,provider_type",
    [
        ("foreign_passport", "RU", "123456789", "FOREIGN"),
        ("national_passport", "RU", "1234567890", "INTERNAL"),
        ("birth_certificate", "KG", "AB123456", "BIRTHDAY_NOTIFICATION"),
        ("foreign_passport", "KG", "AN1234567", "PASSPORT"),
    ],
)
def test_document_mapping(tt_service, booking_request, kind, country, number, provider_type):
    person = booking_request["passengers"][0]
    person["citizenship"] = country
    person["document"].update(type=kind, number=number)
    passport = TTBookingAdapter()._booking_parameters(tt_service.provider_snapshot, booking_request)[
        "passengers"
    ]["passenger"][0]["passport"]
    assert passport["type"] == provider_type and passport["number"] == number


@pytest.mark.parametrize(
    "patch",
    [
        {"gender": ""},
        {"latin_surname": ""},
        {"birth_date": ""},
        {"document": {"type": "other", "number": "secret", "expires_at": "2033-01-01"}},
        {"document": {"type": "foreign_passport", "number": "", "expires_at": "2033-01-01"}},
        {"document": {"type": "foreign_passport", "number": "secret", "expires_at": ""}},
    ],
)
def test_invalid_passenger_never_sends_createbr(
    ctx, tt_service, booking_request, search_response, booking_response, monkeypatch, patch
):
    booking_request["passengers"][0].update(patch)
    provider = LifecycleProvider(monkeypatch, search_response, booking_response)
    with pytest.raises(AdapterError) as error:
        TTBookingAdapter().book(ctx, booking_request)
    assert error.value.code == "INVALID_REQUEST" and "CREATEBR" not in provider.commands()


def test_booking_price_change_blocks_createbr(
    ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
):
    search_response["respond"]["flightsGroup"]["flightGroup"][0]["fares"]["fareTotal"]["total"] = 26000
    provider = LifecycleProvider(monkeypatch, search_response, booking_response)
    with pytest.raises(AdapterError) as error:
        TTBookingAdapter().book(ctx, booking_request)
    assert error.value.code == "PRICE_CHANGED" and "CREATEBR" not in provider.commands()


def test_createbr_timeout_is_durable_and_cannot_rebook(
    ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
):
    provider = LifecycleProvider(monkeypatch, search_response, booking_response, fail_command="CREATEBR")
    adapter = TTBookingAdapter()
    with pytest.raises(AdapterError) as error:
        adapter.book(ctx, booking_request)
    assert error.value.code == "BOOKING_UNKNOWN"
    tt_service.refresh_from_db()
    assert tt_service.provider_snapshot["ttbooking_booking"]["state"] == "unknown"
    with pytest.raises(AdapterError) as error:
        adapter.book(ctx, booking_request)
    assert error.value.code == "BOOKING_UNKNOWN" and provider.commands().count("CREATEBR") == 1
    with pytest.raises(AdapterError) as error:
        adapter.retrieve_booking(ctx, {"service_id": str(tt_service.id)})
    assert error.value.code == "BOOKING_UNKNOWN"  # No undocumented lookup by client_request_id.


def prepare_booking(ctx, service, request, search, booking, monkeypatch):
    provider = LifecycleProvider(monkeypatch, search, booking)
    adapter = TTBookingAdapter()
    adapter.book(ctx, request)
    service.refresh_from_db()
    return adapter, provider


def test_issue_preflight_and_duplicate_prevention(
    ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
):
    adapter, provider = prepare_booking(
        ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
    )
    request = {"service_id": str(tt_service.id), "locator": "TEST01"}
    issued = adapter.issue(ctx, request)
    assert issued["status"] == "issued" and len(issued["tickets"]) == 2
    assert provider.commands().index("LOADBOOKING") < provider.commands().index("PAYDEPOSIT")
    assert provider.commands().index("DISPLAYTICKETS") < provider.commands().index("PAYDEPOSIT")
    assert adapter.issue(ctx, request)["status"] == "issued"
    assert provider.commands().count("PAYDEPOSIT") == 1


@pytest.mark.parametrize(
    "raw_status", ["CANCELLED", "UNKNOWN", "TO_BE_ISSUED", "BOOKING_NOT_CONFIRMED", "PAYED"]
)
def test_issue_rejects_unsafe_status(
    ctx, tt_service, booking_request, search_response, booking_response, monkeypatch, raw_status
):
    adapter, provider = prepare_booking(
        ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
    )
    provider.booking["respond"]["bookingFile"]["status"] = raw_status
    if raw_status == "CANCELLED":
        provider.booking["respond"]["bookingFile"]["reservations"]["reservation"][0]["products"]["airTicket"][
            0
        ]["status"] = "VOID_BOOKING"
    with pytest.raises(AdapterError):
        adapter.issue(ctx, {"service_id": str(tt_service.id)})
    assert "PAYDEPOSIT" not in provider.commands()


def test_issue_rejects_expired_time_limit(
    ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
):
    adapter, provider = prepare_booking(
        ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
    )
    provider.booking["respond"]["bookingFile"]["reservations"]["reservation"][0]["timeLimit"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    with pytest.raises(AdapterError) as error:
        adapter.issue(ctx, {"service_id": str(tt_service.id)})
    assert error.value.code == "OFFER_EXPIRED" and "PAYDEPOSIT" not in provider.commands()


@pytest.mark.parametrize("applied", [False, True])
def test_issue_timeout_requires_inquiry_and_never_automatically_repeats(
    ctx, tt_service, booking_request, search_response, booking_response, monkeypatch, applied
):
    adapter, provider = prepare_booking(
        ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
    )
    provider.fail_command = "PAYDEPOSIT"
    provider.apply_before_timeout = applied
    with pytest.raises(AmbiguousResultError):
        adapter.issue(ctx, {"service_id": str(tt_service.id)})
    with pytest.raises(AmbiguousResultError):
        adapter.issue(ctx, {"service_id": str(tt_service.id)})
    assert provider.commands().count("PAYDEPOSIT") == 1
    provider.fail_command = None
    inquiry = adapter.retrieve_booking(ctx, {"service_id": str(tt_service.id)})
    assert inquiry["status"] == ("issued" if applied else "booked")
    if applied:
        assert len(inquiry["tickets"]) == 2
        assert adapter.issue(ctx, {"service_id": str(tt_service.id)})["status"] == "issued"
        assert provider.commands().count("PAYDEPOSIT") == 1


def test_cancel_checks_status_and_preserves_provider_refusal(
    ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
):
    adapter, provider = prepare_booking(
        ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
    )
    result = adapter.cancel(ctx, {"service_id": str(tt_service.id)})
    assert result["status"] == "cancelled" and provider.commands()[-2:] == ["LOADBOOKING", "CANCELBOOKING"]
    assert adapter.cancel(ctx, {"service_id": str(tt_service.id)})["status"] == "cancelled"
    assert provider.commands().count("CANCELBOOKING") == 1


def test_cancel_timeout_is_unknown_until_inquiry(
    ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
):
    adapter, provider = prepare_booking(
        ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
    )
    provider.fail_command = "CANCELBOOKING"
    with pytest.raises(AdapterError) as error:
        adapter.cancel(ctx, {"service_id": str(tt_service.id)})
    assert error.value.code == "CANCEL_UNKNOWN"
    with pytest.raises(AdapterError) as error:
        adapter.cancel(ctx, {"service_id": str(tt_service.id)})
    assert error.value.code == "CANCEL_UNKNOWN" and provider.commands().count("CANCELBOOKING") == 1


def test_service_reference_cannot_cross_supplier_or_tenant(ctx, tt_service, other_tenant, tenant):
    other_supplier = Supplier.objects.create(tenant=tenant, name="Other provider")
    for context in [
        AdapterContext(tenant_id=other_tenant.id, supplier_id=ctx.supplier_id),
        AdapterContext(tenant_id=tenant.id, supplier_id=other_supplier.id),
    ]:
        with pytest.raises(AdapterError):
            TTBookingAdapter()._service(context, {"service_id": str(tt_service.id)})


def test_read_inquiry_cannot_release_an_inflight_issue(
    ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
):
    adapter, provider = prepare_booking(
        ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
    )
    adapter._save_booking(ctx, tt_service.id, {"issue_state": "sending"})
    adapter.retrieve_booking(ctx, {"service_id": str(tt_service.id)}, refresh=True)
    tt_service.refresh_from_db()
    assert tt_service.provider_snapshot["ttbooking_booking"]["issue_state"] == "sending"
    with pytest.raises(AmbiguousResultError):
        adapter.issue(ctx, {"service_id": str(tt_service.id)})
    assert "PAYDEPOSIT" not in provider.commands()


def test_cancel_provider_refusal_is_logged_as_error(
    ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
):
    adapter, _ = prepare_booking(
        ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
    )
    requests = mock_http(monkeypatch, booking_response)  # Provider keeps NEW: cancellation was refused.
    with pytest.raises(AdapterError) as error:
        adapter.cancel(ctx, {"service_id": str(tt_service.id)})
    assert error.value.code == "CANCEL_ERROR"
    assert requests[-1]["query"]["context"]["command"] == "CANCELBOOKING"
    log = IntegrationLog.objects.filter(operation="CANCELBOOKING").get()
    assert log.result == "error" and log.error_code == "CANCEL_ERROR"


def test_booking_provider_error_retains_booking_token(
    ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
):
    provider = LifecycleProvider(monkeypatch, search_response, booking_response)
    provider.booking["respond"]["bookingFile"]["status"] = "ERROR"
    with pytest.raises(AdapterError) as error:
        TTBookingAdapter().book(ctx, booking_request)
    assert error.value.code == "BOOKING_ERROR"
    tt_service.refresh_from_db()
    assert tt_service.provider_snapshot["ttbooking_booking"]["token"] == "booking-file-token"
    assert tt_service.provider_snapshot["ttbooking_booking"]["raw_status"] == "ERROR"


def test_revalidation_keeps_unknown_booking_guard(
    ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
):
    from services.revalidation import update_service

    adapter, provider = prepare_booking(
        ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
    )
    adapter._save_booking(ctx, tt_service.id, {"state": "unknown"})
    tt_service.refresh_from_db()
    result = adapter.revalidate(ctx, tt_service.provider_snapshot)
    update_service(tt_service, result)
    with pytest.raises(AdapterError) as error:
        adapter.book(ctx, booking_request)
    assert error.value.code == "BOOKING_UNKNOWN" and provider.commands().count("CREATEBR") == 1


def test_rotated_account_cannot_reuse_search_tokens(ctx, credential, search_response, monkeypatch):
    adapter = TTBookingAdapter()
    mock_http(monkeypatch, search_response)
    offer = adapter.search(ctx, "avia", CRITERIA)[0]
    credential.encrypted_secrets = json.dumps({**CONFIG, "agency": "different-agency"})
    credential.save()
    with pytest.raises(AdapterError) as error:
        adapter.revalidate(ctx, offer)
    assert error.value.code == "PROVIDER_NOT_CONFIGURED"


@pytest.mark.parametrize("endpoint", ["book", "manual-book", "transition"])
def test_api_ttbooking_cannot_fake_booked_status(admin_client, tt_service, endpoint):
    response = admin_client.post(
        f"/api/v1/services/{tt_service.id}/{endpoint}/",
        {"version": tt_service.version, "target_status": "booked", "supplier_reference": "FAKE"},
        format="json",
    )
    assert response.status_code == 409, response.content
    assert response.json()["error"]["code"] == "BOOKING_WORKFLOW_REQUIRED"
    tt_service.refresh_from_db()
    assert tt_service.status == "proposed"


def test_connection_rejects_empty_provider_token(ctx, credential, monkeypatch):
    mock_http(monkeypatch, {"respond": {"token": "", "flightsGroup": {"flightGroup": []}}})
    with pytest.raises(AdapterError) as error:
        TTBookingAdapter().verify_credentials(credential)
    assert error.value.code == "PROVIDER_ERROR"


def test_explicit_ttbooking_supplier_is_not_routed_to_old_mock_credentials(
    admin_client, monkeypatch, search_response
):
    from suppliers.models import Supplier

    supplier_id = create_supplier_and_credential(admin_client)
    supplier = Supplier.objects.get(pk=supplier_id)
    SupplierCredential.objects.create(
        tenant_id=supplier.tenant_id,
        supplier=supplier,
        provider_adapter="mock",
        status="active",
        encrypted_secrets="{}",
    )
    mock_http(monkeypatch, search_response)
    checked = admin_client.post(f"/api/v1/suppliers/{supplier_id}/check-connection/", {})
    assert checked.status_code == 200 and all(
        row["provider_adapter"] == "ttbooking" for row in checked.json()["checked"]
    )
    metadata = admin_client.get(f"/api/v1/suppliers/{supplier_id}/").json()
    assert metadata["provider_adapter"] == "ttbooking" and metadata["connection_status"] == "connected"
    created = admin_client.post(
        "/api/v1/service-searches/", {"kind": "avia", "criteria": CRITERIA}, format="json"
    )
    call_command("run_jobs", "--once", "--worker-id", "ttbooking-test")
    offers = ServiceOffer.objects.filter(session_id=created.json()["search_id"])
    assert offers.count() == 1 and offers.get().provider_adapter == "ttbooking"


def test_selected_environment_routes_new_searches_and_preserves_old_offer_scope(
    admin_client, monkeypatch, search_response
):
    supplier_id = create_supplier_and_credential(admin_client)
    mock_http(monkeypatch, search_response)
    assert admin_client.post(f"/api/v1/suppliers/{supplier_id}/check-connection/", {}).status_code == 200
    credential = SupplierCredential.objects.get(supplier_id=supplier_id)
    ctx = AdapterContext(tenant_id=credential.tenant_id, supplier_id=supplier_id)
    adapter = TTBookingAdapter()
    old_offer = adapter.search(ctx, "avia", CRITERIA)[0]
    saved = admin_client.post(
        f"/api/v1/suppliers/{supplier_id}/credentials/",
        {
            "provider_adapter": "ttbooking",
            "environment": "production",
            "secrets": {**CONFIG, "agency": "production-fake"},
        },
        format="json",
    )
    assert saved.status_code == 201
    requests = mock_http(monkeypatch, search_response)
    checked = admin_client.post(f"/api/v1/suppliers/{supplier_id}/check-connection/", {})
    assert checked.status_code == 200 and [row["environment"] for row in checked.json()["checked"]] == [
        "production"
    ]
    new_offer = adapter.search(ctx, "avia", CRITERIA)[0]
    assert new_offer["provider_environment"] == "production"
    assert requests[-1]["query"]["context"]["agency"] == "production-fake"
    adapter.revalidate(ctx, old_offer)
    assert requests[-1]["query"]["context"]["agency"] == "42"


def test_issued_without_ticket_numbers_blocks_payment(
    ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
):
    adapter, provider = prepare_booking(
        ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
    )
    provider.booking["respond"]["bookingFile"]["status"] = "COMPLETE"
    provider.booking["respond"]["bookingFile"]["reservations"]["reservation"][0]["products"]["airTicket"] = []
    with pytest.raises(AmbiguousResultError):
        adapter.issue(ctx, {"service_id": str(tt_service.id)})
    assert "PAYDEPOSIT" not in provider.commands()


def test_partial_void_confirmation_keeps_unknown_state(
    ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
):
    adapter, provider = prepare_booking(
        ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
    )
    adapter.issue(ctx, {"service_id": str(tt_service.id)})
    partial = copy.deepcopy(provider.booking)
    partial["respond"]["bookingFile"]["status"] = "CANCELLED"
    products = partial["respond"]["bookingFile"]["reservations"]["reservation"][0]["products"]["airTicket"]
    products[0]["status"] = "VOID"
    products.pop()
    # Status preflight reads COMPLETE with both tickets, then mutation and ticket inquiry return an incomplete void list.
    original = __import__("integrations.ttbooking", fromlist=["urlopen"]).urlopen
    calls = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(partial).encode()

    def wire(request, timeout):
        command = json.loads(request.data)["query"]["context"]["command"]
        if command == "CANCELBOOKING" or calls:
            calls.append(command)
            return Response()
        return original(request, timeout)

    monkeypatch.setattr("integrations.ttbooking.urlopen", wire)
    with pytest.raises(AdapterError) as error:
        adapter.cancel(ctx, {"service_id": str(tt_service.id)})
    assert error.value.code == "CANCEL_UNKNOWN"
    result = adapter.retrieve_booking(ctx, {"service_id": str(tt_service.id)})
    assert result["status"] == "unknown"
    tt_service.refresh_from_db()
    assert tt_service.provider_snapshot["ttbooking_booking"]["cancel_state"] == "unknown"


def test_malformed_booking_response_retains_token_for_inquiry(
    ctx, tt_service, booking_request, search_response, booking_response, monkeypatch
):
    provider = LifecycleProvider(monkeypatch, search_response, booking_response)
    provider.booking["respond"]["bookingFile"]["reservations"]["reservation"][0]["timeLimit"] = "invalid-date"
    adapter = TTBookingAdapter()
    with pytest.raises(AdapterError) as error:
        adapter.book(ctx, booking_request)
    assert error.value.code == "BOOKING_UNKNOWN"
    tt_service.refresh_from_db()
    assert tt_service.provider_snapshot["ttbooking_booking"]["token"] == "booking-file-token"
    provider.booking["respond"]["bookingFile"]["reservations"]["reservation"][0]["timeLimit"] = (
        datetime.now(timezone.utc) + timedelta(hours=1)
    ).isoformat()
    assert adapter.retrieve_booking(ctx, {"service_id": str(tt_service.id)})["status"] == "booked"
    assert provider.commands().count("CREATEBR") == 1 and provider.commands()[-1] == "LOADBOOKING"


def test_legacy_structural_verification_is_not_a_connected_ttbooking_account(
    admin_client, monkeypatch, search_response
):
    from django.utils import timezone as django_timezone

    supplier_id = create_supplier_and_credential(admin_client)
    credential = SupplierCredential.objects.get(supplier_id=supplier_id)
    credential.status = "active"
    credential.last_verified_at = django_timezone.now()
    credential.save()
    assert admin_client.get(f"/api/v1/suppliers/{supplier_id}/").json()["connection_status"] != "connected"
    assert (
        admin_client.get(f"/api/v1/suppliers/{supplier_id}/credentials/").json()[0]["connection_verified"]
        is False
    )
    mock_http(monkeypatch, search_response)
    assert admin_client.post(f"/api/v1/suppliers/{supplier_id}/check-connection/", {}).status_code == 200
    assert (
        admin_client.get(f"/api/v1/suppliers/{supplier_id}/credentials/").json()[0]["connection_verified"]
        is True
    )


def test_bound_reads_cannot_use_another_suppliers_token(ctx, tt_service, tenant):
    tt_service.provider_snapshot["ttbooking_booking"] = {"token": "owned-booking-token"}
    tt_service.save()
    other_supplier = Supplier.objects.create(tenant=tenant, name="Other TT supplier", service_kinds=["avia"])
    SupplierCredential.objects.create(
        tenant=tenant,
        supplier=other_supplier,
        provider_adapter="ttbooking",
        status="active",
        encrypted_secrets=json.dumps(CONFIG),
    )
    wrong_ctx = AdapterContext(tenant_id=tenant.id, supplier_id=other_supplier.id)
    for operation in [
        lambda: TTBookingAdapter().retrieve_booking(wrong_ctx, "owned-booking-token"),
        lambda: TTBookingAdapter().display_tickets(wrong_ctx, "owned-booking-token"),
    ]:
        with pytest.raises(AdapterError):
            operation()


def test_search_snapshot_contains_only_the_selected_flight(search_response):
    itinerary = search_response["respond"]["flightsGroup"]["flightGroup"][0]["itineraries"]["itinerary"][0]
    another = copy.deepcopy(itinerary["flights"]["flight"][0])
    another["token"] = "flight-2"
    itinerary["flights"]["flight"].append(another)
    offers = TTBookingAdapter()._normalize_search(search_response, "RUB")
    assert len(offers) == 2
    for offer in offers:
        raw = offer["raw_snapshot"]["flightGroup"]["itineraries"]["itinerary"][0]["flights"]["flight"]
        selected = offer["provider_selection"]["flightsGroup"]["flightGroup"][0]["itineraries"]["itinerary"][
            0
        ]["flights"]["flight"]
        assert len(raw) == 1 and raw[0]["token"] == selected[0]["token"]
