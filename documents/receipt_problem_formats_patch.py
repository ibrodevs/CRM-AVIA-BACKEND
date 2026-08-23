from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from io import BytesIO

from pypdf import PdfReader

from documents.receipt_multiform_patch import _aggregate_rail_receipts
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


def _decimal(value: str | None) -> Decimal:
    try:
        return Decimal(re.sub(r"\s+", "", value or "0").replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _pdf_pages(content: bytes) -> list[str]:
    pages: list[str] = []
    try:
        reader = PdfReader(BytesIO(content), strict=False)
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                pages.append("")
    except Exception:
        return []
    return pages


def _ru_date(value: str) -> str:
    match = re.search(r"(\d{1,2})\s+([А-Яа-яЁё]+)\s+(\d{4})", value or "")
    if not match:
        return ""
    month = RU_MONTHS.get(match.group(2).lower())
    if not month:
        return ""
    return f"{int(match.group(1)):02d}.{month:02d}.{match.group(3)}"


def _city_name(value: str, code: str) -> str:
    return re.sub(
        rf",\s*{re.escape(code)}(?:\s+\([^)]*\))?$",
        "",
        value or "",
        flags=re.IGNORECASE,
    ).strip()


def _money(flat: str, label: str) -> Decimal:
    match = re.search(
        rf"{label}\s*:\s*RUB\s*(\d[\d ]*(?:[,.]\d{{1,2}})?)",
        flat,
        flags=re.IGNORECASE,
    )
    return _decimal(match.group(1)) if match else Decimal("0")


def _parse_s7_ticket(text: str) -> dict | None:
    """Parse the Passenger Service Center S7 itinerary layout.

    These PDFs have two materially different text-layer layouts in the wild:
    pdfminer can either preserve the table columns or collapse almost the whole
    page into one line.  pypdf, however, consistently preserves the route rows,
    so this parser intentionally works from normalized pypdf lines and does not
    depend on labels living on their own lines.
    """

    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in (text or "").splitlines()
        if re.sub(r"\s+", " ", line).strip()
    ]
    flat = " ".join(lines)
    if (
        "S7 Airlines" not in flat
        or "ЭЛЕКТРОННЫЙ БИЛЕТ" not in flat
        or not re.search(r"маршрут-квитанц", flat, re.IGNORECASE)
    ):
        return None

    order_match = re.search(r"Заказ\s*№\s*(\d+)", flat, re.IGNORECASE)
    reference_match = re.search(r"код\s+бронирования:\s*([A-Z0-9]+)", flat, re.IGNORECASE)
    passenger_row = re.search(
        r"Пассажир\s+Номер\s+документа\s+Номер\s+билета\s+Бонусная\s+карта\s+Продажа\s+"
        r"(?P<passenger>[A-ZА-ЯЁ][A-ZА-ЯЁ \-]{3,100}?)\s+"
        r"(?P<document>\d{10})\s+"
        r"(?P<ticket>\d{3}\s+\d{10}|\d{13})"
        r"(?:\s+(?P<loyalty>\d{6,15}))?\s+"
        r"(?P<sale>\d{2}\.\d{2}\.\d{4})",
        flat,
        flags=re.IGNORECASE,
    )
    if not passenger_row:
        return None

    route_pattern = re.compile(
        r"^(?P<from>.+?,\s*(?P<fromCode>[A-Z]{3})(?:\s+\([^)]*\))?)\s+"
        r"(?P<to>.+?,\s*(?P<toCode>[A-Z]{3})(?:\s+\([^)]*\))?)\s+"
        r"Авиакомпания-$"
    )
    segments: list[dict] = []
    for index, line in enumerate(lines):
        route = route_pattern.match(line)
        if not route:
            continue

        block_lines: list[str] = []
        for value in lines[index + 1 :]:
            if route_pattern.match(value) or value == "РАСЧЕТ ТАРИФА:":
                break
            block_lines.append(value)
            if len(block_lines) >= 10:
                break
        block = " ".join(block_lines)
        times = [value for value in block_lines if re.fullmatch(r"\d{1,2}:\d{2}", value)]
        dates = [parsed for parsed in (_ru_date(value) for value in block_lines) if parsed]
        flight_match = re.search(r"\b(S7-\d{3,5})\b", block, re.IGNORECASE)
        cabin_match = re.search(r"\b(ECONOMY|BUSINESS)\b", block, re.IGNORECASE)
        fare_basis_match = re.search(
            r"\bS7-\d{3,5}\s+(?:ECONOMY|BUSINESS)\s+([A-Z0-9-]{2,20})\b",
            block,
            re.IGNORECASE,
        )
        baggage_match = re.search(r"\b(\d+\s*PC)\b", block, re.IGNORECASE)
        status_match = re.search(r"\b(OK|CONFIRMED)\b", block, re.IGNORECASE)

        if not flight_match or len(times) < 2 or not dates:
            continue

        segment = {
            "from": _city_name(route.group("from"), route.group("fromCode")),
            "fromCode": route.group("fromCode"),
            "to": _city_name(route.group("to"), route.group("toCode")),
            "toCode": route.group("toCode"),
            "date": dates[0],
            "endDate": dates[1] if len(dates) > 1 else dates[0],
            "dep": times[0],
            "arr": times[1],
            "flightNo": flight_match.group(1).upper(),
            "carrier": "S7 Airlines",
            "cls": cabin_match.group(1).upper() if cabin_match else "",
            "cabin": cabin_match.group(1).upper() if cabin_match else "",
            "fareBasis": fare_basis_match.group(1).upper() if fare_basis_match else "",
            "baggage": baggage_match.group(1).replace(" ", "").upper() if baggage_match else "",
            "status": status_match.group(1).upper() if status_match else "",
            "dir": "out" if not segments else "seg",
        }
        if segments and segment["toCode"] == segments[0]["fromCode"]:
            segment["dir"] = "back"
        segments.append(segment)

    if not segments:
        return None

    passenger = passenger_row.group("passenger").strip()
    document_number = passenger_row.group("document")
    ticket_number = re.sub(r"\s+", " ", passenger_row.group("ticket")).strip()
    issue_date = passenger_row.group("sale")
    loyalty_card = passenger_row.group("loyalty") or ""
    fare = _money(flat, r"ТАРИФ")
    taxes = _money(flat, r"СБОР/TAX")
    total = _money(flat, r"ВСЕГО\s+К\s+ОПЛАТЕ")
    fees = max(total - fare - taxes, Decimal("0")) if total else Decimal("0")

    tax_breakdown: list[dict] = []
    tax_block = re.search(
        r"СБОР/TAX\s*:\s*RUB\s*\d[\d ]*(?:[,.]\d{1,2})?\s*(?P<body>.*?)\s+ВСЕГО\s+К\s+ОПЛАТЕ",
        flat,
        flags=re.IGNORECASE,
    )
    if tax_block:
        for code, amount in re.findall(r"\b([A-Z]{2,3})(\d+(?:[,.]\d{1,2})?)RUB\b", tax_block.group("body")):
            tax_breakdown.append(
                {
                    "code": code.upper(),
                    "label": code.upper(),
                    "amount": str(_decimal(amount)),
                    "currency": "RUB",
                }
            )

    hand_baggage_match = re.search(r"Ручная\s+кладь\s+(\d+\s*кг)", flat, re.IGNORECASE)
    booking_classes = list(dict.fromkeys(segment.get("cabin") for segment in segments if segment.get("cabin")))
    fare_bases = list(dict.fromkeys(segment.get("fareBasis") for segment in segments if segment.get("fareBasis")))
    baggage_values = list(dict.fromkeys(segment.get("baggage") for segment in segments if segment.get("baggage")))
    statuses = list(dict.fromkeys(segment.get("status") for segment in segments if segment.get("status")))
    roundtrip = bool(
        len(segments) > 1
        and segments[0]["fromCode"] == segments[-1]["toCode"]
        and segments[0]["toCode"] == segments[-1]["fromCode"]
    )

    return {
        "issuer": "S7 Airlines",
        "passenger_name": passenger,
        "passengers": [
            {
                "name": passenger,
                "dob": "",
                "document": document_number,
                "ticketNo": ticket_number,
                "loyaltyCard": loyalty_card,
            }
        ],
        "reference": reference_match.group(1) if reference_match else "",
        "ticket_number": ticket_number,
        "document_number": document_number,
        "date_of_birth": "",
        "issue_date": issue_date,
        "supplier_order_number": order_match.group(1) if order_match else "",
        "booking_class": " / ".join(booking_classes),
        "fare_basis": " / ".join(fare_bases),
        "baggage": " / ".join(baggage_values),
        "hand_baggage": hand_baggage_match.group(1) if hand_baggage_match else "",
        "booking_status": " / ".join(statuses),
        "fare": fare,
        "taxes": taxes,
        "fees": fees,
        "total": total,
        "originalTotal": total,
        "currency": "RUB",
        "fare_breakdown": [
            {
                "code": "FARE",
                "label": "Тариф",
                "amount": str(fare),
                "currency": "RUB",
            }
        ],
        "tax_breakdown": tax_breakdown,
        "fee_breakdown": [],
        "service_kind": "avia",
        "service_type": "Авиа",
        "trip_type": "roundtrip" if roundtrip else ("complex" if len(segments) > 1 else "oneway"),
        "segments": segments,
    }


