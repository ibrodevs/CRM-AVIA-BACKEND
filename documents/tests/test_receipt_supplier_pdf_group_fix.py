import copy
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from documents.receipt_supplier_pdf_font_codec import install_receipt_supplier_pdf_font_codec
from documents.receipt_supplier_pdf_group_fix import install_receipt_supplier_pdf_group_fix
from documents.receipt_supplier_pdf_writer_fix import install_receipt_supplier_pdf_writer_fix

install_receipt_supplier_pdf_font_codec()
install_receipt_supplier_pdf_writer_fix()
install_receipt_supplier_pdf_group_fix()

from documents import receipt_supplier_pdf_patch as supplier_pdf  # noqa: E402
from documents.receipt_metadata import receipt_verified_data  # noqa: E402
from documents.services import extract_receipt_fields  # noqa: E402


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


def _single_line_pdf(text: str) -> bytes:
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
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin1"))
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


def test_recognized_receipt_page_overrides_group_ordinal():
    before = _group_payload()
    after = _group_payload(first_ticket="1539.60", first_total="4919.20")
    before["groupTickets"][0]["receiptPage"] = 3
    after["groupTickets"][0]["receiptPage"] = 3

    targets = supplier_pdf._collect_targets(before, after)

    assert {target.page_index for target in targets} == {2}


def test_single_receipt_parent_price_edit_updates_its_source_ticket():
    child = {
        "service_kind": "rail",
        "receiptPage": 1,
        "ticketCost": "1668.40",
        "reservedSeatCost": "1279.10",
        "fare": "2947.50",
        "fees": "0",
        "total": "2947.50",
    }
    before = {
        **child,
        "receipts": [copy.deepcopy(child)],
    }
    after = copy.deepcopy(before)
    after.update({
        "ticketCost": "1768.40",
        "reservedSeatCost": "1329.10",
        "fare": "3097.50",
        "total": "3097.50",
    })

    targets = supplier_pdf._collect_targets(before, after)

    assert {
        target.key: (target.old, target.new, target.page_index)
        for target in targets
    } == {
        "receipt[0].ticketCost": (Decimal("1668.40"), Decimal("1768.40"), 0),
        "receipt[0].reservedSeatCost": (Decimal("1279.10"), Decimal("1329.10"), 0),
        "receipt[0].total": (Decimal("2947.50"), Decimal("3097.50"), 0),
    }


def test_rail_printed_fee_components_are_updated_with_the_total():
    before = _group_payload()
    after = copy.deepcopy(before)
    before["groupTickets"][0]["agencyServiceFee"] = "100"
    before["groupTickets"][0]["additionalFees"] = "50"
    before["groupTickets"][0]["total"] = "4969.20"
    after["groupTickets"][0]["agencyServiceFee"] = "150"
    after["groupTickets"][0]["additionalFees"] = "75"
    after["groupTickets"][0]["total"] = "5044.20"

    targets = supplier_pdf._collect_targets(before, after)

    assert {target.key for target in targets} == {
        "receipt[0].agencyServiceFee",
        "receipt[0].additionalFees",
        "receipt[0].total",
    }


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


def test_carrier_taxes_and_payable_total_change_together():
    source = _single_line_pdf("CARRIER TAXES 42.50 TOTAL 292.50")
    before = {"service_kind": "avia", "taxes": "42.50", "total": "292.50"}
    after = {"service_kind": "avia", "taxes": "45.00", "total": "295.00"}

    corrected, report = supplier_pdf.patch_supplier_pdf(source, before, after)

    assert corrected is not None
    assert report["unapplied"] == []
    assert source == _single_line_pdf("CARRIER TAXES 42.50 TOTAL 292.50")
    text = PdfReader(BytesIO(corrected)).pages[0].extract_text()
    assert "CARRIER TAXES 45.00" in text
    assert "TOTAL 295.00" in text


