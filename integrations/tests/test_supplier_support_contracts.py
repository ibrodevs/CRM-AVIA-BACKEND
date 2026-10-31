"""Regression fixtures for supplier support answers received 2026-10-30."""

# ruff: noqa: F401,F811
from copy import deepcopy
from urllib.error import URLError
from uuid import uuid4

import pytest

from integrations.adapters import AdapterError, AmbiguousResultError
from integrations.tests.test_json_suppliers import (
    context,
    credential,
    iway_ready,
    service,
    supplier,
    transport,
)
from integrations.ttbooking import TTBookingAdapter

pytestmark = pytest.mark.django_db


def file_result(status="NEW", *, tickets=False, deadline="2028-03-10T10:00:00+03:00"):
    products = {"airTicket": []}
    if tickets:
        products["airTicket"] = [
            {
                "carrier": {"code": "SU"},
                "status": "SELL",
                "number": "5551234567890",
                "recordLocator": "TESTPN",
                "passenger": {
                    "passport": {
                        "firstName": "IVAN",
                        "lastName": "TESTOV",
                        "birthday": "1990-01-01T00:00:00+03:00",
                        "number": "TEST12345",
                    }
                },
            }
        ]
    return {
        "respond": {
            "token": 12345678,
            "bookingFile": {
                "status": status,
                "reservations": {
                    "reservation": [{"recordLocator": "", "timeLimit": deadline, "products": products}]
                },
            },
        }
    }


def ready_tt(supplier):
    credential(
        supplier,
        "ttbooking",
        agency="fixture-agency",
        user="fixture-user",
        password_key="private-key",
        currency="RUB",
    )
    adapter = TTBookingAdapter()
    snapshot = {
        "provider_adapter": "ttbooking",
        "kind": "avia",
        "provider_context": {"supplier_id": str(supplier.pk), "environment": "sandbox"},
        "price": {"amount": "100", "currency": "RUB"},
        "itinerary": {
            "segments": [{"origin": "MOW", "destination": "LED", "departure": "2027-03-10T12:00:00+03:00"}]
        },
        "fare": {"passenger_prices": [{"passengerType": "ADULT", "count": 1}]},
        "ttbooking": {
            "selection_token": "same-selection-token",
            "selected_group": {"flightGroup": [{"token": "chosen-group"}]},
        },
    }
    row = service(supplier, adapter.validated(context(supplier), snapshot))
    request = {
        "service_id": str(row.pk),
        "contact_info": {"name": "Fixture contact", "phone": "+79990000000", "email": "test@example.invalid"},
        "passengers": [
            {
                "participant_id": str(uuid4()),
                "latin_given_name": "IVAN",
                "latin_surname": "TESTOV",
                "birth_date": "1990-01-01",
                "gender": "male",
                "citizenship": "RU",
                "document": {"type": "foreign_passport", "number": "TEST12345", "expires_at": "2030-01-01"},
            }
        ],
    }
    return adapter, row, request


def commands(transport):
    import json

    return [json.loads(r.data)["context"]["command"] for r in transport.calls]


def book_tt(supplier, transport):
    adapter, row, request = ready_tt(supplier)
    transport.responses = [
        {"respond": {"token": "prebooking-token"}},
        {"respond": {"token": 12345678, "status": "BOOKING"}},
        file_result(),
    ]
    result = adapter.book(context(supplier), request)
    assert result["status"] == "booked"
    return adapter, row, request, result


def test_tt_split_result_is_called_once_and_empty_token_means_no_offers(supplier, transport):
    adapter, row, request = ready_tt(supplier)
    transport.responses = [{"respond": {"token": ""}}]
    assert (
        adapter.search(
            context(supplier), "avia", {"origin": "MOW", "destination": "LED", "date": "2027-03-10"}
        )
        == []
    )
    assert commands(transport) == ["SEARCHFLIGHTS"]
    transport.responses = [{"respond": {"flightsGroup": {"flightGroup": []}}}]
    adapter._async_result(
        context(supplier), adapter._config(context(supplier)), "SEARCHRESULT", "search-token"
    )
    assert commands(transport)[-1] == "SEARCHRESULT"
    assert len(transport.calls) == 2


