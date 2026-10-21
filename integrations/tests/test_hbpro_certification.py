# ruff: noqa: F401, F811
import io
import json
from copy import deepcopy
from zipfile import ZipFile

import openpyxl
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from booking.models import BookingWorkflowItem
from crm.models import Person, PersonDocument
from integrations.adapters import AdapterError, get_adapter
from integrations.certification import export_runs, summary
from integrations.hotelbook_lock import booking_lease
from integrations.hotelbook_observability import external_ids, normalized_status, sanitize
from integrations.models import HotelbookCertificationRun, IntegrationLog
from integrations.scheduled_tasks import queue_hotelbook_status_sync
from integrations.tests.test_hotelbook import (  # noqa: F401,F811
    ITEM_ID,
    SEARCH_ID,
    context,
    criteria,
    gateway,
    hb_supplier,
    jobs,
    offer,
    post,
    ready_booking,
    start,
)
from services.models import OrderService

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "statuses,expected",
    [
        (["CONFIRMED"], "confirmed"),
        (["CANCELED", "CANCELED"], "cancelled"),
        (["CONFIRM_IN_PROGRESS"], "pending_supplier_confirmation"),
        (["NEW", "CONFIRMED"], "pending_supplier_confirmation"),
        (["REMARK_IN_PROGRESS"], "pending_supplier_confirmation"),
        (["CONFIRMED", "CANCELED"], "unknown"),
        (["ONLINE"], "unknown"),
        ([], "unknown"),
    ],
)
def test_status_mapping(statuses, expected):
    assert normalized_status([{"status": s} for s in statuses]) == expected


def test_masking_nested_pii_and_free_text():
    payload = {
        "password": "pw",
        "api-key": "key",
        "headers": {"Authorization": "Bearer jwt"},
        "rooms": [{"guests": [{"firstName": "PETR", "lastName": "IVANOV", "gender": "M"}]}],
        "description": "Echo PETR user@example.com Bearer jwt",
        "searchId": SEARCH_ID,
    }
    result = sanitize(payload, ["PETR"])
    encoded = json.dumps(result)
    assert not any(
        value in encoded for value in ('"pw"', '"key"', "jwt", "PETR", "IVANOV", "user@example.com")
    )
    assert result["searchId"] == SEARCH_ID and result["rooms"][0]["guests"][0]["gender"] == "M"


def test_structured_external_ids():
    assert external_ids(
        {"searchId": SEARCH_ID},
        {"items": [{"itemId": ITEM_ID, "providerData": {"providerReferenceId": "REF-1"}}]},
    ) == {"searchId": [SEARCH_ID], "itemId": [ITEM_ID], "providerReferenceId": ["REF-1"]}


def test_telemetry_filters_and_permissions(
    admin_client, operator_client, tenant, hb_supplier, gateway, other_tenant
):
    get_adapter("hotelbook").search(context(tenant, hb_supplier), "hotel", criteria())
    IntegrationLog.objects.create(tenant=other_tenant, provider_adapter="hotelbook", operation="other")
    url = f"/api/v1/integration-operations/?supplier={hb_supplier.id}&operation=search&http_status=201&result=success"
    data = admin_client.get(url).json()
    assert data["count"] == 1
    log = data["results"][0]
    assert log["environment"] == "sandbox" and log["http_method"] == "POST"
    assert log["url"].endswith("/hotel/gateway/search") and log["request_sanitized"]["hotels"] == [1251539]
    assert log["response_sanitized"]["searchId"] == SEARCH_ID
    assert "fixture-jwt" not in json.dumps(log)
    assert operator_client.get(url).status_code == 403
    assert admin_client.get("/api/v1/integration-operations/?order_id=bad").status_code == 400
    assert admin_client.get("/api/v1/integration-operations/?date_from=bad").status_code == 400
    assert (
        admin_client.get("/api/v1/integration-operations/").json()["count"]
        == IntegrationLog.objects.filter(tenant=tenant).count()
    )