def test_reordered_tax_rows_patch_by_code_without_requiring_unprinted_aggregate():
    source = _single_line_pdf("YR 12.50 XT 30.00 TOTAL 292.50")
    before = {
        "service_kind": "avia",
        "taxes": "42.50",
        "total": "292.50",
        "taxBreakdown": [
            {"code": "YR", "label": "Carrier surcharge", "amount": "12.50"},
            {"code": "XT", "label": "Airport tax", "amount": "30.00"},
        ],
    }
    after = {
        "service_kind": "avia",
        "taxes": "45.00",
        "total": "295.00",
        "taxBreakdown": [
            {"code": "XT", "label": "Airport tax", "amount": "30.00"},
            {"code": "YR", "label": "Carrier surcharge", "amount": "15.00"},
        ],
    }

    targets = supplier_pdf._collect_targets(before, after)
    corrected, report = supplier_pdf.patch_supplier_pdf(source, before, after)

    assert {
        target.key: (target.old, target.new) for target in targets
    } == {
        "taxes": (Decimal("42.50"), Decimal("45.00")),
        "taxBreakdown[1]": (Decimal("12.50"), Decimal("15.00")),
        "total": (Decimal("292.50"), Decimal("295.00")),
    }
    assert next(target for target in targets if target.key == "taxes").required is False
    assert corrected is not None
    assert report["unapplied"] == []
    assert report["optional_unapplied"] == ["taxes"]
    text = PdfReader(BytesIO(corrected)).pages[0].extract_text()
    assert "YR 15.00" in text
    assert "XT 30.00" in text
    assert "TOTAL 295.00" in text


@pytest.mark.parametrize(
    ("service_kind", "source_text", "before", "after", "expected"),
    [
        (
            "rail",
            "TICKET 80.00 RESERVED SEAT 20.00 SERVICE FEE 5.00 ADDITIONAL 2.00 TOTAL 107.00",
            {"ticketCost": "80", "reservedSeatCost": "20", "agencyServiceFee": "5", "additionalFees": "2", "total": "107"},
            {"ticketCost": "85", "reservedSeatCost": "22", "agencyServiceFee": "6", "additionalFees": "3", "total": "116"},
            ("TICKET 85.00", "RESERVED SEAT 22.00", "SERVICE FEE 6.00", "ADDITIONAL 3.00", "TOTAL 116.00"),
        ),
        (
            "hotel",
            "ROOM RATE 180.00 SERVICE FEE 10.00 TOTAL 190.00",
            {"fare": "180", "supplierCost": "180", "fees": "10", "agencyServiceFee": "10", "total": "190"},
            {"fare": "200", "supplierCost": "200", "fees": "12", "agencyServiceFee": "12", "total": "212"},
            ("ROOM RATE 200.00", "SERVICE FEE 12.00", "TOTAL 212.00"),
        ),
        (
            "transfer",
            "TRANSFER PRICE 45.00 ADDITIONAL 5.00 TOTAL 50.00",
            {"fare": "45", "supplierCost": "45", "fees": "5", "additionalFees": "5", "total": "50"},
            {"fare": "47", "supplierCost": "47", "fees": "7", "additionalFees": "7", "total": "54"},
            ("TRANSFER PRICE 47.00", "ADDITIONAL 7.00", "TOTAL 54.00"),
        ),
    ],
)
def test_each_service_kind_updates_printed_components_and_total(
    service_kind, source_text, before, after, expected
):
    source = _single_line_pdf(source_text)
    before = {"service_kind": service_kind, **before}
    after = {"service_kind": service_kind, **after}

    corrected, report = supplier_pdf.patch_supplier_pdf(source, before, after)

    assert corrected is not None, report
    assert report["unapplied"] == []
    text = PdfReader(BytesIO(corrected)).pages[0].extract_text()
    for fragment in expected:
        assert fragment in text


