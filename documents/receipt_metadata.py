from decimal import Decimal

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


def receipt_verified_data(fields: dict, *, parser_status: str) -> dict:
    source = json_safe(fields or {})
    passenger = source.get("passenger") or source.get("passenger_name") or ""
    segments = source.get("legs") or source.get("segments") or []
    service_kind = source.get("service_kind") or "other"
    service_type = source.get("service_type") or SERVICE_KIND_LABELS.get(service_kind, "Прочее")
    total = source.get("total")
    verified = {
        **source,
        "carrier": source.get("carrier") or source.get("issuer") or "",
        "passenger": passenger,
        "passengers": source.get("passengers")
        or (
            [
                {
                    "name": passenger,
                    "dob": source.get("dob") or source.get("date_of_birth") or "",
                    "document": source.get("docNo") or source.get("document_number") or "",
                    "ticketNo": source.get("ticketNo") or source.get("ticket_number") or "",
                }
            ]
            if passenger
            else []
        ),
        "legs": segments,
        "ref": source.get("ref") or source.get("reference") or "",
        "ticketNo": source.get("ticketNo") or source.get("ticket_number") or "",
        "docNo": source.get("docNo") or source.get("document_number") or "",
        "dob": source.get("dob") or source.get("date_of_birth") or "",
        "issueDate": source.get("issueDate") or source.get("issue_date") or "",
        "cls": source.get("cls") or source.get("booking_class") or "",
        "fareBasis": source.get("fareBasis") or source.get("fare_basis") or "",
        "handBaggage": source.get("handBaggage") or source.get("hand_baggage") or "",
        "tripType": source.get("tripType")
        or source.get("trip_type")
        or ("stay" if service_kind == "hotel" else "oneway"),
        "taxBreakdown": source.get("taxBreakdown") or source.get("tax_breakdown") or [],
        "feeBreakdown": source.get("feeBreakdown") or source.get("fee_breakdown") or [],
        "originalTotal": source.get("originalTotal", total or 0),
        "recognitionPending": parser_status != "parsed",
        "service_kind": service_kind,
        "service_type": service_type,
    }
    if service_kind == "hotel":
        first_segment = segments[0] if segments else {}
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
