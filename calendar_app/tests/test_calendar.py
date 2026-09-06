from datetime import timedelta

import pytest
from django.utils import timezone

from calendar_app.models import Trip, TripConflict
from crm.models import Person
from documents.models import Document
from finance.models import FinancialObligation
from integrations.models import IntegrationIncident
from orders.models import Order, OrderParticipant
from services.models import OrderService

pytestmark = pytest.mark.django_db


def create_order(client, tenant, user, surname="Календарь"):
    person = Person.objects.create(
        tenant=tenant,
        surname=surname,
        given_name="Тест",
        created_by=user,
    )
    response = client.post(
        "/api/v1/orders/",
        {"request_type": "individual", "client_person": str(person.id)},
        format="json",
    )
    assert response.status_code == 201, response.content
    return response.json()


def test_calendar_event_validates_service_order(admin_client, tenant, admin_user):
    first = create_order(admin_client, tenant, admin_user, "Первый")
    second = create_order(admin_client, tenant, admin_user, "Второй")
    service = OrderService.objects.create(
        tenant=tenant,
        order_id=second["id"],
        kind="avia",
        title="Чужой билет",
        currency="USD",
        client_total="100.00",
        created_by=admin_user,
    )

    response = admin_client.post(
        "/api/v1/calendar/events/",
        {
            "kind": "reminder",
            "title": "Проверить услугу",
            "starts_at": (timezone.now() + timedelta(hours=1)).isoformat(),
            "order": first["id"],
            "service": str(service.id),
        },
        format="json",
    )

    assert response.status_code == 400
    assert "service" in response.json()["error"]["fields"]


def test_calendar_event_create_and_complete(admin_client, tenant, admin_user):
    order = create_order(admin_client, tenant, admin_user)
    response = admin_client.post(
        "/api/v1/calendar/events/",
        {
            "kind": "task",
            "title": "Проверить документы",
            "starts_at": (timezone.now() + timedelta(hours=1)).isoformat(),
            "order": order["id"],
            "priority": "high",
        },
        format="json",
    )

    assert response.status_code == 201, response.content
    body = response.json()
    assert body["status"] == "scheduled"

    done = admin_client.post(f"/api/v1/calendar/events/{body['id']}/complete/", {}, format="json")
    assert done.status_code == 200, done.content
    assert done.json()["status"] == "done"


def test_calendar_feed_contains_real_trip_context(admin_client, tenant, admin_user):
    created = create_order(admin_client, tenant, admin_user, "Поездка")
    order = Order.objects.get(pk=created["id"])
    OrderParticipant.objects.create(
        tenant=tenant,
        order=order,
        person=order.client_person,
        created_by=admin_user,
    )
    starts_at = timezone.now() + timedelta(days=2)
    service = OrderService.objects.create(
        tenant=tenant,
        order=order,
        kind="avia",
        title="FRU → IST",
        status="booked",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=5),
        ticketing_deadline=starts_at - timedelta(days=1),
        currency="USD",
        client_total="500.00",
        created_by=admin_user,
    )
    FinancialObligation.objects.create(
        tenant=tenant,
        order=order,
        service=service,
        direction="client_receivable",
        currency="USD",
        original_amount="500.00",
        created_by=admin_user,
    )
    paid_service = OrderService.objects.create(
        tenant=tenant,
        order=order,
        kind="hotel",
        title="Оплаченная гостиница",
        status="confirmed",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(days=3),
        currency="USD",
        client_total="300.00",
        created_by=admin_user,
    )
    FinancialObligation.objects.create(
        tenant=tenant,
        order=order,
        service=paid_service,
        direction="client_receivable",
        status="settled",
        currency="USD",
        original_amount="300.00",
        created_by=admin_user,
    )
    supplier_only_service = OrderService.objects.create(
        tenant=tenant,
        order=order,
        kind="transfer",
        title="Трансфер без клиентского начисления",
        status="confirmed",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        currency="USD",
        client_total="0.00",
        created_by=admin_user,
    )
    FinancialObligation.objects.create(
        tenant=tenant,
        order=order,
        service=supplier_only_service,
        direction="supplier_payable",
        currency="USD",
        original_amount="50.00",
        created_by=admin_user,
    )
    Document.objects.create(
        tenant=tenant,
        order=order,
        service=service,
        kind="ticket",
        status="generated",
        title="Билет FRU → IST",
        created_by=admin_user,
    )
    trip = Trip.objects.create(
        tenant=tenant,
        order=order,
        title="Бишкек → Стамбул",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(days=3),
        created_by=admin_user,
    )
    TripConflict.objects.create(
        trip=trip,
        kind="short_connection",
        severity="high",
        details={"message": "Недостаточно времени на пересадку"},
    )
    IntegrationIncident.objects.create(
        tenant=tenant,
        order=order,
        service=service,
        error_code="PROVIDER_TIMEOUT",
        severity="high",
        sanitized_error="Поставщик не ответил вовремя",
    )

    response = admin_client.get("/api/v1/calendar/feed/")

    assert response.status_code == 200, response.content
    payload = next(row for row in response.json()["trips"] if row["id"] == str(trip.id))
    assert payload["client_name"] == order.client_person.full_name
    assert payload["participants"][0]["name"] == order.client_person.full_name
    services = {row["title"]: row for row in payload["services"]}
    assert services["FRU → IST"]["paid"] is False
    assert services["Оплаченная гостиница"]["paid"] is True
    assert services["Трансфер без клиентского начисления"]["paid"] is None
    assert payload["documents"][0]["name"] == "Билет FRU → IST"
    assert payload["conflicts"][0]["kind"] == "short_connection"
    assert payload["incidents"][0]["error_code"] == "PROVIDER_TIMEOUT"
