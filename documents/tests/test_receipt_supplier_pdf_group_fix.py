from io import BytesIO

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from documents.receipt_supplier_pdf_font_codec import install_receipt_supplier_pdf_font_codec
from documents.receipt_supplier_pdf_group_fix import install_receipt_supplier_pdf_group_fix
from documents.receipt_supplier_pdf_writer_fix import install_receipt_supplier_pdf_writer_fix

install_receipt_supplier_pdf_font_codec()
install_receipt_supplier_pdf_writer_fix()
install_receipt_supplier_pdf_group_fix()

from documents import receipt_supplier_pdf_patch as supplier_pdf  # noqa: E402


def _grouped_rail_pdf() -> bytes:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        }
    )
    font_ref = writer._add_object(font)

    for ticket, reserved, total in (
        ("1439.60", "3379.60", "4819.20"),
        ("1246.90", "3289.80", "4536.70"),
    ):
        page = writer.add_blank_page(width=595, height=842)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
        )
        stream = DecodedStreamObject()
        stream.set_data(
            (
                "BT /F1 12 Tf 72 720 Td "
                f"(TICKET {ticket} RESERVED SEAT {reserved} TOTAL {total}) Tj ET"
            ).encode("latin1")
        )
        page[NameObject("/Contents")] = writer._add_object(stream)

    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _single_avia_pdf() -> bytes:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        }
    )
    font_ref = writer._add_object(font)
    page = writer.add_blank_page(width=595, height=842)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (FEE 500 TOTAL 25470) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _group_payload(first_ticket="1439.60", first_total="4819.20"):
    first_fare = str(round(float(first_ticket) + 3379.60, 2))
    group_total = str(round(float(first_total) + 4536.70, 2))
    return {
        "fare": group_total,
        "fees": "0",
        "total": group_total,
        "groupTickets": [
            {
                "ticketCost": first_ticket,
                "reservedSeatCost": "3379.60",
                "agencyServiceFee": "0",
                "additionalFees": "0",
                "fare": first_fare,
                "fees": "0",
                "total": first_total,
            },
            {
                "ticketCost": "1246.90",
                "reservedSeatCost": "3289.80",
                "agencyServiceFee": "0",
                "additionalFees": "0",
                "fare": "4536.70",
                "fees": "0",
                "total": "4536.70",
            },
        ],
    }


def test_grouped_rail_targets_ignore_parent_aggregates_and_child_aliases():
    before = _group_payload()
    after = _group_payload(first_ticket="1539.60", first_total="4919.20")

    targets = supplier_pdf._collect_targets(before, after)

    assert {(target.key, target.page_index) for target in targets} == {
        ("receipt[0].ticketCost", 0),
        ("receipt[0].total", 0),
    }
    assert not any(target.key in {"fare", "fees", "total"} for target in targets)
    assert not any(target.key.endswith(".fare") or target.key.endswith(".fees") for target in targets)


def test_grouped_rail_supplier_pdf_changes_only_edited_ticket_page():
    source = _grouped_rail_pdf()
    before = _group_payload()
    after = _group_payload(first_ticket="1539.60", first_total="4919.20")

    corrected, report = supplier_pdf.patch_supplier_pdf(source, before, after)

    assert corrected is not None
    assert report["requested"] == 2
    assert report["applied"] == 2
    assert report["unapplied"] == []

    source_reader = PdfReader(BytesIO(source))
    corrected_reader = PdfReader(BytesIO(corrected))

    assert "TICKET 1439.60" in source_reader.pages[0].extract_text()
    assert "TOTAL 4819.20" in source_reader.pages[0].extract_text()
    assert "TICKET 1539.60" in corrected_reader.pages[0].extract_text()
    assert "TOTAL 4919.20" in corrected_reader.pages[0].extract_text()

    # The sibling ticket must stay byte-for-byte semantically unchanged.
    assert corrected_reader.pages[1].extract_text() == source_reader.pages[1].extract_text()


def test_avia_synced_fee_and_breakdown_patch_one_printed_amount_once():
    source = _single_avia_pdf()
    before = {
        "fees": "500",
        "total": "25470",
        "feeBreakdown": [
            {"code": "FEE", "label": "FEE", "amount": "500", "currency": "RUB"}
        ],
    }
    after = {
        "fees": "550",
        "total": "25520",
        "feeBreakdown": [
            {"code": "FEE", "label": "FEE", "amount": "550", "currency": "RUB"}
        ],
    }

    targets = supplier_pdf._collect_targets(before, after)
    corrected, report = supplier_pdf.patch_supplier_pdf(source, before, after)

    assert {target.key for target in targets} == {"fees", "total"}
    assert corrected is not None
    assert report["requested"] == 2
    assert report["applied"] == 2
    assert report["unapplied"] == []
    assert "FEE 550 TOTAL 25520" in PdfReader(BytesIO(corrected)).pages[0].extract_text()


def test_equal_independent_breakdown_rows_are_not_collapsed():
    before = {
        "taxBreakdown": [
            {"code": "YR", "label": "YR", "amount": "500"},
            {"code": "XT", "label": "XT", "amount": "500"},
        ]
    }
    after = {
        "taxBreakdown": [
            {"code": "YR", "label": "YR", "amount": "550"},
            {"code": "XT", "label": "XT", "amount": "550"},
        ]
    }

    targets = supplier_pdf._collect_targets(before, after)

    assert {target.key for target in targets} == {
        "taxBreakdown[0]",
        "taxBreakdown[1]",
    }
