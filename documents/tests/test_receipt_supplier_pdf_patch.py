from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    ByteStringObject,
    ContentStream,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from documents.receipt_supplier_pdf_font_codec import install_receipt_supplier_pdf_font_codec
from documents.receipt_supplier_pdf_writer_fix import install_receipt_supplier_pdf_writer_fix

install_receipt_supplier_pdf_font_codec()
install_receipt_supplier_pdf_writer_fix()

from documents import receipt_supplier_pdf_patch as supplier_pdf  # noqa: E402
from documents import receipt_supplier_pdf_writer_fix as writer_fix  # noqa: E402


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


def _two_currency_fare_pdf() -> bytes:
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
    stream.set_data(
        b"BT /F1 12 Tf 72 720 Td "
        b"(FARE EUR138 EQUIVALENT FARE PAID RUB13110 TAX RUB3690 TOTAL RUB16800) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _fragmented_amount_pdf() -> bytes:
    """Supplier-style stream with one independently positioned glyph per Tj."""

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
    commands = [b"BT /F1 12 Tf 72 720 Td (TOTAL ) Tj\n"]
    for character in "875.00":
        commands.append(f"({character}) Tj\n".encode("ascii"))
    commands.append(b"ET")
    stream = DecodedStreamObject()
    stream.set_data(b"".join(commands))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _nested_mixed_font_amount_pdf() -> bytes:
    """Amount split across equivalent font subsets inside a Form XObject."""

    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)

    def font(base_font: str):
        return writer._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject(base_font),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        }))

    form = DecodedStreamObject()
    form[NameObject("/Type")] = NameObject("/XObject")
    form[NameObject("/Subtype")] = NameObject("/Form")
    form[NameObject("/BBox")] = ArrayObject(
        [NumberObject(0), NumberObject(0), NumberObject(500), NumberObject(500)]
    )
    form[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/ArialFull"): font("/ArialMT"),
            NameObject("/ArialSubset"): font("/Arial"),
        })
    })
    form.set_data(
        b"BT /ArialFull 12 Tf 72 400 Td (TOTAL 8) Tj "
        b"/ArialSubset 12 Tf (75.0) Tj /ArialFull 12 Tf (0) Tj ET"
    )
    form_ref = writer._add_object(form)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/XObject"): DictionaryObject({NameObject("/Receipt"): form_ref})
    })
    page_stream = DecodedStreamObject()
    page_stream.set_data(b"q /Receipt Do Q")
    page[NameObject("/Contents")] = writer._add_object(page_stream)
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


def _cpdf_null_spaced_it_pdf() -> bytes:
    """Minimal dompdf/CPDF stream with UTF-16 NULs between TJ operands."""

    writer = PdfWriter()
    page = writer.add_blank_page(width=595.28, height=841.89)
    cid_info = DictionaryObject({
        NameObject("/Registry"): TextStringObject("Adobe"),
        NameObject("/Ordering"): TextStringObject("Identity"),
        NameObject("/Supplement"): NumberObject(0),
    })
    cid_font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/CIDFontType2"),
        NameObject("/BaseFont"): NameObject("/Arial"),
        NameObject("/CIDSystemInfo"): cid_info,
    })
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type0"),
        NameObject("/BaseFont"): NameObject("/Arial"),
        NameObject("/Encoding"): NameObject("/Identity-H"),
        NameObject("/DescendantFonts"): ArrayObject([writer._add_object(cid_font)]),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    })

    def split_row(label: str, amount: str, y: int) -> bytes:
        return (
            b"BT /F1 10 Tf 72 " + str(y).encode() + b" Td [("
            + label.encode("utf-16-be")
            + b")\x00 -670\x00 ("
            + amount.encode("utf-16-be")
            + b")] TJ ET\n"
        )

    def row(value: str, y: int) -> bytes:
        return (
            b"BT /F1 10 Tf 72 " + str(y).encode() + b" Td [("
            + value.encode("utf-16-be") + b")] TJ ET\n"
        )

    stream = DecodedStreamObject()
    stream.set_data(b"".join([
        split_row("ТАРИФ", "RUB17205", 720),
        split_row("ЭКВИВ. В ВАЛ. ПЛ", "RUB17205", 700),
        split_row("СБОР/TAX", "RUB2549", 680),
        split_row("ВСЕГО К ОПЛАТЕ", "RUB21884", 660),
        row("УВЕДОМЛЕНИЕ: текст сохранён", 620),
        row("ПОДПИСЬ ПАССАЖИРА ___________", 580),
    ]))
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