def test_tt_booking_creates_supplier_order_in_booking_not_status_job(supplier, transport):
    adapter, row, request, result = book_tt(supplier, transport)
    assert commands(transport) == ["CREATEBOOKING", "CREATERESULT", "DISPLAYSTATUS"]
    row.refresh_from_db()
    assert row.provider_snapshot["ttbooking_booking"]["order_token"] == "12345678"
    assert result["ticketing_deadline"] == "2028-03-10T10:00:00+03:00"
    transport.responses = [file_result()]
    assert adapter.retrieve_booking(context(supplier), result["locator"])["status"] == "booked"
    assert commands(transport)[-1] == "DISPLAYSTATUS"


def test_tt_lost_create_response_has_explicit_idempotent_recovery(supplier, transport):
    adapter, row, request = ready_tt(supplier)
    transport.responses = [URLError("timeout")]
    with pytest.raises(AdapterError):
        adapter.book(context(supplier), request)
    row.refresh_from_db()
    assert adapter.can_resume_booking(row)
    locator = row.provider_snapshot["ttbooking_booking"]["locator"]
    before = len(transport.calls)
    with pytest.raises(AdapterError) as error:
        adapter.retrieve_booking(context(supplier), locator)
    assert error.value.code == "BOOKING_RESUME_REQUIRED"
    assert len(transport.calls) == before
    transport.responses = [
        {"respond": {"token": "same-prebooking"}},
        {"respond": {"status": "BOOKING", "token": 12345678}},
        file_result(),
    ]
    result = adapter.resume_booking(context(supplier), request)
    assert result["status"] == "booked" and result["locator"] == locator
    import json

    assert (
        json.loads(transport.calls[0].data)["parameters"] == json.loads(transport.calls[1].data)["parameters"]
    )


def test_tt_lost_createresult_reuses_prebooking_token(supplier, transport):
    adapter, row, request = ready_tt(supplier)
    transport.responses = [{"respond": {"token": "prebooking-token"}}, URLError("timeout")]
    with pytest.raises(AdapterError):
        adapter.book(context(supplier), request)
    transport.responses = [{"respond": {"status": "BOOKING", "token": 12345678}}, file_result()]
    assert adapter.resume_booking(context(supplier), request)["status"] == "booked"
    assert commands(transport).count("CREATEBOOKING") == 1
    assert commands(transport).count("CREATERESULT") == 2


def test_tt_recovery_blocks_changed_passengers(supplier, transport):
    adapter, row, request = ready_tt(supplier)
    transport.responses = [URLError("timeout")]
    with pytest.raises(AdapterError):
        adapter.book(context(supplier), request)
    request["passengers"][0]["latin_given_name"] = "CHANGED"
    with pytest.raises(AdapterError) as error:
        adapter.resume_booking(context(supplier), request)
    assert error.value.code == "RESUME_DATA_CHANGED"
    assert len(transport.calls) == 1


def test_tt_paydeposit_parses_actual_tickets_and_ticket_level_pnr(supplier, transport):
    adapter, row, request, result = book_tt(supplier, transport)
    transport.responses = [
        file_result(),
        file_result("COMPLETE", tickets=True),
        file_result("COMPLETE", tickets=True),
    ]
    issued = adapter.issue(context(supplier), {"locator": result["locator"]})
    assert issued["status"] == "issued"
    assert issued["pnr"] == "TESTPN"
    assert issued["tickets"][0]["ticket_number"] == "5551234567890"
    assert issued["tickets"][0]["validating_carrier"] == "SU"
    assert issued["tickets"][0]["participant_id"] == request["passengers"][0]["participant_id"]
    assert commands(transport)[-3:] == ["DISPLAYSTATUS", "PAYDEPOSIT", "DISPLAYTICKETS"]
    assert "passport" not in str(issued)


