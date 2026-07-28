import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO


def _pages(content):
    pages = []
    try:
        from pypdf import PdfReader

        for page in PdfReader(BytesIO(content), strict=False).pages:
            try:
                pages.append(re.sub(r"[ \t]+", " ", (page.extract_text() or "").replace("\r", "\n")))
            except Exception:
                continue
    except Exception:
        pass
    return [page for page in pages if page.strip()]


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


def _unique(values):
    return list(dict.fromkeys(value for value in values if value))


def _rail_segments_need_replacement(segments):
    if not segments:
        return True
    invalid_labels = re.compile(
        r"часов(?:ой|ые)\s+пояс|поездом|контрольн(?:ый|ого)\s+купон|"
        r"правил[аы]\s+проезд|номер\s+поезд|вагон|место",
        re.IGNORECASE,
    )
    for segment in segments:
        route_values = [str(segment.get("from") or ""), str(segment.get("to") or "")]
        if any(len(value) > 100 or invalid_labels.search(value) for value in route_values):
            return True
    return False


def _clean(value):
    cleaned = re.sub(r"\s+", " ", value or "")
    cleaned = re.sub(r"(?<=[а-яё])(?=[А-ЯЁ])", " ", cleaned)
    for joined, separated in {
        "большойдвуспальной": "большой двуспальной",
        "оплатыдополнительных": "оплаты дополнительных",
        "времяпроживания": "время проживания",
        "позднемзаезде": "позднем заезде",
        "регистрацииработает": "регистрации работает",
        "отменитьбронирование": "отменить бронирование",
        "условиямитарифа": "условиями тарифа",
        "гостя вотель": "гостя в отель",
    }.items():
        cleaned = re.sub(re.escape(joined), separated, cleaned, flags=re.I)
    return cleaned.strip(" \n\t,")


def _match_value(text, patterns):
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.S)
        if match:
            return _clean(match.group(1))
    return ""


def _full_date(value):
    match = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{2})", value or "")
    return f"{match.group(1)}.{match.group(2)}.20{match.group(3)}" if match else value


def _nights(segments):
    if not segments:
        return ""
    try:
        start = datetime.strptime(segments[0].get("date", ""), "%d.%m.%Y")
        end = datetime.strptime(segments[0].get("endDate", ""), "%d.%m.%Y")
    except (TypeError, ValueError):
        return ""
    return max((end - start).days, 0)


