import re
from decimal import Decimal, InvalidOperation
from io import BytesIO


def _clean(text: str) -> str:
    text = (text or "").replace("\xa0", " ").replace("\r", "\n")
    return re.sub(r"[ \t]+", " ", text)


def _amount(value: str):
    try:
        return Decimal(re.sub(r"\s+", "", value).replace(",", "."))
    except (InvalidOperation, AttributeError):
        return None


def _page_texts(content: bytes) -> list[str]:
    try:
        from pypdf import PdfReader

        return [_clean(page.extract_text() or "") for page in PdfReader(BytesIO(content), strict=False).pages]
    except Exception:
        return []


def _hotel_fields(text: str) -> dict:
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
        flags=re.IGNORECASE,
    )
    hotel = re.search(
        r"voucher upon arrival\s+(.+?\d?\*)\s+\d{2}\.\d{2}\.\d{4}",
        flat,
        flags=re.IGNORECASE,
    )
    booking = re.search(
        r"(?:Номер\s*бронирования|Booking\s*reference\s*number)\s*"
        r"(?:Booking\s*reference\s*number)?\s*(\d{5,20})",
        flat,
        flags=re.IGNORECASE,
    )
    order = re.search(r"Order number in the booking system\s*(\d{5,20})", flat, flags=re.IGNORECASE)
    room = re.search(
        r"(?:Тип\s*номера|Room\s*type)\s*(?:Room\s*type)?\s*(.+?)(?=\s+(?:Тип\s*питания|Meal\s*type))",
        flat,
        flags=re.IGNORECASE,
    )
    issue = re.search(r"Date of issue\s*(\d{2}\.\d{2}\.\d{4})", flat, flags=re.IGNORECASE)
    if not (stay and guest and hotel):
        return {}
    passenger = re.sub(r"^(?:MR|MRS|MS)\s+", "", guest.group(1).strip(), flags=re.IGNORECASE)
    hotel_name = hotel.group(1).strip()
    return {
        "issuer": hotel_name,
        "passenger_name": passenger,
        "reference": booking.group(1) if booking else (order.group(1) if order else ""),
        "ticket_number": booking.group(1) if booking else "",
        "issue_date": issue.group(1) if issue else "",
        "segments": [{
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
        }],
        "trip_type": "stay",
        "service_kind": "hotel",
        "service_type": "Гостиница",
    }


def _rail_page(text: str) -> dict:
    flat = re.sub(r"\s+", " ", text)
    ticket = re.search(r"№\s*([\d ]{13,24})", flat)
    passport = re.search(r"ПАСПОРТ РФ\s*(\d{10})\s*(\d{2}\.\d{2}\.\d{4})", flat)
    passenger = re.search(
        r"ПАСПОРТ РФ\s*\d{10}\s*\d{2}\.\d{2}\.\d{4}\s*RUS\s*[МMЖF]\s*"
        r"([А-ЯЁ][А-ЯЁ\-]+(?:\s+[А-ЯЁ][А-ЯЁ\-]+){1,3})(?=\s+Посадка)",
        flat,
    )
    route = re.search(
        r"(\d{1,2}:\d{2})\s+(?:\1\s*)?(\d{2}\.\d{2}\.\d{4}).*?"
        r"Санкт-Петербург-Главн\..*?"
        r"(\d{1,2}:\d{2})\s+(?:\3\s*)?(\d{2}\.\d{2}\.\d{4}).*?Москва Октябрьская",
        flat,
        flags=re.DOTALL,
    )
    train_data = re.search(r"ПОЕЗД ВАГОН МЕСТО\s*(\d{3})\s+(?:\1\s*)?(\d{2})\s+(?:\2\s*)?(\d{3})", flat)
    total_matches = re.findall(r"Итого\s+(?:Вкл\. НДС\s+)?([\d ]+[,.]\d{2})\s*₽", flat, flags=re.IGNORECASE)
    if not total_matches:
        total_matches = re.findall(r"Вкл\. НДС\s+([\d ]+[,.]\d{2})\s*₽", flat, flags=re.IGNORECASE)
    if not (ticket and passenger and route):
        return {}
    number = re.sub(r"\s+", "", ticket.group(1))
    total = _amount(total_matches[-1]) if total_matches else None
    train = train_data.group(1) if train_data else ""
    coach = train_data.group(2) if train_data else ""
    seat = train_data.group(3) if train_data else ""
    return {
        "issuer": "ОАО РЖД",
        "passenger_name": passenger.group(1).strip(),
        "reference": re.search(r"Заказ:\s*(\d{10,20})", flat).group(1) if re.search(r"Заказ:\s*(\d{10,20})", flat) else "",
        "ticket_number": number,
        "document_number": passport.group(1) if passport else "",
        "date_of_birth": passport.group(2) if passport else "",
        "booking_class": "1С" if re.search(r"БИЗНЕС КЛАСС\s*1С", flat) else "",
        "fare": total,
        "taxes": Decimal("0") if total is not None else None,
        "fees": Decimal("0") if total is not None else None,
        "total": total,
        "currency": "RUB",
        "segments": [{
            "from": "Санкт-Петербург-Главн.",
            "fromCode": "",
            "to": "Москва Октябрьская",
            "toCode": "",
            "date": route.group(2),
            "dep": route.group(1),
            "arr": route.group(3),
            "flightNo": train,
            "coach": coach,
            "seat": seat,
            "dir": "out",
        }],
        "trip_type": "oneway",
        "service_kind": "rail",
        "service_type": "ЖД",
    }


def install_receipt_parser_patch() -> None:
    from documents import services

    if getattr(services.extract_receipt_fields, "_layout_patch", False):
        return
    original = services.extract_receipt_fields

    def extract_receipt_fields(content: bytes, *, mime: str = "", name: str = "") -> dict:
        result = original(content, mime=mime, name=name)
        if not (mime == "application/pdf" or content.startswith(b"%PDF")):
            return result
        pages = _page_texts(content)
        if not pages:
            return result

        all_text = "\n".join(pages)
        if re.search(r"Ваучер\s+отеля|Hotel\s+voucher", all_text, flags=re.IGNORECASE):
            fields = _hotel_fields(pages[0])
            if fields:
                result["fields"].update(fields)
                result["raw"].update(fields)
                result["status"] = "parsed"
                result["confidence"] = Decimal("0.960")
                result["warnings"] = ["Стоимость в исходном ваучере не указана; остальные поля распознаны."]
            return result

        if re.search(r"ЭЛЕКТРОННЫЙ БИЛЕТ\. КОНТРОЛЬНЫЙ КУПОН", all_text):
            receipts = [item for item in (_rail_page(page) for page in pages) if item]
            if receipts:
                first = receipts[0]
                total = sum((item["total"] or Decimal("0") for item in receipts), Decimal("0"))
                aggregate = dict(first)
                aggregate["passenger_name"] = ", ".join(item["passenger_name"] for item in receipts)
                aggregate["ticket_number"] = ", ".join(item["ticket_number"] for item in receipts)
                aggregate["total"] = total
                aggregate["fare"] = total
                aggregate["segments"] = [item["segments"][0] for item in receipts]
                aggregate["receipt_count"] = len(receipts)
                aggregate["receipts"] = receipts
                result["fields"].update(aggregate)
                result["raw"].update(aggregate)
                result["status"] = "parsed"
                result["confidence"] = Decimal("0.970")
                result["warnings"] = [f"Распознано ЖД-бланков: {len(receipts)}. Проверьте данные перед сохранением."]
            return result
        return result

    extract_receipt_fields._layout_patch = True
    services.extract_receipt_fields = extract_receipt_fields