def test_tt_complete_without_tickets_is_not_issued(supplier, transport):
    adapter, row, request, result = book_tt(supplier, transport)
    transport.responses = [file_result("COMPLETE"), file_result("COMPLETE")]
    assert adapter.retrieve_booking(context(supplier), result["locator"])["status"] == "pending"


def test_tt_payment_timeout_reconciles_without_second_debit(supplier, transport):
    adapter, row, request, result = book_tt(supplier, transport)
    transport.responses = [file_result(), URLError("timeout")]
    with pytest.raises(AmbiguousResultError):
        adapter.issue(context(supplier), {"locator": result["locator"]})
    transport.responses = [file_result("COMPLETE"), file_result("COMPLETE", tickets=True)]
    assert adapter.issue(context(supplier), {"locator": result["locator"]})["status"] == "issued"
    assert commands(transport).count("PAYDEPOSIT") == 1


def test_tt_payment_without_confirmed_status_then_error_never_reissues(supplier, transport):
    adapter, row, request, result = book_tt(supplier, transport)
    transport.responses = [file_result(), {"respond": {"token": 12345678}}]
    pending = adapter.issue(context(supplier), {"locator": result["locator"]})
    assert pending["status"] == "pending" and not pending.get("tickets")
    transport.responses = [file_result("ERROR")]
    failed = adapter.retrieve_booking(context(supplier), result["locator"])
    assert failed["status"] == "failed" and not failed.get("tickets")
    transport.responses = [file_result("ERROR")]
    with pytest.raises(AdapterError) as error:
        adapter.issue(context(supplier), {"locator": result["locator"]})
    assert error.value.code == "BOOKING_NOT_CONFIRMED"
    assert commands(transport).count("PAYDEPOSIT") == 1
    assert "CANCELBOOKING" not in commands(transport)


@pytest.mark.parametrize(
    "raw", ["PROCESS", "BOOKING_NOT_CONFIRMED", "REQUEST_STATUS", "OPERATOR", "UNKNOWN", "EXCHANGE"]
)
def test_tt_unconfirmed_status_never_allows_payment(supplier, transport, raw):
    adapter, row, request, result = book_tt(supplier, transport)
    transport.responses = [file_result(raw)]
    with pytest.raises(AdapterError):
        adapter.issue(context(supplier), {"locator": result["locator"]})
    assert "PAYDEPOSIT" not in commands(transport)


def test_tt_expired_deadline_blocks_payment(supplier, transport):
    adapter, row, request, result = book_tt(supplier, transport)
    transport.responses = [file_result(deadline="2020-01-01T00:00:00+03:00")]
    with pytest.raises(AdapterError) as error:
        adapter.issue(context(supplier), {"locator": result["locator"]})
    assert error.value.code == "TICKETING_DEADLINE"
    assert "PAYDEPOSIT" not in commands(transport)


@pytest.mark.parametrize(
    "order_status,ticket_status", [("VOID", "VOID"), ("CANCELLED", "VOID_BOOKING")]
)
def test_tt_void_uses_cancellation_and_preserves_void_status(
    supplier, transport, order_status, ticket_status
):
    adapter, row, request, result = book_tt(supplier, transport)
    cancelled_response = file_result(order_status, tickets=True)
    cancelled_response["respond"]["bookingFile"]["reservations"]["reservation"][0]["products"][
        "airTicket"
    ][0]["status"] = ticket_status
    transport.responses = [
        file_result("COMPLETE"),
        file_result("COMPLETE", tickets=True),
        cancelled_response,
    ]
    cancelled = adapter.cancel(context(supplier), result["locator"])
    assert cancelled["status"] == "cancelled" and cancelled["supplier_status"] == order_status
    assert not cancelled.get("tickets")
    assert commands(transport)[-1] == "CANCELBOOKING"