def test_poll_pending_then_confirm_and_stop(admin_client, tenant, hb_supplier, gateway):
    workflow, service_data = ready_booking(admin_client, tenant, hb_supplier)
    gateway.item_status = "CONFIRM_IN_PROGRESS"
    item = start(admin_client, workflow)["items"][0]
    service = OrderService.objects.get(pk=service_data["id"])
    assert service.status == "approval"
    assert (
        service.provider_snapshot["hotelbook_booking"]["normalized_status"] == "pending_supplier_confirmation"
    )
    assert queue_hotelbook_status_sync() == "queued 1"
    assert queue_hotelbook_status_sync() == "queued 0"
    gateway.item_status = "CONFIRMED"
    jobs()
    service.refresh_from_db()
    assert (
        service.status == "booked"
        and service.provider_snapshot["hotelbook_booking"]["normalized_status"] == "confirmed"
    )
    assert queue_hotelbook_status_sync() == "queued 0"
    assert BookingWorkflowItem.objects.get(pk=item["id"]).status == "booked"


def test_poll_timeout_backoff_and_manual_inquiry(admin_client, tenant, hb_supplier, gateway):
    workflow, service_data = ready_booking(admin_client, tenant, hb_supplier)
    gateway.item_status = "CONFIRM_IN_PROGRESS"
    item = start(admin_client, workflow)["items"][0]
    gateway.failure = ("/orders/4321", TimeoutError())
    assert queue_hotelbook_status_sync() == "queued 1"
    jobs()
    service = OrderService.objects.get(pk=service_data["id"])
    sync = service.provider_snapshot["hotelbook_booking"]["sync"]
    assert sync["attempts"] == 1 and sync["last_error"] == "TIMEOUT"
    assert queue_hotelbook_status_sync() == "queued 0"
    assert IntegrationLog.objects.filter(operation="order_info", error_code="TIMEOUT").exists()
    gateway.failure = None
    gateway.item_status = "CONFIRMED"
    post(admin_client, f"booking-workflows/{workflow['id']}/status-inquiry/", {"item": item["id"]})
    jobs()
    service.refresh_from_db()
    assert service.status == "booked"


def test_operation_lease_blocks_duplicate_calls(admin_client, tenant, hb_supplier, gateway):
    workflow, service_data = ready_booking(admin_client, tenant, hb_supplier)
    item = start(admin_client, workflow)["items"][0]
    service = OrderService.objects.get(pk=service_data["id"])
    before = len(gateway.calls)
    with booking_lease(service):
        with pytest.raises(AdapterError) as exc:
            get_adapter("hotelbook").cancel(context(tenant, hb_supplier), item["locator"])
    assert exc.value.code == "BOOKING_BUSY" and len(gateway.calls) == before


def test_failed_cancellation_logs_and_never_blindly_resends(admin_client, tenant, hb_supplier, gateway):
    workflow, _ = ready_booking(admin_client, tenant, hb_supplier)
    item = start(admin_client, workflow)["items"][0]
    gateway.failure = ("/cancel", (400, "ERR004#1"))
    adapter = get_adapter("hotelbook")
    with pytest.raises(AdapterError) as exc:
        adapter.cancel(context(tenant, hb_supplier), item["locator"])
    assert exc.value.code == "CANCELLATION_FAILED"
    assert IntegrationLog.objects.filter(
        operation="cancel", result="error", provider_error__details__errorCode="ERR004#1"
    ).exists()
    with pytest.raises(AdapterError):
        adapter.cancel(context(tenant, hb_supplier), item["locator"])
    assert sum(c[1].endswith("/cancel") for c in gateway.calls) == 1


def test_export_keeps_pages_secrets_and_excel_template(tenant, hb_supplier, gateway):
    from hashlib import sha256
    from pathlib import Path

    original = Path("docs/hbpro/certification-template.xlsx").read_bytes()
    run = HotelbookCertificationRun.objects.create(tenant=tenant, supplier=hb_supplier, scenario=2)
    ctx = context(tenant, hb_supplier)
    ctx.certification_run_id = run.id
    get_adapter("hotelbook").search(ctx, "hotel", criteria())
    content = export_runs([run])
    with ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        assert "hbpro-certification/scenario-2/2.a.Search.json" in names
        search = json.loads(archive.read("hbpro-certification/scenario-2/2.a.Search.json"))
        assert (
            search["url"]
            and search["http_method"] == "POST"
            and search["external_ids"]["searchId"] == [SEARCH_ID]
        )
        assert all(
            s not in b"".join(archive.read(n) for n in names if n.endswith(".json"))
            for s in (b"fixture-password", b"fixture-login", b"fixture-jwt")
        )
        filled = openpyxl.load_workbook(
            io.BytesIO(archive.read("hbpro-certification/certification-filled.xlsx"))
        )
        assert "414209" not in str(filled["Ru"]["B17"].value)
        assert SEARCH_ID in filled["Ru"]["B14"].value
        assert filled["Ru"]["A14"].value == openpyxl.load_workbook(io.BytesIO(original))["Ru"]["A14"].value
    assert (
        sha256(original).digest()
        == sha256(Path("docs/hbpro/certification-template.xlsx").read_bytes()).digest()
    )
    assert summary(run)["readiness"] == "PARTIAL"


