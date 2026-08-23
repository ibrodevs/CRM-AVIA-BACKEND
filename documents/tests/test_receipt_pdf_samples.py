from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from documents.models import Document
from documents.receipt_supplier_pdf_patch import SUPPLIER_PDF_SYNC_JOB

pytestmark = pytest.mark.django_db

SAMPLES_ROOT = Path(__file__).resolve().parents[3] / "CRM-Admin" / "квитанции"


@pytest.mark.parametrize(
    ("folder", "name", "kind", "passenger", "total", "route_from", "route_to", "date"),
    [
        (
            "Авиа",
            "Aleksandr_Zaliubin_421_2124922516_8202068.pdf",
            "avia",
            "ZALIUBIN ALEKSANDR MIKHAILOVICH",
            "54643.00",
            "Москва, Домодедово",
            "Чита",
            "17.06.2025",
        ),
        (
            "Авиа",
            "Galina_Sivaks_1111484695_8302818.pdf",
            "avia",
            "SIVAKS GALINA",
            "2897.00",
            "РИМ, ФЬЮМИЧИНО/ЛЕОНАРДО ДА ВИНЧИ",
            "HAHN, ХАН",
            "29.07.2025",
        ),
        (
            "Авиа",
            "Nataliia_Popovich_235_3497052386_8325212.pdf",
            "avia",
            "POPOVICH NATALIIA",
            "20982.63",
            "ЗАГРЕБ, ПЛЕСО",
            "СТАМБУЛА",
            "22.06.2025",
        ),
        (
            "Авиа",
            "Сергей_Вадимович_Богацков_425_6123628210_8309085.pdf",
            "avia",
            "БОГАЦКОВ СЕРГЕЙ В",
            "10099.00",
            "Саратов",
            "Сочи",
            "05.06.2025",
        ),
        (
            "ЖД",
            "ЖД - Антонов Вадим Сергеевич (1).pdf",
            "rail",
            "АНТОНОВ ВАДИМ СЕРГЕЕВИЧ",
            "1268.10",
            "Москва Казанская",
            "Рязань 2",
            "27.05.2025",
        ),
        (
            "ЖД",
            "ЖД - Антонов Вадим Сергеевич.pdf",
            "rail",
            "АНТОНОВ В. С.",
            "875.00",
            "РЯЗАНЬ 1",
            "МОСКВА КАЗАНСКАЯ",
            "27.05.2025",
        ),
        (
            "ЖД",
            "ЖД - Кутуков Сергей Алексеевич.pdf",
            "rail",
            "КУТУКОВ С. А.",
            "3324.90",
            "МОСКВА КАЗАНСКАЯ",
            "РЯЗАНЬ 2",
            "27.05.2025",
        ),
        (
            "Отель",
            "gel'man_mixail_vladimirovich_2025-06-02.pdf",
            "hotel",
            "Гельман Михаил Владимирович",
            None,
            "",
            "Скай Порт",
            "02.06.2025",
        ),
        (
            "Отель",
            "voucher-ru-328343646.pdf",
            "hotel",
            "Fakhretdinov Ravilievich Maksim",
            None,
            "",
            "Гостиница Арена",
            "15.06.2025",
        ),
        (
            "Отель",
            "voucher-ru-945135381.pdf",
            "hotel",
            "Koloskov Evgenii, Koloskova Iuliia",
            None,
            "",
            "Hotel Eclat Beijing",
            "20.05.2025",
        ),
        (
            "Трансфер",
            "Ваучер трансфера.pdf",
            "transfer",
            "Сорокина Ольга",
            "4200.00",
            "Аэропорт Храброво (Калининград)",
            "Калининград",
            "22.05.2025",
        ),
    ],
)
def test_every_client_pdf_is_recognized_on_receipt_import_api(
    admin_client,
    folder,
    name,
    kind,
    passenger,
    total,
    route_from,
    route_to,
    date,
):
    path = SAMPLES_ROOT / folder / name
    response = admin_client.post(
        "/api/v1/receipt-imports/",
        {"file": SimpleUploadedFile(name, path.read_bytes(), content_type="application/pdf")},
        format="multipart",
    )
    assert response.status_code == 201, response.content

    result = admin_client.get(f"/api/v1/receipt-imports/{response.json()['id']}/result/")
    assert result.status_code == 200, result.content
    body = result.json()
    assert body["parser_status"] == "parsed"
    assert body["extracted"]["service_kind"] == kind
    assert body["draft"]["passenger_name"] == passenger
    assert body["draft"]["total"] == total
    assert body["draft"]["segments"]
    assert route_from.casefold() in body["draft"]["segments"][0]["from"].casefold()
    assert route_to.casefold() in body["draft"]["segments"][0]["to"].casefold()
    assert body["draft"]["segments"][0]["date"] == date
    if name.startswith("Aleksandr_Zaliubin"):
        assert body["draft"]["fare"] == "53545.00"
        assert body["draft"]["taxes"] == "1098.00"
    if name == "gel'man_mixail_vladimirovich_2025-06-02.pdf":
        verified = body["verified_data"]
        assert verified["hotel"] == {
            "name": "Скай Порт",
            "category": "",
            "country": "Россия",
            "city": "Обь",
            "address": (
                "Россия, 633104, Новосибирская область, Обь, Толмачево, "
                "пр-т. Мозжерина, д. 8"
            ),
            "phone": "",
            "email": "",
            "map": "",
        }
        assert verified["rooms"][0]["meal"] == "Завтрак"
        assert verified["rooms"][0]["earlyCheckIn"] == "09:30"
        assert verified["rooms"][0]["lateCheckOut"] == "18:30"
        assert verified["nights"] == 3
        assert "Без штрафа до 31.05.2025" in verified["hotelTerms"]["cancellation"]
    if name == "voucher-ru-328343646.pdf":
        verified = body["verified_data"]
        assert verified["issueDate"] == "26.05.2025"
        assert verified["supplierOrderNo"] == "328343646"
        assert verified["hotel"]["address"] == "119048, 10-letiya Oktyabrya street 11, Москва"
        assert verified["hotel"]["phone"] == "+74951815101"
        assert verified["hotel"]["map"] == "55.725307 37.56367"
        assert verified["rooms"][0]["bedType"] == "Двуспальная кровать"
        assert verified["rooms"][0]["meal"] == "Без питания"
        assert verified["nights"] == 3
        assert verified["hotelTerms"]["cityTax"]
        assert verified["hotelTerms"]["registrationFee"]
    if name == "voucher-ru-945135381.pdf":
        verified = body["verified_data"]
        assert [guest["name"] for guest in verified["passengers"]] == [
            "Koloskov Evgenii",
            "Koloskova Iuliia",
        ]
        assert verified["hotel"]["address"] == "100020, No.9 Dongdaqiao Road, Пекин"
        assert verified["hotel"]["phone"] == "861085612888"
        assert verified["hotel"]["map"] == "39.917427 116.44342"
        assert verified["rooms"][0]["adults"] == 2
        assert verified["rooms"][0]["meal"] == "Завтрак"
        assert verified["rooms"][0]["guestIds"] == ["Koloskov Evgenii", "Koloskova Iuliia"]
        assert verified["hotelTerms"]["deposit"] == "1500 CNY с номера за весь период проживания"
    if name == "Ваучер трансфера.pdf":
        verified = body["verified_data"]
        assert verified["issueDate"] == "21.05.2025"
        assert verified["supplierOrderNo"] == "C-0151866"
        assert verified["passengers"][0]["phone"] == "+7 910 792-59-61"
        assert verified["legs"][0]["flightNo"] == "SU-105"
        assert verified["legs"][0]["toAddress"].startswith("Mercure Kaliningrad")
        assert verified["legs"][1]["fromAddress"].startswith("Mercure Kaliningrad")
        assert verified["vehicle"]["className"] == "Комфорт"
        assert verified["vehicle"]["passengers"] == "1"
        assert "5 часов" in verified["transferTerms"]["cancellation"]
        assert verified["transferTerms"]["supportContacts"] == "88007751454"
        assert verified["transferTerms"]["meetAndGreet"]
        assert verified["transferTerms"]["baggageHelp"]


