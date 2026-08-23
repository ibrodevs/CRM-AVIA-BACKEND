from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from documents.receipt_supplier_pdf_font_codec import install_receipt_supplier_pdf_font_codec
from documents.receipt_supplier_pdf_writer_fix import install_receipt_supplier_pdf_writer_fix

install_receipt_supplier_pdf_font_codec()
install_receipt_supplier_pdf_writer_fix()

from documents import receipt_supplier_pdf_patch as supplier_pdf  # noqa: E402


def _simple_supplier_pdf() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (TOTAL 25470 RUB) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _simple_fare_pdf() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    })
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (FARE 23720 RUB TAX 1250 RUB TOTAL 25470 RUB) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _malformed_identity_h_supplier_pdf() -> bytes:
    """Viewer-renderable Identity-H stream that pypdf ContentStream rejects.

    The Cyrillic letter Щ is U+0429, therefore its UTF-16BE low byte is 0x29
    (')'). Some real supplier PDFs write those bytes directly in literal strings
    without escaping the delimiter. PDF viewers tolerate the file, while pypdf's
    content parser stops at the embedded 0x29.
    """

    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    cid_info = DictionaryObject(
        {
            NameObject("/Registry"): TextStringObject("Adobe"),
            NameObject("/Ordering"): TextStringObject("Identity"),
            NameObject("/Supplement"): NumberObject(0),
        }
    )
    cid_font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/CIDFontType2"),
            NameObject("/BaseFont"): NameObject("/Arial"),
            NameObject("/CIDSystemInfo"): cid_info,
        }
    )
    cid_ref = writer._add_object(cid_font)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type0"),
            NameObject("/BaseFont"): NameObject("/Arial"),
            NameObject("/Encoding"): NameObject("/Identity-H"),
            NameObject("/DescendantFonts"): ArrayObject([cid_ref]),
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )

    def text_row(text: str, y: int) -> bytes:
        return (
            b"BT /F1 10 Tf 72 "
            + str(y).encode()
            + b" Td [("
            + text.encode("utf-16-be")
            + b")] TJ ET\n"
        )

    stream = DecodedStreamObject()
    stream.set_data(
        b"".join(
            [
                text_row("СУММА Щ", 720),
                text_row("СБОР АСБ", 700),
                text_row(": RUB400", 680),
                text_row("ВСЕГО К ОПЛАТЕ", 660),
                text_row(": RUB26973", 640),
            ]
        )
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_amount_format_keeps_supplier_separators():
    assert supplier_pdf._format_like("4 819,20", Decimal("4919.2")) == "4 919,20"
    assert supplier_pdf._format_like("25470", Decimal("25520")) == "25520"


def test_group_ticket_target_keeps_its_source_page_index():
    before = {
        "groupTickets": [
            {"total": "4819.20"},
            {"total": "3272.10"},
        ]
    }
    after = {
        "groupTickets": [
            {"total": "4819.20"},
            {"total": "3372.10"},
        ]
    }
    targets = supplier_pdf._collect_targets(before, after)
    assert len(targets) == 1
    assert targets[0].key == "receipt[1].total"
    assert targets[0].page_index == 1
    assert targets[0].old == Decimal("3272.10")
    assert targets[0].new == Decimal("3372.10")


def test_type1_supplier_pdf_amount_changes_in_place_with_same_font_resource():
    source = _simple_supplier_pdf()
    corrected, report = supplier_pdf.patch_supplier_pdf(
        source,
        {"total": "25470"},
        {"total": "25520"},
    )

    assert corrected is not None
    assert report["requested"] == 1
    assert report["applied"] == 1
    assert report["unapplied"] == []
    assert report["font_preserved"] is True
    assert report["source_immutable"] is True

    source_reader = PdfReader(BytesIO(source))
    corrected_reader = PdfReader(BytesIO(corrected))
    assert "TOTAL 25470 RUB" in source_reader.pages[0].extract_text()
    assert "TOTAL 25520 RUB" in corrected_reader.pages[0].extract_text()

    source_font = source_reader.pages[0]["/Resources"]["/Font"]["/F1"].get_object()
    corrected_font = corrected_reader.pages[0]["/Resources"]["/Font"]["/F1"].get_object()
    assert source_font["/BaseFont"] == corrected_font["/BaseFont"] == "/Helvetica"
    assert source_font["/Subtype"] == corrected_font["/Subtype"] == "/Type1"


def test_supplier_pdf_can_close_fare_as_it_without_hiding_taxes_or_total():
    source = _simple_fare_pdf()
    corrected, report = supplier_pdf.patch_supplier_pdf(
        source,
        {"fare": "23720", "taxes": "1250", "total": "25470"},
        {
            "fare": "23720", "taxes": "1250", "total": "25470",
            "output": {"priceMode": "it"},
        },
    )

    assert corrected is not None
    assert report["requested"] == report["applied"] == 1
    text = PdfReader(BytesIO(corrected)).pages[0].extract_text()
    assert "FARE IT RUB" in text
    assert "TAX 1250 RUB" in text
    assert "TOTAL 25470 RUB" in text


def test_malformed_identity_h_supplier_pdf_uses_raw_stream_same_font_fallback():
    source = _malformed_identity_h_supplier_pdf()
    corrected, report = supplier_pdf.patch_supplier_pdf(
        source,
        {"fees": "400", "total": "26973"},
        {"fees": "450", "total": "27023"},
    )

    assert corrected is not None
    assert report["requested"] == 2
    assert report["applied"] == 2
    assert report["unapplied"] == []
    assert report["strategy"] == "raw_stream"
    assert report["fallback"] is True
    assert report["font_preserved"] is True
    assert report["source_immutable"] is True

    source_reader = PdfReader(BytesIO(source), strict=False)
    corrected_reader = PdfReader(BytesIO(corrected), strict=False)
    source_stream = source_reader.pages[0].get_contents().get_data()
    corrected_stream = corrected_reader.pages[0].get_contents().get_data()
    assert "400".encode("utf-16-be") in source_stream
    assert "450".encode("utf-16-be") in corrected_stream
    assert "26973".encode("utf-16-be") in source_stream
    assert "27023".encode("utf-16-be") in corrected_stream

    source_font = source_reader.pages[0]["/Resources"]["/Font"]["/F1"].get_object()
    corrected_font = corrected_reader.pages[0]["/Resources"]["/Font"]["/F1"].get_object()
    assert source_font["/BaseFont"] == corrected_font["/BaseFont"] == "/Arial"
    assert source_font["/Subtype"] == corrected_font["/Subtype"] == "/Type0"


def test_missing_source_amount_publishes_complete_correction_appendix():
    source = _simple_supplier_pdf()
    corrected, report = supplier_pdf.patch_supplier_pdf(
        source,
        {"total": "25470", "fees": "500"},
        {"total": "25520", "fees": "550"},
    )

    assert corrected is not None
    assert report["strategy"] == "financial_correction_appendix"
    assert report["requested"] == 2
    assert report["applied"] == 2
    assert report["unapplied"] == []
    reader = PdfReader(BytesIO(corrected))
    assert len(reader.pages) == 2
    appendix = reader.pages[-1].extract_text()
    assert "CRM PRICE CORRECTION" in appendix
    assert "fees | 500 | 550" in appendix
    assert "total | 25470 | 25520" in appendix


def test_new_price_is_visible_when_supplier_voucher_contains_no_price():
    source = _simple_supplier_pdf()
    corrected, report = supplier_pdf.patch_supplier_pdf(
        source,
        {"service_kind": "hotel", "fare": None, "supplierCost": None, "total": None},
        {"service_kind": "hotel", "fare": "200", "supplierCost": "200", "total": "200"},
    )

    assert corrected is not None
    assert report["strategy"] == "financial_correction_appendix"
    assert report["applied"] == 3
    appendix = PdfReader(BytesIO(corrected)).pages[-1].extract_text()
    assert "SERVICE: HOTEL" in appendix
    assert "fare | NOT SET | 200" in appendix
    assert "supplierCost | NOT SET | 200" in appendix
    assert "total | NOT SET | 200" in appendix


def test_confirmed_pricing_values_override_stale_supplier_snapshot():
    document = SimpleNamespace(
        metadata={
            "receipt_import": {
                "corrected_fields": {
                    "fare": "23720",
                    "taxes": "1250",
                    "fees": "550",
                    "total": "25520",
                    "currency": "RUB",
                }
            }
        }
    )
    submitted = {
        "fare": "23720",
        "taxes": "1250",
        "fees": "500",
        "total": "25470",
        "currency": "RUB",
        "feeBreakdown": [
            {"code": "ASB", "label": "Сбор АСБ", "amount": "500", "currency": "RUB"}
        ],
    }

    corrected = supplier_pdf._confirmed_verified_data(document, submitted, "parsed")

    assert corrected["fees"] == "550"
    assert corrected["total"] == "25520"
    assert corrected["feeBreakdown"][0]["amount"] == "500"


def test_confirmed_client_total_is_printed_in_corrected_supplier_pdf():
    document = SimpleNamespace(
        metadata={
            "receipt_import": {
                "client_total": "26020",
                "markup": "500",
                "corrected_fields": {
                    "fare": "23720",
                    "taxes": "1250",
                    "fees": "550",
                    "total": "25520",
                    "currency": "RUB",
                },
            }
        }
    )

    corrected = supplier_pdf._confirmed_verified_data(document, {}, "parsed")

    assert corrected["fare"] == "23720"
    assert corrected["fees"] == "550"
    assert corrected["total"] == "26020"


def test_draft_base_keeps_service_specific_source_prices_from_extraction():
    draft = SimpleNamespace(
        import_job=SimpleNamespace(
            raw_extraction={
                "service_kind": "hotel",
                "supplierCost": "180.00",
                "agencyServiceFee": "10.00",
            }
        ),
        issuer="Test Hotel",
        passenger_name="Guest",
        segments=[],
        fare=Decimal("180.00"),
        taxes=Decimal("0.00"),
        fees=Decimal("10.00"),
        total=Decimal("190.00"),
        currency="USD",
        fare_breakdown=[],
        tax_breakdown=[],
        fee_breakdown=[],
        receipt_items=[],
    )

    verified = supplier_pdf._draft_base_verified(draft, "parsed")

    assert verified["service_kind"] == "hotel"
    assert verified["supplierCost"] == "180.00"
    assert verified["agencyServiceFee"] == "10.00"
