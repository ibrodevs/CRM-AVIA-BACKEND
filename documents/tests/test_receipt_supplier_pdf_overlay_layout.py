from __future__ import annotations

from io import BytesIO

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from documents import receipt_supplier_pdf_writer_fix as writer_fix


class _LayoutCharacter:
    def __init__(self, text, x, y, *, font_name="Courier", size=6.0):
        self._text = text
        self.fontname = font_name
        self.size = size
        self.adv = size * 0.6
        self.x0 = x
        self.y0 = y
        self.x1 = x + self.adv
        self.y1 = y + size
        self.matrix = (1, 0, 0, 1, x, y + size * 0.2)

    def get_text(self):
        return self._text


def _layout_line(text, x, y, *, size=6.0):
    characters = []
    cursor = x
    for value in text:
        character = _LayoutCharacter(value, cursor, y, size=size)
        characters.append(character)
        cursor = character.x1
    return {
        "page": 0,
        "text": text,
        "characters": characters,
        "bbox": (x, y, cursor, y + size),
    }


def _source_pdf():
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Courier"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    })
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/OriginalFont"): font_ref}),
    })
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /OriginalFont 6 Tf 400 330 Td (FARE) Tj ET\n"
        b"BT /OriginalFont 6 Tf 520 330 Td (3167.30) Tj ET\n"
        b"BT /OriginalFont 8 Tf 400 300 Td (TOTAL) Tj ET\n"
        b"BT /OriginalFont 8 Tf 510 300 Td (5261.50) Tj ET\n"
        b"BT /OriginalFont 8 Tf 510.3 300 Td (5261.50) Tj ET\n"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_visual_overlay_reuses_supplier_font_and_clears_duplicate_total_first(monkeypatch):
    lines = [
        _layout_line("FARE", 400, 330),
        _layout_line("3167.30", 520, 330),
        _layout_line("TOTAL", 400, 300, size=8),
        _layout_line("5261.50", 510, 300, size=8),
        _layout_line("5261.50", 510.3, 300, size=8),
    ]
    monkeypatch.setattr(writer_fix, "_layout_lines", lambda _content: [lines])

    corrected, report = writer_fix._patch_supplier_pdf_overlay(
        _source_pdf(),
        {"ticketCost": "3167.30", "total": "5261.50"},
        {"ticketCost": "4167.30", "total": "6261.50"},
    )

    assert corrected is not None
    assert report["requested"] == report["applied"] == 2
    assert report["replacements"] == 3
    assert report["font_preserved"] is True

    page = PdfReader(BytesIO(corrected), strict=False).pages[0]
    fonts = page["/Resources"]["/Font"]
    assert "/OriginalFont" in fonts
    assert "/CRMCorrection" not in fonts

    stream = page.get_contents().get_data().decode("latin1")
    overlay = stream[stream.rfind("\nQ\n") + 3 :]
    assert overlay.count(" re f Q") == 3
    assert overlay.count("BT /OriginalFont 6.000 Tf") == 1
    assert overlay.count("BT /OriginalFont 8.000 Tf") == 2
    assert "BT /CRMCorrection" not in overlay
    # Every mask is emitted before replacement text, so duplicate masks cannot
    # erase a freshly drawn total. Vertical padding stays below 0.11 pt.
    assert overlay.rfind(" re f Q") < overlay.find("BT /OriginalFont")
    rectangle_heights = [
        float(line.split()[-4])
        for line in overlay.splitlines()
        if line.endswith(" re f Q")
    ]
    assert sorted(rectangle_heights) == [6.1, 8.1, 8.1]
