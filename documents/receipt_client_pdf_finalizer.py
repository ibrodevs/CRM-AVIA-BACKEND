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


def _valid_issue_date(value: str) -> bool:
    return bool(re.fullmatch(r"\d{2}\.\d{2}\.20\d{2}", _clean(value)))


def _issue_date_from_text(text: str) -> str:
    numeric = re.search(r"(?:Дата (?:выдачи|оформления)|Date of issue)\D{0,30}(\d{2})[./-](\d{2})[./-](20\d{2})", text, re.I)
    if numeric:
        return ".".join(numeric.groups())
    named = re.search(r"(?:Дата (?:выдачи|оформления)|Date of issue)?\D{0,20}(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+(20\d{2})", text, re.I)
    if not named:
        return ""
    months = {name: index for index, name in enumerate((
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    ), 1)}
    return f"{int(named.group(1)):02d}.{months[named.group(2).lower()]:02d}.{named.group(3)}"


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
            issue_date = _clean(str(fields.get("issue_date") or ""))
            ticket_number = re.sub(r"\D", "", str(fields.get("ticket_number") or ""))
            issue_digits = re.sub(r"\D", "", issue_date)
            if not _valid_issue_date(issue_date) or (ticket_number and issue_digits == ticket_number):
                fields["issue_date"] = _issue_date_from_text(text)

            # IT is a tariff display mode, not a number. Supplier layouts put
            # it next to the fare/cost heading, so never manufacture a random
            # numeric amount when the source explicitly uses this marker.
            has_it_fare = bool(re.search(
                r"(?:СТОИМОСТЬ|Тариф|Fare|Итого)[^\f]{0,100}?\bIT\b|\bIT\b[^\f]{0,100}?(?:СТОИМОСТЬ|Тариф|Fare)",
                text,
                re.IGNORECASE,
            ))
            if has_it_fare:
                output = fields.get("output") if isinstance(fields.get("output"), dict) else {}
                output["priceMode"] = "it"
                fields["output"] = output

            passenger_name = re.sub(
                r"\s+(?:ПСП|PASSPORT|PASS)\s*[A-ZА-ЯЁ0-9-]{5,}\s*$",
                "",
                _clean(str(fields.get("passenger_name") or "")),
                flags=re.IGNORECASE,
            ).strip()
            if passenger_name and passenger_name != fields.get("passenger_name"):
                fields["passenger_name"] = passenger_name
                passengers = fields.get("passengers")
                if isinstance(passengers, list) and passengers and isinstance(passengers[0], dict):
                    passengers[0]["name"] = passenger_name
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
            rooms = fields.get("rooms") if isinstance(fields.get("rooms"), list) else []
            has_early = bool(re.search(r"Ранний\s+заезд", text, re.IGNORECASE))
            has_late = bool(re.search(r"Поздний\s+выезд", text, re.IGNORECASE))
            for room in rooms:
                if not isinstance(room, dict):
                    continue
                if has_early and not room.get("earlyCheckIn"):
                    room["earlyCheckIn"] = room.get("checkInTime") or ""
                if has_late and not room.get("lateCheckOut"):
                    room["lateCheckOut"] = room.get("checkOutTime") or ""
            booking = _clean(str(fields.get("hotel_booking_number") or fields.get("hotelBookingNo") or ""))
            if len(booking) > 80 or re.search(r"Аннуляция|Без штрафа|Дополнительно", booking, re.IGNORECASE):
                fields["hotel_booking_number"] = ""
                fields["hotelBookingNo"] = ""

        raw = result.get("raw")
        if isinstance(raw, dict):
            raw["passenger_name"] = fields.get("passenger_name")
            raw["passengers"] = fields.get("passengers", [])
            raw["segments"] = fields.get("segments", [])
            raw["issue_date"] = fields.get("issue_date", "")
            if fields.get("output"):
                raw["output"] = fields["output"]
            if fields.get("hotelTerms"):
                raw["hotelTerms"] = fields["hotelTerms"]
            if fields.get("rooms"):
                raw["rooms"] = fields["rooms"]
        return result

    wrapped._client_pdf_finalizer = True
    services.extract_receipt_fields = wrapped