def test_iway_twelve_year_old_does_not_require_child_seat(supplier, transport):
    adapter, row, request = iway_ready(supplier)
    request["passengers"][0]["birth_date"] = "2015-03-10"
    transport.responses = [
        {"result": {"token": "fixture-token"}},
        {"result": [{"allow_booking": True}]},
        {"result": {"order_id": 10, "transaction": 20, "status": 0, "payable_status": 0}},
    ]
    assert adapter.book(context(supplier), request)["status"] == "booked"


def test_iway_extras_price_and_type_constraints(supplier):
    from integrations.iway import IwayAdapter

    source = {
        "price_id": 1,
        "price": "100",
        "currency": "RUB",
        "additional_services": [
            {"id": 10, "type": "booster", "price": "20", "currency": "RUB"},
            {"id": 20, "type": "tollroad", "price": "30", "currency": "RUB"},
        ],
    }
    offer = IwayAdapter()._normalize(
        context(supplier), source, {"additional_service_types": ["booster", "tollroad"]}
    )
    assert offer["price"]["amount"] == "150"
    assert [r["id"] for r in offer["iway"]["selected_services"]] == [10, 20]
    with pytest.raises(AdapterError):
        IwayAdapter()._normalize(
            context(supplier), source, {"additional_service_types": ["booster", "booster"]}
        )


def test_iway_recovery_never_recreates_after_approve(supplier):
    adapter, row, request = iway_ready(supplier)
    row.provider_snapshot["iway_booking"] = {"locator": "fixture", "create_sent": True, "approve_sent": True}
    assert not adapter.can_resume_booking(row)


def test_tt_zero_tokens_and_numeric_currency_are_preserved():
    adapter = TTBookingAdapter()
    result = {
        "respond": {
            "token": "search-token",
            "flightsGroup": {
                "flightGroup": [
                    {
                        "token": 0,
                        "fares": {
                            "fareCurrency": 643,
                            "fareTotal": {"total": 100},
                            "fareSeats": {"fareSeat": [{"passengerType": "ADULT", "count": "1"}]},
                        },
                        "itineraries": {
                            "itinerary": [
                                {
                                    "token": 0,
                                    "flights": {
                                        "flight": [
                                            {
                                                "token": 0,
                                                "segments": {
                                                    "segment": [
                                                        {
                                                            "locationBegin": {"code": "DME"},
                                                            "locationEnd": {"code": "LED"},
                                                            "carrier": {"code": "S7"},
                                                            "dateBegin": "2027-03-10T12:00:00",
                                                            "flightNumber": "1015",
                                                        }
                                                    ]
                                                },
                                            }
                                        ]
                                    },
                                }
                            ]
                        },
                    }
                ]
            },
        }
    }
    offer = adapter._normalize_search(result, "USD")[0]
    group = offer["provider_selection"]["flightsGroup"]["flightGroup"][0]
    assert group["token"] == 0
    assert group["itineraries"]["itinerary"][0]["token"] == 0
    assert group["itineraries"]["itinerary"][0]["flights"]["flight"][0]["token"] == 0
    assert offer["price"]["currency"] == "RUB"
    assert offer["fare"]["passenger_prices"][0]["count"] == 1


def test_tt_passenger_email_uses_crm_email_or_booking_contact(supplier):
    from datetime import date

    adapter, row, request = ready_tt(supplier)
    passenger = request["passengers"][0]
    result = adapter._passport(passenger, date(2027, 3, 10), request["contact_info"])
    assert result["email"] == request["contact_info"]["email"]
    assert result["isEmailAbsent"] is False
    passenger["email"] = "person@example.invalid"
    assert (
        adapter._passport(passenger, date(2027, 3, 10), request["contact_info"])["email"]
        == "person@example.invalid"
    )


