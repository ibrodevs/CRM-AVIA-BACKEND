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
        assert body["verified_data"]["passenger"] == "TELEGIN IVAN KONSTANTINOVICH"
        assert body["verified_data"]["carrier"] == "Smartavia"
        assert body["verified_data"]["recognitionPending"] is False

        from documents.models import ReceiptImportJob

        import_job = ReceiptImportJob.objects.get(pk=response.json()["id"])
        source_document = import_job.file_version.document
        assert source_document.source == "supplier"
        assert source_document.current_version == 1
        assert import_job.file_version.version == 1
        assert import_job.file_version.original_name == "receipt.txt"
        assert source_document.metadata["receipt_import"]["parser_status"] == "parsed"
        assert source_document.metadata["supplier_original"]["verified_data"]["passenger"] == (
            "TELEGIN IVAN KONSTANTINOVICH"
        )

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
        assert source_document.metadata["receipt_import"]["verified_data"]["recognitionPending"] is False
        assert source_document.metadata["supplier_original"]["verified_data"]["fees"] == "500"

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

    def test_reprocess_receipts_restores_failed_unconfirmed_import(self, admin_client):
        from django.core.management import call_command

        from documents.models import ReceiptImportJob

        receipt = upload_file(
            "transfer.txt",
            (
                "Service: Transfer\n"
                "Passenger: SOROKINA OLGA\n"
                "Route: Airport - Hotel\n"
                "Departure date: 22.05.2025\n"
                "Currency: RUB\n"
                "Total: 4200\n"
            ).encode(),
        )
        response = admin_client.post("/api/v1/receipt-imports/", {"file": receipt}, format="multipart")
        assert response.status_code == 201, response.content
        job = ReceiptImportJob.objects.get(pk=response.json()["id"])
        job.parser_status = "manual_review"
        job.raw_extraction = {}
        job.save(update_fields=["parser_status", "raw_extraction"])
        job.draft.passenger_name = ""
        job.draft.total = None
        job.draft.save(update_fields=["passenger_name", "total"])

        call_command("reprocess_receipts")

        job.refresh_from_db()
        job.draft.refresh_from_db()
        job.file_version.document.refresh_from_db()
        assert job.parser_status == "parsed"
        assert job.guessed_type == "transfer"
        assert job.draft.passenger_name == "SOROKINA OLGA"
        assert job.draft.total == job.draft.total.__class__("4200.00")
        assert job.file_version.document.metadata["receipt_import"]["service_kind"] == "transfer"

    def test_receipt_import_pdf_garbage_is_not_used_as_text(self, admin_client):
        response = admin_client.post(
            "/api/v1/receipt-imports/",
            {
                "file": upload_file(
                    "receipt.pdf",
                    b"%PDF-1.4\n/dStyles <</Para [18 0 R 19 0 R 20 0 R]>> /ShowGrid false\n%%EOF",
                    "application/pdf",
                )
            },
            format="multipart",
        )
        assert response.status_code == 201, response.content

        result = admin_client.get(f"/api/v1/receipt-imports/{response.json()['id']}/result/")
        assert result.status_code == 200, result.content
        body = result.json()
        assert body["parser_status"] == "manual_review"
        assert body["draft"]["passenger_name"] == ""

    def test_receipt_import_confirm_creates_corrected_version_and_order_service(self, admin_client, order):
        from documents.models import ReceiptImportJob
        from services.models import OrderService

        receipt = upload_file(
            "service_receipt.txt",
            (
                "Passenger: IVANOV IVAN\n"
                "Carrier: Test Air\n"
                "PNR: ABC123\n"
                "Ticket No: 5551234567890\n"
                "Currency: USD\n"
                "Fare: 250.00\n"
                "Taxes: 42.50\n"
                "Total: 292.50\n"
            ).encode(),
        )
        response = admin_client.post("/api/v1/receipt-imports/", {"file": receipt}, format="multipart")
        assert response.status_code == 201, response.content
        import_job = ReceiptImportJob.objects.get(pk=response.json()["id"])
        original_document = import_job.file_version.document
        assert original_document.source == "supplier"
        assert original_document.current_version == 1

        confirm = admin_client.post(
            f"/api/v1/receipt-imports/{response.json()['id']}/confirm/",
            {
                "issuer": "Test Air",
                "passenger_name": "IVANOV IVAN EDITED",
                "segments": [],
                "fare": "250.00",
                "taxes": "42.50",
                "fees": "15.00",
                "currency": "USD",
                "order": order["id"],
                "create_services": True,
                "service_type": "Авиа",
                "client_total": "320.00",
                "markup": "55.00",
                "commission": "7.00",
            },
            format="json",
        )
        assert confirm.status_code == 200, confirm.content

        original_document.refresh_from_db()
        assert str(original_document.id) == confirm.json()["document_id"]
        assert str(original_document.order_id) == order["id"]
        assert original_document.source == "corrected"
        assert original_document.current_version == 2
        versions = list(original_document.versions.order_by("version"))
        assert versions[0].version == 1
        assert versions[0].origin == "uploaded"
        assert versions[0].original_name == "service_receipt.txt"
        assert versions[1].version == 2
        assert versions[1].origin == "generated"
        assert versions[1].correction_reason

        service = OrderService.objects.get(pk=original_document.service_id)
        assert str(service.order_id) == order["id"]
        assert service.kind == "avia"
        assert service.source == OrderService.Source.IMPORT
        assert service.supplier_cost == service.supplier_cost.__class__("292.50")
        assert service.agency_fee == service.agency_fee.__class__("15.00")
        assert service.markup == service.markup.__class__("55.00")
        assert service.commission == service.commission.__class__("7.00")
        assert service.client_total == service.client_total.__class__("320.00")
        assert original_document.metadata["receipt_import"]["created_service"] == str(service.id)

    def test_receipt_editor_update_persists_binding_finances_and_output_settings(
        self,
        admin_client,
        order,
    ):
        from documents.models import ReceiptImportJob

        receipt = upload_file(
            "rail_receipt.txt",
            (
                "Пассажир: СУЛЕЙМАНОВ РЕНАТ РАШИДОВИЧ\n"
                "Услуга: rail\n"
                "Маршрут: МОСКВА → НИЖНИЙ НОВГОРОД\n"
                "Дата отправления: 27.10.2025\n"
                "Поезд: 721АА\n"
                "Номер заказа: 77506905747822\n"
                "Валюта: RUB\n"
                "Итого: 2877.70\n"
            ).encode(),
        )
        imported = admin_client.post(
            "/api/v1/receipt-imports/",
            {"file": receipt},
            format="multipart",
        )
        assert imported.status_code == 201, imported.content
        job = ReceiptImportJob.objects.get(pk=imported.json()["id"])
        document = job.file_version.document

        response = admin_client.post(
            f"/api/v1/documents/{document.id}/receipt/",
            {
                "order": order["id"],
                "verified_data": {
                    "service_kind": "rail",
                    "service_type": "ЖД",
                    "passenger": "СУЛЕЙМАНОВ РЕНАТ РАШИДОВИЧ",
                    "supplierOrderNo": "77506905747822",
                    "crmOrderNo": order["number"],
                    "ticketCost": "1500.00",
                    "reservedSeatCost": "1377.70",
                    "agencyServiceFee": "100.00",
                    "additionalFees": "0",
                    "fare": "2877.70",
                    "fees": "100.00",
                    "total": "2977.70",
                    "currency": "RUB",
                    "legs": [
                        {
                            "from": "МОСКВА",
                            "to": "НИЖНИЙ НОВГОРОД",
                            "date": "27.10.2025",
                            "flightNo": "721АА",
                        }
                    ],
                },
                "output_settings": {
                    "mode": "agency",
                    "template": "Основной фирменный",
                    "priceMode": "total",
                },
                "audit_log": [{"label": "Стоимость плацкарты", "after": "1377.70"}],
            },
            format="json",
        )

        assert response.status_code == 200, response.content
        document.refresh_from_db()
        assert str(document.order_id) == order["id"]
        assert document.amount == document.amount.__class__("2977.70")
        verified = document.metadata["supplier_original"]["verified_data"]
        assert verified["supplierOrderNo"] == "77506905747822"
        assert verified["ticketCost"] == "1500.00"
        assert verified["reservedSeatCost"] == "1377.70"
        assert document.metadata["supplier_original"]["output_settings"]["mode"] == "agency"
        assert document.metadata["supplier_original"]["audit_log"][0]["label"] == (
            "Стоимость плацкарты"
        )
        assert document.metadata["receipt_import"]["stage"] == "confirmed"

    def test_receipt_editor_close_saves_unconfirmed_draft(self, admin_client):
        from documents.models import ReceiptImportJob

        receipt = upload_file(
            "hotel_voucher.txt",
            (
                "Услуга: hotel\n"
                "Гость: НАГОРНЫЙ КОНСТАНТИН\n"
                "Отель: Лесная Сафмар\n"
                "Итого: 15000 RUB\n"
            ).encode(),
        )
        imported = admin_client.post(
            "/api/v1/receipt-imports/",
            {"file": receipt},
            format="multipart",
        )
        assert imported.status_code == 201, imported.content
        job = ReceiptImportJob.objects.get(pk=imported.json()["id"])
        document = job.file_version.document

        response = admin_client.post(
            f"/api/v1/documents/{document.id}/receipt/",
            {
                "draft": True,
                "verified_data": {
                    "service_kind": "hotel",
                    "service_type": "Гостиница",
                    "passenger": "НАГОРНЫЙ КОНСТАНТИН",
                    "hotel": {
                        "name": "Лесная Сафмар",
                        "address": "Москва, ул. Лесная, д. 15",
                    },
                    "total": "15000",
                    "currency": "RUB",
                },
            },
            format="json",
        )

        assert response.status_code == 200, response.content
        document.refresh_from_db()
        verified = document.metadata["supplier_original"]["verified_data"]
        assert verified["hotel"]["address"] == "Москва, ул. Лесная, д. 15"
        assert verified["recognitionPending"] is True
        assert document.metadata["receipt_import"]["stage"] == "draft"

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

    def test_receipt_import_maps_route_and_service_fields(self, admin_client):
        receipt = upload_file(
            "detailed_receipt.txt",
            (
                "Service: Air ticket\n"
                "Passenger: IVANOV IVAN\n"
                "Carrier: Test Air\n"
                "PNR: ABC123\n"
                "Ticket No: 5551234567890\n"
                "Document number: ID9876543\n"
                "Date of birth: 15.04.1990\n"
                "Issued date: 21.07.2026\n"
                "Route: FRU - IST\n"
                "Departure date: 12.08.2026\n"
                "Flight: TK347\n"
                "Departure: 09:30\n"
                "Arrival: 12:20\n"
                "Class: Y\n"
                "Fare basis: YOWKG\n"
                "Baggage: 1PC\n"
                "Hand baggage: 8KG\n"
                "Currency: USD\n"
                "Fare: 250.00\n"
                "Taxes: 42.50\n"
                "Total: 292.50\n"
            ).encode(),
        )
        response = admin_client.post("/api/v1/receipt-imports/", {"file": receipt}, format="multipart")
        assert response.status_code == 201, response.content

        result = admin_client.get(f"/api/v1/receipt-imports/{response.json()['id']}/result/")
        body = result.json()
        segment = body["draft"]["segments"][0]
        assert body["extracted"]["service_kind"] == "avia"
        assert body["extracted"]["issue_date"] == "21.07.2026"
        assert body["extracted"]["booking_class"] == "Y"
        assert body["extracted"]["fare_basis"] == "YOWKG"
        assert body["extracted"]["baggage"] == "1PC"
        assert body["extracted"]["hand_baggage"] == "8KG"
        assert segment["from"] == "FRU"
        assert segment["to"] == "IST"
        assert segment["date"] == "12.08.2026"
        assert segment["flightNo"] == "TK347"
        assert segment["dep"] == "09:30"
        assert segment["arr"] == "12:20"

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

    def test_receipt_import_extracts_utf16_hex_pdf_airline_receipt(self, admin_client):
        lines = [
            "Электронный билет (маршрут/квитанция для пассажира)",
            "ДАТА :",
            "29ЯНВ26",
            "ФАМИЛИЯ :",
            "VLASOV/IGOR ALEKSANDROVICH",
            "MR",
            "ПС4621115548",
            "ОТПРВ/НАЗН :",
            "SVOSVO",
            "ВЫДАН ОТ",
            ": АЭРОФЛОТ",
            "КОД БРОНИРОВАНИЯ",
            ": 8STR64",
            "НОМЕР БИЛЕТА",
            ": 555 2379040899",
            "МАРШРУТ/ПЕРЕВОЗЧИК",
            "РЕЙС",
            "КЛАСС",
            "ДАТА",
            "ВРЕМЯ ОТПР",
            "ВРЕМЯ ПРИБ",
            "СТАТУС",
            "МОСКВА, ШЕРЕМЕТЬЕВО",
            "SVO B / АЭРОФЛОТ",
            "SU-1412",
            "T",
            "02ФЕВ",
            "0945",
            "1420",
            "OK",
            "ЕКАТЕРИНБУРГ, КОЛЬЦОВО",
            "SVX / АЭРОФЛОТ",
            "SU-1419",
            "G",
            "11ФЕВ",
            "1530",
            "1640",
            "OK",
            "МОСКВА, ШЕРЕМЕТЬЕВО",
            "SVO B",
            "Расчет тарифа/Fare calculation SVO SU SVX100.00 SU SVO100.00NUC200.00END ROE1.0",
            "ТАРИФ",
            ": RUB20000",
            "СБОР/TAX",
            ": RUB1172",
            "RI932RUB YR240RUB",
            "ИТОГО ПО БИЛЕТУ",
            ": RUB21172",
            "СБОР СА",
            ": RUB0",
            "СБОР АСБ",
            ": RUB650",
            "ВСЕГО К ОПЛАТЕ",
            ": RUB21822",
        ]
        stream = "BT\n" + "\n".join(
            f"[<{line.encode('utf-16-be').hex().upper()}>] TJ" for line in lines
        ) + "\nET"
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
            {"file": upload_file("aeroflot_receipt.pdf", pdf, "application/pdf")},
            format="multipart",
        )
        assert response.status_code == 201, response.content

        result = admin_client.get(f"/api/v1/receipt-imports/{response.json()['id']}/result/")
        body = result.json()
        assert body["parser_status"] == "parsed"
        assert body["extracted"]["service_kind"] == "avia"
        assert body["extracted"]["reference"] == "8STR64"
        assert body["extracted"]["ticket_number"] == "555 2379040899"
        assert body["draft"]["passenger_name"] == "VLASOV/IGOR ALEKSANDROVICH"
        assert body["draft"]["issuer"] == "АЭРОФЛОТ"
        assert body["draft"]["fare"] == "20000.00"
        assert body["draft"]["taxes"] == "1172.00"
        assert body["draft"]["fees"] == "650.00"
        assert body["draft"]["total"] == "21822.00"
        assert body["draft"]["currency"] == "RUB"
        assert body["draft"]["trip_type"] == "roundtrip"
        assert body["draft"]["fare_breakdown"][0] == {
            "code": "SU",
            "label": "SVO → SVX",
            "amount": "100.00",
            "currency": "NUC",
            "from": "SVO",
            "to": "SVX",
            "carrier": "SU",
        }
        assert body["draft"]["fare_breakdown"][-1]["code"] == "ROE"
        assert body["draft"]["tax_breakdown"] == [
            {"code": "RI", "label": "RI", "amount": "932", "currency": "RUB"},
            {"code": "YR", "label": "YR", "amount": "240", "currency": "RUB"},
        ]
        assert body["draft"]["fee_breakdown"][-1]["amount"] == "650"
        assert body["draft"]["segments"][0]["fromCode"] == "SVO"
        assert body["draft"]["segments"][0]["toCode"] == "SVX"
        assert body["draft"]["segments"][0]["date"] == "02.02.2026"
        assert body["draft"]["segments"][0]["dep"] == "09:45"
        assert body["draft"]["segments"][0]["arr"] == "14:20"
        assert body["draft"]["segments"][1]["dir"] == "back"

    @pytest.mark.parametrize(
        ("name", "content", "service_kind", "service_type"),
        [
            ("blank.pdf", "Flight ticket\nPassenger: A\nPNR: AVA123\nFare: 10 USD\nTotal: 10 USD", "avia", "Авиа"),
            ("blank.pdf", "Rail ticket\nTrain 702\nWagon 4\nPassenger: A\nTotal: 10 USD", "rail", "ЖД"),
            ("blank.pdf", "Hotel voucher\nRoom deluxe\nCheck-in 12.08\nPassenger: A\nTotal: 10 USD", "hotel", "Гостиница"),
            ("blank.pdf", "Transfer voucher\nPickup airport\nDriver phone\nPassenger: A\nTotal: 10 USD", "transfer", "Трансфер"),
            ("blank.pdf", "Service receipt\nPassenger: A\nDocument: X12345\nTotal: 10 USD", "other", "Прочее"),
        ],
    )
    def test_receipt_import_detects_service_kind(self, admin_client, name, content, service_kind, service_type):
        response = admin_client.post(
            "/api/v1/receipt-imports/",
            {"file": upload_file(name, content.encode(), "text/plain")},
            format="multipart",
        )
        assert response.status_code == 201, response.content

        result = admin_client.get(f"/api/v1/receipt-imports/{response.json()['id']}/result/")
        assert result.status_code == 200, result.content
        body = result.json()
        assert body["extracted"]["service_kind"] == service_kind
        assert body["extracted"]["service_type"] == service_type
