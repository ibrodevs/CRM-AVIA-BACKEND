from decimal import Decimal

import pytest
from django.core.management import call_command

from services.models import PriceSnapshot, SearchSession, ServiceOffer
from suppliers.models import Supplier, SupplierMarkupRule

pytestmark = pytest.mark.django_db


def run_jobs_once():
    call_command("run_jobs", "--once", "--worker-id", "test")


@pytest.fixture
def supplier(tenant, admin_user):
    return Supplier.objects.create(
        tenant=tenant,
        name="Mock GDS",
        status="active",
        service_kinds=["avia", "hotel"],
        created_by=admin_user,
    )


CRITERIA = {
    "origin": "FRU",
    "destination": "IST",
    "date": "2026-08-01",
    "cabin": "economy",
    "currency": "USD",
}


class TestSearchFlow:
    def _search(self, client, criteria=None, kind="avia") -> dict:
        response = client.post(
            "/api/v1/service-searches/", {"kind": kind, "criteria": criteria or CRITERIA}, format="json"
        )
        assert response.status_code == 202, response.content
        return response.json()

    def test_search_completes_with_offers(self, admin_client, supplier):
        body = self._search(admin_client)
        run_jobs_once()
        session = admin_client.get(f"/api/v1/service-searches/{body['search_id']}/").json()
        assert session["status"] == "completed"
        assert session["provider_runs"][0]["offers_count"] == 3
        offers = admin_client.get(f"/api/v1/service-searches/{body['search_id']}/offers/").json()
        assert offers["count"] == 3
        first = offers["results"][0]
        assert first["price"]["currency"] == "USD"
        assert first["itinerary"]["segments"][0]["origin"] == "FRU"

        prices = [Decimal(o["price"]["amount"]) for o in offers["results"]]
        assert prices == sorted(prices)

    def test_provider_failure_marks_partial(self, admin_client, supplier):
        body = self._search(admin_client, {**CRITERIA, "_mock": {"fail": "timeout"}})
        run_jobs_once()
        session = admin_client.get(f"/api/v1/service-searches/{body['search_id']}/").json()
        assert session["status"] == "failed"
        assert session["provider_runs"][0]["status"] == "timeout"

    def test_markup_applied_with_snapshot(self, admin_client, supplier, admin_user, tenant):
        SupplierMarkupRule.objects.create(
            tenant=tenant,
            supplier=supplier,
            service_kind="avia",
            amount_type="percent",
            amount_value=Decimal("10"),
            priority=1,
            created_by=admin_user,
        )
        body = self._search(admin_client)
        run_jobs_once()
        offer = ServiceOffer.objects.filter(session_id=body["search_id"]).first()
        assert offer.applied_markup_rules, "наценка должна быть применена"
        snapshot = PriceSnapshot.objects.filter(offer=offer).first()
        assert snapshot is not None
        assert snapshot.rounding == "ROUND_HALF_UP"
        assert "markup" in snapshot.formula

    def test_search_events_emitted(self, admin_client, supplier, admin_user):
        self._search(admin_client)
        run_jobs_once()
        events = admin_client.get("/api/v1/events/?cursor=0").json()["events"]
        types = [e["type"] for e in events]
        assert "search.progress" in types
        assert "search.completed" in types

    def test_cancel_search(self, admin_client, supplier):
        body = self._search(admin_client)
        response = admin_client.post(f"/api/v1/service-searches/{body['search_id']}/cancel/")
        assert response.json()["status"] == "cancelled"
        run_jobs_once()
        session = SearchSession.objects.get(pk=body["search_id"])
        assert session.status == "cancelled"
        assert session.offers.count() == 0


class TestOfferOperations:
    def _offer(self, client, supplier) -> dict:
        body = client.post(
            "/api/v1/service-searches/", {"kind": "avia", "criteria": CRITERIA}, format="json"
        ).json()
        run_jobs_once()
        return client.get(f"/api/v1/service-searches/{body['search_id']}/offers/").json()["results"][0]

    def test_revalidate(self, admin_client, supplier):
        offer = self._offer(admin_client, supplier)
        response = admin_client.post(f"/api/v1/service-offers/{offer['id']}/revalidate/")
        assert response.status_code == 200
        assert response.json()["revalidation"]["status"] == "valid"

    def test_fare_rules(self, admin_client, supplier):
        offer = self._offer(admin_client, supplier)
        response = admin_client.get(f"/api/v1/service-offers/{offer['id']}/fare-rules/")
        assert "refund" in response.json()["fare_rules"]

    def test_manual_offer(self, admin_client, supplier):
        response = admin_client.post(
            "/api/v1/service-offers/manual/",
            {
                "kind": "transfer",
                "itinerary": {"description": "Трансфер аэропорт-отель"},
                "price": {"amount": "50.00", "currency": "USD"},
                "supplier": str(supplier.id),
            },
            format="json",
        )
        assert response.status_code == 201
        assert response.json()["is_manual"] is True