def test_iway_child_booster_is_sent_with_correct_total(supplier, transport):
    import json

    adapter, row, request = iway_ready(supplier)
    request["passengers"][0]["birth_date"] = "2020-03-10"
    row.provider_snapshot["iway"]["selected_services"] = [{"id": 15, "type": "booster", "price": "5"}]
    row.provider_snapshot["price"]["amount"] = "25"
    row.provider_snapshot = adapter.validated(context(supplier), row.provider_snapshot)
    row.save(update_fields=["provider_snapshot"])
    transport.responses = [
        {"result": {"token": "fixture-token"}},
        {"result": [{"allow_booking": True}]},
        {"result": {"order_id": 10, "transaction": 20, "status": 0, "payable_status": 0}},
    ]
    assert adapter.book(context(supplier), request)["status"] == "booked"
    body = json.loads(transport.calls[-1].data)["trips"][0]
    assert body["additional_services"] == [{"id": 15}]
    assert body["children_amount"] == 1


def test_iway_lost_unapproved_create_can_resume_with_audit_history(supplier, transport):
    adapter, row, request = iway_ready(supplier)
    transport.responses = [
        {"result": {"token": "fixture-token"}},
        {"result": [{"allow_booking": True}]},
        URLError("lost response"),
    ]
    with pytest.raises(AdapterError):
        adapter.book(context(supplier), request)
    row.refresh_from_db()
    locator = row.provider_snapshot["iway_booking"]["locator"]
    assert adapter.can_resume_booking(row)

    # Revalidation is mandatory for replacement; here it confirms the same signed offer.
    def revalidate(ctx, snapshot):
        return {"snapshot": adapter.validated(ctx, snapshot), "price_changed": False, "terms_changed": False}

    adapter.revalidate = revalidate
    transport.responses = [
        {"result": [{"allow_booking": True, "similar_orders": [999]}]},
        {"result": {"order_id": 10, "transaction": 20, "status": 0, "payable_status": 0}},
    ]
    result = adapter.resume_booking(context(supplier), request)
    assert result["locator"] == locator and result["status"] == "booked"
    row.refresh_from_db()
    assert len(row.provider_snapshot["iway_booking"]["abandoned_attempts"]) == 1
    assert not adapter.can_resume_booking(row)


def iway_cancel_setup(supplier):
    adapter, row, request = iway_ready(supplier)
    snapshot = deepcopy(row.provider_snapshot)
    snapshot["iway_booking"] = {
        "locator": "cancel-fixture",
        "order_id": 10,
        "transaction": 20,
        "state": "complete",
        "normalized_status": "issued",
        "agreed_price": {"amount": "20", "currency": "EUR"},
    }
    row.provider_snapshot = snapshot
    row.save(update_fields=["provider_snapshot"])
    return (
        adapter,
        row,
        {
            "order_id": 10,
            "transaction": 20,
            "status": 2,
            "payable_status": 1,
            "price": {"price": 20},
            "currency": "EUR",
            "additional_services": [],
        },
    )


def test_iway_paid_cancel_requires_signed_current_penalty_consent(supplier, transport):
    adapter, row, trip = iway_cancel_setup(supplier)
    transport.responses = [
        {"result": {"token": "fixture-token"}},
        {"result": {"orders": [trip]}},
        {"result": {"result": True, "status": 5}},
    ]
    quote = adapter.cancellation_quote(context(supplier), "cancel-fixture")
    assert quote["percent"] == 100 and quote["penalty"] == "20" and quote["currency"] == "EUR"
    transport.responses = [
        {"result": {"orders": [trip]}},
        {"result": {"result": True, "status": 5}},
        {"result": {"order_id": 10, "transaction": 20, "status": 5, "payable_status": 5}},
    ]
    assert (
        adapter.cancel(context(supplier), "cancel-fixture", confirmation=quote["confirmation"])["status"]
        == "cancelled"
    )


def test_iway_changed_penalty_or_invalid_confirmation_blocks_cancel(supplier, transport):
    adapter, row, trip = iway_cancel_setup(supplier)
    transport.responses = [
        {"result": {"token": "fixture-token"}},
        {"result": {"orders": [trip]}},
        {"result": {"result": True, "status": 4}},
    ]
    quote = adapter.cancellation_quote(context(supplier), "cancel-fixture")
    transport.responses = [{"result": {"orders": [trip]}}, {"result": {"result": True, "status": 5}}]
    with pytest.raises(AdapterError) as error:
        adapter.cancel(context(supplier), "cancel-fixture", confirmation=quote["confirmation"])
    assert error.value.code == "CANCEL_PRICE_CHANGED"
    assert not any("/trips/cancel" in r.full_url for r in transport.calls)


