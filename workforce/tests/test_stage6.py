import pytest
from django.core.management import call_command

from conftest import auth_client

pytestmark = pytest.mark.django_db


def run_jobs_once():
    call_command("run_jobs", "--once", "--worker-id", "test")


class TestNotifications:
    def test_rule_creates_notification_from_event(self, admin_client, admin_user, operator_user, tenant):

        admin_client.post(
            "/api/v1/notification-rules/",
            {
                "event_type": "order.*",
                "name": "Заказы",
                "priority": "high",
                "recipients": {"roles": ["operator"]},
                "channels": ["desktop"],
            },
            format="json",
        )
        from common.outbox import emit_event
        from tenancy.context import tenant_context

        with tenant_context(tenant.id):
            emit_event("order.updated", "Order", tenant_id=tenant.id)
        run_jobs_once()
        operator = auth_client(operator_user)
        body = operator.get("/api/v1/notifications/").json()
        assert body["unread_count"] == 1
        assert body["results"][0]["priority"] == "high"

    def test_personal_state_isolated(self, admin_client, admin_user, operator_user, tenant):
        from notifications.models import Notification

        for user in (admin_user, operator_user):
            Notification.objects.create(tenant=tenant, user=user, title="Тест")
        operator = auth_client(operator_user)
        notification_id = operator.get("/api/v1/notifications/").json()["results"][0]["id"]
        operator.post(f"/api/v1/notifications/{notification_id}/read/")

        assert admin_client.get("/api/v1/notifications/").json()["unread_count"] == 1

    def test_deadline_check_no_duplicates(self, admin_client, tenant, admin_user):
        from datetime import timedelta

        from django.utils import timezone

        from crm.models import Person
        from services.models import OrderService

        person = Person.objects.create(tenant=tenant, surname="Д", given_name="Д", created_by=admin_user)
        order = admin_client.post(
            "/api/v1/orders/",
            {
                "request_type": "individual",
                "client_person": str(person.id),
            },
            format="json",
        ).json()
        from orders.models import Order

        OrderService.objects.create(
            tenant=tenant,
            order=Order.objects.get(pk=order["id"]),
            kind="avia",
            title="Тест",
            status="booked",
            currency="USD",
            ticketing_deadline=timezone.now() + timedelta(hours=1),
            created_by=admin_user,
        )
        call_command("run_scheduled_jobs", "--only", "notifications.check_deadlines")
        call_command("run_scheduled_jobs", "--only", "notifications.check_deadlines")
        from notifications.models import Notification

        assert Notification.objects.filter(event_type="service.deadline").count() == 2


class TestShifts:
    def test_single_open_shift(self, admin_client):
        assert admin_client.post("/api/v1/shifts/start/", {}, format="json").status_code == 201
        response = admin_client.post("/api/v1/shifts/start/", {}, format="json")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SHIFT_ALREADY_OPEN"

    def test_close_with_discrepancy_confirmation(self, admin_client):
        shift = admin_client.post(
            "/api/v1/shifts/start/",
            {
                "opening_balance": "1000.00",
                "currency": "KGS",
            },
            format="json",
        ).json()
        response = admin_client.post(
            f"/api/v1/shifts/{shift['id']}/close/",
            {
                "closing_balance": "900.00",
            },
            format="json",
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DISCREPANCY_CONFIRMATION_REQUIRED"
        response = admin_client.post(
            f"/api/v1/shifts/{shift['id']}/close/",
            {
                "closing_balance": "900.00",
                "confirm_discrepancy": True,
            },
            format="json",
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "closed"
        assert body["closing_report"] is not None

        assert admin_client.post("/api/v1/shifts/start/", {}, format="json").status_code == 201


class TestDashboardAndMeta:
    def test_dashboard_shape(self, admin_client):
        body = admin_client.get("/api/v1/dashboard/?role_scope=my").json()
        assert set(body) >= {
            "orders",
            "trips_today",
            "my_tasks",
            "sla",
            "finance",
            "integration_incidents",
            "attention",
            "calculated_at",
        }

    def test_meta(self, admin_client):
        body = admin_client.get("/api/v1/meta/").json()
        assert "orders.view" in body["user"]["permissions"]
        assert {"code": "new", "display": "New"} in body["enums"]["order_statuses"] or any(
            s["code"] == "new" for s in body["enums"]["order_statuses"]
        )
        assert "avia" in body["enums"]["service_kinds"]
        assert body["settings"]["base_currency"] == "USD"

    def test_operator_dashboard_no_finance(self, operator_user):
        operator = auth_client(operator_user)
        body = operator.get("/api/v1/dashboard/").json()
        assert body["finance"] == {}


class TestIncidents:
    def test_incident_lifecycle(self, admin_client, admin_user, tenant):
        from integrations.models import IntegrationIncident

        incident = IntegrationIncident.objects.create(
            tenant=tenant,
            error_code="TIMEOUT",
            severity="high",
            operation="search",
        )

        response = admin_client.post(
            f"/api/v1/integration-incidents/{incident.id}/resolve/", {}, format="json"
        )
        assert response.status_code == 400
        response = admin_client.post(
            f"/api/v1/integration-incidents/{incident.id}/resolve/",
            {"resolution_code": "PROVIDER_RECOVERED"},
            format="json",
        )
        assert response.status_code == 200
        assert response.json()["status"] == "resolved"

    def test_unknown_issue_retry_blocked(self, admin_client, tenant):
        from integrations.models import IntegrationIncident

        incident = IntegrationIncident.objects.create(
            tenant=tenant,
            error_code="ISSUE_UNKNOWN",
            severity="critical",
            operation="booking.issue",
        )
        response = admin_client.post(f"/api/v1/integration-incidents/{incident.id}/retry/", {}, format="json")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "RETRY_UNSAFE"


class TestSeed:
    def test_seed_idempotent(self, db, settings):
        settings.DEBUG = True
        call_command("seed_demo_data")
        from orders.models import Order

        count = Order.objects.count()
        call_command("seed_demo_data")
        assert Order.objects.count() == count
