"""Сервисный сбор бланка определяется договором контрагента, а не фронтендом."""

import pytest

from crm.models import Agreement, Company, Contract, FeeRule

pytestmark = pytest.mark.django_db

URL = "/api/v1/service-fee/resolve/"


@pytest.fixture
def company(admin_client):
    response = admin_client.post(
        "/api/v1/companies/",
        {"legal_name": "ОсОО Договорной клиент", "short_name": "Договорной", "tax_id": "250820260001"},
        format="json",
    )
    assert response.status_code == 201, response.content
    return response.json()


def conditions(*, fees, currency="RUB", contract_status="Действующий", agreement_status="Действующий",
               contract_date="06.08.2026", agreement_date="06.08.2026"):
    return {
        "settlement": "предоплата",
        "currency": currency,
        "contracts": [
            {
                "no": "№ 123",
                "date": contract_date,
                "status": contract_status,
                "agreements": [
                    {
                        "no": "ДС № 1",
                        "date": agreement_date,
                        "version": 1,
                        "status": agreement_status,
                        "template": "standard",
                        "fees": fees,
                    }
                ],
            }
        ],
    }


def save_conditions(client, company, **kwargs):
    response = client.put(
        f"/api/v1/companies/{company['id']}/financial-conditions/",
        conditions(**kwargs),
        format="json",
    )
    assert response.status_code == 200, response.content
    return response.json()


def resolve(client, **body):
    body.setdefault("date", "2026-08-25")
    response = client.post(URL, body, format="json")
    assert response.status_code == 200, response.content
    return response.json()


