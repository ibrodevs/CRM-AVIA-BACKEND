import json
import zlib

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from documents.models import Document

pytestmark = pytest.mark.django_db


@pytest.fixture
def person(tenant, admin_user):
    from crm.models import Person

    return Person.objects.create(tenant=tenant, surname="Док", given_name="Клиент", created_by=admin_user)


@pytest.fixture
def order(admin_client, person):
    return admin_client.post(
        "/api/v1/orders/",
        {"request_type": "individual", "client_person": str(person.id)},
        format="json",
    ).json()


def upload_file(name="doc.txt", content=b"hello", content_type="text/plain"):
    return SimpleUploadedFile(name, content, content_type=content_type)


class TestDocuments:
    def test_multipart_upload_creates_version(self, admin_client, order):
        response = admin_client.post(
            "/api/v1/documents/",
            {
                "file": upload_file(),
                "document": json.dumps(
                    {
                        "order": order["id"],
                        "kind": "other",
                        "title": "Документ заказа",
                        "source": "upload",
                    }
                ),
            },
            format="multipart",
        )
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["current_version"] == 1
        document = Document.objects.get(pk=body["id"])
        assert document.versions.count() == 1

    def test_new_version_does_not_overwrite_previous(self, admin_client, order):
        created = admin_client.post(
            "/api/v1/documents/",
            {
                "file": upload_file("v1.txt", b"version 1"),
                "document": json.dumps(
                    {"order": order["id"], "kind": "other", "title": "Версионируемый документ"}
                ),
            },
            format="multipart",
        ).json()
        response = admin_client.post(
            f"/api/v1/documents/{created['id']}/versions/",
            {"file": upload_file("v2.txt", b"version 2"), "reason": "исправление"},
            format="multipart",
        )
        assert response.status_code == 201, response.content
        assert response.json()["version"] == 2
        versions = admin_client.get(f"/api/v1/documents/{created['id']}/versions/").json()
        assert [row["version"] for row in versions] == [2, 1]

    def test_service_must_belong_to_document_order(self, admin_client, order, tenant, admin_user):
        from crm.models import Person

        other_person = Person.objects.create(
            tenant=tenant, surname="Другой", given_name="Док", created_by=admin_user
        )
        other_order = admin_client.post(
            "/api/v1/orders/",
            {"request_type": "individual", "client_person": str(other_person.id)},
            format="json",
        ).json()
        service = admin_client.post(
            f"/api/v1/orders/{other_order['id']}/services/",
            {"kind": "hotel", "title": "Чужая услуга", "currency": "USD", "client_total": "100.00"},
            format="json",
        ).json()
        response = admin_client.post(
            "/api/v1/documents/",
            {
                "file": upload_file(),
                "document": json.dumps(
                    {
                        "order": order["id"],
                        "service": service["id"],
                        "kind": "voucher",
                        "title": "Чужой ваучер",
                    }
                ),
            },
            format="multipart",
        )
        assert response.status_code == 400
        assert response.json()["error"]["fields"]["service"]

    def test_receipt_import_extracts_text_fields(self, admin_client):
        receipt = upload_file(
            "receipt.txt",
            (
                "Passenger: TELEGIN IVAN KONSTANTINOVICH\n"
                "Carrier: Smartavia\n"
                "PNR: V942WP\n"
                "Ticket No: 316 2445197354\n"
                "Currency: RUB\n"
                "Fare: 25328\n"
                "Taxes: 120\n"
                "Total: 25448\n"
            ).encode(),
        )
        response = admin_client.post("/api/v1/receipt-imports/", {"file": receipt}, format="multipart")
        assert response.status_code == 201, response.content

        result = admin_client.get(f"/api/v1/receipt-imports/{response.json()['id']}/result/")
        assert result.status_code == 200, result.content
        body = result.json()
        assert body["parser_status"] == "parsed"
        assert body["draft"]["passenger_name"] == "TELEGIN IVAN KONSTANTINOVICH"
        assert body["draft"]["fare"] == "25328.00"
        assert body["draft"]["taxes"] == "120.00"
        assert body["draft"]["total"] == "25448.00"
        assert body["draft"]["currency"] == "RUB"

        from documents.models import ReceiptImportJob

        import_job = ReceiptImportJob.objects.get(pk=response.json()["id"])
        source_document = import_job.file_version.document
        assert source_document.source == "supplier"
        assert source_document.current_version == 1
        assert import_job.file_version.version == 1
        assert import_job.file_version.original_name == "receipt.txt"

        confirm = admin_client.post(
            f"/api/v1/receipt-imports/{response.json()['id']}/confirm/",
            {
                "issuer": body["draft"]["issuer"],
                "passenger_name": body["draft"]["passenger_name"],
                "segments": [],
                "fare": "25328",
                "taxes": "120",
                "fees": "500",
                "currency": "RUB",
            },
            format="json",
        )
        assert confirm.status_code == 200, confirm.content
        source_document.refresh_from_db()
        assert str(source_document.id) == confirm.json()["document_id"]
        assert source_document.source == "corrected"
        assert source_document.current_version == 2
        assert source_document.versions.order_by("version").first().original_name == "receipt.txt"
        assert source_document.versions.order_by("version").last().origin == "generated"
        assert source_document.metadata["receipt_import"]["corrected_fields"]["fees"] == "500"

    def test_receipt_import_without_text_requires_manual_review(self, admin_client):
        response = admin_client.post(
            "/api/v1/receipt-imports/",
            {"file": upload_file("blank.txt", b"\x00\x01\x02")},
            format="multipart",
        )
        assert response.status_code == 201, response.content

        result = admin_client.get(f"/api/v1/receipt-imports/{response.json()['id']}/result/")
        assert result.status_code == 200, result.content
        body = result.json()
        assert body["parser_status"] == "manual_review"
        assert body["draft"]["passenger_name"] == ""
        assert body["draft"]["fare"] is None

    def test_receipt_import_extracts_russian_fields(self, admin_client):
        receipt = upload_file(
            "russian_receipt.txt",
            (
                "Пассажир: ИВАНОВ ИВАН ИВАНОВИЧ\n"
                "Перевозчик: Smartavia\n"
                "Код бронирования: V942WP\n"
                "Номер билета: 316 2445197354\n"
                "Документ: 2213067219\n"
                "Дата рождения: 18.05.1993\n"
                "Валюта: RUB\n"
                "Тариф: 25 328.00\n"
                "Таксы и сборы: 120.00\n"
                "Итого: 25 448.00\n"
            ).encode(),
        )
        response = admin_client.post("/api/v1/receipt-imports/", {"file": receipt}, format="multipart")
        assert response.status_code == 201, response.content

        result = admin_client.get(f"/api/v1/receipt-imports/{response.json()['id']}/result/")
        body = result.json()
        assert body["parser_status"] == "parsed"
        assert body["draft"]["passenger_name"] == "ИВАНОВ ИВАН ИВАНОВИЧ"
        assert body["draft"]["fare"] == "25328.00"
        assert body["draft"]["taxes"] == "120.00"
        assert body["draft"]["total"] == "25448.00"
        assert body["draft"]["currency"] == "RUB"
        assert body["extracted"]["reference"] == "V942WP"
        assert body["extracted"]["ticket_number"] == "316 2445197354"
        assert body["extracted"]["document_number"] == "2213067219"
        assert body["extracted"]["date_of_birth"] == "18.05.1993"

    def test_receipt_import_extracts_pdf_stream_text(self, admin_client):
        text = (
            "Passenger: PETROV PETR\\n"
            "Carrier: S7 Airlines\\n"
            "PNR: KJ7T2L\\n"
            "Ticket No: 421 2135356261\\n"
            "Currency: RUB\\n"
            "Fare: 14200\\n"
            "Taxes: 3160\\n"
            "Total: 17360"
        )
        stream = "BT /F1 12 Tf 72 720 Td (" + text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") Tj ET"
        compressed = zlib.compress(stream.encode("latin-1"))
        pdf = (
            b"%PDF-1.4\n1 0 obj<<>>endobj\n2 0 obj<< /Length "
            + str(len(compressed)).encode()
            + b" /Filter /FlateDecode >>stream\n"
            + compressed
            + b"\nendstream\nendobj\n%%EOF"
        )
        response = admin_client.post(
            "/api/v1/receipt-imports/",
            {"file": upload_file("receipt.pdf", pdf, "application/pdf")},
            format="multipart",
        )
        assert response.status_code == 201, response.content

        result = admin_client.get(f"/api/v1/receipt-imports/{response.json()['id']}/result/")
        body = result.json()
        assert body["parser_status"] == "parsed"
        assert body["draft"]["passenger_name"] == "PETROV PETR"
        assert body["draft"]["issuer"] == "S7 Airlines"
        assert body["draft"]["fare"] == "14200.00"
        assert body["draft"]["taxes"] == "3160.00"
        assert body["draft"]["total"] == "17360.00"
        assert body["extracted"]["reference"] == "KJ7T2L"
        assert body["extracted"]["ticket_number"] == "421 2135356261"