def _hotel_details(text, fields):
    flat = _clean(text)
    hotel_name = _clean(fields.get("issuer"))
    bilingual_name = re.match(r"(.+?\d\s*\*)\s*[A-Z][A-Za-z]", hotel_name)
    if bilingual_name:
        hotel_name = _clean(bilingual_name.group(1))
    segment = (fields.get("segments") or [{}])[0]
    room_name = re.sub(r",?\s*для$", "", _clean(segment.get("flightNo")), flags=re.I)

    address = _match_value(
        flat,
        [
            r"Адрес\s*Address\s+(.+?)(?=\s+Телефон\s*Phone\b)",
            r"Адрес:\s*(.+?)(?=\s+Детали\s+размещения\b)",
        ],
    )
    partner = re.search(
        r"Размещение\s+забронировано\s+нашим\s+партнером\s+"
        r"(?P<hotel>.+?)\s+(?P<address>\d{4,6}.+?)\s+"
        r"(?P<phone>\+?\d[\d ()-]{7,})\s+Заезд\b",
        flat,
        re.I,
    )
    if partner:
        hotel_name = hotel_name or _clean(partner.group("hotel"))
        address = address or _clean(partner.group("address"))

    city_country = re.search(
        r"Город:\s*(?P<city>.+?),\s*(?P<country>.+?)\s+Название\s+отеля:",
        flat,
        re.I,
    )
    city = _clean(city_country.group("city")) if city_country else ""
    country = _clean(city_country.group("country")) if city_country else ""
    if not country and re.search(r"(?:\bРоссия\b|\bRussia\b)", address, re.I):
        country = "Россия"
    if not city:
        for candidate in ("Москва", "Пекин", "Обь"):
            if re.search(rf"\b{candidate}\b", address, re.I):
                city = candidate
                break

    phone = _match_value(
        flat,
        [
            r"Телефон\s*Phone\s+(\+?\d[\d ()-]{7,})(?=\s+Электронный)",
        ],
    )
    if not phone and partner:
        phone = _clean(partner.group("phone"))
    email = _match_value(
        flat,
        [r"Электронный\s+адрес\s*Email\s+([\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,})"],
    )
    map_value = _match_value(flat, [r"\bGPS\s+(-?\d{1,3}[.,]\d+\s+-?\d{1,3}[.,]\d+)"])
    category = _match_value(hotel_name, [r"\b(\d)\s*\*"])

    bed_type = _match_value(flat, [r"Кровати:\s*(.+?)(?=\s+Гости:)"])
    if not bed_type and re.search(r"двуспальн\w*\s+кроват", room_name, re.I):
        bed_type = "Двуспальная кровать"
    adults_value = _match_value(flat, [r"\bдля\s+(\d+)\s+взросл"])
    children_value = _match_value(flat, [r"\b(\d+)\s+(?:детей|реб[её]нк)"])

    meal_text = _match_value(
        flat,
        [
            r"Тип\s+питания\s*Meal\s+type\s+(.+?)(?=\s+Номер\s+бронирования)",
            r"Питание:\s*(.+?)(?=\s+Дата\s+заезда)",
            r"\bПитание\s+(Питание\s+не\s+включено|Завтрак\s+включ[её]н)(?=\s+(?:Депозит|GPS))",
        ],
    )
    meal_lower = meal_text.lower()
    if "не включ" in meal_lower:
        meal = "Без питания"
    elif "завтрак" in meal_lower:
        meal = "Завтрак"
    elif "полупансион" in meal_lower:
        meal = "Полупансион"
    elif "полный пансион" in meal_lower:
        meal = "Полный пансион"
    elif "all inclusive" in meal_lower:
        meal = "All Inclusive"
    else:
        meal = "Другое" if meal_text else "Без питания"

    guest_names = [
        _clean(name)
        for name in re.split(r"\s*,\s*", fields.get("passenger_name") or "")
        if _clean(name)
    ]
    passengers = [
        {
            "name": name,
            "dob": "",
            "document": "",
            "ticketNo": "",
            "guestType": "Взрослый",
        }
        for name in guest_names
    ]
    adults = int(adults_value) if adults_value else max(len(passengers), 1)
    children = int(children_value) if children_value else 0

    early = _match_value(flat, [r"Ранний\s+заезд\s+в\s+(\d{1,2}:\d{2})"])
    late = _match_value(flat, [r"Поздний\s+выезд\s+в\s+(\d{1,2}:\d{2})"])
    cancellation = _match_value(
        flat,
        [
            r"Аннуляция\s*/\s*Изменение:\s*(.+?)(?=\s+Дополнительно:)",
            r"(При\s+отмене\s+или\s+изменении\s+заказа.+?договора\s*\.)",
            r"Условия\s+отмены\s+и\s+изменения\s+заказа\s+(.+?)(?=\s+Пожалуйста,\s+предупредите)",
        ],
    )
    amendment = cancellation
    no_show = _match_value(
        flat,
        [
            r"(В\s+случае\s+незаезда.+?штраф\w*\s+санкци\w*(?:\s+за\s+«?no-show»?)?\s*\.)",
            r"(При\s+аннуляции\s+заказа\s+или\s+неявке.+?тарифа\s*\.)",
        ],
    )
    important = _match_value(
        flat,
        [
            r"Важная\s+информация\s+(.+?)(?=\s+Условия\s+отмены)",
            r"(Если\s+Вы\s+прибываете\s+в\s+отель.+?«?no-show»?\s*\.)",
            r"(При\s+заселении\s+обязательно.+?удостоверяющий\s+личность\s*\.)",
        ],
    )
    deposit = _match_value(
        flat,
        [
            r"Депозит\s+Залог\s+(.+?)(?=\s+GPS\b)",
            r"(кредитную\s+карту\s+или\s+наличн\w+\s+депозит.+?(?:и\s+др|проживания)\s*\.)",
        ],
    )
    guest_comment = _match_value(
        flat,
        [
            r"(Пожалуйста,\s+предупредите\s+заранее.+?штрафн\w+\s+санкци\w+\s+за\s+незаезд\s*\.)",
            r"(При\s+заселении\s+обязательно.+?удостоверяющий\s+личность\s*\.)",
        ],
    )

    supplier_order = _match_value(
        flat,
        [
            r"Номер\s+заказа\s+в\s+системе\s*бронирования\s*Order\s+number\s+in\s+the\s+booking\s+system\s+(\d+)",
            r"\bБронирование\s+(\d+)\s+от\b",
        ],
    )
    hotel_booking = _match_value(
        flat,
        [
            r"Номер\s+бронирования\s*Booking\s+reference\s+number\s+(\d+)",
            r"Номер\s+бронирования:\s*(.+?)(?=\s+Аннуляция)",
        ],
    )
    reference = _clean(fields.get("reference"))
    if not supplier_order and reference and reference != "рования":
        supplier_order = reference
    if not hotel_booking and reference and reference != "рования":
        hotel_booking = reference

    issue_date = _clean(fields.get("issue_date")) or _match_value(
        flat,
        [
            r"Дата\s+выдачи\s*Date\s+of\s+issue\s+(\d{2}\.\d{2}\.\d{4})",
            r"\bБронирование\s+\d+\s+от\s+(\d{2}\.\d{2}\.\d{2,4})",
            r"Забронировано:\s*(\d{2}\.\d{2}\.\d{4})",
        ],
    )

    room = {
        "category": room_name,
        "name": room_name,
        "bedType": bed_type,
        "adults": adults,
        "children": children,
        "meal": meal,
        "earlyCheckIn": early,
        "lateCheckOut": late,
        "guestIds": guest_names,
        "conditions": meal_text,
    }
    possible_fee = "Может взиматься отелем и оплачиваться гостем напрямую."
    return {
        "issuer": hotel_name,
        "passengers": passengers,
        "issueDate": _full_date(issue_date),
        "supplierOrderNo": supplier_order,
        "hotelBookingNo": hotel_booking,
        "bookingStatus": "Подтверждено",
        "hotel": {
            "name": hotel_name,
            "category": f"{category}*" if category else "",
            "country": country,
            "city": city,
            "address": address,
            "phone": phone,
            "email": email,
            "map": map_value,
        },
        "rooms": [room],
        "nights": _nights(fields.get("segments") or []),
        "hotelTerms": {
            "deposit": deposit,
            "cityTax": possible_fee if re.search(r"городск\w+\s+налог", flat, re.I) else "",
            "resortFee": possible_fee if re.search(r"resort/facility\s+fee", flat, re.I) else "",
            "registrationFee": (
                possible_fee if re.search(r"регистрационн\w+\s+сбор", flat, re.I) else ""
            ),
            "cancellation": cancellation,
            "noShow": no_show,
            "amendment": amendment,
            "important": important,
            "guestComment": guest_comment,
        },
    }


