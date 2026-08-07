from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation

from documents.receipt_quality_guard import apply_receipt_quality_guard


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _present(value) -> bool:
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


def _segment_route_ready(row: dict, *, require_number: bool = True) -> bool:
    origin = row.get("fromCode") or row.get("from")
    destination = row.get("toCode") or row.get("to")
    if not (_present(origin) and _present(destination) and _present(row.get("date"))):
        return False
    if require_number and not _present(row.get("flightNo")):
        return False
    return True


def _rail_receipts_ready(fields: dict) -> bool:
    receipts = fields.get("receipts") or fields.get("railTickets") or []
    if not isinstance(receipts, list) or not receipts:
        return False
    for receipt in receipts:
        if not isinstance(receipt, dict):
            return False
        passenger = receipt.get("passenger") or receipt.get("passenger_name")
        ticket = receipt.get("ticketNo") or receipt.get("ticket_number")
        total = _decimal(receipt.get("total"))
        segments = receipt.get("segments") or receipt.get("legs") or []
        segment = next((row for row in segments if isinstance(row, dict)), None)
        if not (_present(passenger) and _present(ticket) and total > 0 and segment):
            return False
        if not _segment_route_ready(segment):
            return False
        if not (_present(segment.get("coach")) and _present(segment.get("seat"))):
            return False
    expected = fields.get("source_coupon_pages")
    if expected is not None:
        try:
            if int(expected) != len(receipts):
                return False
        except (TypeError, ValueError):
            return False
    return True


def strong_receipt_result(result: dict) -> bool:
    if not isinstance(result, dict) or str(result.get("status") or "").lower() != "parsed":
        return False
    if _decimal(result.get("confidence")) < Decimal("0.970"):
        return False
    warnings = [str(item) for item in (result.get("warnings") or [])]
    if any(item.startswith("Нужно проверить") for item in warnings):
        return False

    fields = result.get("fields") or {}
    if not isinstance(fields, dict):
        return False
    kind = str(fields.get("service_kind") or "").lower()
    segments = _segments(fields)

    if kind == "rail":
        if _rail_receipts_ready(fields):
            return True
        return bool(
            _present(fields.get("passenger_name"))
            and _present(fields.get("ticket_number"))
            and _decimal(fields.get("total")) > 0
            and segments
            and all(_segment_route_ready(row) for row in segments)
        )

    if kind == "avia":
        return bool(
            _present(fields.get("passenger_name"))
            and _present(fields.get("ticket_number"))
            and _decimal(fields.get("total")) > 0
            and segments
            and all(_segment_route_ready(row) for row in segments)
        )

    if kind == "hotel":
        hotel = fields.get("hotel") if isinstance(fields.get("hotel"), dict) else {}
        rooms = fields.get("rooms") if isinstance(fields.get("rooms"), list) else []
        passengers = fields.get("passengers") if isinstance(fields.get("passengers"), list) else []
        stay = segments[0] if segments else {}
        return bool(
            (_present(fields.get("passenger_name")) or passengers)
            and _present(hotel.get("name") or fields.get("issuer"))
            and rooms
            and _present(stay.get("date"))
            and _present(stay.get("endDate"))
        )

    if kind == "transfer":
        return bool(
            _present(fields.get("passenger_name"))
            and segments
            and all(_segment_route_ready(row, require_number=False) for row in segments)
        )

    return False


def install_receipt_recognition_performance_patch() -> None:
    from documents import receipt_recognition_engine as engine

    if getattr(engine, "_performance_patch_installed", False):
        return

    original = engine._enhance_pdf_result

    def optimized(original_parser, content: bytes, *, mime: str, name: str, initial: dict) -> dict:
        if strong_receipt_result(initial):
            result = apply_receipt_quality_guard(deepcopy(initial))
            raw = result.setdefault("raw", {})
            if not isinstance(raw, dict):
                raw = {}
                result["raw"] = raw
            raw["recognition_performance_path"] = "high_confidence_initial"
            engine._mark_diagnostics(
                result,
                sources=["current_pipeline:high_confidence_fastpath"],
                candidate_count=1,
                chosen_score=engine._result_score(result),
            )
            return result
        return original(original_parser, content, mime=mime, name=name, initial=initial)

    engine._enhance_pdf_result = optimized
    engine._performance_patch_installed = True
