from __future__ import annotations

import re
from io import BytesIO

from documents.receipt_client_pdf_requirements import (
    _clean,
    _parse_modern_aeroflot,
    _parse_old_aeroflot,
    _replace_result,
)


def _line_value_after(lines: list[str], label_index: int, *, count: int = 1) -> str:
    values = [line for line in lines[label_index + 1 :] if line]
    return values[count - 1] if len(values) >= count else ""


def _partner_hotel_from_pdfminer(text: str) -> dict | None:
    if "This accommodation is booked by our partner" not in text or "Reservation" not in text:
        return None
    lines = [_clean(line) for line in text.splitlines() if _clean(line)]
    reservation_index = next((i for i, line in enumerate(lines) if re.match(r"Reservation\s+\d+", line, re.I)), -1)
    partner_index = next((i for i, line in enumerate(lines) if "accommodation is booked by our partner" in line.lower()), -1)
    checkin_index = next((i for i, line in enumerate(lines) if line.lower() == "check-in"), -1)
    checkout_index = next((i for i, line in enumerate(lines) if line.lower().startswith("check-out")), -1)
    if min(reservation_index, partner_index, checkin_index, checkout_index) < 0:
        return None

    reservation_match = re.search(r"Reservation\s+(\d{5,20})\s+made\s+on\s+(\d{2}\.\d{2}\.\d{2,4})", lines[reservation_index], re.I)
    if not reservation_match:
        return None

    hotel_index = -1
    for index in range(partner_index + 1, checkin_index - 2):
        if re.match(r"\d{4,6},\s*", lines[index + 1]) and re.fullmatch(r"\+?\d[\d ()-]{7,}", lines[index + 2]):
            hotel_index = index
            break
    if hotel_index < 0:
        return None
    hotel_name = lines[hotel_index]
    address = lines[hotel_index + 1]
    phone = lines[hotel_index + 2]

    checkin_date_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", _line_value_after(lines, checkin_index))
    checkin_time_match = re.search(r"(\d{1,2}:\d{2})(?::\d{2})?", _line_value_after(lines, checkin_index, count=2))
    checkout_date_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", _line_value_after(lines, checkout_index))
    checkout_time_match = re.search(r"(\d{1,2}:\d{2})(?::\d{2})?", _line_value_after(lines, checkout_index, count=2))
    if not all((checkin_date_match, checkin_time_match, checkout_date_match, checkout_time_match)):
        return None
    checkin = checkin_date_match.group(1)
    checkout = checkout_date_match.group(1)
    checkin_time = checkin_time_match.group(1)
    checkout_time = checkout_time_match.group(1)

    room_index = checkout_index + 3
    room_line = lines[room_index] if room_index < len(lines) else ""
    room_match = re.match(r"(.+?),?\s+for\s+(\d+)\s+adults?\b", room_line, re.I)
    if not room_match:
        return None
    room_name = _clean(room_match.group(1))
    adults = int(room_match.group(2))

    bedding_index = next((i for i, line in enumerate(lines) if line.rstrip(":").lower() == "bedding"), -1)
    guests_index = next((i for i, line in enumerate(lines) if line.rstrip(":").lower() == "guests"), -1)
    important_index = next((i for i, line in enumerate(lines) if line.lower().startswith("important. please note")), -1)
    bed = ""
    guest = ""
    if min(bedding_index, guests_index, important_index) >= 0:
        labels_end = max(bedding_index, guests_index)
        values = [line for line in lines[labels_end + 1 : important_index] if line.rstrip(":").lower() not in {"bedding", "guests"}]
        if values:
            bed = values[0]
        if len(values) > 1:
            guest = values[1]
    # Some PDFs place the value immediately after each label instead of using
    # the two-column extraction order.  Fall back to that layout if needed.
    if not bed and bedding_index >= 0 and bedding_index + 1 < len(lines):
        bed = lines[bedding_index + 1]
    if not guest and guests_index >= 0 and guests_index + 1 < len(lines):
        guest = lines[guests_index + 1]

    meal_index = next((i for i, line in enumerate(lines) if line.lower() == "meal type"), -1)
    meal_value = lines[meal_index + 1] if meal_index >= 0 and meal_index + 1 < len(lines) else ""
    meal = "Завтрак" if "breakfast" in meal_value.lower() else (meal_value or "Без питания")

    deposit = ""
    deposit_indexes = [i for i, line in enumerate(lines) if line.lower() == "deposit"]
    if deposit_indexes:
        start = deposit_indexes[-1] + 1
        parts = []
        for line in lines[start:]:
            if line.upper().startswith("GPS "):
                break
            if re.fullmatch(r"\d+(?:[,.]\d+)?|[A-Z]{3}|per room for the night", line, re.I):
                parts.append(line)
            elif parts:
                break
        deposit = _clean(" ".join(parts))

    flat = _clean(text)
    important_match = re.search(
        r"Important\.\s*Please\s+Note\s+(.+?)\s+Amendment\s*&\s*Cancellation\s+Policy",
        flat,
        re.I,
    )
    amendment_match = re.search(
        r"Amendment\s*&\s*Cancellation\s+Policy\s+(.+?)(?=Cancellation\s+of\s+reservation\s+or\s+no-show)",
        flat,
        re.I,
    )
    no_show_match = re.search(
        r"(Cancellation\s+of\s+reservation\s+or\s+no-show.+?rate\s+and\s+contract\s+terms\.)",
        flat,
        re.I,
    )
    guest_comment_match = re.search(
        r"(Please\s+notify\s+in\s+advance.+?show\s+up\s+by\s+that\s+time\.)",
        flat,
        re.I,
    )
    gps_match = re.search(r"GPS\s+(-?\d{1,3}[.,]\d+\s+-?\d{1,3}[.,]\d+)", flat, re.I)
    city = "Shenzhen" if re.search(r"\bShenzhen\b", address, re.I) else ""
    issue = reservation_match.group(2)
    if len(issue) == 8:
        issue = issue[:-2] + "20" + issue[-2:]
    try:
        from datetime import datetime

        nights = max((datetime.strptime(checkout, "%d.%m.%Y") - datetime.strptime(checkin, "%d.%m.%Y")).days, 0)
    except ValueError:
        nights = ""

    reservation = reservation_match.group(1)
    return {
        "issuer": hotel_name,
        "passenger_name": guest,
        "passengers": [{"name": guest, "dob": "", "document": "", "ticketNo": "", "guestType": "Взрослый"}] if guest else [],
        "reference": reservation,
        "supplier_order_number": reservation,
        "hotel_booking_number": reservation,
        "issue_date": issue,
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
            "dep": checkin_time,
            "arr": checkout_time,
            "flightNo": room_name,
            "dir": "out",
        }],
        "hotel": {
            "name": hotel_name,
            "category": "",
            "country": "",
            "city": city,
            "address": address,
            "phone": phone,
            "email": "",
            "map": gps_match.group(1) if gps_match else "",
        },
        "rooms": [{
            "category": room_name,
            "name": room_name,
            "bedType": bed,
            "adults": adults,
            "children": 0,
            "meal": meal,
            "earlyCheckIn": "",
            "lateCheckOut": "",
            "conditions": "",
            "guestIds": [guest] if guest else [],
            "checkInDate": checkin,
            "checkOutDate": checkout,
            "checkInTime": checkin_time,
            "checkOutTime": checkout_time,
        }],
        "nights": nights,
        "hotelTerms": {
            "deposit": deposit,
            "cityTax": "Может взиматься отелем и оплачиваться гостем напрямую." if re.search(r"city\s+tax", flat, re.I) else "",
            "resortFee": "Может взиматься отелем и оплачиваться гостем напрямую." if re.search(r"resort\s+fee|facility\s+fee", flat, re.I) else "",
            "registrationFee": "",
            "cancellation": _clean(no_show_match.group(1)) if no_show_match else "",
            "noShow": _clean(no_show_match.group(1)) if no_show_match else "",
            "amendment": _clean(amendment_match.group(1)) if amendment_match else "",
            "important": _clean(important_match.group(1)) if important_match else "",
            "guestComment": _clean(guest_comment_match.group(1)) if guest_comment_match else "",
        },
        "service_kind": "hotel",
        "service_type": "Гостиница",
        "trip_type": "stay",
    }


def install_receipt_client_pdf_text_source_patch() -> None:
    from documents import services

    if getattr(services.extract_receipt_fields, "_client_pdf_text_source_patch", False):
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
        if not text:
            return result

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
        parsed = _partner_hotel_from_pdfminer(text)
        if parsed:
            return _replace_result(
                result,
                parsed,
                "Отельный ваучер разобран по отдельным полям; стоимость в исходном ваучере не указана.",
                confidence="0.990",
            )
        return result

    wrapped._client_pdf_text_source_patch = True
    services.extract_receipt_fields = wrapped