def test_single_rail_receipt_parent_price_edit_is_served_as_corrected_pdf(admin_client):
    """Cover the exact import -> editor preview -> supplier PDF sequence.

    A one-ticket RZD file keeps a compatibility child in ``receipts`` while
    the editor shows and changes the parent form. The preview endpoint must
    reconcile that parent edit, create a new version and serve it instead of
    silently falling back to the upload.
    """

    name = "ЖД - Кутуков Сергей Алексеевич.pdf"
    path = SAMPLES_ROOT / "ЖД" / name
    imported = admin_client.post(
        "/api/v1/receipt-imports/",
        {"file": SimpleUploadedFile(name, path.read_bytes(), content_type="application/pdf")},
        format="multipart",
    )
    assert imported.status_code == 201, imported.content

    result = admin_client.get(f"/api/v1/receipt-imports/{imported.json()['id']}/result/")
    assert result.status_code == 200, result.content
    body = result.json()
    verified = deepcopy(body["verified_data"])
    child = {
        **verified,
        "groupTickets": [],
        "receiptItems": [],
        "receipt_items": [],
        "receipts": [],
        "railTickets": [],
        "receiptCount": 1,
        "receiptPage": 1,
    }
    verified["receipts"] = [child]
    document = Document.objects.get(pk=body["source_document_id"])
    metadata = deepcopy(document.metadata)
    metadata["supplier_original"]["base_verified_data"] = deepcopy(verified)
    metadata["supplier_original"]["verified_data"] = deepcopy(verified)
    metadata["receipt_import"]["verified_data"] = deepcopy(verified)
    document.metadata = metadata
    document.save(update_fields=["metadata"])

    ticket_cost = Decimal(str(verified["ticketCost"]))
    reserved_seat = Decimal(str(verified["reservedSeatCost"]))
    total = Decimal(str(verified["total"]))
    # The React editor serializes the unchanged hidden singleton child as
    # JSON numbers. Its recognized snapshot still contains decimal strings.
    # These are monetarily equal and must not suppress the visible parent edit.
    for key in ("ticketCost", "reservedSeatCost", "fare", "fees", "total"):
        value = verified["receipts"][0].get(key)
        if value not in (None, ""):
            verified["receipts"][0][key] = float(Decimal(str(value)))
    verified["ticketCost"] = str(ticket_cost + Decimal("10.00"))
    verified["reservedSeatCost"] = str(reserved_seat + Decimal("5.00"))
    verified["fare"] = str(Decimal(str(verified["fare"])) + Decimal("15.00"))
    verified["total"] = str(total + Decimal("15.00"))

    with override_settings(SYNC_JOB_KINDS=(SUPPLIER_PDF_SYNC_JOB,)):
        updated = admin_client.post(
            f"/api/v1/documents/{body['source_document_id']}/receipt/",
            {"draft": True, "preview_sync": True, "verified_data": verified},
            format="json",
        )
    assert updated.status_code == 200, updated.content
    correction = updated.json()["supplier_pdf_correction"]
    assert correction["status"] == "corrected", correction
    assert correction["requested"] == correction["applied"]
    assert correction["requested"] >= 2
    assert correction["corrected_version"] > correction["source_version"]

    preview = admin_client.get(
        f"/api/v1/documents/{body['source_document_id']}/supplier-pdf/?disposition=inline"
    )
    assert preview.status_code == 200
    assert preview["X-Supplier-PDF-Mode"] == "corrected"
    assert preview["X-Supplier-Correction-Status"] == "corrected"
    assert preview["Cache-Control"].startswith("private, no-store")
