import pytest

from conftest import auth_client
from crm.models import Agreement, Contract, FeeRule, SettlementProfile

pytestmark = pytest.mark.django_db


@pytest.fixture
def company(admin_client):
    response = admin_client.post(
        "/api/v1/companies/",
        {"legal_name": "ОсОО Финансовый клиент", "short_name": "Финклиент", "tax_id": "040820260001"},
        format="json",
    )
    assert response.status_code == 201, response.content
    return response.json()


def payload(*, settlement="депозит", balance=15000, reserved=2000):
    return {
        "settlement": settlement,
        "currency": "USD",
        "deposit": {"balance": balance, "reserved": reserved},
        "credit": None,
        "contracts": [
            {
                "no": "№ 2026-001",
                "date": "06.08.2026",
                "status": "Действующий",
                "agreements": [
                    {
                        "no": "ДС № 1",
                        "date": "06.08.2026",
                        "version": 1,
                        "status": "Действующий",
                        "template": "standard",
                        "fees": {
                            "Авиа": {
                                "service": {"type": "percent", "value": 5},
                                "issue": {"type": "fixed", "value": 10},
                            },
                            "ЖД": {
                                "service": {"type": "fixed", "value": 7},
                            },
                        },
                        "descs": {"Авиа": "Организация воздушной перевозки"},
                        "feeDescs": {
                            "Авиа": {
                                "service": "Сервисный сбор агентства",
                                "issue": "Сбор за оформление",
                            }
                        },
                    }
                ],
            }
        ],
    }


class TestCompanyFinancialConditions:
    def test_empty_company_is_not_configured(self, admin_client, company):
        response = admin_client.get(f"/api/v1/companies/{company['id']}/financial-conditions/")

        assert response.status_code == 200, response.content
        assert response.json() == {"configured": False, "value": None}

    def test_put_creates_real_settlement_contract_agreement_and_fee_rules(self, admin_client, company):
        response = admin_client.put(
            f"/api/v1/companies/{company['id']}/financial-conditions/",
            payload(),
            format="json",
        )

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["configured"] is True
        assert body["value"]["settlement"] == "депозит"
        assert body["value"]["deposit"]["balance"] == 15000
        assert body["value"]["deposit"]["reserved"] == 2000
        assert body["value"]["contracts"][0]["no"] == "№ 2026-001"
        assert body["value"]["contracts"][0]["date"] == "06.08.2026"
        assert body["value"]["contracts"][0]["agreements"][0]["fees"]["Авиа"]["service"]["value"] == 5

        settlement = SettlementProfile.objects.get(company_id=company["id"])
        assert settlement.mode == SettlementProfile.Mode.DEPOSIT
        assert settlement.deposit_balance == 15000
        assert settlement.deposit_reserved == 2000
        assert Contract.objects.filter(company_id=company["id"], number="№ 2026-001").count() == 1
        assert Agreement.objects.filter(contract__company_id=company["id"], agreement_version=1).count() == 1
        assert FeeRule.objects.filter(agreement__contract__company_id=company["id"]).count() == 3

    def test_put_updates_existing_rows_instead_of_duplicating(self, admin_client, company):
        url = f"/api/v1/companies/{company['id']}/financial-conditions/"
        first = admin_client.put(url, payload(), format="json")
        assert first.status_code == 200, first.content
        saved = first.json()["value"]
        saved["deposit"]["balance"] = 22000
        saved["contracts"][0]["agreements"][0]["fees"]["Авиа"]["service"]["value"] = 6

        second = admin_client.put(url, saved, format="json")

        assert second.status_code == 200, second.content
        assert second.json()["value"]["deposit"]["balance"] == 22000
        assert Contract.objects.filter(company_id=company["id"]).count() == 1
        assert Agreement.objects.filter(contract__company_id=company["id"]).count() == 1
        assert FeeRule.objects.get(
            agreement__contract__company_id=company["id"],
            service_kind="avia",
            fee_kind="service",
        ).value == 6

    def test_reserve_cannot_exceed_deposit(self, admin_client, company):
        response = admin_client.put(
            f"/api/v1/companies/{company['id']}/financial-conditions/",
            payload(balance=100, reserved=101),
            format="json",
        )

        assert response.status_code == 400
        assert "deposit.reserved" in response.json()["error"]["fields"]
        assert not SettlementProfile.objects.filter(company_id=company["id"]).exists()

    def test_operator_without_finance_permission_cannot_save(self, operator_user, admin_client, company):
        operator = auth_client(operator_user)
        response = operator.put(
            f"/api/v1/companies/{company['id']}/financial-conditions/",
            payload(),
            format="json",
        )

        assert response.status_code == 403
        assert not SettlementProfile.objects.filter(company_id=company["id"]).exists()
