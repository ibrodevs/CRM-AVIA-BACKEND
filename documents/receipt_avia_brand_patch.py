import re
from decimal import Decimal


_LEGAL_ENTITY_PREFIX = re.compile(
    r"^(?:ИП|ООО|АО|ПАО|ОАО|ЗАО|ОсОО|ТОО|LLC|JSC)\b",
    re.IGNORECASE,
)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip(" ,;:\t")


def _is_legal_entity_name(value: str) -> bool:
    return bool(_LEGAL_ENTITY_PREFIX.search(_clean(value)))


def _airport_name(value: str, code: str) -> str:
    cleaned = _clean(value)
    cleaned = re.sub(rf",?\s*{re.escape(code)}(?:\s*\([^)]*\))?$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" ,") or code


def _passenger_details(text: str) -> dict:
    header = re.search(
        r"Пассажир\s+Номер документа\s+Номер билета\s+Бонусная карта\s+Продажа\s*\n(?P<row>[^\n]+)",
        text,
        flags=re.IGNORECASE,
    )
    if not header:
        return {}
    row = _clean(header.group("row"))
    match = re.match(
        r"(?P<name>.+?)\s+(?P<document>\d{6,14})\s+"
        r"(?P<ticket>\d{3}\s+\d{8,12})\s+(?P<bonus>\d{4,})\s+"
        r"(?P<sale>\d{2}\.\d{2}\.\d{4})$",
        row,
    )
    if not match:
        return {}
    return {
        "passenger_name": _clean(match.group("name")),
        "document_number": match.group("document"),
        "ticket_number": _clean(match.group("ticket")),
        "loyalty_card": match.group("bonus"),
        "issue_date": match.group("sale"),
    }


def parse_brand_avia_ticket(text: str) -> dict:
    """Parse airline tickets where seller details are printed above the itinerary.

    Some PDF text layers expose a legal entity header such as
    ``ИП Хмель Марина Валерьевна`` before the actual route. The generic route
    fallback can split that full name into two endpoints. This parser anchors
    the itinerary to IATA airport codes and the airline header instead.
    """
    if not re.search(r"ЭЛЕКТРОННЫЙ БИЛЕТ", text or "", flags=re.IGNORECASE):
        return {}

    airline_match = re.search(
        r"Рейс под брендом авиакомпании\s+([^\n]+)",
        text,
        flags=re.IGNORECASE,
    )
    route_match = re.search(
        r"(?m)^(?P<from>.+?,\s*(?P<from_code>[A-Z]{3}))\s+"
        r"(?P<to>.+?,\s*(?P<to_code>[A-Z]{3})(?:\s*\([^)]*\))?)\s+"
        r"Авиакомпания[^\n]*$",
        text,
        flags=re.IGNORECASE,
    )
    if not airline_match or not route_match:
        return {}

    airline = _clean(airline_match.group(1))
    if _is_legal_entity_name(airline):
        return {}

    tariff_start = re.search(r"РАСЧ[ЕЁ]Т ТАРИФА", text[route_match.end() :], flags=re.IGNORECASE)
    section_end = route_match.end() + tariff_start.start() if tariff_start else len(text)
    section = text[route_match.end() : section_end]

    times = re.findall(r"\b\d{1,2}:\d{2}\b", section)
    dates = re.findall(
        r"\b(?:Пн|Вт|Ср|Чт|Пт|Сб|Вс),?\s*\d{1,2}\s+[А-ЯЁа-яё]+\s+20\d{2}\b",
        section,
        flags=re.IGNORECASE,
    )
    flight = re.search(
        r"\b(?P<number>[A-ZА-Я]{2}-?\d{2,5})\s+"
        r"(?P<cabin>BUSINESS|ECONOMY|PREMIUM|БИЗНЕС|ЭКОНОМ)\s+"
        r"(?P<fare>[A-Z0-9-]{2,24})\s+"
        r"(?P<baggage>\d+\s*(?:KG|PC|КГ)(?:\s*[xх]\s*\d+)?)\s+"
        r"(?P<status>[A-ZА-Я]{2,4})\b",
        section,
        flags=re.IGNORECASE,
    )

    from_code = route_match.group("from_code").upper()
    to_code = route_match.group("to_code").upper()
    segment = {
        "from": _airport_name(route_match.group("from"), from_code),
        "fromCode": from_code,
        "to": _airport_name(route_match.group("to"), to_code),
        "toCode": to_code,
        "date": dates[0] if dates else "",
        "endDate": dates[1] if len(dates) > 1 else (dates[0] if dates else ""),
        "dep": times[0] if times else "",
        "arr": times[1] if len(times) > 1 else "",
        "carrier": airline,
        "flightNo": _clean(flight.group("number")) if flight else "",
        "cls": _clean(flight.group("cabin")) if flight else "",
        "cabin": _clean(flight.group("cabin")) if flight else "",
        "fareBasis": _clean(flight.group("fare")) if flight else "",
        "baggage": _clean(flight.group("baggage")) if flight else "",
        "status": _clean(flight.group("status")) if flight else "",
        "dir": "out",
    }

    details = {
        "carrier": airline,
        "airline": airline,
        "issuer": airline,
        "segments": [segment],
        "trip_type": "oneway",
        "booking_class": segment["cls"],
        "fare_basis": segment["fareBasis"],
        "baggage": segment["baggage"],
        "booking_status": segment["status"],
    }
    details.update(_passenger_details(text))
    return details


def install_receipt_avia_brand_patch():
    from documents import services

    if getattr(services.extract_receipt_fields, "_avia_brand_patch", False):
        return
    original = services.extract_receipt_fields

    def wrapped(content: bytes, *, mime: str = "", name: str = ""):
        result = original(content, mime=mime, name=name)
        if not (mime == "application/pdf" or content.startswith(b"%PDF")):
            return result

        text = services._extract_pdf_text(content)
        details = parse_brand_avia_ticket(text)
        if not details:
            return result

        fields = result.setdefault("fields", {})
        if fields.get("service_kind") not in {None, "", "avia"}:
            return result
        fields.update(details)
        fields["service_kind"] = "avia"
        fields["service_type"] = "Авиа"
        result.setdefault("raw", {}).update(details)
        result["status"] = "parsed"
        result["confidence"] = max(
            Decimal(str(result.get("confidence") or 0)),
            Decimal("0.980"),
        )
        result["warnings"] = [
            warning
            for warning in result.get("warnings", [])
            if "маршрут" not in str(warning).lower()
        ]
        return result

    wrapped._avia_brand_patch = True
    services.extract_receipt_fields = wrapped