def test_iway_unknown_current_cost_does_not_fake_a_paid_cancel_quote(supplier, transport):
    adapter, row, trip = iway_cancel_setup(supplier)
    trip.pop("price")
    transport.responses = [
        {"result": {"token": "fixture-token"}},
        {"result": {"orders": [trip]}},
        {"result": {"result": True, "status": 5}},
    ]
    with pytest.raises(AdapterError) as error:
        adapter.cancellation_quote(context(supplier), "cancel-fixture")
    assert error.value.code == "CANCEL_PRICE_UNKNOWN"


def test_iway_order_total_already_includes_extras(supplier):
    adapter, row, trip = iway_cancel_setup(supplier)
    trip["price"] = {"price": 1980}
    trip["currency"] = "RUB"
    trip["additional_services"] = [{"type": "tollroad", "price": "300.00"}]
    adapter._result(row, row.provider_snapshot, trip)
    assert row.provider_snapshot["iway_booking"]["current_supplier_price"]["amount"] == "1980"


def test_iway_unapproved_draft_abandon_never_calls_approve_or_supplier_cancel(supplier, transport):
    adapter, row, trip = iway_cancel_setup(supplier)
    trip["status"] = 0
    trip["payable_status"] = 0
    transport.responses = [{"result": {"token": "fixture-token"}}, {"result": {"orders": [trip]}}]
    result = adapter.cancel(context(supplier), "cancel-fixture")
    assert result["status"] == "cancelled" and result["local_only"] is True
    assert not any("/approve/" in r.full_url or "/trips/cancel" in r.full_url for r in transport.calls)
    row.refresh_from_db()
    assert row.provider_snapshot["iway_booking"]["abandoned_unapproved"]
    with pytest.raises(AdapterError):
        adapter.issue(context(supplier), {"locator": "cancel-fixture"})


def workflow_item(row, status="unknown"):
    from booking.models import BookingWorkflow, BookingWorkflowItem

    workflow = BookingWorkflow.objects.create(tenant_id=row.tenant_id, order=row.order, status="partial")
    return BookingWorkflowItem.objects.create(
        tenant_id=row.tenant_id, workflow=workflow, service=row, status=status, locator="fixture-locator"
    )


