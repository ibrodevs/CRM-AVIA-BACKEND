"""Создание заказа из квитанции не должно портить соседние графы бланка.

В редакторе оператор правит все графы сразу, поэтому дефект там не виден.
При создании заказа подтверждается только то, что оператор изменил, — и правка
тарифа переписывала ещё и строку «Эквив. тарифа», которую никто не менял.
Причина: одинаковая сумма считалась «одной и той же по всей странице», и это
снимало проверку подписи графы.
"""

from __future__ import annotations

from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from documents.models import Document

pytestmark = pytest.mark.django_db

# Бланк поставщика: тариф и эквивалент тарифа напечатаны одной и той же суммой.
BLANK_LINES = (
    "Passenger IVANOV IVAN",
    "Carrier Smartavia",
    "Currency RUB",
    "Fare 8878",
    "Equivalent fare paid 8878",
    "Tax/fee/charge 5220",
    "Total 14098",
)

RECOGNIZED = {
    "issuer": "Smartavia",
    "passenger_name": "IVANOV IVAN",
    "fare": "8878",
    "taxes": "5220",
    "fees": "0",
    "total": "14098",
    "currency": "RUB",
    "segments": [],
    "service_kind": "avia",
}


def _supplier_pdf() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    })
    font_reference = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference}),
    })
    stream = DecodedStreamObject()
    stream.set_data("".join(
        f"BT /F1 9 Tf 60 {760 - index * 14} Td ({line}) Tj ET\n"
        for index, line in enumerate(BLANK_LINES)
    ).encode("latin1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _corrected_text(document: Document) -> str:
    version_number = (document.metadata or {}).get("receipt_import", {}).get(
        "supplier_corrected_version"
    )
    assert version_number, "исправленная копия бланка не создана"
    version = document.versions.filter(version=version_number).first()
    with version.file.open("rb") as handle:
        return PdfReader(BytesIO(handle.read())).pages[0].extract_text()


@pytest.fixture
def imported_receipt(admin_client):
    """Настоящий разбор бланка: суммы берутся из PDF, а не из заглушки."""
    upload = SimpleUploadedFile("receipt.pdf", _supplier_pdf(), content_type="application/pdf")
    response = admin_client.post("/api/v1/receipt-imports/", {"file": upload}, format="multipart")
    assert response.status_code == 201, response.content
    return response.json()


def _confirm(admin_client, imported, **overrides):
    verified = {
        "carrier": "Smartavia", "passenger": "IVANOV IVAN", "currency": "RUB",
        "fare": "100", "taxes": "5220", "fees": "0", "total": "5320",
        "fareBreakdown": [], "taxBreakdown": [], "feeBreakdown": [], "legs": [],
        "output": {"mode": "original"},
        **overrides.pop("verified", {}),
    }
    payload = {
        "issuer": "Smartavia", "passenger_name": "IVANOV IVAN", "segments": [],
        "fare": "100", "taxes": "5220", "fees": "0", "currency": "RUB",
        "original_total": "14098", "client_total": "7320", "markup": "2000", "commission": "0",
        "supplier_original": {
            "name": "receipt.pdf",
            "verified_data": verified,
            "output_settings": {"mode": "original"},
            "audit_log": [],
        },
        **overrides,
    }
    response = admin_client.post(
        f"/api/v1/receipt-imports/{imported['id']}/confirm/", payload, format="json"
    )
    assert response.status_code == 200, response.content
    return response.json()


class TestOrderFromReceipt:
    def test_an_untouched_row_keeps_the_supplier_amount(self, admin_client, imported_receipt):
        confirmed = _confirm(admin_client, imported_receipt)
        document = Document.objects.get(pk=imported_receipt["document_id"])
        text = _corrected_text(document)

        assert "Fare 100" in text
        # Эквивалент тарифа оператор не менял — он обязан остаться как в бланке.
        assert "Equivalent fare paid 8878" in text
        assert "Tax/fee/charge 5220" in text
        # Одна правка на графу, а не одна на каждое совпадение суммы.
        assert confirmed["supplier_pdf_correction"]["replacements"] == 2

    def test_the_payable_total_becomes_the_client_total(self, admin_client, imported_receipt):
        _confirm(admin_client, imported_receipt)
        document = Document.objects.get(pk=imported_receipt["document_id"])
        assert "Total 7320" in _corrected_text(document)

    def test_editing_both_fare_rows_updates_both(self, admin_client, monkeypatch):
        """Когда обе графы распознаны и обе изменены — меняются обе."""
        recognized = {**RECOGNIZED, "equivalentFare": "8878"}
        monkeypatch.setattr(
            "documents.views.extract_receipt_fields",
            lambda *_args, **_kwargs: {
                "status": "parsed", "fields": dict(recognized),
                "confidence": 0.9, "raw": dict(recognized), "warnings": [],
            },
        )
        upload = SimpleUploadedFile("receipt.pdf", _supplier_pdf(), content_type="application/pdf")
        imported = admin_client.post(
            "/api/v1/receipt-imports/", {"file": upload}, format="multipart"
        ).json()

        _confirm(admin_client, imported, verified={"equivalentFare": "100"})

        document = Document.objects.get(pk=imported["document_id"])
        text = _corrected_text(document)
        assert "Fare 100" in text
        assert "Equivalent fare paid 100" in text

    def test_a_row_the_parser_never_recognized_is_left_as_printed(
        self, admin_client, imported_receipt
    ):
        """Распознаватель не извлёк «Эквив. тарифа» — подменять её нечем.

        Раньше графа менялась заодно с тарифом, потому что суммы совпадали.
        Оставить бланк поставщика как есть честнее, чем подставить в него
        значение, которого система в нём не нашла.
        """
        _confirm(admin_client, imported_receipt, verified={"equivalentFare": "100"})
        document = Document.objects.get(pk=imported_receipt["document_id"])
        assert "Equivalent fare paid 8878" in _corrected_text(document)
