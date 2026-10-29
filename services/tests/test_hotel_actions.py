# ruff: noqa: F401, F811
from copy import deepcopy
from unittest.mock import patch

import pytest

from aftersales.models import HotelOperation
from common.models import AuditEvent, BackgroundJob
from integrations.adapters import AdapterError, ProviderAdapter, get_adapter
from integrations.tests.test_hotelbook import gateway, hb_supplier, jobs, ready_booking, start
from services.hotel_actions.presentation import booking_summary
from services.models import OrderService

pytestmark = pytest.mark.django_db


@pytest.fixture
def booked(admin_client, tenant, hb_supplier, gateway):
    workflow, service = ready_booking(admin_client, tenant, hb_supplier)
    assert start(admin_client, workflow)["items"][0]["status"] == "booked"
    return OrderService.objects.get(pk=service["id"])


def url(service):
    return f"/api/v1/services/{service.pk}/hotel-actions/"


def create(client, service, action, *, key="operation", scope="booking", refs=None, changes=None):
    return client.post(
        url(service),
        {
            "operation_type": action,
            "scope": scope,
            "room_refs": refs if refs is not None else [r["ref"] for r in booking_summary(service)["rooms"]],
            "requested_changes": changes or {},
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY=key,
    )


def confirm(client, service, op, **changes):
    return client.post(
        f"{url(service)}{op['id']}/confirm/",
        {"confirmed": True, "accept_unknown_penalty": True, "version": op["version"], **changes},
        format="json",
    )


def cancels(gateway):
    return [call for call in gateway.calls if call[0] == "POST" and call[1].endswith("/cancel")]


def test_summary_uses_real_hbpro_booking_without_extra_dictionary_or_status_calls(
    admin_client, booked, gateway
):
    before = len(gateway.calls)
    response = admin_client.get(url(booked))
    assert response.status_code == 200
    data = response.json()
    assert len(gateway.calls) == before
    summary = data["summary"]
    assert summary["supplier_booking_reference"] == "4321"
    assert summary["service_id"] == str(booked.pk)
    assert summary["room_count"] == 1 and summary["guest_count"] == 1
    assert summary["nights"] == 2 and summary["cancellation_policy"]
    assert summary["financial"]["penalty"] is None and summary["financial"]["refund"] is None
    assert data["capabilities"]["cancel_booking"]["mode"] == "api"
    assert data["capabilities"]["cancel_rooms"]["mode"] == "unsupported"
    assert data["capabilities"]["change_dates"]["mode"] == "manual"
    assert data["capabilities"]["add_room"]["mode"] == "new_booking"


def test_one_supplier_item_is_not_one_room_and_guests_keep_room_assignments(booked):
    snapshot = deepcopy(booked.provider_snapshot)
    snapshot["hotelbook"]["offer"]["rooms"][0]["quantity"] = 2
    booked.provider_snapshot = snapshot
    booked.passengers.update(room_ref="room:1")
    summary = booking_summary(booked)
    assert len(summary["rooms"]) == 2
    assert [r["ref"] for r in summary["rooms"]] == ["room:0", "room:1"]
    assert not summary["rooms"][0]["guests"] and len(summary["rooms"][1]["guests"]) == 1
    assert all(r["price"] is None for r in summary["rooms"])


@pytest.mark.parametrize(
    "action,scope,refs",
    [
        ("cancel_rooms", "rooms", ["room:0"]),
        ("cancel_booking", "rooms", ["room:0"]),
        ("cancel_booking", "booking", []),
        ("remove_room", "rooms", ["room:0"]),
        ("add_room", "booking", ["room:0"]),
    ],
)
def test_unsupported_partial_and_new_booking_never_execute_supplier_mutation(
    admin_client, booked, gateway, action, scope, refs
):
    response = create(admin_client, booked, action, scope=scope, refs=refs)
    assert response.status_code == 409, response.content
    assert not cancels(gateway) and not HotelOperation.objects.exists()


def test_whole_cancel_requires_financial_confirmation_and_executes_once(admin_client, booked, gateway):
    costs = (booked.supplier_cost, booked.client_total, booked.agency_fee)
    response = create(admin_client, booked, "cancel_booking")
    assert response.status_code == 201, response.content
    op = response.json()
    assert op["status"] == "awaiting_operator_confirmation"
    assert not cancels(gateway)
    assert confirm(admin_client, booked, op, confirmed=False).status_code == 400
    assert confirm(admin_client, booked, op, accept_unknown_penalty=False).status_code == 400
    assert confirm(admin_client, booked, op).status_code == 202
    assert confirm(admin_client, booked, op).status_code == 202
    assert BackgroundJob.objects.filter(kind="hotel.operation").count() == 1
    jobs()
    assert len(cancels(gateway)) == 1
    booked.refresh_from_db()
    assert booked.status == "cancelled" and costs == (
        booked.supplier_cost,
        booked.client_total,
        booked.agency_fee,
    )
    operation = HotelOperation.objects.get(pk=op["id"])
    assert operation.status == "completed" and operation.confirmed_at and operation.completed_at
    assert operation.financial_snapshot["confirmation"]["accepted_unknown_penalty"] is True
    assert operation.history.filter(action="financial_confirmation").exists()
    assert operation.history.filter(action="result").exists()
    assert AuditEvent.objects.filter(action="hotel.result", resource_id=str(operation.pk)).exists()
    # Explicit worker redelivery cannot repeat a cancel.
    from services.hotel_actions.operations import execute_operation

    execute_operation(operation.job)
    assert len(cancels(gateway)) == 1


def test_timeout_stays_unknown_blocks_resubmission_and_refresh_reconciles(admin_client, booked, gateway):
    op = create(admin_client, booked, "cancel_booking").json()
    confirm(admin_client, booked, op)
    gateway.failure = ("/cancel", TimeoutError())
    jobs()
    operation = HotelOperation.objects.get(pk=op["id"])
    assert operation.status == "unknown"
    booked.refresh_from_db()
    assert booked.status == "booked"
    count = len(cancels(gateway))
    assert create(admin_client, booked, "cancel_booking", key="another-cancel").status_code == 409
    assert confirm(admin_client, booked, op).status_code == 202
    jobs()
    assert len(cancels(gateway)) == count
    gateway.failure = None
    gateway.item_status = "CANCELED"
    assert create(admin_client, booked, "refresh", key="status-inquiry").status_code == 201
    jobs()
    operation.refresh_from_db()
    booked.refresh_from_db()
    assert operation.status == "completed" and booked.status == "cancelled"
    assert operation.history.filter(action="status_reconciled").exists()
    assert len(cancels(gateway)) == count


def test_manual_request_is_durable_and_does_not_change_booking_or_money(admin_client, booked, gateway):
    before = len(gateway.calls), booked.status, booked.supplier_cost, deepcopy(booked.provider_snapshot)
    response = create(
        admin_client,
        booked,
        "change_dates",
        scope="rooms",
        changes={"description": "Перенести выезд на один день"},
    )
    assert response.status_code == 201, response.content
    op = response.json()
    assert op["execution_mode"] == "manual" and op["status"] == "created"
    assert (
        create(
            admin_client,
            booked,
            "change_dates",
            scope="rooms",
            changes={"description": "Перенести выезд на один день"},
        ).json()["id"]
        == op["id"]
    )
    assert (
        create(admin_client, booked, "change_dates", changes={"description": "Другой запрос"}).status_code
        == 409
    )
    resolve = f"{url(booked)}{op['id']}/resolve/"
    for target in ["checking", "completed"]:
        response = admin_client.post(
            resolve,
            {"status": target, "note": "Зафиксирован ответ поставщика", "version": op["version"]},
            format="json",
        )
        assert response.status_code == 200, response.content
        op = response.json()
    booked.refresh_from_db()
    assert before == (len(gateway.calls), booked.status, booked.supplier_cost, booked.provider_snapshot)
    assert op["result_snapshot"]["booking_unchanged"] and len(op["history"]) == 3


def test_cancel_confirmation_checks_financial_permissions(
    admin_client, operator_client, operator_user, booked
):
    booked.order.operator = operator_user
    booked.order.save(update_fields=["operator"])
    op = create(admin_client, booked, "cancel_booking").json()
    response = confirm(operator_client, booked, op)
    assert response.status_code == 403
    assert not BackgroundJob.objects.filter(kind="hotel.operation").exists()


def test_service_visibility_and_kind_are_enforced(admin_client, operator_client, booked, other_tenant):
    assert operator_client.get(url(booked)).status_code == 404
    from conftest import auth_client, make_user

    client = auth_client(make_user(other_tenant, "outsider@hotel.local", "admin"))
    assert client.get(url(booked)).status_code == 404
    booked.kind = "avia"
    booked.save(update_fields=["kind"])
    assert admin_client.get(url(booked)).status_code == 404


def test_legacy_ticket_form_cannot_create_hotel_case(admin_client, booked):
    response = admin_client.post(
        "/api/v1/after-sales/",
        {"order": str(booked.order_id), "service": str(booked.pk), "type": "exchange"},
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "HOTEL_ACTIONS_REQUIRED"


def test_no_imagined_api_capabilities_or_free_cancellation(booked):
    base = ProviderAdapter().hotel_action_capabilities(booked)
    assert base["refresh"]["mode"] == "unsupported"
    assert base["cancel_booking"]["mode"] == "manual"
    for adapter in [get_adapter("hotelbook"), get_adapter("aanda")]:
        assert adapter.hotel_action_capabilities(booked)["cancel_rooms"]["mode"] == "unsupported"
    assert booking_summary(booked)["financial"]["penalty_exact"] is False


def test_supplier_response_is_sanitized_and_unknown_not_success(admin_client, booked):
    with patch(
        "integrations.hotelbook.HotelbookAdapter.retrieve_booking",
        return_value={"status": "pending", "token": "never-store-this"},
    ):
        response = create(admin_client, booked, "refresh")
        assert response.status_code == 201
        jobs()
    operation = HotelOperation.objects.get()
    assert operation.status == "unknown" and operation.supplier_response["token"] == "[REDACTED]"
    assert "never-store-this" not in str(operation.result_snapshot)


def test_stale_confirmation_and_cross_service_operations_are_rejected(admin_client, booked):
    op = create(admin_client, booked, "cancel_booking").json()
    booked.version += 1
    booked.save(update_fields=["version"])
    assert confirm(admin_client, booked, op).status_code == 409
    other = OrderService.objects.create(
        tenant=booked.tenant, order=booked.order, kind="hotel", source="manual", title="Other hotel"
    )
    assert confirm(admin_client, other, op).status_code == 404
    assert not BackgroundJob.objects.filter(kind="hotel.operation").exists()


def test_shared_supplier_booking_cannot_be_cancelled_as_one_crm_service(admin_client, booked):
    OrderService.objects.create(
        tenant=booked.tenant,
        order=booked.order,
        supplier=booked.supplier,
        kind="hotel",
        source="api",
        title="Another service of same supplier booking",
        provider_snapshot=booked.provider_snapshot,
    )
    data = admin_client.get(url(booked)).json()
    assert data["capabilities"]["cancel_booking"]["mode"] == "unsupported"
    assert create(admin_client, booked, "cancel_booking").status_code == 409


def test_legacy_cancel_marker_blocks_new_cancel_but_allows_status_inquiry(admin_client, booked):
    snapshot = deepcopy(booked.provider_snapshot)
    snapshot["hotelbook_booking"]["cancel_requested_ids"] = ["supplier-item"]
    booked.provider_snapshot = snapshot
    booked.save(update_fields=["provider_snapshot"])
    caps = admin_client.get(url(booked)).json()["capabilities"]
    assert caps["cancel_booking"]["mode"] == "unsupported"
    assert caps["refresh"]["mode"] == "api"
    assert create(admin_client, booked, "cancel_booking").status_code == 409


def test_client_cannot_submit_costs_or_claim_a_free_cancellation(admin_client, booked):
    assert (
        create(admin_client, booked, "cancel_booking", changes={"cancellation_mode": "free"}).status_code
        == 400
    )
    assert (
        create(
            admin_client, booked, "change_dates", changes={"description": "request", "supplier_cost": "0"}
        ).status_code
        == 400
    )
    assert (
        create(
            admin_client, booked, "change_dates", refs=["room:99"], changes={"description": "request"}
        ).status_code
        == 400
    )
    assert not HotelOperation.objects.exists()


def test_late_worker_result_cannot_undo_confirmed_status_inquiry(admin_client, booked):
    op = create(admin_client, booked, "cancel_booking").json()
    confirm(admin_client, booked, op)

    def already_reconciled(*args):
        from django.utils import timezone

        HotelOperation.objects.filter(pk=op["id"]).update(
            status="completed", result_snapshot={"service_status": "cancelled"}, completed_at=timezone.now()
        )
        return {"status": "pending"}

    with patch("integrations.hotelbook.HotelbookAdapter.cancel", side_effect=already_reconciled):
        jobs()
    operation = HotelOperation.objects.get(pk=op["id"])
    assert operation.status == "completed" and operation.result_snapshot["service_status"] == "cancelled"


def test_refresh_recovers_existing_unknown_workflow_without_booking_again(admin_client, booked, gateway):
    item = booked.workflow_items.get()
    item.status = "unknown"
    item.error_code = "BOOKING_UNKNOWN"
    item.save(update_fields=["status", "error_code"])
    item.workflow.status = "partial"
    item.workflow.save(update_fields=["status"])
    booked.status = "approval"
    booked.save(update_fields=["status"])
    book_count = sum(call[0] == "POST" and call[1].endswith("/book") for call in gateway.calls)
    assert create(admin_client, booked, "refresh").status_code == 201
    jobs()
    item.refresh_from_db()
    item.workflow.refresh_from_db()
    booked.refresh_from_db()
    assert booked.status == "booked" and item.status == "booked" and not item.error_code
    assert item.workflow.status == "completed"
    assert sum(call[0] == "POST" and call[1].endswith("/book") for call in gateway.calls) == book_count
