"""Закрытие тарифа на IT в оригинале поставщика.

Бланк печатает тариф дважды: опубликованный в валюте тарифа и эквивалент в
валюте расчёта. Закрываются обе графы — и только они. Итог, таксы, сборы и
строка расчёта остаются такими, как их напечатал поставщик.
"""

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

# Реальный блок «Сведения об оплате» из присланного клиентом бланка.
CLIENT_PAYMENT_BLOCK = [
    "Form of payment INV",
    "Fare calculation GOJ WZ TBS157.68NUC157.68END ROE0.875142",
    "Fare EUR138.00",
    "Equivalent fare paid RUB13110",
    "Tax/fee/charge RUB1425YQ RUB430SA RUB1835TU",
    "Total RUB16800",
]

BEFORE = {
    "fare": "13110",
    "publishedFare": "138.00",
    "equivalentFare": "13110",
    "taxes": "3690",
    "fees": "0",
    "total": "16800",
    "currency": "RUB",
    "taxBreakdown": [
        {"code": "YQ", "amount": "1425"},
        {"code": "SA", "amount": "430"},
        {"code": "TU", "amount": "1835"},
    ],
}


def _pdf(lines) -> bytes:
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
    body = b"BT /F1 10 Tf 72 760 Td "
    for line in lines:
        body += b"(" + line.encode("latin-1") + b") Tj 0 -18 Td "
    body += b"ET"
    stream = DecodedStreamObject()
    stream.set_data(body)
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _corrected_text(lines, before=None):
    before = before or BEFORE
    after = {**before, "output": {"mode": "original", "priceMode": "it"}}
    corrected, report = supplier_pdf.patch_supplier_pdf(_pdf(lines), before, after)
    assert corrected is not None, report
    return PdfReader(BytesIO(corrected)).pages[0].extract_text(), report


class TestClientBlank:
    def test_both_printed_fare_rows_are_closed(self):
        text, report = _corrected_text(CLIENT_PAYMENT_BLOCK)
        assert "Fare IT" in text
        assert "Equivalent fare paid IT" in text
        assert "EUR138.00" not in text
        assert "RUB13110" not in text

    def test_every_other_amount_stays_exactly_as_printed(self):
        text, _ = _corrected_text(CLIENT_PAYMENT_BLOCK)
        # Закрывается только тариф: итог, таксы и расчёт не трогаем.
        assert "Total RUB16800" in text
        assert "RUB1425YQ" in text
        assert "RUB430SA" in text
        assert "RUB1835TU" in text
        assert "Fare calculation GOJ WZ TBS157.68NUC157.68END ROE0.875142" in text
        assert "Form of payment INV" in text

    def test_a_blank_without_a_fare_row_falls_back_and_records_it(self):
        # Если графы тарифа в бланке нет, замену делать негде: срабатывает
        # запасная стратегия, а в отчёте видно, что строки тарифа не закрылись
        # на месте — по этому признаку бланк уходит на ручную проверку.
        source = _pdf(["Tax/fee/charge RUB1425YQ", "Total RUB16800"])
        after = {**BEFORE, "output": {"mode": "original", "priceMode": "it"}}
        corrected, report = supplier_pdf.patch_supplier_pdf(source, BEFORE, after)
        assert report["fallback"] is True
        assert set(report["raw_unapplied"]) == {"publishedFare.it", "equivalentFare.it"}
        text = PdfReader(BytesIO(corrected)).pages[0].extract_text()
        # Исходная страница при этом не переписана: чужие цифры не тронуты.
        assert "Tax/fee/charge RUB1425YQ" in text
        assert "Total RUB16800" in text


class TestTargets:
    def test_only_the_fare_rows_become_targets(self):
        after = {**BEFORE, "output": {"priceMode": "it"}}
        keys = {target.key for target in supplier_pdf._collect_targets(BEFORE, after)}
        assert keys == {"publishedFare.it", "equivalentFare.it"}

    def test_a_repeated_amount_does_not_create_a_second_target(self):
        # fare и equivalentFare здесь равны: вторая цель на ту же сумму делала
        # правку неприменимой целиком, а стратегии и так заменяют все вхождения.
        fields = dict(supplier_pdf._it_closed_fields(BEFORE))
        assert fields == {"publishedFare": True, "equivalentFare": True}

    def test_a_fare_breakdown_is_left_alone(self):
        before = {**BEFORE, "fareBreakdown": [{"code": "BASE", "amount": "13110"}]}
        after = {**before, "output": {"priceMode": "it"}}
        keys = {target.key for target in supplier_pdf._collect_targets(before, after)}
        assert not any(key.startswith("fareBreakdown") for key in keys)

    def test_nothing_is_closed_without_it_mode(self):
        after = {**BEFORE, "output": {"priceMode": "total"}}
        assert supplier_pdf._collect_targets(BEFORE, after) == []


class TestSingleFareRow:
    """Бланк с одной графой тарифа: закрывается она и ничего кроме."""

    def test_only_the_fare_row_changes(self):
        lines = ["Fare RUB13110", "Tax RUB3690", "Total RUB16800"]
        before = {"fare": "13110", "taxes": "3690", "total": "16800", "currency": "RUB"}
        text, report = _corrected_text(lines, before)
        assert "Fare IT" in text
        assert "Tax RUB3690" in text
        assert "Total RUB16800" in text
        assert report["requested"] == report["applied"] == 1