def test_certification_export_tenant_and_staff(
    admin_client, operator_client, tenant, other_tenant, hb_supplier
):
    run = HotelbookCertificationRun.objects.create(tenant=tenant, supplier=hb_supplier, scenario=1)
    url = f"/api/v1/hbpro-certification-runs/{run.id}/export/"
    assert operator_client.get(url).status_code == 403
    assert admin_client.get(url).status_code == 200
    run.tenant = other_tenant
    run.save()
    assert admin_client.get(url).status_code == 404


def test_certification_financial_guard(admin_user, tenant, hb_supplier):
    run = HotelbookCertificationRun.objects.create(tenant=tenant, supplier=hb_supplier, scenario=1)
    with pytest.raises(CommandError, match="confirm-financial-actions"):
        call_command(
            "hbpro_certification", "book", tenant=str(tenant.id), actor=str(admin_user.id), run=[str(run.id)]
        )


def test_scenario_1_prepare_is_readonly(admin_user, tenant, hb_supplier, gateway):
    gateway.current = offer(1251201)
    gateway.current.update(confirmationMode="ON_REQUEST", checkOut="2027-03-13")
    gateway.check_out = "2027-03-13"
    call_command(
        "hbpro_certification",
        "prepare",
        tenant=str(tenant.id),
        actor=str(admin_user.id),
        supplier=str(hb_supplier.id),
        scenario=1,
        check_in="2027-03-10",
        stdout=io.StringIO(),
    )
    run = HotelbookCertificationRun.objects.get()
    assert run.snapshot["hotelbook"]["hotel_id"] == 1251201
    assert run.snapshot["hotelbook"]["offer"]["confirmationMode"] == "ON_REQUEST"
    assert run.calls.filter(operation="search").exists()
    assert not run.calls.filter(operation__in=["book", "create_order", "cancel"]).exists()
    assert run.snapshot["certification_run_id"] == str(run.id)


def test_actual_two_room_booking_one_request(admin_client, tenant, hb_supplier, gateway):
    workflow, service_data = ready_booking(admin_client, tenant, hb_supplier)
    service = OrderService.objects.get(pk=service_data["id"])
    # Existing fixture created the initial one-person service. Expand the real offer
    # and order to two twin rooms; provider receives a single order and book call.
    snapshot = deepcopy(service.provider_snapshot)
    snapshot["hotelbook"]["offer"]["rooms"][0].update(quantity=2, adults=2)
    gateway.current = deepcopy(snapshot["hotelbook"]["offer"])
    gateway.current["price"]["amount"] = float(gateway.current["price"]["amount"])
    service.provider_snapshot = snapshot
    service.save()
    first = service.passengers.get().participant
    selected = [first]
    for index, gender in enumerate(["female", "male", "female"]):
        person = Person.objects.create(
            tenant=tenant,
            surname=f"Guest{index}",
            given_name="Test",
            latin_surname=f"GUEST{index}",
            latin_given_name="TEST",
            gender=gender,
            birth_date="1990-01-01",
            citizenship="KG",
        )
        PersonDocument.objects.create(
            tenant=tenant, person=person, type="foreign_passport", number=f"fixture-{index}"
        )
        selected_id = post(
            admin_client,
            f"orders/{service.order_id}/participants/",
            {"person": str(person.id), "role": "passenger"},
        )["id"]
        selected.append(service.order.participants.get(pk=selected_id))
    participant_ids = [str(p.id) for p in selected]
    response = admin_client.put(
        f"/api/v1/services/{service.id}/passengers/",
        {
            "version": service.version,
            "participants": participant_ids,
            "room_assignments": {p: f"room:{i // 2}" for i, p in enumerate(participant_ids)},
            "main_guest": participant_ids[0],
        },
        format="json",
    )
    assert response.status_code == 200, response.content
    gateway.book_rooms = []
    for room_index in range(2):
        guests = []
        for participant in selected[room_index * 2 : room_index * 2 + 2]:
            person = participant.person
            guests.append(
                {
                    "firstName": person.latin_given_name,
                    "lastName": person.latin_surname,
                    "gender": "M" if person.gender == "male" else "F",
                    "citizenship": 77,
                    "isChild": False,
                }
            )
        gateway.book_rooms.append({"hash": "opaque-room-hash", "guests": guests})
    gateway.items = [ITEM_ID, "0a04ef24-24a4-4000-8000-000000000002"]
    result = start(admin_client, workflow)
    assert result["items"][0]["status"] == "booked"
    assert sum(c[1].endswith("/hotel/gateway/book") for c in gateway.calls) == 1
    assert sum(c[1].endswith("/order/gateway/orders") and c[0] == "POST" for c in gateway.calls) == 1
    service.refresh_from_db()
    assert len(service.provider_snapshot["hotelbook_booking"]["room_items"]) == 2
    assert [r["room_index"] for r in service.provider_snapshot["hotelbook_booking"]["room_requests"]] == [
        0,
        1,
    ]