def test_page_wide_group_price_uses_child_index_when_ticket_starts_on_page_three():
    first = {
        "receiptPage": 1,
        "fare": "6400",
        "total": "6400",
        "fareBreakdown": [{"code": "FARE", "amount": "6400"}],
    }
    second = {
        "receiptPage": 3,
        "fare": "6400",
        "total": "6400",
        "fareBreakdown": [{"code": "FARE", "amount": "6400"}],
    }
    before = {"receipts": [first, second]}
    after = {
        "receipts": [
            first,
            {
                **second,
                "fare": "6500",
                "total": "6500",
                "fareBreakdown": [{"code": "FARE", "amount": "6500"}],
            },
        ]
    }

    targets = supplier_pdf._collect_targets(before, after)
    safe = writer_fix._page_wide_target_keys(before, after, targets, supplier_pdf)

    assert safe == {
        "receipt[1].fare",
        "receipt[1].total",
        "receipt[1].fareBreakdown[0]",
    }
    assert {target.page_index for target in targets} == {2}


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


def test_fragmented_supplier_amount_replaces_each_original_glyph_without_overlay():
    source = _fragmented_amount_pdf()
    corrected, report = supplier_pdf.patch_supplier_pdf(
        source,
        {"total": "875.00"},
        {"total": "976.01"},
    )

    assert corrected is not None
    assert report["strategy"] == "content_stream"
    assert report["font_preserved"] is True
    assert report["requested"] == report["applied"] == 1
    reader = PdfReader(BytesIO(corrected), strict=False)
    text = reader.pages[0].extract_text()
    assert "TOTAL 976.01" in text
    assert "875.00" not in text
    assert "/CRMCorrection" not in reader.pages[0]["/Resources"]["/Font"]


def test_nested_mixed_font_amount_uses_one_equivalent_face_without_overlay():
    source = _nested_mixed_font_amount_pdf()
    corrected, report = supplier_pdf.patch_supplier_pdf(
        source,
        {"total": "875.00"},
        {"total": "976.01"},
    )

    assert corrected is not None
    assert report["strategy"] == "content_stream"
    assert report["font_preserved"] is True
    assert report["requested"] == report["applied"] == 1
    reader = PdfReader(BytesIO(corrected), strict=False)
    assert "TOTAL 976.01" in reader.pages[0].extract_text()
    assert "875.00" not in reader.pages[0].extract_text()
    form = reader.pages[0]["/Resources"]["/XObject"]["/Receipt"].get_object()
    assert b"/ArialFull 12 Tf\n<37362e30> Tj" in form.get_data()


def test_fragmented_amount_rejects_missing_subset_digits_and_uses_full_face():
    raw_stream = DecodedStreamObject()
    raw_stream.set_data(
        b"BT /Subset 12 Tf <00310030> Tj /Full 12 Tf <0020> Tj "
        b"/Subset 12 Tf <00380030> Tj /Full 12 Tf <0038002e00300030> Tj ET"
    )
    stream = ContentStream(raw_stream, PdfReader(BytesIO(_simple_supplier_pdf())))
    ascii_map = {code: chr(code) for code in range(32, 127)}
    subset = {
        "kind": "multibyte",
        "encoding": "utf-16-be",
        "char_map": {character: character for character in "018"},
        "inverse": {character: character for character in "018"},
        "base_font": "/ABCDEF+Arial",
    }
    full = {
        "kind": "multibyte",
        "encoding": "utf-16-be",
        "char_map": {chr(code): chr(code) for code in ascii_map},
        "inverse": {chr(code): chr(code) for code in ascii_map},
        "base_font": "/ArialMT",
    }
    target = SimpleNamespace(
        old=Decimal("10808.00"),
        new=Decimal("20308.00"),
        aliases=(),
    )

    assert supplier_pdf._encode_text("20", subset) is None
    replaced = writer_fix._replace_fragmented_text_all(
        stream,
        {"/Subset": subset, "/Full": full},
        target,
        supplier_pdf,
    )

    assert replaced == 1
    assert b"/Full 12 Tf\n<00320030> Tj" in stream.get_data()
    assert b"/Full 12 Tf\n<00330030> Tj" in stream.get_data()


