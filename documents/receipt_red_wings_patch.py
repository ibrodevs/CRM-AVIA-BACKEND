from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from io import BytesIO

from documents.receipt_client_pdf_requirements import _clean, _named_date, _replace_result


_WEEKDAYS = {
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
}


def _red_wings_passenger(value: str) -> str:
    value = _clean(value)
    # Supplier prints Russian/English courtesy titles in the passenger cell.
    # They are not part of the passenger name stored in CRM.
    value = re.sub(r"\s+(?:Г-ЖА|Г-Н|MR|MRS|MS)$", "", value, flags=re.IGNORECASE)
    return _clean(value.replace("/", " "))


def _red_wings_payment_amounts(section: str) -> dict:
    """Read the bilingual Red Wings payment table in its visual column order."""

    if not section or re.search(r"\bIT\b", section):
        return {}
    tokens = []
    pattern = re.compile(
        r"(?P<currency>RUB|EUR|USD|KGS|KZT)\s*"
        r"(?P<amount>\d[\d\s]*(?:[,.]\d{1,2})?)(?P<code>[A-ZА-Я]{2,3})?(?=\s|$)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(section):
        try:
            amount = Decimal(match.group("amount").replace(" ", "").replace(",", "."))
        except (InvalidOperation, ValueError):
            continue
        tokens.append({
            "amount": amount,
            "currency": match.group("currency").upper(),
            "code": (match.group("code") or "").upper(),
        })
    # Published fare, equivalent paid fare, zero or more tax rows, total.
    if len(tokens) < 3:
        return {}
    published, equivalent, total = tokens[0], tokens[1], tokens[-1]
    tax_rows = tokens[2:-1]
    return {
        "published": published,
        "equivalent": equivalent,
        "tax_rows": tax_rows,
        "taxes": sum((row["amount"] for row in tax_rows), Decimal("0")),
        "total": total,
    }


def _route_value(value: str) -> bool:
    value = _clean(value)
    if not value or not re.search(r"[A-Za-zА-Яа-яЁё]", value):
        return False
    if value.lower() in _WEEKDAYS:
        return False
    if re.fullmatch(r"\d{1,2}\s+[А-Яа-яЁё]+\s+20\d{2}", value):
        return False
    if re.fullmatch(r"\d+\s*ч(?:\s*\d+\s*мин)?", value, re.IGNORECASE):
        return False
    if re.search(
        r"Перевозчик|Carrier|Статус|Status|Недействителен|Fare basis|тариф|Brand|"
        r"Рейс выполняет|Flight operated|Багаж|Baggage|Класс|Class|Ticket number|Issued by|Date of issue",
        value,
        re.IGNORECASE,
    ):
        return False
    return True


def _parse_red_wings(text: str) -> dict | None:
    """Parse the TCH bilingual Red Wings e-ticket used by the client.

    The layout differs from the Aeroflot bilingual form in two important ways:
    route/airport names are emitted after both date columns, and the ticket /
    issuer / issue-date values are emitted after all three labels. A generic
    row-by-row parser therefore loses the route and often mislabels the carrier.
    """

    if (
        "Electronic ticket" not in text
        or "Рейс/Flight" not in text
        or not re.search(r"\bРЕД\s+ВИНГС\b", text, re.IGNORECASE)
    ):
        return None

    lines = [_clean(line) for line in text.splitlines() if _clean(line)]
    flat = " ".join(lines)

    passenger_match = re.search(
        r"Name of passenger\s+(.+?)\s+Документ/Document",
        flat,
        re.IGNORECASE,
    )
    document_match = re.search(
        r"Документ/Document\s+([A-ZА-ЯЁ]{1,4}\d{6,14})",
        flat,
        re.IGNORECASE,
    )
    booking_match = re.search(
        r"Данные бронирования\s+Booking ref\s+(.+?)\s+Место выдачи",
        flat,
        re.IGNORECASE,
    )

    # pdfminer outputs the labels first and then the three values in visual
    # column order. Keep that layout explicit instead of assuming the value is
    # immediately adjacent to its label.
    ticket_meta = re.search(
        r"Номер билета\s+Ticket number\s+Выдан от/Issued by\s+Дата выдачи\s+Date of issue\s+"
        r"(?P<ticket>\d{3}\s+\d{10}|\d{13})\s+"
        r"(?P<issuer>.+?)\s+"
        r"(?P<issue>\d{1,2}\s+[А-Яа-яЁё]+\s+20\d{2})\s+Данные бронирования",
        flat,
        re.IGNORECASE,
    )
    if not ticket_meta:
        return None

    details_start = text.find("Дополнительные детали")
    marker = re.search(r"Рейс/Flight", text)
    if marker is None:
        return None
    block = text[marker.end() : details_start if details_start >= 0 else len(text)]
    block_lines = [_clean(line) for line in block.splitlines() if _clean(line)]

    flight_match = next(
        (
            match
            for line in block_lines
            if (match := re.fullmatch(r"([A-Z0-9]{2})\s*[- ]?\s*(\d{2,5})", line, re.IGNORECASE))
        ),
        None,
    )
    times = [line for line in block_lines if re.fullmatch(r"\d{1,2}:\d{2}", line)]
    dates = [line for line in block_lines if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", line)]
    if not flight_match or len(times) < 2 or len(dates) < 2:
        return None

    carrier_index = next(
        (index for index, line in enumerate(block_lines) if line.lower().startswith("перевозчик/carrier:")),
        -1,
    )
    last_date_index = max(
        index for index, line in enumerate(block_lines) if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", line)
    )
    route_end = carrier_index if carrier_index > last_date_index else len(block_lines)
    route_values = [
        line
        for line in block_lines[last_date_index + 1 : route_end]
        if _route_value(line)
    ]
    if len(route_values) < 2:
        return None

    # In this TCH template the text extraction order is:
    # origin city, origin airport, destination city, destination airport.
    if len(route_values) >= 4:
        from_city, from_airport, to_city, to_airport = route_values[:4]
    else:
        from_city, to_city = route_values[:2]
        from_airport = to_airport = ""

    block_flat = "\n".join(block_lines)
    carrier_match = re.search(r"Перевозчик/Carrier:\s*([^\n]+)", block_flat, re.IGNORECASE)
    status_match = re.search(r"Статус/Status:\s*([A-ZА-ЯЁ]+)", block_flat, re.IGNORECASE)
    fare_basis_match = re.search(r"Вид тарифа/Fare basis:\s*([A-Z0-9-]+)", block_flat, re.IGNORECASE)
    brand_match = re.search(r"Бренд/Brand:\s*([^\n]+)", block_flat, re.IGNORECASE)

    duration_match = re.search(
        r"В пути/Travel time:\s*(\d+\s*ч(?:\s*\d+\s*мин)?)",
        flat,
        re.IGNORECASE,
    )
    baggage_match = re.search(
        r"Багаж/Baggage allow:\s*([^\s]+)",
        flat,
        re.IGNORECASE,
    )
    class_match = re.search(r"Класс/Class:\s*([^\s]+)", flat, re.IGNORECASE)
    operated_match = re.search(
        r"Рейс выполняет/Flight operated by:\s*(.+?)\s+Дополнительные детали",
        flat,
        re.IGNORECASE,
    )
    payment_match = re.search(r"Форма оплаты/Form of payment\s+([^\s]+)", flat, re.IGNORECASE)

    cabin = booking_class = ""
    if class_match:
        class_value = _clean(class_match.group(1))
        if "/" in class_value:
            cabin, booking_class = class_value.rsplit("/", 1)
        else:
            cabin = class_value

    passenger = _red_wings_passenger(passenger_match.group(1)) if passenger_match else ""
    document_number = document_match.group(1) if document_match else ""
    ticket_number = ticket_meta.group("ticket")
    issuer = _clean(ticket_meta.group("issuer"))
    issue_date = _named_date(ticket_meta.group("issue"))
    booking_reference = _clean(booking_match.group(1)) if booking_match else ""
    carrier = _clean(carrier_match.group(1)) if carrier_match else issuer
    operated_by = _clean(operated_match.group(1)) if operated_match else carrier
    baggage = _clean(baggage_match.group(1)) if baggage_match else ""
    fare_basis = fare_basis_match.group(1) if fare_basis_match else ""
    status = status_match.group(1) if status_match else ""
    brand = _clean(brand_match.group(1)) if brand_match else ""
    duration = _clean(duration_match.group(1)) if duration_match else ""

    payment_section = ""
    payment_start = text.find("Сведения об оплате")
    notice_start = text.find("Уведомление")
    if payment_start >= 0:
        payment_section = text[payment_start : notice_start if notice_start > payment_start else len(text)]
    price_is_it = bool(re.search(r"\bIT\b", payment_section))
    payment_amounts = _red_wings_payment_amounts(payment_section)
    published_fare = payment_amounts.get("published", {})
    equivalent_fare = payment_amounts.get("equivalent", {})
    tax_rows = payment_amounts.get("tax_rows", [])
    payment_total = payment_amounts.get("total", {})

    segment = {
        "from": from_city,
        "fromCode": "",
        "fromAddress": from_airport,
        "to": to_city,
        "toCode": "",
        "toAddress": to_airport,
        "date": dates[0],
        "endDate": dates[1],
        "dep": times[0],
        "arr": times[1],
        "duration": duration,
        "flightNo": flight_match.group(1).upper() + flight_match.group(2),
        "carrier": carrier,
        "operatedBy": operated_by,
        "cls": booking_class,
        "status": status,
        "fareBasis": fare_basis,
        "cabin": cabin,
        "baggage": baggage,
        "brand": brand,
        "dir": "out",
    }

    # The supplier explicitly prints confidential IT pricing and no numeric
    # amount/currency. Keep those values unknown instead of turning IT into 0.
    return {
        "issuer": issuer,
        "carrier": carrier,
        "passenger_name": passenger,
        "passengers": [
            {
                "name": passenger,
                "dob": "",
                "document": document_number,
                "ticketNo": ticket_number,
                "ref": booking_reference,
            }
        ] if passenger else [],
        "reference": booking_reference,
        "ticket_number": ticket_number,
        "document_number": document_number,
        "date_of_birth": "",
        "issue_date": issue_date,
        "booking_class": booking_class,
        "booking_status": status,
        "fare_basis": fare_basis,
        "baggage": baggage,
        "hand_baggage": "",
        "fare": equivalent_fare.get("amount"),
        "publishedFare": published_fare.get("amount"),
        "publishedFareCurrency": published_fare.get("currency", ""),
        "equivalentFare": equivalent_fare.get("amount"),
        "equivalentFareCurrency": equivalent_fare.get("currency", ""),
        "taxes": payment_amounts.get("taxes"),
        "fees": Decimal("0") if payment_amounts else None,
        "total": payment_total.get("amount"),
        "originalTotal": payment_total.get("amount"),
        "currency": payment_total.get("currency") or equivalent_fare.get("currency", ""),
        "segments": [segment],
        "fare_breakdown": [],
        "tax_breakdown": [
            {
                "code": row.get("code") or "TAX",
                "label": "Сбор перевозчика",
                "amount": row["amount"],
                "currency": row["currency"],
            }
            for row in tax_rows
        ],
        "fee_breakdown": [],
        "service_kind": "avia",
        "service_type": "Авиа",
        "trip_type": "oneway",
        "brand": brand,
        "payment_method": _clean(payment_match.group(1)) if payment_match else "",
        "output": {"priceMode": "it" if price_is_it else "total"},
    }


def install_receipt_red_wings_patch() -> None:
    from documents import services

    if getattr(services.extract_receipt_fields, "_red_wings_patch", False):
        return
    original = services.extract_receipt_fields

    def wrapped(content: bytes, *, mime: str = "", name: str = ""):
        result = original(content, mime=mime, name=name)
        if not (mime == "application/pdf" or content.startswith(b"%PDF")):
            return result
        try:
            from pdfminer.high_level import extract_text

            text = extract_text(BytesIO(content)) or ""
        except Exception:
            return result
        parsed = _parse_red_wings(text)
        if not parsed:
            return result
        return _replace_result(
            result,
            parsed,
            "Маршрут-квитанция Red Wings распознана полностью: пассажир, документ, билет, бронирование, маршрут, аэропорты, рейс, даты/время, класс, багаж и тарифные условия сохранены; стоимость IT не подменяется числом.",
            confidence="0.998",
        )

    wrapped._red_wings_patch = True
    services.extract_receipt_fields = wrapped