def test_resume_endpoint_queues_only_safe_booking_recovery(admin_client, supplier):
    from common.models import BackgroundJob

    adapter, row, request = iway_ready(supplier)
    row.provider_snapshot["iway_booking"] = {"locator": "fixture-locator", "create_sent": True}
    row.save(update_fields=["provider_snapshot"])
    item = workflow_item(row)
    response = admin_client.post(
        f"/api/v1/booking-workflows/{item.workflow_id}/resume/",
        {"item": str(item.id)},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    assert response.status_code == 202, response.content
    assert BackgroundJob.objects.get(pk=response.json()["job_id"]).kind == "booking.resume"
    row.provider_snapshot["iway_booking"]["approve_sent"] = True
    row.save(update_fields=["provider_snapshot"])
    response = admin_client.post(
        f"/api/v1/booking-workflows/{item.workflow_id}/resume/",
        {"item": str(item.id)},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    assert response.status_code == 409


def test_resume_does_not_access_other_tenant_workflow(admin_client, other_tenant):
    from suppliers.models import Supplier

    other = Supplier.objects.create(tenant=other_tenant, name="Other")
    row = service(
        other,
        {
            "provider_adapter": "iway",
            "kind": "transfer",
            "iway_booking": {"locator": "fixture", "create_sent": True},
        },
    )
    item = workflow_item(row)
    response = admin_client.post(
        f"/api/v1/booking-workflows/{item.workflow_id}/resume/",
        {"item": str(item.id)},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    assert response.status_code == 404


def test_void_requires_explicit_confirmation_and_correct_kind(admin_client, supplier):
    adapter, row, request = ready_tt(supplier)
    row.provider_snapshot["ttbooking_booking"] = {
        "locator": "fixture-locator",
        "order_token": "12345678",
        "supplier_status": "COMPLETE",
        "normalized_status": "issued",
    }
    row.save(update_fields=["provider_snapshot"])
    item = workflow_item(row, "issued")
    response = admin_client.post(
        f"/api/v1/booking-workflows/{item.workflow_id}/void/",
        {"item": str(item.id)},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    assert response.status_code == 409
    response = admin_client.post(
        f"/api/v1/booking-workflows/{item.workflow_id}/void/",
        {"item": str(item.id), "confirm": True},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    assert response.status_code == 202, response.content


def test_tt_explicit_insufficient_deposit_can_only_retry_on_new_manual_request(supplier, transport):
    adapter, row, request, result = book_tt(supplier, transport)
    refusal = file_result()
    refusal["respond"]["messages"] = {"message": [{"type": "MESSAGE", "message": "100 > 0NOT ENOUGHT MONEY"}]}
    transport.responses = [file_result(), refusal]
    with pytest.raises(AdapterError) as error:
        adapter.issue(context(supplier), {"locator": result["locator"]})
    assert error.value.code == "INSUFFICIENT_DEPOSIT"
    row.refresh_from_db()
    assert row.provider_snapshot["ttbooking_booking"]["issue_sent"] is False
    assert commands(transport).count("PAYDEPOSIT") == 1
    transport.responses = [
        file_result(),
        file_result("COMPLETE", tickets=True),
        file_result("COMPLETE", tickets=True),
    ]
    assert adapter.issue(context(supplier), {"locator": result["locator"]})["status"] == "issued"
    assert commands(transport).count("PAYDEPOSIT") == 2


def test_ticket_links_to_crm_passenger_and_void_updates_existing_ticket(supplier, monkeypatch):
    from types import SimpleNamespace

    from avia.models import Ticket
    from booking.job_handlers import _save_tickets, void_tickets
    from orders.models import OrderParticipant
    from services.models import ServicePassenger

    adapter, row, request = ready_tt(supplier)
    participant = OrderParticipant.objects.create(
        tenant_id=row.tenant_id, order=row.order, person=row.order.client_person
    )
    passenger = ServicePassenger.objects.create(tenant_id=row.tenant_id, service=row, participant=participant)
    item = workflow_item(row, "issued")
    row.provider_snapshot["ttbooking_booking"] = {
        "locator": item.locator,
        "order_token": "12345678",
        "supplier_status": "COMPLETE",
    }
    row.save(update_fields=["provider_snapshot"])
    _save_tickets(
        item.workflow,
        row,
        item,
        {
            "status": "issued",
            "pnr": "TESTPN",
            "pnrs": ["TESTPN"],
            "tickets": [
                {
                    "ticket_number": "5551234567890",
                    "validating_carrier": "SU",
                    "participant_id": str(participant.pk),
                    "pnr": "TESTPN",
                }
            ],
        },
    )
    ticket = Ticket.objects.get()
    assert ticket.passenger_id == passenger.id
    assert ticket.booking.provider_adapter == "ttbooking"
    monkeypatch.setattr(
        TTBookingAdapter,
        "cancel",
        lambda *args: {"locator": item.locator, "status": "cancelled", "supplier_status": "VOID"},
    )
    void_tickets(
        SimpleNamespace(
            payload={"item_id": str(item.id)},
            tenant_id=row.tenant_id,
            correlation_id="void-fixture",
            id=uuid4(),
            kind="booking.void",
        )
    )
    ticket.refresh_from_db()
    assert ticket.status == "void"
    row.refresh_from_db()
    assert row.status == "cancelled"
