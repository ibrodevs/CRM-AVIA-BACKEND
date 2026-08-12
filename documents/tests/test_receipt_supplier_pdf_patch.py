from decimal import Decimal
from io import BytesIO

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from documents.receipt_supplier_pdf_font_codec import install_receipt_supplier_pdf_font_codec
from documents.receipt_supplier_pdf_patch import _collect_targets, _format_like, patch_supplier_pdf


install_receipt_supplier_pdf_font_codec()


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


def test_amount_format_keeps_supplier_separators():
    assert _format_like("4 819,20", Decimal("4919.2")) == "4 919,20"
    assert _format_like("25470", Decimal("25520")) == "25520"
    assert _format_like("1,250.00", Decimal("1300")) == "1,300.00"


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
    targets = _collect_targets(before, after)
    assert len(targets) == 1
    assert targets[0].key == "receipt[1].total"
    assert targets[0].page_index == 1
    assert targets[0].old == Decimal("3272.10")
    assert targets[0].new == Decimal("3372.10")


def test_type1_supplier_pdf_amount_changes_in_place_with_same_font_resource():
    source = _simple_supplier_pdf()
    corrected, report = patch_supplier_pdf(
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


def test_no_partial_supplier_pdf_is_published_when_a_requested_amount_is_missing():
    source = _simple_supplier_pdf()
    corrected, report = patch_supplier_pdf(
        source,
        {"total": "25470", "fees": "500"},
        {"total": "25520", "fees": "550"},
    )

    assert corrected is None
    assert report["requested"] == 2
    assert report["applied"] == 1
    assert report["unapplied"]
