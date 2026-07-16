import pytest
from django.core.management import call_command

from crm.models import Person, PersonDocument

pytestmark = pytest.mark.django_db


def run_jobs_once():
    call_command("run_jobs", "--once", "--worker-id", "test")


@pytest.fixture
def ready_order(admin_client, tenant, admin_user):
    """Заказ с полностью заполненным пассажиром и услугой avia."""
    person = Person.objects.create(
        tenant=tenant, surname="Иванов", given_name="Пётр",
        latin_surname="IVANOV", latin_given_name="PETR",
        birth_date="1990-01-01", gender="male", citizenship="KG",
        created_by=admin_user,
    )
    PersonDocument.objects.create(
        tenant=tenant, person=person, type="foreign_passport",
        number="AC7654321", issuing_country="KG", expires_at="2033-01-01",
        created_by=admin_user,
    )
    order = admin_client.post("/api/v1/orders/", {
        "request_type": "individual", "client_person": str(person.id),
        "planned_start": "2026-08-01", "planned_end": "2026-08-10",
        "participants": [{"person": str(person.id), "role": "passenger"}],
    }, format="json").json()
    service = admin_client.post(f"/api/v1/orders/{order['id']}/services/", {
        "kind": "avia", "title": "FRU-IST TK100", "currency": "USD",
        "client_total": "450.00",
    }, format="json").json()
    return order, service, person


def make_workflow(client, order, service) -> dict:
    return client.post("/api/v1/booking-workflows/", {
        "order": order["id"], "services": [service["id"]],
    }, format="json").json()


class TestPreflight:
    def test_preflight_ok(self, admin_client, ready_order):
        order, service, _ = ready_order
        workflow = make_workflow(admin_client, order, service)
        result = admin_client.post(
            f"/api/v1/booking-workflows/{workflow['id']}/preflight/").json()
        assert result["ok"] is True, result
        assert result["blocking_errors"] == []

    def test_preflight_blocks_missing_data(self, admin_client, tenant, admin_user):
        person = Person.objects.create(tenant=tenant, surname="Без", given_name="Латиницы",
                                       created_by=admin_user)
        order = admin_client.post("/api/v1/orders/", {
            "request_type": "individual", "client_person": str(person.id),
            "participants": [{"person": str(person.id)}],
        }, format="json").json()
        service = admin_client.post(f"/api/v1/orders/{order['id']}/services/", {
            "kind": "avia", "title": "Тест", "currency": "USD", "client_total": "100.00",
        }, format="json").json()
        workflow = make_workflow(admin_client, order, service)
        result = admin_client.post(
            f"/api/v1/booking-workflows/{workflow['id']}/preflight/").json()
        assert result["ok"] is False
        codes = {e["code"] for e in result["blocking_errors"]}
        assert "PASSENGER_DATA_MISSING" in codes
        assert "DOCUMENT_MISSING" in codes

    def test_start_requires_preflight(self, admin_client, ready_order):
        order, service, _ = ready_order
        workflow = make_workflow(admin_client, order, service)
        response = admin_client.post(
            f"/api/v1/booking-workflows/{workflow['id']}/start/", {},
            format="json", HTTP_IDEMPOTENCY_KEY="start-0")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PREFLIGHT_REQUIRED"


class TestBookingSaga:
    def _booked_workflow(self, client, ready_order) -> dict:
        order, service, _ = ready_order
        workflow = make_workflow(client, order, service)
        client.post(f"/api/v1/booking-workflows/{workflow['id']}/preflight/")
        response = client.post(f"/api/v1/booking-workflows/{workflow['id']}/start/", {},
                               format="json", HTTP_IDEMPOTENCY_KEY=f"start-{workflow['id']}")
        assert response.status_code == 202, response.content
        run_jobs_once()
        return client.get(
            f"/api/v1/booking-workflows/{workflow['id']}/status/").json()

    def test_booking_success(self, admin_client, ready_order):
        workflow = self._booked_workflow(admin_client, ready_order)
        assert workflow["status"] == "completed"
        item = workflow["items"][0]
        assert item["status"] == "booked"
        assert item["locator"]
        # услуга получила PNR и статус booked
        _, service, _ = ready_order
        body = admin_client.get(
            f"/api/v1/orders/{workflow['order']}/services/").json()["results"][0]
        assert body["status"] == "booked"
        assert body["external_id"] == item["locator"]

    def test_issue_success(self, admin_client, ready_order):
        workflow = self._booked_workflow(admin_client, ready_order)
        response = admin_client.post(
            f"/api/v1/booking-workflows/{workflow['id']}/issue/", {},
            format="json", HTTP_IDEMPOTENCY_KEY=f"issue-{workflow['id']}")
        assert response.status_code == 202
        run_jobs_once()
        body = admin_client.get(
            f"/api/v1/booking-workflows/{workflow['id']}/status/").json()
        assert body["items"][0]["status"] == "issued"
        from avia.models import Ticket

        assert Ticket.objects.count() == 1

    def test_issue_unknown_blocks_retry(self, admin_client, ready_order):
        workflow = self._booked_workflow(admin_client, ready_order)
        # timeout при выписке -> unknown
        response = admin_client.post(
            f"/api/v1/booking-workflows/{workflow['id']}/issue/",
            {"_mock": {"fail": "timeout"}},
            format="json", HTTP_IDEMPOTENCY_KEY=f"issue-t-{workflow['id']}")
        assert response.status_code == 202
        run_jobs_once()
        body = admin_client.get(
            f"/api/v1/booking-workflows/{workflow['id']}/status/").json()
        assert body["items"][0]["status"] == "unknown"
        # повторная выписка заблокирована
        response = admin_client.post(
            f"/api/v1/booking-workflows/{workflow['id']}/issue/", {},
            format="json", HTTP_IDEMPOTENCY_KEY=f"issue-r-{workflow['id']}")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ISSUE_BLOCKED_UNKNOWN"
        # создан инцидент
        from integrations.models import IntegrationIncident

        assert IntegrationIncident.objects.filter(error_code="ISSUE_UNKNOWN").exists()
        # status inquiry разблокирует
        item_id = body["items"][0]["id"]
        response = admin_client.post(
            f"/api/v1/booking-workflows/{workflow['id']}/status-inquiry/",
            {"item": item_id}, format="json")
        assert response.status_code == 202
        run_jobs_once()
        body = admin_client.get(
            f"/api/v1/booking-workflows/{workflow['id']}/status/").json()
        assert body["items"][0]["status"] == "booked"  # provider вернул booked, дубля нет
        from avia.models import Ticket

        assert Ticket.objects.count() == 0  # дубль билета не создан

    def test_compensating_cancellation(self, admin_client, ready_order):
        workflow = self._booked_workflow(admin_client, ready_order)
        response = admin_client.post(
            f"/api/v1/booking-workflows/{workflow['id']}/cancel/",
            {"reason": "клиент передумал"},
            format="json", HTTP_IDEMPOTENCY_KEY=f"cancel-{workflow['id']}")
        assert response.status_code == 202
        run_jobs_once()
        body = admin_client.get(
            f"/api/v1/booking-workflows/{workflow['id']}/status/").json()
        assert body["status"] == "cancelled"
        assert body["items"][0]["status"] == "compensated"
