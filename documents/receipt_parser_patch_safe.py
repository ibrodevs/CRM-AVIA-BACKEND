import re
from decimal import Decimal, InvalidOperation
from io import BytesIO


def _pages(content):
    try:
        from pypdf import PdfReader

        return [
            re.sub(r"[ \t]+", " ", (page.extract_text() or "").replace("\r", "\n"))
            for page in PdfReader(BytesIO(content), strict=False).pages
        ]
    except Exception:
        return []


def _decimal(value):
    try:
        return Decimal(re.sub(r"\s+", "", value).replace(",", "."))
    except (InvalidOperation, AttributeError):
        return None


def _json_safe(value):
    """Convert parser details before saving them into a Django JSONField."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _hotel(text):
    flat = re.sub(r"\s+", " ", text)
    stay = re.search(
        r"(\d{2}\.\d{2}\.\d{4})\s+(\d{1,2}:\d{2})\s+"
        r"(\d{2}\.\d{2}\.\d{4})\s+(\d{1,2}:\d{2})",
        flat,
    )
    guest = re.search(
        r"(?:ФИ\s*гостя|Guest\s*name)\s*(?:Guest\s*name)?\s*"
        r"((?:MR|MRS|MS)?\s*[А-ЯЁA-Z][А-ЯЁA-Z\- ]{3,80}?)(?=\s+(?:Тип\s*номера|Room\s*type))",
        flat,
        re.I,
    )
    hotel = re.search(r"voucher upon arrival\s+(.+?\d?\*)\s+\d{2}\.\d{2}\.\d{4}", flat, re.I)
    booking = re.search(
        r"(?:Номер\s*бронирования|Booking\s*reference\s*number)\s*"
        r"(?:Booking\s*reference\s*number)?\s*(\d{5,20})",
        flat,
        re.I,
    )
    room = re.search(
        r"(?:Тип\s*номера|Room\s*type)\s*(?:Room\s*type)?\s*"
        r"(.+?)(?=\s+(?:Тип\s*питания|Meal\s*type))",
        flat,
        re.I,
    )
    issue = re.search(r"Date of issue\s*(\d{2}\.\d{2}\.\d{4})", flat, re.I)
    if not (stay and guest and hotel):
        return None
    name = re.sub(r"^(?:MR|MRS|MS)\s+", "", guest.group(1).strip(), flags=re.I)
    hotel_name = hotel.group(1).strip()
    return {
        "issuer": hotel_name,
        "passenger_name": name,
        "reference": booking.group(1) if booking else "",
        "ticket_number": booking.group(1) if booking else "",
        "issue_date": issue.group(1) if issue else "",
        "service_kind": "hotel",
        "service_type": "Гостиница",
        "trip_type": "stay",
        "segments": [
            {
                "from": "",
                "fromCode": "",
                "to": hotel_name,
                "toCode": "",
                "date": stay.group(1),
                "endDate": stay.group(3),
                "dep": stay.group(2),
                "arr": stay.group(4),
                "flightNo": room.group(1).strip() if room else "",
                "dir": "out",
            }
        ],
    }


def _rail(text):
    flat = re.sub(r"\s+", " ", text)
    ticket = re.search(r"№\s*([\d ]{13,24})", flat)
    passport = re.search(r"ПАСПОРТ РФ\s*(\d{10})\s*(\d{2}\.\d{2}\.\d{4})", flat)
    passenger = re.search(
        r"ПАСПОРТ РФ\s*\d{10}\s*\d{2}\.\d{2}\.\d{4}\s*RUS\s*[МMЖF]\s*"
        r"([А-ЯЁ][А-ЯЁ\-]+(?:\s+[А-ЯЁ][А-ЯЁ\-]+){1,3})(?=\s+Посадка)",
        flat,
    )
    route = re.search(
        r"(?P<dep>\d{1,2}:\d{2})\s+(?P<date>\d{2}\.\d{2}\.\d{4}).*?"
        r"Санкт-Петербург-Главн\..*?"
        r"(?P<arr>\d{1,2}:\d{2})\s+(?P<arr_date>\d{2}\.\d{2}\.\d{4}).*?"
        r"Москва Октябрьская",
        flat,
        re.S,
    )
    train_data = re.search(
        r"ПОЕЗД ВАГОН МЕСТО\s*(\d{3})\s+(?:\d{3}\s*)?(\d{2})\s+(?:\d{2}\s*)?(\d{3})",
        flat,
    )
    totals = re.findall(r"Вкл\. НДС\s+([\d ]+[,.]\d{2})\s*₽", flat, re.I)
    if not (ticket and passenger and route):
        return None
    total = _decimal(totals[-1]) if totals else None
    train, coach, seat = train_data.groups() if train_data else ("", "", "")
    order = re.search(r"Заказ:\s*(\d{10,20})", flat)
    return {
        "issuer": "ОАО РЖД",
        "passenger_name": passenger.group(1).strip(),
        "reference": order.group(1) if order else "",
        "ticket_number": re.sub(r"\s+", "", ticket.group(1)),
        "document_number": passport.group(1) if passport else "",
        "date_of_birth": passport.group(2) if passport else "",
        "booking_class": "1С" if re.search(r"БИЗНЕС КЛАСС\s*1С", flat) else "",
        "fare": total,
        "taxes": Decimal("0") if total is not None else None,
        "fees": Decimal("0") if total is not None else None,
        "total": total,
        "currency": "RUB",
        "service_kind": "rail",
        "service_type": "ЖД",
        "trip_type": "oneway",
        "segments": [
            {
                "from": "Санкт-Петербург-Главн.",
                "fromCode": "",
                "to": "Москва Октябрьская",
                "toCode": "",
                "date": route.group("date"),
                "dep": route.group("dep"),
                "arr": route.group("arr"),
                "endDate": route.group("arr_date"),
                "flightNo": train,
                "coach": coach,
                "seat": seat,
                "dir": "out",
            }
        ],
    }


def install_receipt_parser_patch():
    from documents import services

    if getattr(services.extract_receipt_fields, "_safe_layout_patch", False):
        return
    original = services.extract_receipt_fields

    def wrapped(content: bytes, *, mime: str = "", name: str = ""):
        result = original(content, mime=mime, name=name)
        if not (mime == "application/pdf" or content.startswith(b"%PDF")):
            return result
        pages = _pages(content)
        if not pages:
            return result
        joined = "\n".join(pages)

        if re.search(r"Ваучер\s+отеля|Hotel\s+voucher", joined, re.I):
            fields = _hotel(pages[0])
            if fields:
                result["fields"].update(fields)
                result["raw"].update(_json_safe(fields))
                result["status"] = "parsed"
                result["confidence"] = Decimal("0.960")
                result["warnings"] = ["Стоимость в исходном ваучере не указана; остальные поля распознаны."]
            return result

        if "ЭЛЕКТРОННЫЙ БИЛЕТ. КОНТРОЛЬНЫЙ КУПОН" in joined:
            receipts = [receipt for receipt in (_rail(page) for page in pages) if receipt]
            if receipts:
                total = sum((receipt["total"] or Decimal("0") for receipt in receipts), Decimal("0"))
                fields = dict(receipts[0])
                fields.update(
                    {
                        "passenger_name": ", ".join(receipt["passenger_name"] for receipt in receipts),
                        "ticket_number": ", ".join(receipt["ticket_number"] for receipt in receipts),
                        "total": total,
                        "fare": total,
                        "taxes": Decimal("0"),
                        "fees": Decimal("0"),
                        "segments": [receipt["segments"][0] for receipt in receipts],
                        "receipt_count": len(receipts),
                        "receipts": receipts,
                    }
                )
                result["fields"].update(fields)
                # raw_extraction is stored in JSONField, so Decimal values must not be written there.
                result["raw"].update(_json_safe(fields))
                result["status"] = "parsed"
                result["confidence"] = Decimal("0.970")
                result["warnings"] = [f"Распознано ЖД-бланков: {len(receipts)}. Проверьте данные перед сохранением."]
            return result
        return result

    wrapped._safe_layout_patch = True
    services.extract_receipt_fields = wrapped