def test_fragmented_amount_uses_observed_regular_face_when_vendor_subset_is_too_narrow():
    raw_stream = DecodedStreamObject()
    raw_stream.set_data(
        b"BT /Regular 12 Tf <00300031003200330034003500360037003800390020002e> Tj "
        b"/Subset 12 Tf [<0031> 0 <0031003700320032>] TJ ET"
    )
    stream = ContentStream(raw_stream, PdfReader(BytesIO(_simple_supplier_pdf())))
    subset = {
        "kind": "multibyte",
        "encoding": "utf-16-be",
        "char_map": {character: character for character in "127"},
        "inverse": {character: character for character in "127"},
        "base_font": "/ABCDEF+Verdana",
    }
    regular = {
        "kind": "multibyte",
        "encoding": "utf-16-be",
        "char_map": {chr(code): chr(code) for code in range(32, 127)},
        "inverse": {},
        "base_font": "/ABCDEF+WixMadeforText-Regular",
    }
    target = SimpleNamespace(
        old=Decimal("11722"),
        new=Decimal("12025"),
        aliases=(),
    )

    replaced = writer_fix._replace_fragmented_text_all(
        stream,
        {"/Subset": subset, "/Regular": regular},
        target,
        supplier_pdf,
    )

    combined, _entries = writer_fix._fragmented_text_entries(
        stream,
        {"/Subset": subset, "/Regular": regular},
        supplier_pdf,
    )
    assert replaced == 1
    assert combined.endswith("12025")
    assert b"/Regular 12 Tf\n[ <0031> 0 <0032003000320035> ] TJ" in stream.get_data()


def test_duplicate_bold_total_updates_its_overprint_reset_for_new_glyph_widths():
    old_text = "4 819,20 ₽"
    codec = {
        "kind": "single-byte",
        "byte_map": {**{code: chr(code) for code in range(32, 127)}, 128: "₽"},
        "inverse": {**{chr(code): code for code in range(32, 127)}, "₽": 128},
        "widths": {
            **{str(digit): 600 for digit in range(10)},
            "1": 300,
            " ": 250,
            ",": 250,
            "₽": 600,
        },
        "default_width": 600,
    }

    def operand(character: str):
        return ByteStringObject(bytes([codec["inverse"][character]]))

    array = ArrayObject(
        [*(operand(character) for character in old_text), FloatObject(4000)]
        + [operand(character) for character in old_text]
    )
    target = SimpleNamespace(
        old=Decimal("4819.20"),
        new=Decimal("5021.31"),
        aliases=(),
    )

    replaced = writer_fix._replace_combined_text_all(
        array,
        codec,
        target,
        "Итого",
        supplier_pdf,
    )

    visible = "".join(
        supplier_pdf._decode_text(item, codec)
        for item in array
        if isinstance(item, ByteStringObject)
    )
    reset = next(float(item) for item in array if not isinstance(item, ByteStringObject))
    assert replaced == 2
    assert visible == "5 021,31 ₽5 021,31 ₽"
    assert reset == 3700


def test_supplier_pdf_closes_only_the_fare_and_leaves_every_other_amount():
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
    # Закрывается ровно графа тарифа: таксы и итог печатаются как были.
    assert report["requested"] == report["applied"] == 1
    text = PdfReader(BytesIO(corrected)).pages[0].extract_text()
    assert "FARE IT" in text
    assert "IT RUB" not in text
    assert "RUB IT" not in text
    assert "TAX 1250 RUB" in text
    assert "TOTAL 25470 RUB" in text


