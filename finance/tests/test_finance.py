from decimal import Decimal

import pytest

from conftest import auth_client
from crm.models import Person
from finance.models import FinancialObligation, LedgerEntry

pytestmark = pytest.mark.django_db


@pytest.fixture
def order(admin_client, tenant, admin_user):
    person = Person.objects.create(
        tenant=tenant, surname="Плательщик", given_name="Тест", created_by=admin_user
    )
    return admin_client.post(
        "/api/v1/orders/",
        {
            "request_type": "individual",
            "client_person": str(person.id),
        },
        format="json",
    ).json()


@pytest.fixture
def obligation(admin_client, order, accountant_user):
    accountant = auth_client(accountant_user)
    return accountant.post(
        "/api/v1/finance/obligations/",
        {
            "order": order["id"],
            "direction": "client_receivable",
            "currency": "USD",
            "original_amount": "500.00",
        },
        format="json",
    ).json()


def create_payment(client, order, amount="500.00", key="pay-1") -> dict:
    response = client.post(
        "/api/v1/finance/payments/",
        {
            "direction": "incoming",
            "order": order["id"],
            "amount": amount,
            "currency": "USD",
            "method": "bank_transfer",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY=key,
    )
    assert response.status_code == 201, response.content
    return response.json()


class TestPayments:
    def test_confirm_with_allocation_marks_order_paid(self, admin_client, accountant_user, order, obligation):
        accountant = auth_client(accountant_user)

        current = order
        for target in ["in_progress", "awaiting_confirmation", "awaiting_payment"]:
            current = admin_client.post(
                f"/api/v1/orders/{order['id']}/transition/",
                {"target_status": target, "version": current["version"]},
                format="json",
            ).json()
        payment = create_payment(accountant, order)
        response = accountant.post(
            f"/api/v1/finance/payments/{payment['id']}/confirm/",
            {
                "version": payment["version"],
                "allocations": [{"obligation": obligation["id"], "amount": "500.00"}],
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="confirm-1",
        )
        assert response.status_code == 200, response.content
        body = response.json()
        assert body["status"] == "confirmed"

        ob = FinancialObligation.objects.get(pk=obligation["id"])
        assert ob.status == "settled"

        assert admin_client.get(f"/api/v1/orders/{order['id']}/").json()["status"] == "paid"

        entries = LedgerEntry.objects.all()
        debit = sum(e.amount for e in entries if e.direction == "debit")
        credit = sum(e.amount for e in entries if e.direction == "credit")
        assert debit == credit == Decimal("500.00")

    def test_double_confirm_idempotent(self, accountant_user, order):
        accountant = auth_client(accountant_user)
        payment = create_payment(accountant, order, key="pay-2")
        first = accountant.post(
            f"/api/v1/finance/payments/{payment['id']}/confirm/",
            {"version": payment["version"]},
            format="json",
            HTTP_IDEMPOTENCY_KEY="confirm-2",
        )
        second = accountant.post(
            f"/api/v1/finance/payments/{payment['id']}/confirm/",
            {"version": payment["version"]},
            format="json",
            HTTP_IDEMPOTENCY_KEY="confirm-2",
        )
        assert first.status_code == second.status_code == 200
        assert LedgerEntry.objects.count() == 2

    def test_allocation_cannot_exceed_payment(self, accountant_user, order, obligation):
        accountant = auth_client(accountant_user)
        payment = create_payment(accountant, order, amount="100.00", key="pay-3")
        accountant.post(
            f"/api/v1/finance/payments/{payment['id']}/confirm/",
            {"version": payment["version"]},
            format="json",
            HTTP_IDEMPOTENCY_KEY="confirm-3",
        )
        response = accountant.post(
            f"/api/v1/finance/payments/{payment['id']}/allocate/",
            {"allocations": [{"obligation": obligation["id"], "amount": "200.00"}]},
            format="json",
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "ALLOCATION_EXCEEDS_PAYMENT"

    def test_manager_cannot_create_payment(self, manager_user, order):
        manager = auth_client(manager_user)
        response = manager.post(
            "/api/v1/finance/payments/",
            {
                "direction": "incoming",
                "amount": "10.00",
                "currency": "USD",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="pay-m",
        )
        assert response.status_code == 403


class TestRefunds:
    def test_refund_formula(self, accountant_user, order):
        accountant = auth_client(accountant_user)
        payment = create_payment(accountant, order, key="pay-r")
        accountant.post(
            f"/api/v1/finance/payments/{payment['id']}/confirm/",
            {"version": payment["version"]},
            format="json",
            HTTP_IDEMPOTENCY_KEY="confirm-r",
        )
        response = accountant.post(
            "/api/v1/finance/refunds/",
            {
                "payment": payment["id"],
                "currency": "USD",
                "original_paid": "500.00",
                "supplier_penalty": "125.00",
                "agency_service_fee": "25.00",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="refund-1",
        )
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["refund_amount"] == "350.00"
        assert body["formula_snapshot"]["rounding"] == "ROUND_HALF_UP"

    def test_refund_cannot_exceed_paid(self, accountant_user, order):
        accountant = auth_client(accountant_user)
        payment = create_payment(accountant, order, amount="100.00", key="pay-r2")
        accountant.post(
            f"/api/v1/finance/payments/{payment['id']}/confirm/",
            {"version": payment["version"]},
            format="json",
            HTTP_IDEMPOTENCY_KEY="confirm-r2",
        )
        response = accountant.post(
            "/api/v1/finance/refunds/",
            {
                "payment": payment["id"],
                "currency": "USD",
                "original_paid": "500.00",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="refund-2",
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "REFUND_EXCEEDS_PAID"


class TestAfterSales:
    def _case(self, client, order, service_id=None) -> dict:
        response = client.post(
            "/api/v1/after-sales/",
            {
                "order": order["id"],
                "type": "refund",
                "currency": "USD",
                **({"service": service_id} if service_id else {}),
            },
            format="json",
        )
        assert response.status_code == 201, response.content
        return response.json()

    def test_full_refund_case_flow(self, admin_client, order):
        case = self._case(admin_client, order)
        assert case["number"].startswith("AS-")
        cid = case["id"]

        admin_client.post(
            f"/api/v1/after-sales/{cid}/transition/", {"target_status": "review"}, format="json"
        )

        quote = admin_client.post(
            f"/api/v1/after-sales/{cid}/quote/",
            {
                "currency": "USD",
                "original_paid": "400.00",
                "supplier_penalty": "100.00",
            },
            format="json",
        ).json()
        assert quote["refund_total"] == "300.00"

        admin_client.post(f"/api/v1/after-sales/{cid}/send-for-approval/", {}, format="json")
        response = admin_client.post(
            f"/api/v1/after-sales/{cid}/client-approve/", {"quote_version": 1}, format="json"
        )
        assert response.status_code == 200

        admin_client.post(
            f"/api/v1/after-sales/{cid}/submit-to-supplier/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY=f"submit-{cid}",
        )
        response = admin_client.post(
            f"/api/v1/after-sales/{cid}/execute/", {}, format="json", HTTP_IDEMPOTENCY_KEY=f"exec-{cid}"
        )
        assert response.status_code == 200, response.content
        assert response.json()["status"] == "completed"

        history = admin_client.get(f"/api/v1/after-sales/{cid}/history/").json()
        actions = [h["action"] for h in history]
        assert "created" in actions and "quote_created" in actions and "client_approved" in actions

    def test_new_quote_invalidates_approval(self, admin_client, order):
        case = self._case(admin_client, order)
        cid = case["id"]
        admin_client.post(
            f"/api/v1/after-sales/{cid}/transition/", {"target_status": "review"}, format="json"
        )
        admin_client.post(
            f"/api/v1/after-sales/{cid}/quote/", {"currency": "USD", "original_paid": "400.00"}, format="json"
        )
        admin_client.post(f"/api/v1/after-sales/{cid}/send-for-approval/", {}, format="json")
        admin_client.post(f"/api/v1/after-sales/{cid}/client-approve/", {"quote_version": 1}, format="json")

        admin_client.post(
            f"/api/v1/after-sales/{cid}/quote/",
            {"currency": "USD", "original_paid": "400.00", "supplier_penalty": "200.00"},
            format="json",
        )
        response = admin_client.post(
            f"/api/v1/after-sales/{cid}/submit-to-supplier/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY=f"submit2-{cid}",
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "CLIENT_APPROVAL_REQUIRED"

    def test_execute_requires_processing(self, admin_client, order):
        case = self._case(admin_client, order)
        response = admin_client.post(
            f"/api/v1/after-sales/{case['id']}/execute/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY=f"exec-early-{case['id']}",
        )
        assert response.status_code == 409
