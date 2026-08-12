from __future__ import annotations

import re
from io import BytesIO


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _modern_route_codes(text: str) -> list[str]:
    flat = _clean(text)
    match = re.search(
        r"Расчет\s+тарифа/Fare\s+calculation\s+(.+?)\s+Тариф/Fare",
        flat,
        re.IGNORECASE,
    )
    if not match:
        return []
    # Supplier fare calculations often concatenate airport code and fare
    # amount, e.g. ``KZN SU MOW9180SU CSY6800RUB15980END``.  A normal word
    # boundary misses MOW/CSY because digits are also word characters.
    codes = re.findall(r"(?<![A-Z])([A-Z]{3})(?=\s|\d)", match.group(1).upper())
    ignored = {"RUB", "END", "USD", "EUR"}
    return [code for code in codes if code not in ignored]


def _hotel_deposit(lines: list[str]) -> str:
    indexes = [index for index, line in enumerate(lines) if line.lower() == "deposit"]
    if not indexes:
        return ""
    start = indexes[-1] + 1
    if start >= len(lines):
        return ""
    # pdfminer can produce either one combined line
    # ``1000 CNY per room for the night`` or four short lines.  Stop before
    # the next narrative paragraph so that payment notes do not pollute the
    # structured deposit field.
    first = lines[start]
    if re.match(r"^\d+(?:[,.]\d+)?\s+[A-Z]{3}\b", first):
        return first
    parts: list[str] = []
    for line in lines[start : start + 5]:
        if line.upper().startswith("GPS "):
            break
        if re.fullmatch(r"\d+(?:[,.]\d+)?|[A-Z]{3}|per room for the night", line, re.IGNORECASE):
            parts.append(line)
            continue
        break
    return _clean(" ".join(parts))


def install_receipt_client_pdf_finalizer() -> None:
    from documents import services

    if getattr(services.extract_receipt_fields, "_client_pdf_finalizer", False):
        return
    original = services.extract_receipt_fields

    def wrapped(content: bytes, *, mime: str = "", name: str = ""):
        result = original(content, mime=mime, name=name)
        if not (mime == "application/pdf" or content.startswith(b"%PDF")):
            return result
        fields = result.get("fields") if isinstance(result, dict) else None
        if not isinstance(fields, dict):
            return result
        try:
            from pdfminer.high_level import extract_text

            text = extract_text(BytesIO(content)) or ""
        except Exception:
            return result
        if not text:
            return result

        service_kind = str(fields.get("service_kind") or "").lower()
        segments = fields.get("segments") if isinstance(fields.get("segments"), list) else []
        if service_kind in {"avia", "flight", "авиа"} and segments:
            codes = _modern_route_codes(text)
            if len(codes) >= len(segments) + 1:
                for index, segment in enumerate(segments):
                    if not isinstance(segment, dict):
                        continue
                    if not segment.get("fromCode"):
                        segment["fromCode"] = codes[index]
                    if not segment.get("toCode"):
                        segment["toCode"] = codes[index + 1]

        if service_kind in {"hotel", "гостиница", "отель"}:
            terms = fields.get("hotelTerms")
            if not isinstance(terms, dict):
                terms = {}
                fields["hotelTerms"] = terms
            if not terms.get("deposit"):
                lines = [_clean(line) for line in text.splitlines() if _clean(line)]
                deposit = _hotel_deposit(lines)
                if deposit:
                    terms["deposit"] = deposit

        raw = result.get("raw")
        if isinstance(raw, dict):
            raw["segments"] = fields.get("segments", [])
            if fields.get("hotelTerms"):
                raw["hotelTerms"] = fields["hotelTerms"]
        return result

    wrapped._client_pdf_finalizer = True
    services.extract_receipt_fields = wrapped