def test_amount_variants_preserve_one_decimal_supplier_precision():
    variants = supplier_pdf._amount_variants(Decimal("3324.9"))

    assert "3 324.9" in variants
    assert "3324.9" in variants


def _client_pdf(name: str) -> Path | None:
    workspace = Path(__file__).resolve().parents[3]
    for directory in workspace.iterdir():
        if directory.is_dir() and directory.name.startswith("PDF"):
            matches = list(directory.rglob(name))
            if matches:
                return matches[0]
    return None


@pytest.mark.skipif(_client_pdf("6RZ483_documents (1).pdf") is None, reason="client PDF folder is not available")
def test_real_grouped_avia_pdf_finds_ticket_pages_and_preserves_unchanged_fare():
    """The second blank starts on PDF page three, not ordinal page two."""

    path = _client_pdf("6RZ483_documents (1).pdf")
    source = path.read_bytes()
    extraction = extract_receipt_fields(source, mime="application/pdf", name=path.name)
    before = receipt_verified_data(
        extraction.get("fields") or {},
        parser_status=extraction.get("status") or "parsed",
    )
    after = copy.deepcopy(before)
    tickets = copy.deepcopy(after.get("receipts") or after.get("receipt_items") or [])
    assert len(tickets) == 2
    assert [ticket.get("receiptPage") for ticket in tickets] == [1, 3]

    # Add a service fee without changing the recognized 6400 fare. Only the
    # printed payable totals must become 6500 on each real ticket page.
    for ticket in tickets:
        ticket["fees"] = "100"
        ticket["total"] = "6500"
    after.update({"groupTickets": tickets, "fare": "12800", "fees": "200", "total": "13000"})

    corrected, report = supplier_pdf.patch_supplier_pdf(source, before, after)

    assert corrected is not None
    assert report["requested"] == report["applied"] == 2
    source_pages = [page.extract_text() or "" for page in PdfReader(BytesIO(source)).pages]
    corrected_pages = [page.extract_text() or "" for page in PdfReader(BytesIO(corrected)).pages]
    assert "MOW SU BQS6400RUB6400END" in corrected_pages[0]
    assert "MOW SU BQS6400RUB6400END" in corrected_pages[2]
    assert "6500.00 RUBИтого по тарифу/сборам" in corrected_pages[0]
    assert "6500.00 RUBИтого по тарифу/сборам" in corrected_pages[2]
    assert "6500.00 RUBИтого по тарифу/сборам" not in source_pages[0]
    assert corrected_pages[1] == source_pages[1]
    assert corrected_pages[3] == source_pages[3]


@pytest.mark.skipif(_client_pdf("Калмыков.pdf") is None, reason="client PDF folder is not available")
def test_real_utair_pdf_rewrites_fare_and_total_inside_original_font_strings():
    path = _client_pdf("Калмыков.pdf")
    source = path.read_bytes()
    extraction = extract_receipt_fields(source, mime="application/pdf", name=path.name)
    before = receipt_verified_data(
        extraction.get("fields") or {},
        parser_status=extraction.get("status") or "parsed",
    )
    after = copy.deepcopy(before)
    after.update({
        "fare": "8900",
        "total": "10100",
        "fareBreakdown": [{"code": "FARE", "label": "Тариф", "amount": "8900", "currency": "RUB"}],
    })

    corrected, report = supplier_pdf.patch_supplier_pdf(source, before, after)

    assert corrected is not None
    assert report["requested"] == report["applied"] == 2
    source_text = PdfReader(BytesIO(source)).pages[0].extract_text() or ""
    corrected_text = PdfReader(BytesIO(corrected)).pages[0].extract_text() or ""
    assert "Тариф/Fare 8800.00РУБ" in source_text
    assert "Тариф/Fare 8900.00РУБ" in corrected_text
    assert "Итого/Total 10000РУБ" in source_text
    assert "Итого/Total 10100РУБ" in corrected_text