class TestContractServiceFee:
    def test_fixed_rail_fee_comes_from_the_active_contract(self, admin_client, company):
        save_conditions(admin_client, company, fees={"ЖД": {"service": {"type": "fixed", "value": 500}}})

        body = resolve(
            admin_client,
            company=company["id"],
            service_kind="ЖД",
            base_amount="4154.10",
            currency="RUB",
        )

        assert body["source"] == "contract"
        assert body["fee"] == "500.00"
        assert body["currency"] == "RUB"
        assert body["calculation"] == "fixed"
        assert body["contract_number"] == "№ 123"
        assert body["agreement_number"] == "ДС № 1"
        assert body["rule_id"]
        assert body["agreement_id"]
        assert body["contract_id"]

    def test_percent_fee_is_calculated_from_the_supplier_base(self, admin_client, company):
        save_conditions(admin_client, company, fees={"Авиа": {"service": {"type": "percent", "value": 5}}})

        body = resolve(
            admin_client,
            company=company["id"],
            service_kind="Авиа",
            base_amount="12000.50",
            currency="RUB",
        )

        assert body["source"] == "contract"
        assert body["calculation"] == "percent"
        # 5% от 12 000,50 = 600,025 → ROUND_HALF_UP до копеек.
        assert body["fee"] == "600.03"
        assert body["currency"] == "RUB"

    def test_zero_contract_fee_is_a_real_condition_not_a_fallback(self, admin_client, company):
        save_conditions(admin_client, company, fees={"ЖД": {"service": {"type": "fixed", "value": 0}}})

        body = resolve(admin_client, company=company["id"], service_kind="ЖД", base_amount="4154.10", currency="RUB")

        assert body["source"] == "contract"
        assert body["fee"] == "0.00"

    def test_service_kind_without_rule_falls_back_to_manual(self, admin_client, company):
        save_conditions(admin_client, company, fees={"Авиа": {"service": {"type": "fixed", "value": 500}}})

        body = resolve(admin_client, company=company["id"], service_kind="ЖД", base_amount="4154.10", currency="RUB")

        assert body["source"] == "manual"
        assert body["fee"] is None
        assert body["reason"] == "no_applicable_rule"

    def test_markup_only_agreement_does_not_provide_a_service_fee(self, admin_client, company):
        save_conditions(admin_client, company, fees={"ЖД": {"markup": {"type": "fixed", "value": 300}}})

        body = resolve(admin_client, company=company["id"], service_kind="ЖД", base_amount="4154.10", currency="RUB")

        assert body["source"] == "manual"
        assert body["reason"] == "no_applicable_rule"

    def test_company_without_conditions_is_manual(self, admin_client, company):
        body = resolve(admin_client, company=company["id"], service_kind="ЖД", base_amount="4154.10", currency="RUB")

        assert body["source"] == "manual"
        assert body["reason"] == "no_active_contract"

    def test_person_and_new_client_are_manual(self, admin_client):
        body = resolve(admin_client, service_kind="ЖД", base_amount="4154.10", currency="RUB")

        assert body["source"] == "manual"
        assert body["reason"] == "no_company"
        assert body["fee"] is None

    def test_expired_agreement_is_ignored(self, admin_client, company):
        save_conditions(
            admin_client,
            company,
            fees={"ЖД": {"service": {"type": "fixed", "value": 500}}},
            agreement_date="01.01.2020",
        )
        Agreement.objects.filter(contract__company_id=company["id"]).update(effective_to="2020-12-31")

        body = resolve(admin_client, company=company["id"], service_kind="ЖД", base_amount="4154.10", currency="RUB")

        assert body["source"] == "manual"
        assert body["reason"] == "no_applicable_rule"

    def test_archived_contract_is_ignored(self, admin_client, company):
        save_conditions(admin_client, company, fees={"ЖД": {"service": {"type": "fixed", "value": 500}}})
        Contract.objects.filter(company_id=company["id"]).update(status="archived")

        body = resolve(admin_client, company=company["id"], service_kind="ЖД", base_amount="4154.10", currency="RUB")

        assert body["source"] == "manual"
        assert body["reason"] == "no_active_contract"

    def test_latest_agreement_version_wins(self, admin_client, company):
        saved = save_conditions(admin_client, company, fees={"ЖД": {"service": {"type": "fixed", "value": 500}}})
        value = saved["value"]
        first = value["contracts"][0]["agreements"][0]
        value["contracts"][0]["agreements"].append(
            {
                **first,
                "id": None,
                "no": "ДС № 2",
                "version": 2,
                "fees": {"ЖД": {"service": {"type": "fixed", "value": 750}}},
            }
        )
        response = admin_client.put(
            f"/api/v1/companies/{company['id']}/financial-conditions/", value, format="json"
        )
        assert response.status_code == 200, response.content

        body = resolve(admin_client, company=company["id"], service_kind="ЖД", base_amount="4154.10", currency="RUB")

        assert body["fee"] == "750.00"
        assert body["agreement_number"] == "ДС № 2"

    def test_fixed_rule_in_another_currency_stays_manual_with_context(self, admin_client, company):
        save_conditions(
            admin_client, company, fees={"ЖД": {"service": {"type": "fixed", "value": 20}}}, currency="USD"
        )

        body = resolve(admin_client, company=company["id"], service_kind="ЖД", base_amount="4154.10", currency="RUB")

        assert body["source"] == "manual"
        assert body["reason"] == "currency_mismatch"
        assert body["contract_fee"] == "20.00"
        assert body["contract_currency"] == "USD"

    def test_batch_resolves_every_blank_with_its_own_base(self, admin_client, company):
        save_conditions(
            admin_client,
            company,
            fees={
                "ЖД": {"service": {"type": "fixed", "value": 500}},
                "Авиа": {"service": {"type": "percent", "value": 10}},
            },
        )

        body = resolve(
            admin_client,
            company=company["id"],
            items=[
                {"key": "rail-1", "service_kind": "ЖД", "base_amount": "4154.10", "currency": "RUB"},
                {"key": "rail-2", "service_kind": "ЖД", "base_amount": "5589.50", "currency": "RUB"},
                {"key": "avia-1", "service_kind": "Авиа", "base_amount": "10000", "currency": "RUB"},
                {"key": "hotel-1", "service_kind": "Гостиница", "base_amount": "8000", "currency": "RUB"},
            ],
        )

        results = {row["key"]: row for row in body["results"]}
        assert results["rail-1"]["fee"] == "500.00"
        assert results["rail-2"]["fee"] == "500.00"
        assert results["avia-1"]["fee"] == "1000.00"
        assert results["hotel-1"]["source"] == "manual"
        assert results["hotel-1"]["reason"] == "no_applicable_rule"

    def test_order_of_a_company_resolves_the_contract_fee(self, admin_client, company):
        save_conditions(admin_client, company, fees={"ЖД": {"service": {"type": "fixed", "value": 500}}})
        order = admin_client.post(
            "/api/v1/orders/",
            {"client_company": company["id"], "request_type": "corporate"},
            format="json",
        )
        assert order.status_code == 201, order.content

        body = resolve(
            admin_client,
            order=order.json()["id"],
            service_kind="ЖД",
            base_amount="4154.10",
            currency="RUB",
        )

        assert body["source"] == "contract"
        assert body["fee"] == "500.00"
        assert body["context"] == "order"

    def test_other_tenant_company_is_not_visible(self, admin_client, company, other_tenant):
        foreign = Company.objects.create(
            tenant=other_tenant, legal_name="Чужая компания", short_name="Чужая", tax_id="999"
        )

        response = admin_client.post(
            URL,
            {"company": str(foreign.id), "service_kind": "ЖД", "base_amount": "1000", "currency": "RUB"},
            format="json",
        )

        assert response.status_code == 404

    def test_operator_can_resolve_the_fee(self, operator_client, admin_client, company):
        save_conditions(admin_client, company, fees={"ЖД": {"service": {"type": "fixed", "value": 500}}})

        body = resolve(operator_client, company=company["id"], service_kind="ЖД", base_amount="4154.10", currency="RUB")

        assert body["source"] == "contract"
        assert body["fee"] == "500.00"

    def test_rule_is_stored_as_a_single_fee_rule_row(self, admin_client, company):
        save_conditions(admin_client, company, fees={"ЖД": {"service": {"type": "fixed", "value": 500}}})

        assert FeeRule.objects.filter(
            agreement__contract__company_id=company["id"], service_kind="rail", fee_kind="service"
        ).count() == 1