def _parse_azimuth_ticket(text: str) -> dict | None:
    """Parse PSC itinerary receipts issued for Azimuth.

    The supplier places the cabin, fare basis, baggage and status on the line
    after the flight.  Generic column heuristics used to shift the header
    ``Статус`` into baggage and leave the segment class/fare empty.
    """

    lines = [_clean for _clean in (
        re.sub(r"\s+", " ", line).strip() for line in (text or "").splitlines()
    ) if _clean]
    flat = " ".join(lines)
    if "авиакомпании Азимут" not in flat or "ЭЛЕКТРОННЫЙ БИЛЕТ" not in flat:
        return None

    passenger_row = re.search(
        r"Продажа\s+(?P<passenger>[А-ЯЁA-Z][А-ЯЁA-Z \-]+?)\s+"
        r"(?:Г-Н\(ГОА\)\s+)?(?P<dob>\d{2}[A-Z]{3}\d{4})\s+"
        r"(?P<document>\d{10})\s+(?P<ticket>\d{3}\s+\d{10})\s+"
        r"(?P<sale>\d{2}\.\d{2}\.\d{4})",
        flat,
    )
    # Anchor the route directly after the Azimuth brand line.  Without this
    # boundary the generic ``[^.]{2,80}`` prefix can start inside the sale date
    # and produce values such as ``2024 Рейс под брендом ... Киров``.
    route = re.search(
        r"Рейс\s+под\s+брендом\s+авиакомпании\s+Азимут\s+"
        r"(?P<from>.+?),\s*(?P<fromCode>[A-Z]{3})\s+"
        r"(?P<to>.+?),\s*(?P<toCode>[A-Z]{3})\s+Авиакомпания-",
        flat,
        re.IGNORECASE,
    )
    segment_row = re.search(
        r"(?P<dep>\d{2}:\d{2})\s+(?P<depDate>[А-Яа-яЁё]{2},\s+\d{1,2}\s+[А-Яа-яЁё]+\s+\d{4})\s+"
        r"(?P<arr>\d{2}:\d{2})\s+(?P<arrDate>[А-Яа-яЁё]{2},\s+\d{1,2}\s+[А-Яа-яЁё]+\s+\d{4})\s+"
        r"Азимут\s+(?P<flight>A4-\d{3,5})\s+(?P<cabin>ECONOMY|BUSINESS)\s+"
        r"(?P<fare>[A-Z0-9-]{2,20})\s+(?P<baggage>\d+\s*(?:М|PC|КГ|KG))\s+(?P<status>OK|CONFIRMED)",
        flat,
        re.IGNORECASE,
    )
    if not passenger_row or not route or not segment_row:
        return None

    total_match = re.search(r"Стоимость:\s*([\d ]+(?:[,.]\d{2})?)\s*руб", flat, re.I)
    fee_values = [
        _decimal(value)
        for value in re.findall(r"в\s+том\s+числе\s+сбор\s+(?:АСБ|СА):\s*([\d ]+(?:[,.]\d{2})?)", flat, re.I)
    ]
    total = _decimal(total_match.group(1)) if total_match else Decimal("0")
    fees = sum(fee_values, Decimal("0"))
    fare = max(total - fees, Decimal("0"))
    baggage_raw = re.sub(r"\s+", "", segment_row.group("baggage")).upper()
    baggage = re.sub(r"М$", "PC", baggage_raw)
    passenger = passenger_row.group("passenger").strip()
    ticket = passenger_row.group("ticket").strip()
    cabin = segment_row.group("cabin").upper()
    fare_basis = segment_row.group("fare").upper()
    status = segment_row.group("status").upper()
    segment = {
        "from": route.group("from").strip(), "fromCode": route.group("fromCode"),
        "to": route.group("to").strip(), "toCode": route.group("toCode"),
        "date": _ru_date(segment_row.group("depDate")),
        "endDate": _ru_date(segment_row.group("arrDate")),
        "dep": segment_row.group("dep"), "arr": segment_row.group("arr"),
        "flightNo": segment_row.group("flight").upper(), "carrier": "Азимут",
        "cls": cabin, "cabin": cabin, "fareBasis": fare_basis,
        "baggage": baggage, "handBaggage": "", "status": status, "dir": "out",
    }
    order = re.search(r"Заказ\s*№\s*(\d+)", flat, re.I)
    reference = re.search(r"код\s+бронирования:\s*([A-Z0-9]+)", flat, re.I)
    return {
        "issuer": "Азимут", "passenger_name": passenger,
        "passengers": [{
            "name": passenger, "dob": passenger_row.group("dob"),
            "document": passenger_row.group("document"), "ticketNo": ticket,
        }],
        "reference": reference.group(1) if reference else "", "ticket_number": ticket,
        "document_number": passenger_row.group("document"),
        "date_of_birth": passenger_row.group("dob"), "issue_date": passenger_row.group("sale"),
        "supplier_order_number": order.group(1) if order else "",
        "booking_class": cabin, "fare_basis": fare_basis, "baggage": baggage,
        "hand_baggage": "", "booking_status": status,
        "fare": fare, "taxes": Decimal("0"), "fees": fees, "total": total,
        "derived_financial_fields": ["fare"],
        "originalTotal": total, "currency": "RUB", "segments": [segment],
        "service_kind": "avia", "service_type": "Авиа", "trip_type": "oneway",
        "fare_breakdown": [{"code": "FARE", "label": "Тариф", "amount": str(fare), "currency": "RUB"}],
        "tax_breakdown": [],
        "fee_breakdown": ([{"code": "ASB", "label": "Сбор АСБ/СА", "amount": str(fees), "currency": "RUB"}] if fees else []),
    }


