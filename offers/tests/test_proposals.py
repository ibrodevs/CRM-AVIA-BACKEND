import pytest

from conftest import auth_client

pytestmark = pytest.mark.django_db


@pytest.fixture
def order(admin_client, tenant, admin_user):
    from crm.models import Person

    person = Person.objects.create(tenant=tenant, surname="Клиент", given_name="КП", created_by=admin_user)
    return admin_client.post(
        "/api/v1/orders/",
        {
            "request_type": "individual",
            "client_person": str(person.id),
        },
        format="json",
    ).json()


PROPOSAL = {
    "type": "travel",
    "purpose": "Командировка",
    "currency": "USD",
    "variants": [
        {
            "name": "Эконом",
            "items": [
                {
                    "title": "Перелёт FRU-IST",
                    "quantity": 1,
                    "price_amount": "450.00",
                    "price_currency": "USD",
                },
            ],
        },
        {
            "name": "Бизнес",
            "items": [
                {
                    "title": "Перелёт FRU-IST бизнес",
                    "quantity": 1,
                    "price_amount": "1200.00",
                    "price_currency": "USD",
                },
            ],
        },
    ],
}


def create_proposal(client, order) -> dict:
    response = client.post("/api/v1/proposals/", {**PROPOSAL, "order": order["id"]}, format="json")
    assert response.status_code == 201, response.content
    return response.json()


class TestProposalLifecycle:
    def test_create_with_variants(self, admin_client, order):
        proposal = create_proposal(admin_client, order)
        assert proposal["number"].startswith("KP-")
        assert len(proposal["variants"]) == 2
        assert proposal["status"] == "draft"

    def test_full_flow_to_approval(self, admin_client, order):
        proposal = create_proposal(admin_client, order)
        pid = proposal["id"]

        response = admin_client.post(
            f"/api/v1/proposals/{pid}/prepare/", {"version": proposal["version"]}, format="json"
        )
        assert response.status_code == 200
        proposal = response.json()
        assert proposal["status"] == "prepared"
        assert proposal["current_version"] == 1

        response = admin_client.post(
            f"/api/v1/proposals/{pid}/send/",
            {"version": proposal["version"]},
            format="json",
            HTTP_IDEMPOTENCY_KEY="send-1",
        )
        proposal = response.json()
        assert proposal["status"] == "sent"

        variant_id = proposal["variants"][0]["id"]
        response = admin_client.post(
            f"/api/v1/proposals/{pid}/approve/",
            {"variant": variant_id, "version": proposal["version"]},
            format="json",
            HTTP_IDEMPOTENCY_KEY="approve-1",
        )
        assert response.status_code == 200, response.content
        proposal = response.json()
        assert proposal["status"] == "approved"
        assert proposal["approved_variant"] == variant_id
        statuses = {v["id"]: v["status"] for v in proposal["variants"]}
        assert statuses[variant_id] == "approved"
        assert list(statuses.values()).count("rejected") == 1

    def test_forbidden_transition(self, admin_client, order):
        proposal = create_proposal(admin_client, order)
        response = admin_client.post(
            f"/api/v1/proposals/{proposal['id']}/approve/",
            {"variant": proposal["variants"][0]["id"], "version": proposal["version"]},
            format="json",
            HTTP_IDEMPOTENCY_KEY="a-2",
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PROPOSAL_STATUS_TRANSITION_FORBIDDEN"

    def test_versions_immutable_snapshot(self, admin_client, order):
        proposal = create_proposal(admin_client, order)
        admin_client.post(
            f"/api/v1/proposals/{proposal['id']}/prepare/", {"version": proposal["version"]}, format="json"
        )
        versions = admin_client.get(f"/api/v1/proposals/{proposal['id']}/versions/").json()
        assert len(versions) == 1
        assert versions[0]["snapshot"]["variants"][0]["items"][0]["price_amount"] == "450.00"

    def test_pdf(self, admin_client, order):
        proposal = create_proposal(admin_client, order)
        admin_client.post(
            f"/api/v1/proposals/{proposal['id']}/prepare/", {"version": proposal["version"]}, format="json"
        )
        response = admin_client.get(f"/api/v1/proposals/{proposal['id']}/pdf/")
        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert bytes(response.content[:5]) == b"%PDF-"

    def test_operator_cannot_approve(self, operator_user, admin_client, order):
        proposal = create_proposal(admin_client, order)
        operator = auth_client(operator_user)
        response = operator.post(
            f"/api/v1/proposals/{proposal['id']}/approve/",
            {"variant": proposal["variants"][0]["id"], "version": proposal["version"]},
            format="json",
            HTTP_IDEMPOTENCY_KEY="a-3",
        )
        assert response.status_code == 403


class TestServiceCards:
    def _card(self, client, order) -> dict:
        response = client.post(
            "/api/v1/service-cards/",
            {
                "order": order["id"],
                "kind": "avia",
                "content": {"title": "FRU-IST", "airline": "TK"},
                "price_snapshot": {"amount": "450.00", "currency": "USD"},
            },
            format="json",
        )
        assert response.status_code == 201, response.content
        return response.json()

    def test_send_and_public_view(self, admin_client, order):
        from rest_framework.test import APIClient

        card = self._card(admin_client, order)
        response = admin_client.post(
            f"/api/v1/service-cards/{card['id']}/send/",
            {"channels": ["telegram"]},
            format="json",
            HTTP_IDEMPOTENCY_KEY="card-send-1",
        )
        assert response.status_code == 200
        assert response.json()["status"] == "sent"

        public = APIClient()
        response = public.get(f"/api/v1/public/service-cards/{card['public_token']}/")
        assert response.status_code == 200
        assert response.json()["status"] == "viewed"

    def test_public_respond_idempotent(self, admin_client, order):
        from rest_framework.test import APIClient

        card = self._card(admin_client, order)
        admin_client.post(
            f"/api/v1/service-cards/{card['id']}/send/",
            {"channels": ["telegram"]},
            format="json",
            HTTP_IDEMPOTENCY_KEY="card-send-2",
        )
        public = APIClient()
        response = public.post(
            f"/api/v1/public/service-cards/{card['public_token']}/respond/",
            {"action": "choose"},
            format="json",
        )
        assert response.status_code == 200
        assert response.json()["status"] == "chosen"

        response = public.post(
            f"/api/v1/public/service-cards/{card['public_token']}/respond/",
            {"action": "decline"},
            format="json",
        )
        assert response.status_code == 200
        assert response.json()["action"] == "choose"
