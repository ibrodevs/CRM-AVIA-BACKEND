from __future__ import annotations

import re


def _clean_supplier_booking(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    compact = re.sub(r"\s+", " ", re.sub(r"[«»\"'.,:;()]", "", raw)).strip().lower()
    if re.fullmatch(r"(?:рования|бронирования|номер бронирования|бронь|номер брони)", compact):
        return ""
    if re.fullmatch(r"(?:заселение|размещение) по фио", compact):
        return ""
    return raw


def _clean_hotel_payload(payload: dict) -> None:
    if not isinstance(payload, dict):
        return
    kind = str(payload.get("service_kind") or "").lower()
    service_type = str(payload.get("service_type") or "").lower()
    if kind != "hotel" and "гостини" not in service_type and "отел" not in service_type:
        return

    for key in ("supplier_order_number", "supplierOrderNo", "order_number", "reference", "ref"):
        if key in payload:
            payload[key] = _clean_supplier_booking(payload.get(key))


def install_receipt_hotel_booking_guard() -> None:
    from documents import services

    if getattr(services.extract_receipt_fields, "_hotel_booking_guard", False):
        return

    original = services.extract_receipt_fields

    def wrapped(content: bytes, *, mime: str = "", name: str = ""):
        result = original(content, mime=mime, name=name)
        _clean_hotel_payload(result.get("fields") or {})
        _clean_hotel_payload(result.get("raw") or {})
        return result

    wrapped._hotel_booking_guard = True
    services.extract_receipt_fields = wrapped