def test_supplier_pdf_closes_published_and_equivalent_air_fares_as_it():
    corrected, report = supplier_pdf.patch_supplier_pdf(
        _two_currency_fare_pdf(),
        {
            "fare": "13110",
            "publishedFare": "138",
            "equivalentFare": "13110",
            "taxes": "3690",
            "total": "16800",
        },
        {
            "fare": "13110",
            "publishedFare": "138",
            "equivalentFare": "13110",
            "taxes": "3690",
            "total": "16800",
            "output": {"priceMode": "it"},
        },
    )

    assert corrected is not None
    assert report["requested"] == report["applied"] == 2
    text = PdfReader(BytesIO(corrected)).pages[0].extract_text()
    assert "FARE IT" in text
    assert "EQUIVALENT FARE PAID IT" in text
    assert "EURIT" not in text
    assert "RUBIT" not in text
    assert "TAX RUB3690" in text
    # Итог — не тариф, его цифры остаются нетронутыми.
    assert "TOTAL RUB16800" in text


def test_raw_it_matches_currency_amount_once_without_overlapping_splices():
    target = supplier_pdf.AmountTarget(
        "fare.it",
        Decimal("17205"),
        "IT",
        ("ТАРИФ",),
    )
    codec = {"kind": "multibyte", "encoding": "utf-16-be"}
    data = "ТАРИФ : RUB17205 ЭКВИВ. ТАРИФ : RUB17205".encode("utf-16-be")

    replacements = writer_fix._find_raw_replacements(
        data,
        target,
        [codec],
        supplier_pdf,
    )

    assert len(replacements) == 2
    assert all(data[start:end].decode("utf-16-be") == "RUB17205" for start, end, _ in replacements)
    assert replacements[0][1] <= replacements[1][0]


def test_cpdf_it_edit_keeps_everything_after_fare_and_page_dimensions():
    source = _cpdf_null_spaced_it_pdf()
    corrected, report = supplier_pdf.patch_supplier_pdf(
        source,
        {"fare": "17205", "taxes": "2549", "total": "21884"},
        {
            "fare": "17205",
            "taxes": "2549",
            "total": "21884",
            "output": {"priceMode": "it"},
        },
    )

    assert corrected is not None
    assert report["strategy"] == "raw_stream"
    assert report["replacements"] == 2
    assert report["stream_repairs"] > 0
    source_page = PdfReader(BytesIO(source), strict=False).pages[0]
    corrected_page = PdfReader(BytesIO(corrected), strict=False).pages[0]
    text = corrected_page.extract_text()
    assert "ТАРИФ IT" in text
    assert "ЭКВИВ. В ВАЛ. ПЛ IT" in text
    assert "IT RUB" not in text
    assert "RUB IT" not in text
    assert "СБОР/TAX RUB2549" in text
    assert "ВСЕГО К ОПЛАТЕ RUB21884" in text
    assert "УВЕДОМЛЕНИЕ: текст сохранён" in text
    assert "ПОДПИСЬ ПАССАЖИРА" in text
    assert corrected_page.mediabox == source_page.mediabox


def test_queued_snapshot_preserves_it_for_parent_and_group_children():
    stored = {
        "fare": "13110",
        "total": "16800",
        "output": {"priceMode": "total"},
        "receipts": [
            {"fare": "13110", "total": "16800", "output": {"priceMode": "total"}},
            {"fare": "7200", "total": "8000", "output": {"priceMode": "total"}},
        ],
    }
    submitted = {
        **stored,
        "output": {"priceMode": "it"},
        "receipts": [
            {"fare": "13110", "total": "16800"},
            {"fare": "7200", "total": "8000"},
        ],
    }

    snapshot = supplier_pdf._request_financial_snapshot(stored, submitted, "parsed")

    assert snapshot["output"]["priceMode"] == "it"
    assert snapshot["receipts"][0]["output"]["priceMode"] == "it"
    assert snapshot["receipts"][1]["output"]["priceMode"] == "it"


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
