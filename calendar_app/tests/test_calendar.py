from datetime import timedelta

import pytest
from django.utils import timezone

from crm.models import Person
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
