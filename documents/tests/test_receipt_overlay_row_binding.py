"""Правка суммы не должна переписывать соседнюю графу бланка.

Подписи граф пересекаются как подстроки: «ТАРИФ» входит в «ЭКВИВ. ТАРИФА»,
«СБОР» — в «СБОР/TAX/FEE/CHARGE». Из-за этого правка тарифа накладывала новую
сумму и на строку «Эквив. тарифа», которая меняться не должна была: в бланке
оказывались два числа одно поверх другого.
"""

from __future__ import annotations

from io import BytesIO

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from documents import receipt_supplier_pdf_patch as supplier_pdf
from documents import receipt_supplier_pdf_writer_fix as writer_fix

# Бланк клиента: подпись слева, сумма справа, строки через 12 пунктов.
ROWS = (
    ("Тариф/Fare", "8878", 700.0),
    ("Эквив. тарифа/Equivalent fare paid", "8878", 688.0),
    ("Сбор/Tax/fee/charge", "5220", 676.0),
    ("Итого/Total", "14098", 664.0),
)


class _Character:
    def __init__(self, text: str, x: float, y: float, size: float = 6.0):
        self._text = text
        self.fontname = "Courier"
        self.size = size
        self.adv = size * 0.6
        self.x0 = x
        self.y0 = y
        self.x1 = x + self.adv
        self.y1 = y + size
        self.matrix = (1, 0, 0, 1, x, y + size * 0.2)

    def get_text(self) -> str:
        return self._text


def _line(text: str, x: float, y: float, size: float = 6.0) -> dict:
    characters = []
    cursor = x
    for value in text:
        character = _Character(value, cursor, y, size)
        characters.append(character)
        cursor = character.x1
    return {"page": 0, "text": text, "characters": characters, "bbox": (x, y, cursor, y + size)}


def _layout(rows=ROWS, *, amount_dy: float = 0.0) -> list[dict]:
    lines: list[dict] = []
    for label, amount, y in rows:
        lines.append(_line(label, 60, y))
        lines.append(_line(amount, 300, y + amount_dy))
    return lines


def _source_pdf() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Courier"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    })
    font_reference = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/OriginalFont"): font_reference}),
    })
    data = "".join(
        f"BT /OriginalFont 6 Tf 60 {y} Td (row) Tj ET\n"
        f"BT /OriginalFont 6 Tf 300 {y} Td ({amount}) Tj ET\n"
        for _label, amount, y in ROWS
    )
    stream = DecodedStreamObject()
    stream.set_data(data.encode("latin1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _overlay_rows(corrected: bytes) -> list[float]:
    """Вертикальные позиции закрашенных прямоугольников, округлённые до строки."""
    stream = PdfReader(BytesIO(corrected), strict=False).pages[0].get_contents().get_data()
    overlay = stream.decode("latin1")
    overlay = overlay[overlay.rfind("\nQ\n") + 3 :]
    # q 1 1 1 rg x0 y0 w h re f Q
    return sorted(
        round(float(line.split()[-6]))
        for line in overlay.splitlines()
        if line.endswith(" re f Q")
    )


BEFORE = {"fare": "8878", "equivalentFare": "8878", "taxes": "5220", "fees": "0", "total": "14098"}


class TestRowOwnership:
    def test_fare_correction_leaves_the_equivalent_fare_row_alone(self, monkeypatch):
        monkeypatch.setattr(writer_fix, "_layout_lines", lambda _content: [_layout()])
        corrected, report = writer_fix._patch_supplier_pdf_overlay(
            _source_pdf(), BEFORE, {**BEFORE, "fare": "100"}
        )

        assert corrected is not None, report
        # Ровно одна правка — на строке «Тариф/Fare» (y = 700), а не две.
        assert report["replacements"] == 1
        assert _overlay_rows(corrected) == [700]

    def test_the_equivalent_fare_row_is_still_patched_by_its_own_field(self, monkeypatch):
        monkeypatch.setattr(writer_fix, "_layout_lines", lambda _content: [_layout()])
        corrected, report = writer_fix._patch_supplier_pdf_overlay(
            _source_pdf(), BEFORE, {**BEFORE, "equivalentFare": "100"}
        )

        assert corrected is not None, report
        assert report["replacements"] == 1
        assert _overlay_rows(corrected) == [688]

    def test_a_fee_correction_does_not_touch_the_tax_row(self):
        # «СБОР» — подстрока «СБОР/TAX/FEE/CHARGE», но строкой владеет такса.
        fees = next(
            target for target in supplier_pdf._collect_targets(
                {**BEFORE, "fees": "300"}, {**BEFORE, "fees": "400"}
            ) if target.key == "fees"
        )
        assert supplier_pdf._target_owns_context(fees, "Сбор/Tax/fee/charge 5220") is False

    def test_a_field_still_owns_a_row_labelled_by_its_more_specific_synonym(self):
        # «Тариф/Fare» — подпись publishedFare, но и общий fare правит эту графу.
        fare = next(
            target for target in supplier_pdf._collect_targets(BEFORE, {**BEFORE, "fare": "100"})
            if target.key == "fare"
        )
        assert supplier_pdf._target_owns_context(fare, "Тариф/Fare 8878") is True


class TestRowProximity:
    def test_an_amount_from_a_neighbouring_row_is_never_claimed(self, monkeypatch):
        """Прежний допуск в 35 пунктов — это пять строк такого бланка.

        В графе «Тариф/Fare» суммы нет, а такое же число напечатано строкой
        ниже и в итоге. Раньше подпись забирала ближайшее число из чужой
        графы; теперь бланк честно уходит на другую стратегию.
        """
        lines = [
            _line("Тариф/Fare", 60, 700),
            _line("Эквив. тарифа/Equivalent fare paid", 60, 688),
            _line("8878", 300, 688),
            _line("Итого/Total", 60, 664),
            _line("8878", 300, 664),
        ]
        monkeypatch.setattr(writer_fix, "_layout_lines", lambda _content: [lines])
        corrected, report = writer_fix._patch_supplier_pdf_overlay(
            _source_pdf(), BEFORE, {**BEFORE, "fare": "100"}
        )
        assert corrected is None
        assert report["unapplied"] == ["fare"]

    def test_an_amount_on_its_own_row_is_applied(self, monkeypatch):
        monkeypatch.setattr(writer_fix, "_layout_lines", lambda _content: [_layout()])
        corrected, report = writer_fix._patch_supplier_pdf_overlay(
            _source_pdf(), BEFORE, {**BEFORE, "total": "15000"}
        )
        assert corrected is not None, report
        assert _overlay_rows(corrected) == [664]
