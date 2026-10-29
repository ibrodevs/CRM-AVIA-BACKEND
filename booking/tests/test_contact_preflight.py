# ruff: noqa: F401, F811
from unittest.mock import patch

import pytest

from booking.models import BookingWorkflow
from crm.models import Company, Person
from integrations.tests.test_hotelbook import gateway, hb_supplier, jobs, post, ready_booking, start
from orders.models import Order

pytestmark = pytest.mark.django_db


def make_corporate(order_id, tenant, *, contact=None, marked=False):
    order = Order.objects.get(pk=order_id)
    company = Company.objects.create(
        tenant=tenant, legal_name="Booking contact company", tax_id=f"contact-{str(order.pk)[:20]}"
    )
    order.client_company = company
    order.client_person = None
    order.contact_person = contact
    order.save(update_fields=["client_company", "client_person", "contact_person"])
    order.participants.update(is_contact=marked)
    return order


def test_hbpro_preflight_and_worker_use_marked_crm_contact_without_contact_name(
    admin_client, tenant, hb_supplier, gateway
):
    workflow, service = ready_booking(admin_client, tenant, hb_supplier, preflight=False)
    order = make_corporate(service["order"], tenant, marked=True)
    assert order.contact_person_id is None
    expected = order.participants.get().person.full_name
    preflight = post(admin_client, f"booking-workflows/{workflow['id']}/preflight/")
    assert preflight["ok"], preflight
    assert start(admin_client, workflow)["items"][0]["status"] == "booked"
    request = next(
        call for call in gateway.calls if call[0] == "POST" and call[1].endswith("/order/gateway/orders")
    )
    assert request[3]["contactInfo"]["name"] == expected
    order.refresh_from_db()
    assert order.contact_person_id is None


@pytest.mark.parametrize(
    "unnamed,expected",
    [(False, "Укажите контактное лицо заказа."), (True, "У контактного лица не заполнено имя.")],
)
def test_contact_errors_block_preflight_before_booking_and_use_exact_messages(
    admin_client, tenant, hb_supplier, gateway, unnamed, expected
):
    workflow, service = ready_booking(admin_client, tenant, hb_supplier, preflight=False)
    person = Person.objects.create(tenant=tenant, surname="   ", given_name="\t") if unnamed else None
    make_corporate(service["order"], tenant, contact=person)
    result = post(admin_client, f"booking-workflows/{workflow['id']}/preflight/")
    assert not result["ok"]
    assert {"code": "CONTACT_REQUIRED", "message": expected} in result["blocking_errors"]
    assert not any(
        call[1].endswith(("/order/gateway/orders", "/hotel/gateway/book")) for call in gateway.calls
    )


def test_cached_successful_preflight_does_not_allow_start_after_contact_was_removed(
    admin_client, tenant, hb_supplier, gateway
):
    workflow, service = ready_booking(admin_client, tenant, hb_supplier)
    make_corporate(service["order"], tenant)
    response = admin_client.post(
        f"/api/v1/booking-workflows/{workflow['id']}/start/",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY="contact-removed",
    )
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "Укажите контактное лицо заказа."
    assert BookingWorkflow.objects.get(pk=workflow["id"]).status == "preflight_ok"


def test_worker_rechecks_contact_after_start_and_does_not_create_supplier_order(
    admin_client, tenant, hb_supplier, gateway
):
    workflow, service = ready_booking(admin_client, tenant, hb_supplier)
    post(admin_client, f"booking-workflows/{workflow['id']}/start/", key="contact-before-worker")
    make_corporate(service["order"], tenant)
    before = len(gateway.calls)
    jobs()
    item = BookingWorkflow.objects.get(pk=workflow["id"]).items.get()
    assert item.error_code == "CONTACT_REQUIRED"
    assert item.error_message == "Укажите контактное лицо заказа."
    assert not any(
        call[1].endswith(("/order/gateway/orders", "/hotel/gateway/book")) for call in gateway.calls[before:]
    )