def _replace_result(result: dict, parsed: dict, *, warning: str) -> dict:
    fields = result.setdefault("fields", {})
    if not isinstance(fields, dict):
        fields = {}
        result["fields"] = fields
    fields.update(parsed)
    raw = result.setdefault("raw", {})
    if isinstance(raw, dict):
        raw.update(_json_safe(parsed))

    warnings = [
        str(value)
        for value in (result.get("warnings") or [])
        if str(value).strip()
        and not re.search(r"не\s+распозн|не\s+удалось|частич", str(value), re.IGNORECASE)
    ]
    warnings.append(warning)
    result["warnings"] = list(dict.fromkeys(warnings))
    result["status"] = "parsed"
    result["confidence"] = Decimal("0.995")
    return result


def install_receipt_problem_formats_patch() -> None:
    from documents import services

    if getattr(services.extract_receipt_fields, "_problem_formats_patch", False):
        return
    original = services.extract_receipt_fields

    def wrapped(content: bytes, *, mime: str = "", name: str = ""):
        result = original(content, mime=mime, name=name)
        if not (mime == "application/pdf" or content.startswith(b"%PDF")):
            return result

        pages = _pdf_pages(content)
        if not pages:
            return result

        # RZD: every graphical control coupon is an independent receipt.  Re-run
        # the page parser here and require a one-to-one mapping so a future text
        # extraction regression cannot silently collapse 8 tickets into one row.
        coupon_pages = [page for page in pages if "КОНТРОЛЬНЫЙ КУПОН" in page]
        if coupon_pages:
            receipts = [receipt for receipt in (_rail(page) for page in coupon_pages) if receipt]
            if len(receipts) == len(coupon_pages):
                aggregate = _aggregate_rail_receipts(receipts, result.get("fields") or {})
                fields = result.setdefault("fields", {})
                fields.clear()
                fields.update(aggregate)
                raw = result.setdefault("raw", {})
                if isinstance(raw, dict):
                    raw.update(_json_safe(aggregate))
                    raw["source_coupon_pages"] = len(coupon_pages)
                result["status"] = "parsed"
                result["confidence"] = Decimal("0.995")
                warnings = [
                    str(value)
                    for value in (result.get("warnings") or [])
                    if str(value).strip() and "ЖД-бланков" not in str(value)
                ]
                warnings.append(
                    f"Проверено ЖД-бланков: {len(receipts)} из {len(coupon_pages)}. "
                    "Пассажир, билет, поезд, вагон, место, маршрут и стоимость сохранены отдельно."
                )
                result["warnings"] = list(dict.fromkeys(warnings))
            else:
                result["status"] = "manual_review"
                result["confidence"] = min(result.get("confidence") or Decimal("0"), Decimal("0.490"))
                warnings = list(result.get("warnings") or [])
                warnings.append(
                    f"Нужно проверить вручную: распознано ЖД-бланков {len(receipts)} из {len(coupon_pages)}."
                )
                result["warnings"] = list(dict.fromkeys(str(value) for value in warnings if str(value).strip()))
            return result

        joined_pypdf = "\n".join(pages)
        azimuth = _parse_azimuth_ticket(joined_pypdf)
        if azimuth:
            return _replace_result(
                result,
                azimuth,
                warning="Азимут распознан полностью: класс, код тарифа, багаж и статус сохранены в сегменте.",
            )
        s7 = _parse_s7_ticket(joined_pypdf)
        if s7:
            return _replace_result(
                result,
                s7,
                warning=(
                    f"S7 распознан полностью: {len(s7['segments'])} сегм.; "
                    "пассажир, билет, маршрут, рейсы, тариф, таксы и итог проверены по текстовому слою."
                ),
            )
        return result

    wrapped._problem_formats_patch = True
    services.extract_receipt_fields = wrapped