def test_staged_certification_command_full_flow(
    admin_client, admin_user, tenant, hb_supplier, gateway, tmp_path
):
    _, original = ready_booking(admin_client, tenant, hb_supplier)
    service = OrderService.objects.get(pk=original["id"])
    participant = service.passengers.get().participant
    gateway.current = offer(1251201)
    gateway.current.update(confirmationMode="ON_REQUEST", checkOut="2027-03-13")
    gateway.check_out = "2027-03-13"
    options = {"tenant": str(tenant.id), "actor": str(admin_user.id), "stdout": io.StringIO()}
    call_command(
        "hbpro_certification",
        "prepare",
        supplier=str(hb_supplier.id),
        scenario=1,
        check_in="2027-03-10",
        **options,
    )
    run = HotelbookCertificationRun.objects.get()
    gateway.item_status = "CONFIRM_IN_PROGRESS"
    call_command(
        "hbpro_certification",
        "book",
        run=[str(run.id)],
        order=str(service.order_id),
        participants=[str(participant.id)],
        confirm_financial_actions=True,
        **options,
    )
    jobs()
    run.refresh_from_db()
    assert run.service.status == "approval"
    assert summary(run)["booking_status"] == "pending_supplier_confirmation"
    gateway.item_status = "CONFIRMED"
    call_command("hbpro_certification", "refresh", run=[str(run.id)], **options)
    jobs()
    run.refresh_from_db()
    assert run.service.status == "booked"
    call_command(
        "hbpro_certification", "cancel", run=[str(run.id)], confirm_financial_actions=True, **options
    )
    jobs()
    run.refresh_from_db()
    assert run.service.status == "cancelled"
    assert summary(run)["readiness"] == "PASS"
    output = tmp_path / "package.zip"
    call_command("hbpro_certification", "export", run=[str(run.id)], output=str(output), **options)
    with ZipFile(output) as archive:
        assert "hbpro-certification/scenario-1/1.h.cancellation.json" in archive.namelist()
        assert json.loads(archive.read("hbpro-certification/summary.json"))[0]["readiness"] == "PASS"


@pytest.mark.parametrize(
    "bad_rooms",
    [
        [None],
        [{"adults": "two"}],
        [{"adults": 1, "quantity": "two"}],
        [{"adults": 1, "childrenAges": ["bad"]}],
        True,
    ],
)
def test_search_input_rejected_before_transport(tenant, hb_supplier, gateway, bad_rooms):
    with pytest.raises(AdapterError) as exc:
        get_adapter("hotelbook").search(
            context(tenant, hb_supplier), "hotel", {**criteria(), "rooms": bad_rooms}
        )
    assert exc.value.code == "SEARCH_CRITERIA_INVALID"
    assert not gateway.calls


def test_connection_verification_is_fresh_and_timed(tenant, hb_supplier, gateway):
    from suppliers.job_handlers import verify_supplier_credentials

    first = verify_supplier_credentials(hb_supplier)
    second = verify_supplier_credentials(hb_supplier)
    assert first["checked"][0]["environment"] == "sandbox" and second["checked"][0]["latency_ms"] >= 0
    assert sum(c[1].endswith("/gateway/login") for c in gateway.calls) == 2


