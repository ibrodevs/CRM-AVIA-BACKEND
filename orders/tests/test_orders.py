import pytest

from conftest import auth_client
from crm.models import Person
from orders.models import OrderStatusHistory

pytestmark = pytest.mark.django_db


@pytest.fixture
def person(tenant, admin_user):
    return Person.objects.create(
        tenant=tenant, surname="Иванов", given_name="Пётр", phone="+996700000001", created_by=admin_user
    )


@pytest.fixture
def order_payload(person):
    return {
        "request_type": "individual",
        "client_person": str(person.id),
        "purpose": "Командировка",
        "route": {
            "kind": "one_way",
            "points": [
                {"location_code": "FRU", "location_type": "airport"},
                {"location_code": "IST", "location_type": "airport"},
            ],
        },
        "participants": [{"person": str(person.id), "role": "passenger", "is_contact": True}],
    }


class TestOrderCreate:
    def test_create_full(self, admin_client, order_payload):
        response = admin_client.post("/api/v1/orders/", order_payload, format="json")
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["number"].startswith("ORD-")
        assert body["status"] == "new"
        assert len(body["route"]["points"]) == 2
        assert len(body["participants"]) == 1

    def test_number_sequential_and_unique(self, admin_client, order_payload):
        numbers = [
            admin_client.post("/api/v1/orders/", order_payload, format="json").json()["number"]
            for _ in range(3)
        ]
        assert len(set(numbers)) == 3
        values = [int(n.split("-")[1]) for n in numbers]
        assert values == sorted(values)

    def test_client_xor_required(self, admin_client, order_payload):
        payload = dict(order_payload)
        del payload["client_person"]
        response = admin_client.post("/api/v1/orders/", payload, format="json")
        assert response.status_code == 400

    def test_route_min_points(self, admin_client, order_payload):
        payload = dict(order_payload)
        payload["route"] = {"kind": "one_way", "points": [{"location_code": "FRU"}]}
        assert admin_client.post("/api/v1/orders/", payload, format="json").status_code == 400

    def test_history_written(self, admin_client, order_payload):
        order_id = admin_client.post("/api/v1/orders/", order_payload, format="json").json()["id"]
        assert OrderStatusHistory.objects.filter(order_id=order_id).count() == 1


class TestOrderStatusMachine:
    def _create(self, client, payload) -> dict:
        return client.post("/api/v1/orders/", payload, format="json").json()

    def _transition(self, client, order, target, **extra):
        return client.post(
            f"/api/v1/orders/{order['id']}/transition/",
            {"target_status": target, "version": order["version"], **extra},
            format="json",
        )

    def test_valid_chain(self, admin_client, order_payload):
        order = self._create(admin_client, order_payload)
        for target in ["in_progress", "awaiting_confirmation", "awaiting_payment", "paid", "completed"]:
            response = self._transition(admin_client, order, target)
            assert response.status_code == 200, f"{target}: {response.content}"
            order = response.json()
            assert order["status"] == target

    def test_forbidden_transition(self, admin_client, order_payload):
        order = self._create(admin_client, order_payload)
        response = self._transition(admin_client, order, "paid")
        assert response.status_code == 409
        body = response.json()["error"]
        assert body["code"] == "ORDER_STATUS_TRANSITION_FORBIDDEN"
        assert "allowed" in body["details"]

    def test_version_conflict(self, admin_client, order_payload):
        order = self._create(admin_client, order_payload)
        response = admin_client.post(
            f"/api/v1/orders/{order['id']}/transition/",
            {"target_status": "in_progress", "version": 99},
            format="json",
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "VERSION_CONFLICT"
        assert response.json()["error"]["details"]["current_version"] == 1

    def test_manual_paid_requires_finance_permission(self, operator_user, tenant, person):
        client = auth_client(operator_user)
        order = self._create(
            client,
            {
                "client_person": str(person.id),
                "request_type": "individual",
            },
        )
        for target in ["in_progress", "awaiting_confirmation", "awaiting_payment"]:
            order = self._transition(client, order, target).json()
        response = self._transition(client, order, "paid")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "MANUAL_PAID_FORBIDDEN"

    def test_terminal_rollback_admin_only(self, admin_client, operator_user, order_payload, person):
        order = self._create(admin_client, order_payload)
        for target in ["in_progress", "cancelled"]:
            order = self._transition(admin_client, order, target, reason="test").json()

        operator = auth_client(operator_user)
        response = operator.post(
            f"/api/v1/orders/{order['id']}/transition/",
            {"target_status": "in_progress", "version": order["version"], "reason": "x"},
            format="json",
        )
        assert response.status_code in (403, 404, 409)

        response = self._transition(admin_client, order, "in_progress")
        assert response.status_code == 400
        response = self._transition(admin_client, order, "in_progress", reason="возобновление")
        assert response.status_code == 200

    def test_status_not_patchable(self, admin_client, order_payload):
        order = self._create(admin_client, order_payload)
        response = admin_client.patch(
            f"/api/v1/orders/{order['id']}/", {"status": "paid", "version": order["version"]}, format="json"
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "FIELD_NOT_PATCHABLE"


class TestOrderScope:
    def test_operator_sees_only_assigned(self, admin_client, operator_user, tenant, order_payload):

        admin_client.post("/api/v1/orders/", order_payload, format="json")
        operator = auth_client(operator_user)
        assert operator.get("/api/v1/orders/").json()["count"] == 0

    def test_reassign_makes_visible(self, admin_client, operator_user, order_payload):
        order = admin_client.post("/api/v1/orders/", order_payload, format="json").json()
        response = admin_client.post(
            f"/api/v1/orders/{order['id']}/reassign/",
            {"operator": str(operator_user.id), "version": order["version"], "reason": "передача"},
            format="json",
        )
        assert response.status_code == 200
        operator = auth_client(operator_user)
        assert operator.get("/api/v1/orders/").json()["count"] == 1


class TestOrderMisc:
    def test_overview(self, admin_client, order_payload):
        order = admin_client.post("/api/v1/orders/", order_payload, format="json").json()
        body = admin_client.get(f"/api/v1/orders/{order['id']}/overview/").json()
        assert set(body) == {
            "order",
            "allowed_actions",
            "services",
            "finance_summary",
            "deadlines",
            "warnings",
        }
        assert "in_progress" in body["allowed_actions"]["transitions"]

    def test_duplicate(self, admin_client, order_payload):
        order = admin_client.post("/api/v1/orders/", order_payload, format="json").json()
        response = admin_client.post(f"/api/v1/orders/{order['id']}/duplicate/")
        assert response.status_code == 201
        copy = response.json()
        assert copy["number"] != order["number"]
        assert len(copy["participants"]) == 1
        assert copy["status"] == "new"

    def test_filters(self, admin_client, order_payload):
        admin_client.post("/api/v1/orders/", order_payload, format="json")
        assert admin_client.get("/api/v1/orders/?status=new").json()["count"] == 1
        assert admin_client.get("/api/v1/orders/?status=paid").json()["count"] == 0
        assert admin_client.get("/api/v1/orders/?q=Командировка").json()["count"] == 1
