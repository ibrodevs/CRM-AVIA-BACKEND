from decimal import Decimal
import re

SERVICE_KIND_LABELS = {
    "avia": "Авиа",
    "rail": "ЖД",
    "hotel": "Гостиница",
    "transfer": "Трансфер",
    "other": "Прочее",
}


def json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def _first_value(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return ""


def _baggage_allowance(value) -> bool:
    return bool(re.fullmatch(r"\d+(?:[.,]\d+)?\s*(?:PC|KG|КГ|КМ)", str(value or "").strip(), re.IGNORECASE))


def _normalize_receipt_segment(segment):
    source = json_safe(segment) if isinstance(segment, dict) else {}
    normalized = {
        **source,
        "from": _first_value(
            source.get("from"),
            source.get("origin"),
            source.get("origin_name"),
            source.get("departure_city"),
            source.get("departure_airport"),
        ),
        "fromCode": _first_value(
            source.get("fromCode"),
            source.get("from_code"),
            source.get("originCode"),
            source.get("origin_code"),
            source.get("origin_iata"),
            source.get("departure_airport_code"),
        ),
        "to": _first_value(
            source.get("to"),
            source.get("destination"),
            source.get("destination_name"),
            source.get("arrival_city"),
            source.get("arrival_airport"),
        ),
        "toCode": _first_value(
            source.get("toCode"),
            source.get("to_code"),
            source.get("destinationCode"),
            source.get("destination_code"),
            source.get("destination_iata"),
            source.get("arrival_airport_code"),
        ),
        "fromAddress": _first_value(source.get("fromAddress"), source.get("from_address")),
        "toAddress": _first_value(source.get("toAddress"), source.get("to_address")),
        "date": _first_value(
            source.get("date"),
            source.get("departureDate"),
            source.get("departure_date"),
            source.get("flight_date"),
        ),
        "endDate": _first_value(source.get("endDate"), source.get("end_date"), source.get("arrival_date")),
        "dep": _first_value(source.get("dep"), source.get("departureTime"), source.get("departure_time")),
        "arr": _first_value(source.get("arr"), source.get("arrivalTime"), source.get("arrival_time")),
        "duration": _first_value(source.get("duration"), source.get("flight_duration")),
        "carrier": _first_value(
            source.get("carrier"),
            source.get("airline"),
            source.get("airline_name"),
            source.get("marketing_carrier"),
        ),
        "flightNo": _first_value(
            source.get("flightNo"),
            source.get("flight_no"),
            source.get("flight_number"),
            source.get("number"),
        ),
        "coach": _first_value(source.get("coach"), source.get("coach_number")),
        "seat": _first_value(source.get("seat"), source.get("seat_number")),
        "cls": _first_value(
            source.get("cls"),
            source.get("bookingClass"),
            source.get("booking_class"),
            source.get("class_code"),
        ),
        "status": _first_value(
            source.get("status"),
            source.get("bookingStatus"),
            source.get("booking_status"),
            source.get("segment_status"),
            source.get("confirmation_status"),
        ),
        "fareBasis": _first_value(
            source.get("fareBasis"),
            source.get("fare_basis"),
            source.get("tariff_code"),
        ),
        "cabin": _first_value(
            source.get("cabin"),
            source.get("cabinClass"),
            source.get("cabin_class"),
            source.get("service_class"),
        ),
        "baggage": _first_value(
            source.get("baggage"),
            source.get("baggage_allowance"),
            source.get("checked_baggage"),
        ),
        "handBaggage": _first_value(
            source.get("handBaggage"),
            source.get("hand_baggage"),
            source.get("carryOn"),
            source.get("carry_on"),
        ),
        "dir": _first_value(source.get("dir"), source.get("direction"), "out"),
    }
    if not normalized["cabin"]:
        booking_class = str(normalized["cls"] or "").strip().upper()
        normalized["cabin"] = {
            "ECONOMY": "ECONOMY",
            "ЭКОНОМ": "ECONOMY",
            "ЭКОНОМИЧЕСКИЙ": "ECONOMY",
            "BUSINESS": "BUSINESS",
            "БИЗНЕС": "BUSINESS",
            "FIRST": "FIRST",
            "ПЕРВЫЙ": "FIRST",
        }.get(booking_class, "")
    if (
        not normalized["handBaggage"]
        and _baggage_allowance(normalized["fareBasis"])
        and _baggage_allowance(normalized["baggage"])
    ):
        normalized["handBaggage"] = normalized["baggage"]
        normalized["baggage"] = normalized["fareBasis"]
        normalized["fareBasis"] = ""
    return normalized


def receipt_verified_data(fields: dict, *, parser_status: str) -> dict:
    source = json_safe(fields or {})
    passenger = source.get("passenger") or source.get("passenger_name") or ""
    raw_segments = source.get("legs") or source.get("segments") or []
    segments = [_normalize_receipt_segment(segment) for segment in raw_segments]
    first_segment = segments[0] if segments else {}
    service_kind = source.get("service_kind") or "other"
    service_type = source.get("service_type") or SERVICE_KIND_LABELS.get(service_kind, "Прочее")
    total = source.get("total")
    verified = {
        **source,
        "carrier": _first_value(
            source.get("carrier"),
            source.get("issuer"),
            source.get("airline"),
            first_segment.get("carrier"),
        ),
        "passenger": passenger,
        "passengers": source.get("passengers")
        or (
            [
                {
                    "name": passenger,
                    "dob": source.get("dob") or source.get("date_of_birth") or "",
                    "document": source.get("docNo") or source.get("document_number") or "",
                    "ticketNo": source.get("ticketNo") or source.get("ticket_number") or "",
                    "ref": source.get("ref") or source.get("reference") or source.get("pnr") or "",
                }
            ]
            if passenger
            else []
        ),
        "legs": segments,
        "ref": _first_value(
            source.get("ref"),
            source.get("reference"),
            source.get("pnr"),
            source.get("booking_reference"),
        ),
        "ticketNo": _first_value(source.get("ticketNo"), source.get("ticket_number"), source.get("ticket_no")),
        "docNo": source.get("docNo") or source.get("document_number") or "",
        "dob": source.get("dob") or source.get("date_of_birth") or "",
        "issueDate": source.get("issueDate") or source.get("issue_date") or "",
        "bookingStatus": _first_value(
            source.get("bookingStatus"),
            source.get("booking_status"),
            source.get("reservation_status"),
            first_segment.get("status"),
        ),
        "cls": _first_value(source.get("cls"), source.get("booking_class"), first_segment.get("cls")),
        "fareBasis": _first_value(source.get("fareBasis"), source.get("fare_basis"), first_segment.get("fareBasis")),
        "handBaggage": source.get("handBaggage") or source.get("hand_baggage") or "",
        "tripType": source.get("tripType")
        or source.get("trip_type")
        or ("stay" if service_kind == "hotel" else "oneway"),
        "fareBreakdown": source.get("fareBreakdown") or source.get("fare_breakdown") or [],
        "taxBreakdown": source.get("taxBreakdown") or source.get("tax_breakdown") or [],
        "feeBreakdown": source.get("feeBreakdown") or source.get("fee_breakdown") or [],
        "originalTotal": source.get("originalTotal", total or 0),
        "recognitionPending": parser_status != "parsed",
        "service_kind": service_kind,
        "service_type": service_type,
    }
    if service_kind == "hotel":
        verified.setdefault(
            "hotel",
            {
                "name": source.get("issuer") or first_segment.get("to", ""),
                "category": "",
                "country": "",
                "city": "",
                "address": "",
                "phone": "",
                "email": "",
                "map": "",
            },
        )
        verified.setdefault(
            "rooms",
            [
                {
                    "category": "",
                    "name": first_segment.get("flightNo", ""),
                    "bedType": "",
                    "adults": 1,
                    "children": 0,
                    "meal": "Без питания",
                    "earlyCheckIn": "",
                    "lateCheckOut": "",
                    "guestIds": [passenger] if passenger else [],
                    "conditions": "",
                }
            ],
        )
    return verified


def receipt_document_metadata(
    current: dict,
    *,
    import_id,
    extraction: dict,
    file_name: str,
    mime: str,
    size: int,
    stage: str | None = None,
) -> dict:
    fields = extraction.get("fields") or {}
    parser_status = extraction.get("status") or "manual_review"
    verified = receipt_verified_data(fields, parser_status=parser_status)
    service_kind = verified.get("service_kind") or "other"
    supplier_original = {
        **((current or {}).get("supplier_original") or {}),
        "name": file_name,
        "mime": mime,
        "size": size,
        "verified_data": verified,
    }
    receipt_import = {
        **((current or {}).get("receipt_import") or {}),
        "stage": stage or ("recognized" if parser_status == "parsed" else "manual_review"),
        "import_id": str(import_id),
        "parser_status": parser_status,
        "confidence": str(extraction.get("confidence") or ""),
        "warnings": json_safe(extraction.get("warnings") or []),
        "service_kind": service_kind,
        "service_type": verified.get("service_type") or SERVICE_KIND_LABELS.get(service_kind, "Прочее"),
        "verified_data": verified,
    }
    return {
        **(current or {}),
        "supplier_original": supplier_original,
        "receipt_import": receipt_import,
    }
