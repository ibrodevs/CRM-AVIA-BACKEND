from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from documents.receipt_parser_patch_safe import _json_safe

RU_MONTHS = {
    "ЯНВ": 1,
    "ФЕВ": 2,
    "МАР": 3,
    "АПР": 4,
    "МАЙ": 5,
    "МАЯ": 5,
    "ИЮН": 6,
    "ИЮЛ": 7,
    "АВГ": 8,
    "СЕН": 9,
    "ОКТ": 10,
    "НОЯ": 11,
    "ДЕК": 12,
}
RU_MONTH_NAMES = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}


def _decimal(value) -> Decimal:
    try:
        return Decimal(re.sub(r"\s+", "", str(value or "0")).replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip(" \t\r\n,;")


def _person(value: str) -> str:
    value = re.sub(r"\s+(?:MR|MRS|MS)$", "", _clean(value), flags=re.IGNORECASE)
    return _clean(value.replace("/", " "))


def _time(value: str) -> str:
    digits = re.sub(r"\D", "", value or "").zfill(4)
    return f"{digits[:2]}:{digits[2:4]}" if len(digits) >= 4 else ""


def _compact_date(value: str, year: int) -> str:
    match = re.fullmatch(r"(\d{1,2})([А-ЯЁ]{3})(\d{2,4})?", (value or "").upper())
    if not match:
        return ""
    day, month_name, parsed_year = match.groups()
    month = RU_MONTHS.get(month_name.replace("Ё", "Е"))
    if not month:
        return ""
    if parsed_year:
        full_year = int(parsed_year)
        if full_year < 100:
            full_year += 2000
    else:
        full_year = year
    return f"{int(day):02d}.{month:02d}.{full_year:04d}"


def _named_date(value: str) -> str:
    match = re.fullmatch(r"(\d{1,2})\s+([А-Яа-яЁё]+)\s+(20\d{2})", _clean(value))
    if not match:
        return ""
    month = RU_MONTH_NAMES.get(match.group(2).lower())
    if not month:
        return ""
    return f"{int(match.group(1)):02d}.{month:02d}.{match.group(3)}"


def _route_code(value: str) -> str:
    match = re.match(r"([A-Z]{3})", value or "")
    return match.group(1) if match else ""


def _replace_result(result: dict, parsed: dict, warning: str, confidence: str = "0.995") -> dict:
    result["fields"] = parsed
    raw = result.setdefault("raw", {})
    if not isinstance(raw, dict):
        raw = {}
        result["raw"] = raw
    raw.update(_json_safe(parsed))
    result["status"] = "parsed"
    result["confidence"] = Decimal(confidence)
    result["warnings"] = [warning]
    return result


def _russian_short_date(day: str, month_name: str, year: str) -> str:
    month = RU_MONTHS.get((month_name or "").upper().replace(".", "").replace("Ё", "Е"))
    if not month:
        return ""
    return f"{int(day):02d}.{month:02d}.{int(year):04d}"


def _parse_russian_aeroflot_page(text: str) -> dict | None:
    """Parse the current Russian-only Aeroflot itinerary layout.

    One supplier PDF may contain several itinerary pages, one per passenger.
    Parsing the pages separately is important: merging their text used to lose
    the second passenger and all six segment attributes requested by the client.
    """
    if "Маршрутная квитанция электронного билета" not in text or "№ эл.билета" not in text:
        return None
    flat = _clean(text)
    issue_match = re.search(r"(\d{1,2}\s+[А-Яа-яЁё]+\s+20\d{2})", text)
    passenger_match = re.search(
        r"Маршрутная квитанция электронного билета\s+([A-Z][A-Z '-]{4,80}?)\s+Документ:",
        flat,
    )
    document_match = re.search(r"Документ:\s*(\d{6,16})", flat)
    ticket_match = re.search(r"№\s*эл\.билета:\s*(\d{13})", flat, re.IGNORECASE)
    reference_match = re.search(r"Код бронирования\*?\s*([A-Z0-9]{5,8})\b", flat, re.IGNORECASE)
    route_match = re.search(
        r"Код бронирования\*?\s*[A-Z0-9]{5,8}\s+(.+?)\s+(.+?)\s+Рейс:\s*([A-Z]{2})\s*(\d{2,5})",
        flat,
        re.IGNORECASE,
    )
    dep_match = re.search(
        r"(\d{1,2})\s+([А-Яа-яЁё]{3})\.?\s+(20\d{2})\s+(\d{2}:\d{2})\s+([A-Z]{3})(?:\s+([A-Z0-9]))?",
        flat,
        re.IGNORECASE,
    )
    arr_match = re.search(
        r"(\d{1,2})\s+([А-Яа-яЁё]{3})\.?\s+(20\d{2})\s+Перевозчик:.+?\b([A-Z]{3})\s+(\d{2}:\d{2})",
        flat,
        re.IGNORECASE,
    )
    carrier_match = re.search(r"Перевозчик:\s*([^*]+?)\*", flat, re.IGNORECASE)
    class_match = re.search(r"Класс:\s*([^/]+?)\s*/\s*([A-Z0-9]+)", flat, re.IGNORECASE)
    fare_basis_match = re.search(r"Вид тарифа:\s*([A-Z0-9-]+)", flat, re.IGNORECASE)
    status_match = re.search(r"Статус:\s*([^\n]+?)(?=\s+Провоз багажа:)", flat, re.IGNORECASE)
    baggage_match = re.search(r"Провоз багажа:\s*(.+?)(?=\s+Посадка заканчивается)", flat, re.IGNORECASE)
    fare_match = re.search(r"Тариф\s+RUB\s*([\d\s]+(?:[,.]\d{1,2})?)", flat, re.IGNORECASE)
    total_match = re.search(r"Итого по тарифу/сборам\s*([\d\s]+(?:[,.]\d{1,2})?)\s*RUB", flat, re.IGNORECASE)
    if not all((passenger_match, ticket_match, route_match, dep_match, arr_match)):
        return None

    fare = _decimal(fare_match.group(1)) if fare_match else Decimal("0")
    total = _decimal(total_match.group(1)) if total_match else fare
    cabin = _clean(class_match.group(1)) if class_match else ""
    booking_class = class_match.group(2).upper() if class_match else ""
    segment = {
        "from": _clean(route_match.group(1)),
        "fromCode": dep_match.group(5).upper() + (f" {dep_match.group(6).upper()}" if dep_match.group(6) else ""),
        "to": _clean(route_match.group(2)),
        "toCode": arr_match.group(4).upper(),
        "date": _russian_short_date(*dep_match.groups()[:3]),
        "endDate": _russian_short_date(*arr_match.groups()[:3]),
        "dep": dep_match.group(4),
        "arr": arr_match.group(5),
        "flightNo": route_match.group(3).upper() + route_match.group(4),
        "carrier": _clean(carrier_match.group(1)) if carrier_match else "",
        "cls": booking_class,
        "status": _clean(status_match.group(1)) if status_match else "",
        "fareBasis": fare_basis_match.group(1).upper() if fare_basis_match else "",
        "cabin": cabin,
        "baggage": _clean(baggage_match.group(1)) if baggage_match else "",
        "dir": "out",
    }
    passenger = _person(passenger_match.group(1))
    ticket = ticket_match.group(1)
    document = document_match.group(1) if document_match else ""
    issue_date = _named_date(issue_match.group(1)) if issue_match else ""
    reference = reference_match.group(1).upper() if reference_match else ""
    return {
        "issuer": "АЭРОФЛОТ",
        "passenger_name": passenger,
        "passengers": [{"name": passenger, "dob": "", "document": document, "ticketNo": ticket}],
        "reference": reference,
        "ticket_number": ticket,
        "document_number": document,
        "date_of_birth": "",
        "issue_date": issue_date,
        "booking_class": booking_class,
        "booking_status": segment["status"],
        "fare_basis": segment["fareBasis"],
        "baggage": segment["baggage"],
        "hand_baggage": "",
        "fare": fare,
        "taxes": Decimal("0"),
        "fees": total - fare if total >= fare else Decimal("0"),
        "total": total,
        "originalTotal": total,
        "currency": "RUB",
        "segments": [segment],
        "fare_breakdown": [{"code": "FARE", "label": "Тариф", "amount": str(fare), "currency": "RUB"}],
        "tax_breakdown": [],
        "fee_breakdown": [],
        "service_kind": "avia",
        "service_type": "Авиа",
        "trip_type": "oneway",
        "output": {"priceMode": "total"},
    }


def _parse_russian_aeroflot_group(text: str) -> dict | None:
    pages = [page for page in text.split("\f") if "Маршрутная квитанция электронного билета" in page]
    receipts = [parsed for page in pages if (parsed := _parse_russian_aeroflot_page(page))]
    if not receipts:
        return None
    parent = dict(receipts[0])
    if len(receipts) == 1:
        return parent
    parent["passengers"] = [passenger for receipt in receipts for passenger in receipt["passengers"]]
    parent["passenger_name"] = ", ".join(receipt["passenger_name"] for receipt in receipts)
    parent["ticket_number"] = ", ".join(receipt["ticket_number"] for receipt in receipts)
    for key in ("fare", "taxes", "fees", "total", "originalTotal"):
        parent[key] = sum((_decimal(receipt.get(key)) for receipt in receipts), Decimal("0"))
    children = []
    for index, receipt in enumerate(receipts, 1):
        child = dict(receipt)
        child.update({"receiptIndex": index, "receiptPage": index, "blankId": receipt["ticket_number"], "recognitionPending": False})
        children.append(child)
    parent.update({
        "receipts": children,
        "receipt_items": children,
        "receipt_count": len(children),
        "recognitionPending": False,
    })
    return parent


def _old_aeroflot_routes(lines: list[str]) -> list[tuple[str, str, str, str, str]]:
    try:
        start = next(index for index, line in enumerate(lines) if "МАРШРУТ/ПЕРЕВОЗЧИК" in line)
    except StopIteration:
        return []
    end = next(
        (index for index in range(start + 1, len(lines)) if "ПЕРЕДАТ" in lines[index]),
        len(lines),
    )
    route_lines = lines[start + 1 : end]
    flight_header = next(
        (index for index, line in enumerate(route_lines) if line == "РЕЙС" or line.startswith("РЕЙС КЛАСС")),
        len(route_lines),
    )
    geography = route_lines[:flight_header]
    origins: list[tuple[int, str, str, str]] = []
    code_pattern = re.compile(r"([A-Z]{3})(?:\s+([A-Z0-9]))?\s*/\s*(.+)")

    def city_before(index: int, lower_bound: int = -1) -> str:
        parts: list[str] = []
        cursor = index - 1
        while cursor > lower_bound and len(parts) < 2:
            current = geography[cursor]
            if re.match(r"^[A-Z]{3}(?:\s+[A-Z0-9])?(?:\s*/|$)", current):
                break
            parts.append(current)
            cursor -= 1
        return _clean(" ".join(reversed(parts)))

    for index, line in enumerate(geography):
        match = code_pattern.fullmatch(line)
        if not match:
            continue
        code = match.group(1) + (f" {match.group(2)}" if match.group(2) else "")
        origins.append((index, code, city_before(index), _clean(match.group(3))))

    routes: list[tuple[str, str, str, str, str]] = []
    for route_index, (index, from_code, from_city, carrier) in enumerate(origins):
        next_origin_index = origins[route_index + 1][0] if route_index + 1 < len(origins) else len(geography)
        destination: tuple[str, str] | None = None
        for cursor in range(index + 1, next_origin_index):
            match = re.fullmatch(r"([A-Z]{3})(?:\s+([A-Z0-9]))?", geography[cursor])
            if not match:
                continue
            to_code = match.group(1) + (f" {match.group(2)}" if match.group(2) else "")
            destination = (to_code, city_before(cursor, index))
        if destination is None and route_index + 1 < len(origins):
            destination = (origins[route_index + 1][1], origins[route_index + 1][2])
        if destination is None:
            for cursor in range(index + 1, len(geography)):
                match = re.fullmatch(r"([A-Z]{3})(?:\s+([A-Z0-9]))?", geography[cursor])
                if match:
                    to_code = match.group(1) + (f" {match.group(2)}" if match.group(2) else "")
                    destination = (to_code, city_before(cursor, index))
                    break
        if destination:
            routes.append((from_code, from_city, carrier, destination[0], destination[1]))
    return routes


def _old_aeroflot_flights(lines: list[str]) -> list[tuple[str, str]]:
    flights: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        direct = re.fullmatch(r"([A-Z]{2})-(\d{2,5})(?:\s+([A-Z]))?", lines[index])
        if direct:
            flights.append((direct.group(1) + direct.group(2), direct.group(3) or ""))
            index += 1
            continue
        split = re.fullmatch(r"([A-Z]{2})-", lines[index])
        if split and index + 1 < len(lines) and re.fullmatch(r"\d{2,5}", lines[index + 1]):
            booking_class = (
                lines[index + 2]
                if index + 2 < len(lines) and re.fullmatch(r"[A-Z]", lines[index + 2])
                else ""
            )
            flights.append((split.group(1) + lines[index + 1], booking_class))
            index += 3
            continue
        index += 1
    return flights


def _parse_old_aeroflot(text: str) -> dict | None:
    if (
        "Электронный билет (маршрут/квитанция для пассажира)" not in text
        or "МАРШРУТ/ПЕРЕВОЗЧИК" not in text
    ):
        return None
    lines = [_clean(line) for line in text.splitlines() if _clean(line)]
    flat = " ".join(lines)
    passenger_match = re.search(r"ФАМИЛИЯ\s*:\s*(.+?)\s+ОТПРВ/НАЗН", flat, re.IGNORECASE)
    ticket_match = re.search(
        r"НОМЕР БИЛЕТА.*?:\s*АЭРОФЛОТ\s*:\s*(\d{3}\s+\d{10})",
        flat,
        re.IGNORECASE,
    )
    if not ticket_match:
        ticket_match = re.search(r"\b(555\s+\d{10})\b", flat)
    document_match = re.search(
        r"ОТПРВ/НАЗН\s*:\s*[A-Z]{6}\s+([A-ZА-ЯЁНПCС]{1,4}\d{6,12})",
        flat,
        re.IGNORECASE,
    )
    booking_match = re.search(r"КОД БРОНИРОВАНИЯ.*?:\s*([A-Z0-9]{5,12})", flat, re.IGNORECASE)
    issue_match = re.search(r"ДАТА\s*:\s*(\d{1,2}[А-ЯЁ]{3}\d{2,4})", flat, re.IGNORECASE)
    issue_raw = issue_match.group(1).upper() if issue_match else ""
    issue_year = 2000 + int(issue_raw[-2:]) if re.search(r"\d{2}$", issue_raw) else datetime.now().year

    routes = _old_aeroflot_routes(lines)
    flights = _old_aeroflot_flights(lines)
    if not flights or len(routes) < len(flights):
        return None

    schedules = []
    for line in lines:
        match = re.fullmatch(r"(\d{1,2}[А-ЯЁ]{3}(?:\d{2,4})?)\s+(\d{3,4})", line.upper())
        if match:
            schedules.append((match.group(1), match.group(2)))

    arrivals: list[str] = []
    try:
        route_start = next(index for index, line in enumerate(lines) if "МАРШРУТ/ПЕРЕВОЗЧИК" in line)
    except StopIteration:
        route_start = 0
    notice_index = next(
        (index for index in range(route_start, len(lines)) if lines[index].startswith("УВЕДОМЛЕНИЕ")),
        len(lines),
    )
    for index in range(route_start, notice_index):
        line = lines[index]
        if not re.fullmatch(r"\d{3,4}", line):
            continue
        if index > 0 and re.fullmatch(r"[A-Z]{2}-", lines[index - 1]):
            continue
        arrivals.append(line)

    statuses = [line for line in lines if line.upper() == "OK"]
    fare_bases: list[str] = []
    cabins: list[str] = []
    for index, line in enumerate(lines):
        if line.upper() not in {"ECONOMY", "BUSINESS"}:
            continue
        cabins.append(line.upper())
        previous = lines[index - 1] if index else ""
        if re.fullmatch(r"[A-Z0-9-]{2,20}", previous) and previous.upper() != "OK":
            fare_bases.append(previous.upper())
    baggage = [
        re.sub(r"\s+", "", line.upper())
        for line in lines
        if re.fullmatch(r"\d+\s*(?:PC|KG|КМ)", line, re.IGNORECASE)
    ]

    segments: list[dict] = []
    for index, (flight_no, booking_class) in enumerate(flights):
        from_code, from_city, carrier, to_code, to_city = routes[index]
        schedule = schedules[index] if index < len(schedules) else ("", "")
        segment = {
            "from": from_city,
            "fromCode": from_code,
            "to": to_city,
            "toCode": to_code,
            "date": _compact_date(schedule[0], issue_year),
            "dep": _time(schedule[1]),
            "arr": _time(arrivals[index]) if index < len(arrivals) else "",
            "flightNo": flight_no,
            "carrier": carrier,
            "cls": booking_class,
            "status": statuses[index] if index < len(statuses) else "",
            "fareBasis": fare_bases[index] if index < len(fare_bases) else "",
            "cabin": cabins[index] if index < len(cabins) else "",
            "baggage": baggage[index] if index < len(baggage) else "",
            "dir": "out" if index == 0 else "seg",
        }
        if index and _route_code(to_code) == _route_code(routes[0][0]):
            segment["dir"] = "back"
        segments.append(segment)

    calculation_start = flat.find("РАСЧЕТ ТАРИФА:")
    calculation_text = flat[calculation_start:] if calculation_start >= 0 else flat
    amounts = [
        _decimal(value)
        for value in re.findall(r":\s*RUB\s*(-?\d[\d ]*(?:[,.]\d{1,2})?)", calculation_text, re.IGNORECASE)
    ]
    fare = amounts[0] if len(amounts) > 0 else Decimal("0")
    taxes = amounts[1] if len(amounts) > 1 else Decimal("0")
    ticket_total = amounts[2] if len(amounts) > 2 else fare + taxes
    sa_fee = amounts[3] if len(amounts) > 3 else Decimal("0")
    asb_fee = amounts[4] if len(amounts) > 4 else Decimal("0")
    payable = amounts[5] if len(amounts) > 5 else ticket_total + sa_fee + asb_fee
    fees = sa_fee + asb_fee
    total = payable if payable > 0 else ticket_total + fees

    # Some agency-issued airline forms print a single human-readable
    # ``СТОИМОСТЬ`` amount instead of the older ``: RUB...`` calculation
    # columns.  Treat it as the complete ticket total; ASB/SA values below it
    # are already included and must not be added for a second time.
    printed_cost = re.search(
        r"СТОИМОСТЬ\s*:\s*(?:В ТОМ ЧИСЛЕ[^\n]*\s*){0,4}"
        r"(\d[\d\s\u00a0]*(?:[,.]\d{1,2})?)\s*РУБ",
        text,
        re.IGNORECASE,
    )
    if total <= 0 and printed_cost:
        total = _decimal(printed_cost.group(1))
        fare = total
        taxes = Decimal("0")
        fees = Decimal("0")
        ticket_total = total
        sa_fee = Decimal("0")
        asb_fee = Decimal("0")

    it_fare = bool(re.search(r"СТОИМОСТЬ\s*:\s*(?:В ТОМ ЧИСЛЕ[^\n]*\s*){0,4}IT\b", text, re.IGNORECASE))

    tax_breakdown = []
    for code, value in re.findall(r"\b([A-Z]{2,3})(-?\d+(?:[,.]\d{1,2})?)RUB\b", flat):
        tax_breakdown.append(
            {"code": code.upper(), "label": code.upper(), "amount": str(_decimal(value)), "currency": "RUB"}
        )
    fee_breakdown = [
        {"code": "SA", "label": "Сбор СА", "amount": str(sa_fee), "currency": "RUB"},
        {"code": "ASB", "label": "Сбор АСБ", "amount": str(asb_fee), "currency": "RUB"},
    ]
    passenger = _person(passenger_match.group(1)) if passenger_match else ""
    ticket_number = ticket_match.group(1) if ticket_match else ""
    document_number = document_match.group(1) if document_match else ""
    booking_reference = booking_match.group(1) if booking_match else ""
    booking_classes = list(dict.fromkeys(value for _, value in flights if value))
    fare_basis = list(dict.fromkeys(value for value in fare_bases if value))
    baggage_values = list(dict.fromkeys(value for value in baggage if value))
    roundtrip = bool(
        len(segments) > 1 and _route_code(segments[0]["fromCode"]) == _route_code(segments[-1]["toCode"])
    )
    return {
        "issuer": "АЭРОФЛОТ",
        "passenger_name": passenger,
        "passengers": [{
            "name": passenger,
            "dob": "",
            "document": document_number,
            "ticketNo": ticket_number,
        }],
        "reference": booking_reference,
        "ticket_number": ticket_number,
        "document_number": document_number,
        "date_of_birth": "",
        "issue_date": _compact_date(issue_raw, issue_year),
        "booking_class": " / ".join(booking_classes),
        "booking_status": " / ".join(dict.fromkeys(statuses)),
        "fare_basis": " / ".join(fare_basis),
        "baggage": " / ".join(baggage_values),
        "hand_baggage": "",
        "fare": fare,
        "taxes": taxes,
        "fees": fees,
        "total": total,
        "originalTotal": total,
        "currency": "RUB",
        "segments": segments,
        "fare_breakdown": [{"code": "FARE", "label": "Тариф", "amount": str(fare), "currency": "RUB"}],
        "tax_breakdown": tax_breakdown,
        "fee_breakdown": fee_breakdown,
        "service_kind": "avia",
        "service_type": "Авиа",
        "trip_type": "roundtrip" if roundtrip else ("complex" if len(segments) > 1 else "oneway"),
        "output": {"priceMode": "it" if it_fare else "total"},
    }


def _is_city_line(value: str) -> bool:
    value = _clean(value)
    if not value or not re.search(r"[A-Za-zА-Яа-яЁё]", value):
        return False
    if re.search(
        r"Вылет|Departure|Прибытие|Arrival|Travel|В пути|Перевозчик|Carrier|Статус|Status|"
        r"Недействителен|Fare basis|тариф|Brand|Рейс выполняет|Багаж|Baggage|Класс|Class",
        value,
        re.IGNORECASE,
    ):
        return False
    if value.lower() in {"понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"}:
        return False
    if re.fullmatch(r"\d+\s*ч(?:\s*\d+\s*мин)?", value, re.IGNORECASE):
        return False
    if re.fullmatch(r"\d{1,2}:\d{2}|\d{2}\.\d{2}\.\d{4}|\d{1,2}\s+[А-Яа-яЁё]+\s+20\d{2}", value):
        return False
    return True


def _parse_modern_aeroflot(text: str) -> dict | None:
    if "Electronic ticket" not in text or "Рейс/Flight" not in text or "Номер билета" not in text:
        return None
    lines = [_clean(line) for line in text.splitlines() if _clean(line)]
    flat = " ".join(lines)
    passenger_match = re.search(r"Name of passenger\s+(.+?)\s+Документ/Document", flat, re.IGNORECASE)
    document_match = re.search(r"Документ/Document\s+([A-ZА-ЯЁ]{1,4}\d{6,14})", flat, re.IGNORECASE)
    ticket_match = re.search(r"Номер билета\s+Ticket number\s+(\d{3}\s+\d{10}|\d{13})", flat, re.IGNORECASE)
    issue_match = re.search(r"Дата выдачи\s+Date of issue\s+(\d{1,2}\s+[А-Яа-яЁё]+\s+20\d{2})", flat, re.IGNORECASE)
    reference_match = re.search(r"Данные бронирования\s+Booking ref\s+(.+?)\s+Место выдачи", flat, re.IGNORECASE)

    blocks = list(re.finditer(r"Рейс/Flight", text))
    details_start = text.find("Дополнительные детали")
    segments: list[dict] = []
    for index, marker in enumerate(blocks):
        end = blocks[index + 1].start() if index + 1 < len(blocks) else (details_start if details_start >= 0 else len(text))
        block_lines = [_clean(line) for line in text[marker.end() : end].splitlines() if _clean(line)]
        flight_match = next(
            (match for line in block_lines if (match := re.fullmatch(r"([A-Z]{2})\s*[- ]?\s*(\d{3,4})", line))),
            None,
        )
        time_indexes = [i for i, line in enumerate(block_lines) if re.fullmatch(r"\d{1,2}:\d{2}", line)]
        date_indexes = [i for i, line in enumerate(block_lines) if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", line)]
        if not flight_match or len(time_indexes) < 2 or not date_indexes:
            continue
        dep_index, arr_index = time_indexes[:2]
        dep_date_index = next((i for i in date_indexes if i > dep_index), date_indexes[0])
        arr_date_index = next((i for i in date_indexes if i > arr_index), date_indexes[-1])
        from_values = [block_lines[i] for i in range(dep_date_index + 1, arr_index) if _is_city_line(block_lines[i])]
        to_values = [block_lines[i] for i in range(arr_date_index + 1, len(block_lines)) if _is_city_line(block_lines[i])]
        if not from_values or not to_values:
            continue
        carrier_match = re.search(r"Перевозчик/Carrier:\s*([^\n]+)", "\n".join(block_lines), re.IGNORECASE)
        status_match = re.search(r"Статус/Status:\s*([A-ZА-Я]+)", "\n".join(block_lines), re.IGNORECASE)
        fare_basis_match = re.search(r"Вид тарифа/Fare basis:\s*([A-Z0-9-]+)", "\n".join(block_lines), re.IGNORECASE)
        segments.append({
            "from": from_values[0],
            "fromCode": "",
            "to": to_values[0],
            "toCode": "",
            "date": block_lines[dep_date_index],
            "endDate": block_lines[arr_date_index],
            "dep": block_lines[dep_index],
            "arr": block_lines[arr_index],
            "flightNo": flight_match.group(1) + flight_match.group(2),
            "carrier": _clean(carrier_match.group(1)) if carrier_match else "АЭРОФЛОТ",
            "cls": "",
            "status": status_match.group(1) if status_match else "",
            "fareBasis": fare_basis_match.group(1) if fare_basis_match else "",
            "cabin": "",
            "baggage": "",
            "dir": "out" if index == 0 else "seg",
        })
    if not segments:
        return None

    fare_calculation = re.search(r"Расчет тарифа/Fare calculation\s+([A-Z0-9 ]+?)\s+Тариф/Fare", flat, re.IGNORECASE)
    route_codes = []
    if fare_calculation:
        route_codes = re.findall(r"\b[A-Z]{3}\b", fare_calculation.group(1))
    if len(route_codes) >= len(segments) + 1:
        for index, segment in enumerate(segments):
            segment["fromCode"] = route_codes[index]
            segment["toCode"] = route_codes[index + 1]
            if index and segment["toCode"] == route_codes[0]:
                segment["dir"] = "back"

    baggage = re.findall(r"Багаж/Baggage allow:\s*([^\n]+)", text, re.IGNORECASE)
    classes = re.findall(r"Класс/Class:\s*([^\n]+)", text, re.IGNORECASE)
    for index, segment in enumerate(segments):
        if index < len(baggage):
            segment["baggage"] = _clean(baggage[index])
        if index < len(classes):
            class_value = _clean(classes[index])
            if "/" in class_value:
                cabin, booking_class = class_value.rsplit("/", 1)
                segment["cabin"] = cabin
                segment["cls"] = booking_class
            else:
                segment["cabin"] = class_value

    fare_match = re.search(r"Тариф/Fare\s+([\d .,-]+)РУБ", flat, re.IGNORECASE)
    total_match = re.search(r"Итого/Total\s+([\d .,-]+)РУБ", flat, re.IGNORECASE)
    tax_section = re.search(r"Сбор/Tax/fee/charge\s+(.+?)\s+Итого/Total", flat, re.IGNORECASE)
    tax_breakdown = []
    if tax_section:
        for code, amount in re.findall(r"([A-Z]{2,3})\s*(\d+(?:[,.]\d+)?)РУБ", tax_section.group(1), re.IGNORECASE):
            tax_breakdown.append({
                "code": code.upper(), "label": code.upper(), "amount": str(_decimal(amount)), "currency": "RUB"
            })
    fare = _decimal(fare_match.group(1)) if fare_match else Decimal("0")
    taxes = sum((_decimal(row["amount"]) for row in tax_breakdown), Decimal("0"))
    total = _decimal(total_match.group(1)) if total_match else fare + taxes
    fees = max(total - fare - taxes, Decimal("0"))
    passenger = _person(passenger_match.group(1)) if passenger_match else ""
    ticket_number = ticket_match.group(1) if ticket_match else ""
    document_number = document_match.group(1) if document_match else ""
    booking_reference = _clean(reference_match.group(1)) if reference_match else ""
    booking_classes = list(dict.fromkeys(segment["cls"] for segment in segments if segment["cls"]))
    fare_bases = list(dict.fromkeys(segment["fareBasis"] for segment in segments if segment["fareBasis"]))
    baggage_values = list(dict.fromkeys(segment["baggage"] for segment in segments if segment["baggage"]))
    statuses = list(dict.fromkeys(segment["status"] for segment in segments if segment["status"]))
    return {
        "issuer": "АЭРОФЛОТ",
        "passenger_name": passenger,
        "passengers": [{"name": passenger, "dob": "", "document": document_number, "ticketNo": ticket_number}],
        "reference": booking_reference,
        "ticket_number": ticket_number,
        "document_number": document_number,
        "date_of_birth": "",
        "issue_date": _named_date(issue_match.group(1)) if issue_match else "",
        "booking_class": " / ".join(booking_classes),
        "booking_status": " / ".join(statuses),
        "fare_basis": " / ".join(fare_bases),
        "baggage": " / ".join(baggage_values),
        "hand_baggage": "",
        "fare": fare,
        "taxes": taxes,
        "fees": fees,
        "total": total,
        "originalTotal": total,
        "currency": "RUB",
        "segments": segments,
        "fare_breakdown": [{"code": "FARE", "label": "Тариф", "amount": str(fare), "currency": "RUB"}],
        "tax_breakdown": tax_breakdown,
        "fee_breakdown": [],
        "service_kind": "avia",
        "service_type": "Авиа",
        "trip_type": "roundtrip" if len(segments) > 1 and segments[-1]["toCode"] == segments[0]["fromCode"] else ("complex" if len(segments) > 1 else "oneway"),
    }


def _hotel_guest_and_bed(lines: list[str]) -> tuple[str, str]:
    try:
        bedding_index = next(index for index, line in enumerate(lines) if line.rstrip(":").lower() == "bedding")
        guests_index = next(index for index, line in enumerate(lines) if line.rstrip(":").lower() == "guests")
    except StopIteration:
        return "", ""
    values = []
    for line in lines[max(bedding_index, guests_index) + 1 :]:
        if re.match(r"Important(?:\.|\s)", line, re.IGNORECASE):
            break
        if line.rstrip(":").lower() in {"bedding", "guests"}:
            continue
        values.append(line)
    bed = values[0] if values else ""
    guest = values[1] if len(values) > 1 else ""
    return _clean(guest), _clean(bed)


def _parse_partner_hotel(text: str) -> dict | None:
    flat = _clean(text)
    header = re.search(
        r"Reservation\s+(?P<reservation>\d{5,20})\s+made\s+on\s+(?P<issue>\d{2}\.\d{2}\.\d{2,4})\s+"
        r"This\s+accommodation\s+is\s+booked\s+by\s+our\s+partner\s+"
        r"(?P<hotel>.+?)\s+(?P<address>\d{4,6},\s*.+?)\s+(?P<phone>\+?\d[\d ()-]{7,})\s+"
        r"Check-in\s+(?P<checkin>\d{2}\.\d{2}\.\d{4}),?\s+from\s+(?P<checkin_time>\d{1,2}:\d{2}(?::\d{2})?)\s+"
        r"Check-out:\s*(?P<checkout>\d{2}\.\d{2}\.\d{4}),?\s+until\s+(?P<checkout_time>\d{1,2}:\d{2}(?::\d{2})?)\s+"
        r"(?P<room>.+?),?\s+for\s+(?P<adults>\d+)\s+adults?",
        flat,
        re.IGNORECASE,
    )
    if not header:
        return None
    lines = [_clean(line) for line in text.splitlines() if _clean(line)]
    guest, bed = _hotel_guest_and_bed(lines)
    if not guest:
        guest_match = re.search(r"Guests:\s*([A-ZА-ЯЁ][A-Za-zА-Яа-яЁё .'-]{3,80})", text, re.IGNORECASE)
        guest = _clean(guest_match.group(1)) if guest_match else ""
    meal_match = re.search(r"Meal\s+type\s+(.+?)\s+Deposit\b", flat, re.IGNORECASE)
    meal_text = _clean(meal_match.group(1)) if meal_match else ""
    meal = "Завтрак" if "breakfast" in meal_text.lower() else (meal_text or "Без питания")
    deposit_match = re.search(r"Deposit\s+Deposit\s+(.+?)\s+GPS\b", flat, re.IGNORECASE)
    deposit = _clean(deposit_match.group(1)) if deposit_match else ""
    gps_match = re.search(r"GPS\s+(-?\d{1,3}[.,]\d+\s+-?\d{1,3}[.,]\d+)", flat, re.IGNORECASE)
    important_match = re.search(
        r"Important\.\s*Please\s+Note\s+(.+?)\s+Amendment\s*&\s*Cancellation\s+Policy",
        flat,
        re.IGNORECASE,
    )
    cancellation_match = re.search(
        r"Amendment\s*&\s*Cancellation\s+Policy\s+(.+?)(?=Please\s+notify\s+in\s+advance|Meal\s+type)",
        flat,
        re.IGNORECASE,
    )
    no_show_match = re.search(r"(Cancellation\s+of\s+reservation\s+or\s+no-show.+?rate\s+and\s+contract\s+terms\.)", flat, re.IGNORECASE)
    guest_comment_match = re.search(r"(Please\s+notify\s+in\s+advance.+?show\s+up\s+by\s+that\s+time\.)", flat, re.IGNORECASE)
    checkin = header.group("checkin")
    checkout = header.group("checkout")
    try:
        nights = max((datetime.strptime(checkout, "%d.%m.%Y") - datetime.strptime(checkin, "%d.%m.%Y")).days, 0)
    except ValueError:
        nights = ""
    reservation = header.group("reservation")
    room_name = _clean(header.group("room"))
    hotel_name = _clean(header.group("hotel"))
    address = _clean(header.group("address"))
    city = "Shenzhen" if re.search(r"\bShenzhen\b", address, re.IGNORECASE) else ""
    return {
        "issuer": hotel_name,
        "passenger_name": guest,
        "passengers": [{"name": guest, "dob": "", "document": "", "ticketNo": "", "guestType": "Взрослый"}] if guest else [],
        "reference": reservation,
        "supplier_order_number": reservation,
        "hotel_booking_number": reservation,
        "issue_date": header.group("issue") if len(header.group("issue")) == 10 else header.group("issue")[:-2] + "20" + header.group("issue")[-2:],
        "booking_status": "Подтверждено",
        "fare": None,
        "taxes": None,
        "fees": None,
        "total": None,
        "currency": "",
        "segments": [{
            "from": "",
            "fromCode": "",
            "to": hotel_name,
            "toCode": "",
            "date": checkin,
            "endDate": checkout,
            "dep": header.group("checkin_time")[:5],
            "arr": header.group("checkout_time")[:5],
            "flightNo": room_name,
            "dir": "out",
        }],
        "hotel": {
            "name": hotel_name,
            "category": "",
            "country": "",
            "city": city,
            "address": address,
            "phone": _clean(header.group("phone")),
            "email": "",
            "map": gps_match.group(1) if gps_match else "",
        },
        "rooms": [{
            "category": room_name,
            "name": room_name,
            "bedType": bed,
            "adults": int(header.group("adults")),
            "children": 0,
            "meal": meal,
            "earlyCheckIn": "",
            "lateCheckOut": "",
            "conditions": "",
            "guestIds": [guest] if guest else [],
            "checkInDate": checkin,
            "checkOutDate": checkout,
            "checkInTime": header.group("checkin_time")[:5],
            "checkOutTime": header.group("checkout_time")[:5],
        }],
        "nights": nights,
        "hotelTerms": {
            "deposit": deposit,
            "cityTax": "Может взиматься отелем и оплачиваться гостем напрямую." if re.search(r"city\s+tax", flat, re.IGNORECASE) else "",
            "resortFee": "Может взиматься отелем и оплачиваться гостем напрямую." if re.search(r"resort\s+fee|facility\s+fee", flat, re.IGNORECASE) else "",
            "registrationFee": "",
            "cancellation": _clean(cancellation_match.group(1)) if cancellation_match else "",
            "noShow": _clean(no_show_match.group(1)) if no_show_match else "",
            "amendment": _clean(cancellation_match.group(1)) if cancellation_match else "",
            "important": _clean(important_match.group(1)) if important_match else "",
            "guestComment": _clean(guest_comment_match.group(1)) if guest_comment_match else "",
        },
        "service_kind": "hotel",
        "service_type": "Гостиница",
        "trip_type": "stay",
    }


def install_receipt_client_pdf_requirements_patch() -> None:
    from documents import services

    if getattr(services.extract_receipt_fields, "_client_pdf_requirements_patch", False):
        return
    original = services.extract_receipt_fields

    def wrapped(content: bytes, *, mime: str = "", name: str = ""):
        result = original(content, mime=mime, name=name)
        if not (mime == "application/pdf" or content.startswith(b"%PDF")):
            return result
        # services._extract_pdf_text is deliberately more resilient than the
        # pypdf-only compatibility parsers: it also tries pdfminer and raw PDF
        # streams.  Two client Aeroflot PDFs contain malformed objects that
        # pypdf rejects, but pdfminer still exposes the complete text layer.
        text = services._extract_pdf_text(content)
        if not text:
            return result

        parsed = _parse_russian_aeroflot_group(text)
        if parsed:
            return _replace_result(
                result,
                parsed,
                f"Групповая маршрут-квитанция распознана: {parsed.get('receipt_count', 1)} бланк(а); пассажиры и параметры рейса сохранены раздельно.",
            )

        parsed = _parse_old_aeroflot(text)
        if parsed:
            return _replace_result(
                result,
                parsed,
                f"Авиабилет распознан полностью: {len(parsed['segments'])} сегм.; все рейсы и расчёт сохранены.",
            )

        parsed = _parse_modern_aeroflot(text)
        if parsed:
            return _replace_result(
                result,
                parsed,
                f"Маршрут-квитанция распознана полностью: {len(parsed['segments'])} сегм.; рейсы, тариф и таксы сохранены.",
            )

        parsed = _parse_partner_hotel(text)
        if parsed:
            return _replace_result(
                result,
                parsed,
                "Отельный ваучер разобран по отдельным полям; стоимость в исходном ваучере не указана.",
                confidence="0.990",
            )
        return result

    wrapped._client_pdf_requirements_patch = True
    services.extract_receipt_fields = wrapped