def test_environment_selects_matching_database_credential(tenant, hb_supplier, settings):
    from suppliers.models import SupplierCredential

    prod = SupplierCredential.objects.create(
        tenant=tenant,
        supplier=hb_supplier,
        environment="production",
        provider_adapter="hotelbook",
        status="active",
        encrypted_secrets=json.dumps({"login": "prod-fixture", "password": "prod-pw"}),
    )
    settings.HBPRO_ENVIRONMENT = "production"
    credential, config = get_adapter("hotelbook")._config(context(tenant, hb_supplier))
    assert credential.id == prod.id and config["login"] == "prod-fixture"


def test_offer_conditions_only_use_documented_returned_fields(tenant, hb_supplier, gateway):
    adapter = get_adapter("hotelbook")
    raw = offer()
    raw["isRemarkFreeFormAllowed"] = True
    normalized = adapter._normalize(
        raw,
        SEARCH_ID,
        "2027-03-10T00:00:00Z",
        {"details": {"earliestCheckInTime": "14:00", "latestCheckOutTime": "12:00"}},
        {},
    )
    assert normalized["itinerary"]["check_in_time"] == "14:00"
    assert normalized["itinerary"]["check_out_time"] == "12:00"
    assert normalized["fare"]["confirmation_mode"] == "ONLINE"
    assert normalized["fare"]["isRemarkFreeFormAllowed"] is True


def test_malformed_offer_is_normalized_error(tenant, hb_supplier, gateway):
    adapter = get_adapter("hotelbook")
    malformed = offer()
    malformed["finePolicies"] = {"cancel": [{"price": {"amount": "not-a-price"}}]}
    with pytest.raises(AdapterError) as error:
        adapter._normalize(malformed, SEARCH_ID, None, {}, {})
    assert error.value.code == "INVALID_RESPONSE"


def test_guest_gender_missing_blocks_supplier_mutation(admin_client, tenant, hb_supplier, gateway):
    workflow, service_data = ready_booking(admin_client, tenant, hb_supplier)
    service = OrderService.objects.get(pk=service_data["id"])
    person = service.passengers.get().participant.person
    person.gender = ""
    person.save()
    result = start(admin_client, workflow)
    assert result["items"][0]["error_code"] == "GUEST_DATA_MISSING"
    assert not any(c[1].endswith("/hotel/gateway/book") for c in gateway.calls)


def test_sync_exhaustion_creates_incident_and_stops(admin_client, tenant, hb_supplier, gateway):
    from integrations.models import IntegrationIncident

    workflow, service_data = ready_booking(admin_client, tenant, hb_supplier)
    gateway.item_status = "CONFIRM_IN_PROGRESS"
    start(admin_client, workflow)
    service = OrderService.objects.get(pk=service_data["id"])
    snapshot = service.provider_snapshot
    snapshot["hotelbook_booking"]["sync"] = {"attempts": 99}
    service.provider_snapshot = snapshot
    service.save()
    assert queue_hotelbook_status_sync() == "queued 1"
    jobs()
    assert IntegrationIncident.objects.filter(service=service, error_code="HBPRO_SYNC_EXHAUSTED").exists()
    assert queue_hotelbook_status_sync() == "queued 0"


def test_special_requests_are_supported_only_when_returned_by_hbpro(
    admin_client, tenant, hb_supplier, gateway
):
    workflow, service_data = ready_booking(admin_client, tenant, hb_supplier)
    service = OrderService.objects.get(pk=service_data["id"])
    participant = service.passengers.get().participant
    body = {
        "version": service.version,
        "participants": [str(participant.id)],
        "special_requests": "Quiet room",
    }
    response = admin_client.put(f"/api/v1/services/{service.id}/passengers/", body, format="json")
    assert response.status_code == 400
    snapshot = service.provider_snapshot
    snapshot["hotelbook"]["offer"]["isRemarkFreeFormAllowed"] = True
    service.provider_snapshot = snapshot
    service.save()
    gateway.current["isRemarkFreeFormAllowed"] = True
    assert (
        admin_client.put(f"/api/v1/services/{service.id}/passengers/", body, format="json").status_code == 200
    )
    assert start(admin_client, workflow)["items"][0]["status"] == "booked"
    request = next(c[3] for c in gateway.calls if c[1].endswith("/hotel/gateway/book"))
    assert request["freeRemark"] == "Quiet room"
    log = IntegrationLog.objects.get(operation="book")
    assert log.request_sanitized["freeRemark"] == "[REDACTED]"
