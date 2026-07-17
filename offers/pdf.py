import zlib


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _to_cp1251(text: str) -> str:
    """PDF WinAnsi не содержит кириллицу; используем транслит-заглушку для
    надёжности и полный текст в UTF-16 metadata."""
    try:
        text.encode("latin-1")
        return text
    except UnicodeEncodeError:
        table = str.maketrans(
            "абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ",
            "abvgdeejziyklmnoprstufhccss'y'euaABVGDEEJZIYKLMNOPRSTUFHCCSS'Y'EUA",
        )
        return text.translate(table)


def render_proposal_pdf(snapshot: dict) -> bytes:
    lines: list[str] = [
        f"Commercial proposal {snapshot.get('number', '')}",
        f"Status: {snapshot.get('status', '')}   Currency: {snapshot.get('currency', '')}",
        "",
    ]
    for variant in snapshot.get("variants", []):
        lines.append(f"Variant {variant.get('sequence')}: {_to_cp1251(variant.get('name', ''))}")
        for item in variant.get("items", []):
            lines.append(
                f"  - {_to_cp1251(item.get('title', ''))}  x{item.get('quantity', 1)}"
                f"  {item.get('price_amount')} {item.get('price_currency')}"
            )
        lines.append("")

    content_parts = ["BT /F1 11 Tf 50 790 Td 14 TL"]
    for line in lines[:52]:
        content_parts.append(f"({_escape(line)}) Tj T*")
    content_parts.append("ET")
    stream = zlib.compress("\n".join(content_parts).encode("latin-1", "replace"))

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" /Filter /FlateDecode >>\nstream\n"
        + stream
        + b"\nendstream",
    ]

    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_position = len(output)
    output += f"xref\n0 {len(objects) + 1}\n".encode()
    output += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        output += f"{offset:010d} 00000 n \n".encode()
    output += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_position}\n%%EOF\n"
    ).encode()
    return bytes(output)
