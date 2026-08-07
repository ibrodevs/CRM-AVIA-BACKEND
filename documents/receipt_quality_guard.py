from __future__ import annotations

from decimal import Decimal
from typing import Any


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _segments(fields: dict) -> list[dict]:
    rows = fields.get("segments") or fields.get("legs") or []
    return [row for row in rows if isinstance(row, dict)]


def _passengers(fields: dict) -> list[dict]:
    rows = fields.get("passengers") or []
    return [row for row in rows if isinstance(row, dict) and _present(row.get("name"))]


def _has_passenger(fields: dict) -> bool:
    return _present(fields.get("passenger_name")) or bool(_passengers(fields))


def _has_ticket(fields: dict) -> bool:
    if _present(fields.get("ticket_number") or fields.get("ticketNo")):
        return True
    return any(_present(row.get("ticketNo") or row.get("ticket_number")) for row in _passengers(fields))


def _has_route(fields: dict) -> bool:
    for segment in _segments(fields):
        origin = segment.get("from") or segment.get("fromCode")
        destination = segment.get("to") or segment.get("toCode")
        if _present(origin) and _present(destination):
            return True
    return False


def _has_stay(fields: dict) -> bool:
    for segment in _segments(fields):
        if _present(segment.get("date")) and _present(segment.get("endDate")):
            return True
    rooms = fields.get("rooms") or []
    return any(
        isinstance(room, dict)
        and _present(room.get("checkInDate"))
        and _present(room.get("checkOutDate"))
        for room in rooms
    )


def _has_flight(fields: dict) -> bool:
    return any(_present(segment.get("flightNo")) for segment in _segments(fields))


def _has_rail_identity(fields: dict) -> bool:
    receipts = fields.get("receipts") or fields.get("railTickets") or []
    if isinstance(receipts, list) and receipts:
        for receipt in receipts:
            if not isinstance(receipt, dict):
                return False
            legs = receipt.get("segments") or receipt.get("legs") or []
            if not _present(receipt.get("ticket_number") or receipt.get("ticketNo")):
                return False
            if not _present(receipt.get("passenger") or receipt.get("passenger_name")):
                return False
            if not any(
                isinstance(leg, dict)
                and _present(leg.get("flightNo"))
                and _present(leg.get("seat"))
                for leg in legs
            ):
                return False
        return True
    return _has_ticket(fields) and any(
        _present(segment.get("flightNo")) for segment in _segments(fields)
    )


def _missing_fields(kind: str, fields: dict) -> list[str]:
    missing: list[str] = []
    if kind == "avia":
        if not _has_passenger(fields):
            missing.append("пассажир")
        if not _has_route(fields):
            missing.append("маршрут")
        if not _has_flight(fields):
            missing.append("номер рейса")
        if not _has_ticket(fields):
            missing.append("номер билета")
    elif kind == "rail":
        if not _has_passenger(fields):
            missing.append("пассажир")
        if not _has_route(fields):
            missing.append("маршрут")
        if not _has_rail_identity(fields):
            missing.append("номер билета / поезд / место")
    elif kind == "hotel":
        hotel = fields.get("hotel") if isinstance(fields.get("hotel"), dict) else {}
        if not _present(hotel.get("name") or fields.get("issuer")):
            missing.append("отель")
        if not _has_passenger(fields):
            missing.append("гость")
        if not _has_stay(fields):
            missing.append("даты проживания")
    elif kind == "transfer":
        if not _has_passenger(fields):
            missing.append("пассажир")
        if not _has_route(fields):
            missing.append("маршрут")
    return missing


def _clean_missing_money(fields: dict) -> None:
    """Do not turn absent supplier prices into authoritative zeroes."""
    source_values = [fields.get("fare"), fields.get("taxes"), fields.get("fees"), fields.get("total")]
    has_explicit_money = any(value not in (None, "", Decimal("0"), 0, "0", "0.0", "0.00") for value in source_values)
    if has_explicit_money:
        return
    for key in (
        "fare",
        "taxes",
        "fees",
        "total",
        "originalTotal",
        "ticketCost",
        "reservedSeatCost",
        "agencyServiceFee",
        "additionalFees",
    ):
        if fields.get(key) in (0, Decimal("0"), "0", "0.0", "0.00"):
            fields[key] = None


def apply_receipt_quality_guard(result: dict) -> dict:
    if not isinstance(result, dict):
        return result
    fields = result.setdefault("fields", {})
    if not isinstance(fields, dict):
        fields = {}
        result["fields"] = fields

    kind = str(fields.get("service_kind") or "other").lower()
    _clean_missing_money(fields)
    missing = _missing_fields(kind, fields)
    warnings = [str(item) for item in (result.get("warnings") or []) if str(item).strip()]

    if missing:
        warning = "Нужно проверить вручную: не распознано — " + ", ".join(missing) + "."
        if warning not in warnings:
            warnings.append(warning)
        result["status"] = "manual_review"
        result["confidence"] = min(result.get("confidence") or Decimal("0"), Decimal("0.490"))
    elif result.get("status") in {"error", "failed"}:
        result["status"] = "manual_review"
        if "Распознавание завершилось частично. Проверьте извлечённые поля." not in warnings:
            warnings.append("Распознавание завершилось частично. Проверьте извлечённые поля.")

    result["warnings"] = warnings
    raw = result.setdefault("raw", {})
    if isinstance(raw, dict):
        raw["quality_missing_fields"] = missing
        raw["quality_review_required"] = result.get("status") == "manual_review"
    return result


def install_receipt_quality_guard() -> None:
    from documents import services

    if getattr(services.extract_receipt_fields, "_quality_guard", False):
        return
    original = services.extract_receipt_fields

    def wrapped(content: bytes, *, mime: str = "", name: str = ""):
        return apply_receipt_quality_guard(original(content, mime=mime, name=name))

    wrapped._quality_guard = True
    services.extract_receipt_fields = wrapped