class TestAttachToOrder:
    def test_attach_offer_creates_service(self, admin_client, supplier, tenant, admin_user):
        from crm.models import Person

        person = Person.objects.create(
            tenant=tenant, surname="Тест", given_name="Тест", created_by=admin_user
        )
        order = admin_client.post(
            "/api/v1/orders/",
            {
                "request_type": "individual",
                "client_person": str(person.id),
            },
            format="json",
        ).json()
        search = admin_client.post(
            "/api/v1/service-searches/",
            {"kind": "avia", "criteria": CRITERIA, "order": order["id"]},
            format="json",
        ).json()
        run_jobs_once()
        offer = admin_client.get(f"/api/v1/service-searches/{search['search_id']}/offers/").json()["results"][
            0
        ]
        response = admin_client.post(
            f"/api/v1/orders/{order['id']}/services/", {"offer_id": offer["id"]}, format="json"
        )
        assert response.status_code == 201, response.content
        service = response.json()
        assert service["kind"] == "avia"
        assert service["status"] == "proposed"
        assert service["client_price"]["amount"] == offer["price"]["amount"]
        registry = admin_client.get("/api/v1/services/?kind=avia")
        assert registry.status_code == 200
        row = registry.json()["results"][0]
        assert row["order_number"] == order["number"]
        assert row["supplier_name"]

    def test_manual_service_binds_order_participants(self, admin_client, tenant, admin_user):
        from crm.models import Person

        person = Person.objects.create(
            tenant=tenant, surname="Пассажир", given_name="Один", created_by=admin_user
        )
        order = admin_client.post(
            "/api/v1/orders/",
            {
                "request_type": "individual",
                "client_person": str(person.id),
                "participants": [{"person": str(person.id), "role": "passenger"}],
            },
            format="json",
        ).json()
        participant_id = order["participants"][0]["id"]
        response = admin_client.post(
            f"/api/v1/orders/{order['id']}/services/",
            {
                "kind": "transfer",
                "title": "Трансфер аэропорт-отель",
                "currency": "USD",
                "client_total": "50.00",
                "participants": [participant_id],
            },
            format="json",
        )
        assert response.status_code == 201, response.content
        service = response.json()
        assert service["passengers_count"] == 1
        assert service["passengers"][0]["participant"] == participant_id

        response = admin_client.put(
            f"/api/v1/services/{service['id']}/passengers/",
            {"version": service["version"], "participants": []},
            format="json",
        )
        assert response.status_code == 200, response.content
        assert response.json()["passengers_count"] == 0

    def test_manual_book_and_issue(self, admin_client, tenant, admin_user):
        from crm.models import Person

        person = Person.objects.create(
            tenant=tenant, surname="Ручной", given_name="Пассажир", created_by=admin_user
        )
        order = admin_client.post(
            "/api/v1/orders/",
            {"request_type": "individual", "client_person": str(person.id)},
            format="json",
        ).json()
        service = admin_client.post(
            f"/api/v1/orders/{order['id']}/services/",
            {
                "kind": "hotel",
                "title": "Hilton Bishkek",
                "currency": "USD",
                "supplier_cost": "100.00",
                "agency_fee": "10.00",
                "client_total": "110.00",
            },
            format="json",
        ).json()

        response = admin_client.post(
            f"/api/v1/services/{service['id']}/manual-book/",
            {
                "version": service["version"],
                "supplier_reference": "HTL-123",
                "booking_number": "BN-123",
                "payment_deadline": "2026-08-01T18:00:00Z",
                "comment": "ручная бронь",
            },
            format="json",
        )
        assert response.status_code == 200, response.content
        booked = response.json()
        assert booked["status"] == "booked"
        assert booked["external_id"] == "HTL-123"

        response = admin_client.post(
            f"/api/v1/services/{service['id']}/manual-issue/",
            {
                "version": booked["version"],
                "voucher_number": "VCH-456",
                "amount": "110.00",
                "currency": "USD",
                "comment": "ваучер получен",
            },
            format="json",
        )
        assert response.status_code == 200, response.content
        issued = response.json()
        assert issued["status"] == "issued"
        assert issued["version"] == booked["version"] + 1

    def test_service_transition_machine(self, admin_client, supplier, tenant, admin_user):
        from crm.models import Person

        person = Person.objects.create(
            tenant=tenant, surname="Тест2", given_name="Тест", created_by=admin_user
        )
        order = admin_client.post(
            "/api/v1/orders/",
            {
                "request_type": "individual",
                "client_person": str(person.id),
            },
            format="json",
        ).json()
        service = admin_client.post(
            f"/api/v1/orders/{order['id']}/services/",
            {
                "kind": "transfer",
                "title": "Трансфер",
                "currency": "USD",
                "client_total": "50.00",
            },
            format="json",
        ).json()

        response = admin_client.post(
            f"/api/v1/services/{service['id']}/transition/",
            {"target_status": "booked", "version": 1},
            format="json",
        )
        assert response.status_code == 200, response.content

        response = admin_client.post(
            f"/api/v1/services/{service['id']}/transition/",
            {"target_status": "refunded", "version": 2},
            format="json",
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SERVICE_STATUS_TRANSITION_FORBIDDEN"
