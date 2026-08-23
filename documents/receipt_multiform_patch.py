from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

from pdfminer.high_level import extract_text as pdfminer_extract_text
from pdfminer.pdfpage import PDFPage
from pypdf import PdfReader

from documents.receipt_parser_patch_safe import _json_safe, _rail

RU_MONTHS = {
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


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _decimal(value) -> Decimal:
    try:
        return Decimal(re.sub(r"\s+", "", str(value or "0")).replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _page_texts(content: bytes) -> tuple[list[str], list[str]]:
    """Return per-page text from both extractors.

    RZD coupons are best preserved by pypdf, while several supplier PDFs with
    malformed elementary objects are only readable through pdfminer. Keeping
    both representations prevents a single broken page from collapsing a
    multi-page import into one aggregate receipt.
    """

    pypdf_pages: list[str] = []
    try:
        reader = PdfReader(BytesIO(content), strict=False)
        for page in reader.pages:
            try:
                pypdf_pages.append(page.extract_text() or "")
            except Exception:
                pypdf_pages.append("")
    except Exception:
        pass

    pdfminer_pages: list[str] = []
    try:
        page_count = sum(
            1
            for _ in PDFPage.get_pages(
                BytesIO(content),
                check_extractable=False,
            )
        )
        for page_number in range(page_count):
            try:
                pdfminer_pages.append(
                    pdfminer_extract_text(
                        BytesIO(content),
                        page_numbers=[page_number],
                    )
                    or ""
                )
            except Exception:
                pdfminer_pages.append("")
    except Exception:
        pass

    return pypdf_pages, pdfminer_pages


def _best_pages(pypdf_pages: list[str], pdfminer_pages: list[str]) -> list[str]:
    count = max(len(pypdf_pages), len(pdfminer_pages))
    pages: list[str] = []
    for index in range(count):
        pypdf_text = pypdf_pages[index] if index < len(pypdf_pages) else ""
        miner_text = pdfminer_pages[index] if index < len(pdfminer_pages) else ""
        if "КОНТРОЛЬНЫЙ КУПОН" in pypdf_text and len(pypdf_text) > 500:
            pages.append(pypdf_text)
        elif len(miner_text.strip()) >= len(pypdf_text.strip()) * 0.8:
            pages.append(miner_text)
        else:
            pages.append(pypdf_text or miner_text)
    return [page for page in pages if page.strip()]


def _parse_ru_date(value: str) -> str:
    match = re.search(r"(\d{1,2})\s+([А-Яа-яЁё]+)\s+(\d{4})", value or "")
    if not match:
        return ""
    month = RU_MONTHS.get(match.group(2).lower())
    if not month:
        return ""
    return f"{int(match.group(1)):02d}.{month:02d}.{match.group(3)}"


def _line_after(lines: list[str], label: str) -> str:
    label_lower = label.lower()
    for index, value in enumerate(lines):
        if value.lower() == label_lower:
            return lines[index + 1] if index + 1 < len(lines) else ""
    return ""


def _line_amount(value: str) -> Decimal:
    match = re.search(r"(?:RUB\s*)?(\d[\d ]*(?:[,.]\d{2})?)", value or "")
    return _decimal(match.group(1)) if match else Decimal("0")


def _parse_psc_air(text: str) -> dict | None:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    joined = _flat(text)
    if "ЭЛЕКТРОННЫЙ БИЛЕТ" not in joined or not re.search(r"маршрут-квитанц", joined, re.I):
        return None

    order_match = re.search(r"Заказ\s*№\s*(\d+)", joined, re.I)
    reference_match = re.search(r"код\s+бронирования:\s*([A-Z0-9]+)", joined, re.I)
    passenger = _line_after(lines, "Пассажир")
    date_of_birth = _line_after(lines, "Дата рождения")
    document_number = _line_after(lines, "Номер документа")
    ticket_number = _line_after(lines, "Номер билета")
    issue_date = _line_after(lines, "Продажа")

    segment: dict = {}
    try:
        route_index = lines.index("Продажа") + 2
    except ValueError:
        route_index = -1
    if route_index >= 0 and route_index + 7 < len(lines):
        from_code = re.match(r"([A-Z]{3})", lines[route_index])
        to_code = re.match(r"([A-Z]{3})", lines[route_index + 4])
        segment = {
            "from": lines[route_index + 1],
            "fromCode": from_code.group(1) if from_code else "",
            "to": lines[route_index + 5],
            "toCode": to_code.group(1) if to_code else "",
            "date": _parse_ru_date(lines[route_index + 3]),
            "endDate": _parse_ru_date(lines[route_index + 7]),
            "dep": lines[route_index + 2],
            "arr": lines[route_index + 6],
            "dir": "out",
        }

    try:
        status_index = lines.index("Статус")
        carrier_values = []
        for value in lines[status_index + 1 :]:
            if re.match(r"(?:Стоимость|РАСЧЕТ ТАРИФА|ТАРИФ|СБОР/TAX)\s*:? ?$", value, re.I):
                break
            carrier_values.append(value)
            if len(carrier_values) >= 7:
                break
    except ValueError:
        carrier_values = []
    issuer = carrier_values[0] if len(carrier_values) > 0 else ""
    flight_number = carrier_values[1] if len(carrier_values) > 1 else ""
    cabin = carrier_values[2] if len(carrier_values) > 2 else ""
    # This supplier has two valid layouts under the same headers:
    # carrier, flight, cabin, fare basis, baggage, carry-on, status; and a
    # shorter layout without fare basis. Read the stable columns from the end
    # so 1PC/8KG can never slide into fare/baggage respectively.
    booking_status = carrier_values[-1] if len(carrier_values) >= 6 else ""
    hand_baggage = carrier_values[-2] if len(carrier_values) >= 6 else ""
    baggage = carrier_values[-3] if len(carrier_values) >= 6 else ""
    fare_values = carrier_values[3:-3] if len(carrier_values) >= 6 else []
    fare_basis = fare_values[0] if fare_values else ""
    segment.update(
        {
            "carrier": issuer,
            "flightNo": flight_number,
            "cabin": cabin,
            "fareBasis": fare_basis,
            "baggage": baggage,
            "handBaggage": hand_baggage,
            "status": booking_status,
        }
    )

    fare = taxes = service_fee = total = Decimal("0")
    agency_fee = provider_fee = Decimal("0")
    if "РАСЧЕТ ТАРИФА:" in lines:
        index = lines.index("РАСЧЕТ ТАРИФА:")
        labels = lines[index + 1 : index + 7]
        amounts = lines[index + 7 : index + 13]
        pairs = {
            labels[i]: _line_amount(amounts[i])
            for i in range(min(len(labels), len(amounts)))
        }
        fare = pairs.get("ТАРИФ", Decimal("0"))
        taxes = pairs.get("СБОР/TAX", Decimal("0"))
        agency_fee = pairs.get("СБОР СА", Decimal("0"))
        provider_fee = pairs.get("СБОР АСБ", Decimal("0"))
        service_fee = agency_fee + provider_fee
        total = pairs.get("ВСЕГО К ОПЛАТЕ", Decimal("0"))
    elif "Стоимость:" in lines:
        index = lines.index("Стоимость:")
        labels = lines[index : index + 3]
        amounts = lines[index + 3 : index + 6]
        pairs = {
            labels[i]: _line_amount(amounts[i])
            for i in range(min(len(labels), len(amounts)))
        }
        total = pairs.get("Стоимость:", Decimal("0"))
        provider_fee = pairs.get("в том числе сбор АСБ:", Decimal("0"))
        agency_fee = pairs.get("в том числе сбор СА:", Decimal("0"))
        service_fee = provider_fee + agency_fee
        fare = max(total - service_fee, Decimal("0"))

    if not passenger or not ticket_number or not segment.get("fromCode") or not segment.get("toCode"):
        return None

    fee_breakdown = []
    if provider_fee:
        fee_breakdown.append(
            {
                "code": "ASB",
                "label": "Сбор АСБ",
                "amount": str(provider_fee),
                "currency": "RUB",
            }
        )
    if agency_fee:
        fee_breakdown.append(
            {
                "code": "SA",
                "label": "Сбор СА",
                "amount": str(agency_fee),
                "currency": "RUB",
            }
        )

    return {
        "issuer": issuer,
        "passenger_name": passenger,
        "passengers": [
            {
                "name": passenger,
                "dob": date_of_birth,
                "document": document_number,
                "ticketNo": ticket_number,
            }
        ],
        "reference": reference_match.group(1) if reference_match else "",
        "ticket_number": ticket_number,
        "document_number": document_number,
        "date_of_birth": date_of_birth,
        "issue_date": issue_date,
        "supplier_order_number": order_match.group(1) if order_match else "",
        "booking_class": cabin,
        "fare_basis": fare_basis,
        "baggage": baggage,
        "hand_baggage": hand_baggage,
        "booking_status": booking_status,
        "fare": fare,
        "taxes": taxes,
        "fees": service_fee,
        "total": total,
        "currency": "RUB",
        "derived_financial_fields": (["fare"] if "Стоимость:" in lines else []),
        "fare_breakdown": [
            {
                "code": "FARE",
                "label": "Тариф",
                "amount": str(fare),
                "currency": "RUB",
            }
        ],
        "tax_breakdown": (
            [
                {
                    "code": "TAX",
                    "label": "Таксы перевозчика",
                    "amount": str(taxes),
                    "currency": "RUB",
                }
            ]
            if taxes
            else []
        ),
        "fee_breakdown": fee_breakdown,
        "service_kind": "avia",
        "service_type": "Авиа",
        "trip_type": "oneway",
        "segments": [segment],
    }


def _parse_psc_hotel(text: str) -> dict | None:
    joined = _flat(text)
    if not re.search(
        r"Подтверждение\s+бронирования|Контакты\s+отеля|Имя\s+гостя",
        joined,
        re.I,
    ):
        return None

    city_country_match = re.search(
        r"Город:\s*(.+?)\s+Название\s+отеля:",
        joined,
        re.I,
    )
    city = country = ""
    if city_country_match:
        parts = [part.strip(" ,") for part in city_country_match.group(1).split(",")]
        city = parts[0] if parts else ""
        country = parts[1] if len(parts) > 1 else ""

    hotel_match = re.search(
        r"Название\s+отеля:\s*[«\"]?(.+?)[»\"]?\s+Адрес:",
        joined,
        re.I,
    )
    address_match = re.search(
        r"Адрес:\s*(.+?)\s+Детали\s+размещения",
        joined,
        re.I,
    )
    issue_match = re.search(r"Забронировано:\s*(\d{2}\.\d{2}\.\d{4})", joined, re.I)
    hotel_name = hotel_match.group(1).strip(" «»\"") if hotel_match else ""
    address = address_match.group(1).strip() if address_match else ""

    guest_starts = list(re.finditer(r"Имя\s+гостя:", joined, re.I))
    passengers: list[dict] = []
    rooms: list[dict] = []
    periods: list[tuple[str, str, str, str]] = []
    for index, start in enumerate(guest_starts):
        end = guest_starts[index + 1].start() if index + 1 < len(guest_starts) else len(joined)
        block = joined[start.start() : end]
        block = re.split(r"\s+При\s+заселении", block, maxsplit=1, flags=re.I)[0]
        name_match = re.search(
            r"Имя\s+гостя:\s*(.+?)\s+Категория\s+номера:",
            block,
            re.I,
        )
        if not name_match:
            continue
        category_match = re.search(
            r"Категория\s+номера:\s*(.+?)\s+Питание:",
            block,
            re.I,
        )
        meal_match = re.search(
            r"Питание:\s*(.+?)\s+Дата\s+заезда\s*/\s*Дата\s+выезда:",
            block,
            re.I,
        )
        period_match = re.search(
            r"Дата\s+заезда\s*/\s*Дата\s+выезда:\s*"
            r"(\d{2}\.\d{2}\.\d{4})\s*(\d{1,2}:\d{2})\s*/\s*"
            r"(\d{2}\.\d{2}\.\d{4})\s*(\d{1,2}:\d{2})",
            block,
            re.I,
        )
        booking_match = re.search(r"Номер\s+бронирования:\s*(.+?)$", block, re.I)
        name = name_match.group(1).strip()
        category = category_match.group(1).strip() if category_match else ""
        meal_source = meal_match.group(1).strip() if meal_match else ""
        meal_lower = meal_source.lower()
        if "завтрак" in meal_lower:
            meal = "Завтрак"
        elif "не включ" in meal_lower:
            meal = "Без питания"
        else:
            meal = meal_source or "Без питания"
        period = (
            (
                period_match.group(1),
                period_match.group(2),
                period_match.group(3),
                period_match.group(4),
            )
            if period_match
            else ("", "", "", "")
        )
        booking_number = booking_match.group(1).strip() if booking_match else ""
        passengers.append(
            {
                "name": name,
                "dob": "",
                "document": "",
                "ticketNo": "",
                "guestType": "Взрослый",
            }
        )
        rooms.append(
            {
                "category": category,
                "name": category,
                "bedType": "Двуспальная кровать" if "двуспаль" in category.lower() else "",
                "adults": 1,
                "children": 0,
                "meal": meal,
                "earlyCheckIn": "",
                "lateCheckOut": "",
                "guestIds": [name],
                "conditions": meal_source,
                "checkInDate": period[0],
                "checkInTime": period[1],
                "checkOutDate": period[2],
                "checkOutTime": period[3],
                "bookingNo": booking_number,
            }
        )
        periods.append(period)

    if not passengers:
        return None

    first_period = periods[0]
    nights = ""
    try:
        start = datetime.strptime(first_period[0], "%d.%m.%Y")
        end = datetime.strptime(first_period[2], "%d.%m.%Y")
        nights = max((end - start).days, 0)
    except (TypeError, ValueError):
        pass

    booking_numbers = [room["bookingNo"] for room in rooms if room.get("bookingNo")]
    shared_booking = booking_numbers[0] if len(set(booking_numbers)) == 1 else ""
    return {
        "issuer": hotel_name,
        "passenger_name": ", ".join(passenger["name"] for passenger in passengers),
        "passengers": passengers,
        "issue_date": issue_match.group(1) if issue_match else "",
        "supplier_order_number": "",
        "supplierOrderNo": "",
        "hotel_booking_number": shared_booking,
        "hotelBookingNo": shared_booking,
        "booking_status": "Подтверждено",
        "bookingStatus": "Подтверждено",
        "service_kind": "hotel",
        "service_type": "Гостиница",
        "trip_type": "stay",
        "segments": [
            {
                "from": "",
                "fromCode": "",
                "to": hotel_name,
                "toCode": "",
                "date": first_period[0],
                "endDate": first_period[2],
                "dep": first_period[1],
                "arr": first_period[3],
                "flightNo": rooms[0]["category"],
                "dir": "out",
            }
        ],
        "hotel": {
            "name": hotel_name,
            "category": "",
            "country": country,
            "city": city,
            "address": address,
            "phone": "",
            "email": "",
            "map": "",
        },
        "rooms": rooms,
        "nights": nights,
        "room_count": len(rooms),
        "guest_count": len(passengers),
    }


def _enrich_rail_receipt(receipt: dict, index: int) -> dict:
    passenger = str(receipt.get("passenger_name") or "").strip()
    ticket_number = str(receipt.get("ticket_number") or "").strip()
    segments = receipt.get("segments") or []
    return {
        **receipt,
        "receiptIndex": index + 1,
        "receiptPage": index + 1,
        "blankId": ticket_number or f"rail-{index + 1}",
        "passenger": passenger,
        "ticketNo": ticket_number,
        "passengers": (
            [
                {
                    "name": passenger,
                    "dob": receipt.get("date_of_birth") or "",
                    "document": receipt.get("document_number") or "",
                    "ticketNo": ticket_number,
                }
            ]
            if passenger
            else []
        ),
        "legs": segments,
        "fareBreakdown": receipt.get("costBreakdown") or receipt.get("fare_breakdown") or [],
        "taxBreakdown": receipt.get("taxBreakdown") or receipt.get("tax_breakdown") or [],
        "feeBreakdown": receipt.get("feeBreakdown") or receipt.get("fee_breakdown") or [],
        "recognitionPending": False,
    }


def _aggregate_rail_receipts(receipts: list[dict], current_fields: dict) -> dict:
    enriched = [_enrich_rail_receipt(receipt, index) for index, receipt in enumerate(receipts)]
    passengers: list[dict] = []
    passenger_keys: set[tuple[str, str]] = set()
    segments: list[dict] = []
    segment_keys: set[tuple] = set()
    for receipt in enriched:
        for passenger in receipt.get("passengers") or []:
            key = (passenger.get("name") or "", passenger.get("ticketNo") or "")
            if key not in passenger_keys:
                passenger_keys.add(key)
                passengers.append(passenger)
        for segment in receipt.get("segments") or []:
            key = (
                segment.get("from"),
                segment.get("to"),
                segment.get("date"),
                segment.get("dep"),
                segment.get("arr"),
                segment.get("flightNo"),
            )
            if key not in segment_keys:
                segment_keys.add(key)
                segments.append(segment)

    total = sum((_decimal(receipt.get("total")) for receipt in enriched), Decimal("0"))
    ticket_cost = sum((_decimal(receipt.get("ticketCost")) for receipt in enriched), Decimal("0"))
    reserved_seat_cost = sum(
        (_decimal(receipt.get("reservedSeatCost")) for receipt in enriched),
        Decimal("0"),
    )
    passenger_names = list(dict.fromkeys(passenger["name"] for passenger in passengers if passenger.get("name")))
    ticket_numbers = [receipt.get("ticketNo") for receipt in enriched if receipt.get("ticketNo")]
    roundtrip = bool(
        len(segments) > 1
        and segments[0].get("from") == segments[-1].get("to")
        and segments[0].get("to") == segments[-1].get("from")
    )
    first = enriched[0]
    return {
        **current_fields,
        **first,
        "passenger_name": ", ".join(passenger_names),
        "passengers": passengers,
        "ticket_number": ", ".join(ticket_numbers),
        "fare": total,
        "taxes": Decimal("0"),
        "fees": Decimal("0"),
        "total": total,
        "ticketCost": ticket_cost,
        "reservedSeatCost": reserved_seat_cost,
        "fare_breakdown": [
            {
                "code": "TICKET",
                "label": "Билет",
                "amount": str(ticket_cost),
                "currency": "RUB",
            },
            {
                "code": "RESERVED_SEAT",
                "label": "Плацкарта",
                "amount": str(reserved_seat_cost),
                "currency": "RUB",
            },
        ],
        "segments": segments,
        "trip_type": "roundtrip" if roundtrip else "oneway",
        "receipt_count": len(enriched),
        "receipts": enriched,
        "railTickets": enriched,
        "service_kind": "rail",
        "service_type": "ЖД",
        "currency": "RUB",
    }


def install_receipt_multiform_patch() -> None:
    from documents import services

    if getattr(services.extract_receipt_fields, "_multiform_patch", False):
        return
    original = services.extract_receipt_fields

    def wrapped(content: bytes, *, mime: str = "", name: str = ""):
        result = original(content, mime=mime, name=name)
        if not (mime == "application/pdf" or content.startswith(b"%PDF")):
            return result

        pypdf_pages, pdfminer_pages = _page_texts(content)
        pages = _best_pages(pypdf_pages, pdfminer_pages)
        if not pages:
            return result

        fields = result.get("fields") or {}
        result["fields"] = fields
        result.setdefault("raw", {})
        warnings = list(result.get("warnings") or [])

        rail_receipts = [receipt for receipt in (_rail(page) for page in pages) if receipt]
        if rail_receipts:
            aggregate = _aggregate_rail_receipts(rail_receipts, fields)
            fields.clear()
            fields.update(aggregate)
            result["raw"].update(_json_safe(aggregate))
            result["status"] = "parsed"
            result["confidence"] = Decimal("0.990")
            warnings = [warning for warning in warnings if "ЖД-бланков" not in str(warning)]
            warnings.append(
                f"Распознано отдельных ЖД-бланков: {len(rail_receipts)}. "
                "Пассажир, билет, вагон, место и стоимость сохранены для каждого бланка."
            )
            result["warnings"] = warnings
            return result

        merged_text = "\n".join(pdfminer_pages or pages)
        hotel = _parse_psc_hotel(merged_text)
        if hotel:
            existing_hotel = fields.get("hotel") if isinstance(fields.get("hotel"), dict) else {}
            existing_terms = fields.get("hotelTerms") if isinstance(fields.get("hotelTerms"), dict) else {}
            parsed_hotel = {**existing_hotel, **{key: value for key, value in hotel["hotel"].items() if value}}
            hotel["hotel"] = parsed_hotel
            hotel["hotelTerms"] = existing_terms
            fields.update(hotel)
            result["raw"].update(_json_safe(hotel))
            result["status"] = "parsed"
            result["confidence"] = Decimal("0.990")
            warnings.append(
                f"Распознано размещений: {hotel['room_count']}; гостей: {hotel['guest_count']}. "
                "Каждый гость привязан к своему номеру."
            )
            result["warnings"] = list(dict.fromkeys(warnings))
            return result

        air = _parse_psc_air(merged_text)
        if air:
            essential_missing = (
                fields.get("service_kind") != "avia"
                or not fields.get("passenger_name")
                or not fields.get("ticket_number")
                or not fields.get("segments")
                or not fields.get("total")
            )
            if essential_missing or re.search(r"ИП\s+Хмель\s+Марина", merged_text, re.I):
                fields.update(air)
                result["raw"].update(_json_safe(air))
                result["status"] = "parsed"
                result["confidence"] = max(
                    result.get("confidence") or Decimal("0"),
                    Decimal("0.985"),
                )
                result["warnings"] = warnings
        return result

    wrapped._multiform_patch = True
    services.extract_receipt_fields = wrapped
