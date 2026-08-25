"""Источник сервисного сбора сохраняется вместе с подтверждённой квитанцией."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from documents.models import Document
from services.models import OrderService

pytestmark = pytest.mark.django_db

RAIL_RECEIPT = (
    "ЭЛЕКТРОННЫЙ БИЛЕТ РЖД\n"
    "Пассажир: НЕМЧИНОВ ИВАН\n"
    "Поезд: 060\n"
    "Вагон: 08 Место: 013\n"
    "Курган - Омск-Пассажирский\n"
    "Currency: RUB\n"
    "Стоимость билета: 3167.30\n"
    "Стоимость плацкарты: 986.80\n"
    "Total: 4154.10\n"
)


@pytest.fixture
def company(admin_client):
    response = admin_client.post(
        "/api/v1/companies/",
        {"legal_name": "ОсОО Сборный клиент", "short_name": "Сборный", "tax_id": "250820260002"},
        format="json",
    )
    assert response.status_code == 201, response.content
    return response.json()


@pytest.fixture
def rail_contract(admin_client, company):
    response = admin_client.put(
        f"/api/v1/companies/{company['id']}/financial-conditions/",
        {
            "settlement": "предоплата",
            "currency": "RUB",
            "contracts": [
                {
                    "no": "№ 123",
                    "date": "06.08.2026",
                    "status": "Действующий",
                    "agreements": [
                        {
                            "no": "ДС № 1",
                            "date": "06.08.2026",
                            "version": 1,
                            "status": "Действующий",
                            "template": "standard",
                            "fees": {"ЖД": {"service": {"type": "fixed", "value": 500}}},
                        }
                    ],
                }
            ],
        },
        format="json",
    )
    assert response.status_code == 200, response.content
    return response.json()


def import_rail_receipt(client):
    upload = SimpleUploadedFile("rail.txt", RAIL_RECEIPT.encode(), content_type="text/plain")
    response = client.post("/api/v1/receipt-imports/", {"file": upload}, format="multipart")
    assert response.status_code == 201, response.content
    return response.json()["id"]


def confirm(client, import_id, **extra):
    payload = {
        "issuer": "РЖД",
        "passenger_name": "НЕМЧИНОВ ИВАН",
        "segments": [],
        "fare": "4154.10",
        "taxes": "0",
        "fees": "500",
        "currency": "RUB",
        "client_total": "4654.10",
        "markup": "0",
        "commission": "0",
        "service_type": "ЖД",
        **extra,
    }
    response = client.post(f"/api/v1/receipt-imports/{import_id}/confirm/", payload, format="json")
    assert response.status_code == 200, response.content
    return response.json()


class TestReceiptServiceFeeSource:
    def test_contract_fee_is_verified_on_the_server_and_stored(self, admin_client, company, rail_contract):
        import_id = import_rail_receipt(admin_client)

        body = confirm(
            admin_client,
            import_id,
            company=company["id"],
            service_fee={"amount": "500", "currency": "RUB", "source": "contract", "service_kind": "ЖД"},
        )

        document = Document.objects.get(pk=body["document_id"])
        fee = document.metadata["receipt_import"]["service_fee"]
        assert fee["source"] == "contract"
        assert fee["amount"] == "500"
        assert fee["calculation"] == "fixed"
        assert fee["value"] == "500.0000"
        assert fee["contract_number"] == "№ 123"
        assert fee["agreement_number"] == "ДС № 1"
        assert fee["verified"] is True
        assert fee["rule_id"] and fee["contract_id"] and fee["agreement_id"]

    def test_contract_claim_without_an_active_rule_falls_back_to_manual(self, admin_client, company):
        import_id = import_rail_receipt(admin_client)

        body = confirm(
            admin_client,
            import_id,
            company=company["id"],
            service_fee={"amount": "500", "currency": "RUB", "source": "contract", "service_kind": "ЖД"},
        )

        fee = Document.objects.get(pk=body["document_id"]).metadata["receipt_import"]["service_fee"]
        assert fee["source"] == "manual"
        assert fee["reason"] == "rule_not_found"
        assert fee["amount"] == "500"

    def test_manual_fee_keeps_its_reason(self, admin_client):
        import_id = import_rail_receipt(admin_client)

        body = confirm(
            admin_client,
            import_id,
            service_fee={
                "amount": "700",
                "currency": "RUB",
                "source": "manual",
                "reason": "no_company",
                "service_kind": "ЖД",
            },
        )

        fee = Document.objects.get(pk=body["document_id"]).metadata["receipt_import"]["service_fee"]
        assert fee["source"] == "manual"
        assert fee["reason"] == "no_company"
        assert fee["amount"] == "700"

    def test_order_of_the_company_verifies_the_contract_fee(self, admin_client, company, rail_contract):
        order = admin_client.post(
            "/api/v1/orders/",
            {"client_company": company["id"], "request_type": "corporate"},
            format="json",
        )
        assert order.status_code == 201, order.content
        import_id = import_rail_receipt(admin_client)

        body = confirm(
            admin_client,
            import_id,
            order=order.json()["id"],
            create_services=True,
            service_fee={"amount": "500", "currency": "RUB", "source": "contract", "service_kind": "ЖД"},
        )

        document = Document.objects.get(pk=body["document_id"])
        assert document.metadata["receipt_import"]["service_fee"]["source"] == "contract"

        service = OrderService.objects.get(pk=document.service_id)
        assert str(service.supplier_cost) == "4154.10"
        assert str(service.agency_fee) == "500.00"
        assert str(service.client_total) == "4654.10"
        assert str(service.markup) == "0.00"
        assert str(service.commission) == "0.00"

    def test_financial_data_survives_a_page_reload(self, admin_client, company, rail_contract):
        import_id = import_rail_receipt(admin_client)
        body = confirm(
            admin_client,
            import_id,
            company=company["id"],
            service_fee={"amount": "500", "currency": "RUB", "source": "contract", "service_kind": "ЖД"},
        )

        reloaded = admin_client.get("/api/v1/documents/")
        assert reloaded.status_code == 200, reloaded.content
        rows = {row["id"]: row for row in reloaded.json()["results"]}
        document = rows[body["document_id"]]
        receipt_import = document["metadata"]["receipt_import"]
        assert receipt_import["service_fee"]["source"] == "contract"
        assert receipt_import["service_fee"]["contract_number"] == "№ 123"
        assert receipt_import["client_total"] == "4654.10"
        assert document["metadata"]["supplier_original"]["verified_data"]["fees"] == "500"
        assert document["amount"] == "4654.10"

    def test_group_pdf_stores_the_blank_count_behind_the_total_fee(self, admin_client, company, rail_contract):
        import_id = import_rail_receipt(admin_client)

        body = confirm(
            admin_client,
            import_id,
            company=company["id"],
            fees="2000",
            service_fee={
                "amount": "2000",
                "currency": "RUB",
                "source": "contract",
                "service_kind": "ЖД",
                "blanks": 4,
            },
        )

        fee = Document.objects.get(pk=body["document_id"]).metadata["receipt_import"]["service_fee"]
        assert fee["source"] == "contract"
        assert fee["amount"] == "2000"
        assert fee["blanks"] == 4
        assert fee["value"] == "500.0000", "в метаданных видно правило договора на один бланк"