def _transfer_details(text, fields):
    flat = _clean(text)
    passenger_name = _clean(fields.get("passenger_name"))
    phone = ""
    if passenger_name:
        phone = _match_value(
            flat,
            [rf"{re.escape(passenger_name)}\s+(\+?\d[\d ()-]{{7,}}?)(?=\s+(?:Аэропорт|Калининград))"],
        )
    address = _match_value(
        flat,
        [
            r"(Mercure\s+Kaliningrad,\s*Озерный\s+пр\.,\s*2,\s*Калининград,\s*"
            r"Калининградская\s+обл\.,\s*Россия,\s*236040)"
        ],
    )
    flight = _match_value(flat, [r"\b([A-ZА-Я]{2}-?\d{2,4})\s+терминал\b"])
    vehicle_class = _match_value(flat, [r"\b(Комфорт)\s+Комфорт\s+(\d+)\s+пассажир"])
    passenger_count = _match_value(flat, [r"\bКомфорт\s+Комфорт\s+(\d+)\s+пассажир"])
    segments = [dict(segment) for segment in fields.get("segments") or []]
    if segments and address:
        segments[0].update(
            {
                "fromAddress": segments[0].get("from") or "",
                "toAddress": address,
                "flightNo": flight,
            }
        )
    if len(segments) > 1:
        segments[1].update(
            {
                "fromAddress": address,
                "toAddress": segments[1].get("to") or "",
            }
        )

    issue_date = _match_value(flat, [r"\bот\s+(\d{2}\.\d{2}\.\d{4})\s+Пассажиры\b"])
    cancellation_deadlines = _unique(
        re.findall(r"до\s+(\d{1,2}:\d{2}\s+\d{2}\.\d{2}\.\d{4})", flat, re.I)
    )
    cancellation = ""
    if re.search(r"Условия\s+изменения\s+и\s+отмены.+?бесплатно\s+за\s+5\s+часов", flat, re.I):
        cancellation = "Бесплатная отмена не позднее чем за 5 часов до каждой поездки."
        if cancellation_deadlines:
            cancellation += f" Крайние сроки: {', '.join(cancellation_deadlines)}."
    support = _match_value(
        flat,
        [r"Телефоны\s+круглосуточной\s+службы\s+поддержки\s+(\d{8,})"],
    )
    has_waiting = bool(
        re.search(
            r"(?:Время\s+бесплатного\s+ожидания|при\s+подаче\s+по\s+адресу.+?20\s+минут)",
            flat,
            re.I,
        )
    )
    has_meet = bool(re.search(r"персональн\w+\s+табличк", flat, re.I))
    has_baggage_help = bool(re.search(r"помощь\s+с\s+багажом", flat, re.I))
    has_requirements = bool(re.search(r"(?:\bдети\b|крупногабаритн\w+\s+багаж)", flat, re.I))
    has_driver_notice = bool(re.search(r"номер\w*\s+телефон\w*\s+водителя", flat, re.I))
    has_phone_notice = bool(re.search(r"Включите\s+телефон", flat, re.I))
    return {
        "issueDate": issue_date,
        "supplierOrderNo": _clean(fields.get("reference")),
        "passengers": [
            {
                "name": passenger_name,
                "phone": phone,
                "signText": "",
                "comment": "",
            }
        ]
        if passenger_name
        else [],
        "segments": segments,
        "vehicle": {
            "className": vehicle_class,
            "category": "",
            "passengers": passenger_count,
            "luggage": "",
            "requirements": (
                "Заранее сообщить о детях или крупногабаритном багаже."
                if has_requirements
                else ""
            ),
        },
        "transferTerms": {
            "cancellation": cancellation,
            "freeWaiting": (
                "20 минут при подаче по адресу или к отелю; 1 час после внутреннего "
                "или международного авиарейса; 20 минут после прибытия поезда."
                if has_waiting
                else ""
            ),
            "meetAndGreet": (
                "Водитель встретит пассажира с персональной табличкой в терминале "
                "аэропорта, на перроне вокзала или в холле гостиницы."
                if has_meet
                else ""
            ),
            "baggageHelp": (
                "Водитель поможет с багажом по пути к автомобилю."
                if has_baggage_help
                else ""
            ),
            "supportContacts": support,
            "supplierComment": (
                "Телефон водителя будет отправлен за 1 час до поездки. "
                "При задержке рейса или поезда дополнительная плата не взимается."
                if has_driver_notice
                else ""
            ),
            "driverComment": "",
            "passengerComment": (
                "Включите телефон после посадки. Если не можете найти водителя, "
                "позвоните в службу поддержки."
                if has_phone_notice
                else ""
            ),
        },
    }


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
    journey = re.search(
        r"\b(?P<train>\d{3,4}[А-ЯЁA-Z]{1,2})\s+"
        r"(?P<date>\d{2}\.\d{2}\.\d{4})\s+"
        r"(?P<dep>\d{1,2}:\d{2})\s+"
        r"(?P<coach>\d{2})[А-ЯЁA-Z]\s+"
        r"(?P<seat>\d{3})\s+"
        r"(?P<from>.+?)\s+-\s+(?P<to>.+?)\s+ПН\d{10}\b",
        flat,
    )
    train_data = re.search(
        r"ПОЕЗД ВАГОН МЕСТО\s*(\d{3})\s+(?:\d{3}\s*)?(\d{2})\s+(?:\d{2}\s*)?(\d{3})",
        flat,
    )
    fare_block = re.search(
        r"Оплата наличными\s+Билет\s+Плацкарта\s+НДС 0%\s+НДС 22%\s+"
        r"(?P<body>.+?)\s+Итого",
        flat,
        re.I,
    )
    fare_parts = (
        re.findall(r"(\d[\d ]*[,.]\d{2})\s*₽", fare_block.group("body"))
        if fare_block
        else []
    )
    totals = re.findall(r"Вкл\. НДС\s+([\d ]+[,.]\d{2})\s*₽", flat, re.I)
    if not (passenger and journey):
        return None
    order = re.search(r"Заказ:\s*(\d{10,20})", flat)
    total = (
        sum((_decimal(value) or Decimal("0") for value in fare_parts[:2]), Decimal("0"))
        if len(fare_parts) >= 2
        else (_decimal(totals[-1]) if totals else None)
    )
    ticket_cost = _decimal(fare_parts[0]) if fare_parts else total
    reserved_seat_cost = _decimal(fare_parts[1]) if len(fare_parts) > 1 else Decimal("0")
    train = journey.group("train")
    coach = journey.group("coach")
    seat = journey.group("seat")
    if train_data:
        train_number, header_coach, header_seat = train_data.groups()
        train = train or train_number
        coach = coach or header_coach
        seat = seat or header_seat
    leading = flat[: passport.start()] if passport else flat
    times = _unique(re.findall(r"\b(\d{1,2}:\d{2})\b", leading))
    dates = _unique(re.findall(r"\b(\d{2}\.\d{2}\.\d{4})\b", leading))
    carrier = re.search(r"Перевозчик:\s*(.+?)\s+ИНН\b", flat, re.I)
    booking_class = re.search(r"\b([123][А-ЯЁA-Z])\s+\1\b", flat)
    ticket_number = re.sub(r"\s+", "", ticket.group(1)) if ticket else ""
    if not ticket_number and order:
        ticket_number = order.group(1)
    return {
        "issuer": carrier.group(1).strip() if carrier else "ОАО РЖД",
        "passenger_name": passenger.group(1).strip(),
        "reference": order.group(1) if order else "",
        "ticket_number": ticket_number,
        "document_number": passport.group(1) if passport else "",
        "date_of_birth": passport.group(2) if passport else "",
        "booking_class": booking_class.group(1) if booking_class else "",
        "fare": total,
        "taxes": Decimal("0") if total is not None else None,
        "fees": Decimal("0") if total is not None else None,
        "total": total,
        "ticketCost": ticket_cost,
        "reservedSeatCost": reserved_seat_cost,
        "agencyServiceFee": Decimal("0"),
        "additionalFees": Decimal("0"),
        "currency": "RUB",
        "service_kind": "rail",
        "service_type": "ЖД",
        "trip_type": "oneway",
        "segments": [
            {
                "from": journey.group("from").strip(),
                "fromCode": "",
                "to": journey.group("to").strip(),
                "toCode": "",
                "date": journey.group("date"),
                "dep": journey.group("dep"),
                "arr": times[1] if len(times) > 1 else "",
                "endDate": dates[1] if len(dates) > 1 else journey.group("date"),
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
                details = _hotel_details(joined, result["fields"])
                result["fields"].update(details)
                result["raw"].update(_json_safe({**fields, **details}))
                result["status"] = "parsed"
                result["confidence"] = Decimal("0.960")
                result["warnings"] = ["Стоимость в исходном ваучере не указана; остальные поля распознаны."]
            return result

        result_fields = result.get("fields") or {}
        if result_fields.get("service_kind") == "hotel":
            details = _hotel_details(joined, result_fields)
            result_fields.update(details)
            result["raw"].update(_json_safe(details))
            return result

        if result_fields.get("service_kind") == "rail":
            receipts = [receipt for receipt in (_rail(page) for page in pages) if receipt]
            if receipts:
                total = sum((receipt["total"] or Decimal("0") for receipt in receipts), Decimal("0"))
                ticket_cost = sum((receipt["ticketCost"] or Decimal("0") for receipt in receipts), Decimal("0"))
                reserved_seat_cost = sum(
                    (receipt["reservedSeatCost"] or Decimal("0") for receipt in receipts),
                    Decimal("0"),
                )
                passengers = _unique(receipt["passenger_name"] for receipt in receipts)
                tickets = _unique(receipt["ticket_number"] for receipt in receipts)
                segments = []
                segment_keys = set()
                for receipt in receipts:
                    segment = receipt["segments"][0]
                    key = (
                        segment["from"],
                        segment["to"],
                        segment["date"],
                        segment["dep"],
                        segment["flightNo"],
                    )
                    if key not in segment_keys:
                        segment_keys.add(key)
                        segments.append(segment)
                roundtrip = bool(
                    len(segments) > 1
                    and segments[0]["from"] == segments[-1]["to"]
                    and segments[0]["to"] == segments[-1]["from"]
                )
                preferred_segments = (
                    segments
                    if _rail_segments_need_replacement(result_fields.get("segments") or [])
                    else result_fields["segments"]
                )
                fields = dict(receipts[0])
                fields.update(
                    {
                        "passenger_name": ", ".join(passengers),
                        "ticket_number": ", ".join(tickets),
                        "total": total,
                        "fare": total,
                        "taxes": Decimal("0"),
                        "fees": Decimal("0"),
                        "ticketCost": ticket_cost,
                        "reservedSeatCost": reserved_seat_cost,
                        "agencyServiceFee": Decimal("0"),
                        "additionalFees": Decimal("0"),
                        "segments": preferred_segments,
                        "trip_type": (
                            "roundtrip"
                            if roundtrip and preferred_segments is segments
                            else result_fields.get("trip_type") or "oneway"
                        ),
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

        if result_fields.get("service_kind") == "transfer":
            details = _transfer_details(joined, result_fields)
            result_fields.update(details)
            result["raw"].update(_json_safe(details))
        return result

    wrapped._safe_layout_patch = True
    services.extract_receipt_fields = wrapped
