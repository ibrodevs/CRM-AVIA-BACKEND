from __future__ import annotations

from decimal import Decimal

from documents.receipt_multiform_patch import _aggregate_rail_receipts
from documents.receipt_parser_patch_safe import _json_safe, _rail
from documents.receipt_problem_formats_patch import _parse_s7_ticket, _pdf_pages, _replace_result


def _base_result() -> dict:
    return {
        "fields": {},
        "raw": {},
        "warnings": [],
        "status": "pending",
        "confidence": Decimal("0"),
    }


def _parse_known_pages(pages: list[str]) -> dict | None:
    """Parse formats that are safer to recognise before the generic parser.

    The generic extraction stack may throw on some supplier PDFs before later
    fallbacks get a chance to run. RZD control coupons and the known S7 layout
    have a deterministic text layer, so they are handled first and can never be
    turned into a fatal upload error by a less-specific parser.
    """

    pages = [page or "" for page in pages]
    coupon_pages = [page for page in pages if "КОНТРОЛЬНЫЙ КУПОН" in page]
    if coupon_pages:
        result = _base_result()
        receipts = [receipt for receipt in (_rail(page) for page in coupon_pages) if receipt]
        if len(receipts) != len(coupon_pages):
            result["status"] = "manual_review"
            result["confidence"] = Decimal("0.490")
            result["raw"]["source_coupon_pages"] = len(coupon_pages)
            result["raw"]["parsed_coupon_pages"] = len(receipts)
            result["warnings"] = [
                f"Нужно проверить вручную: распознано ЖД-бланков {len(receipts)} "
                f"из {len(coupon_pages)}."
            ]
            return result

        aggregate = _aggregate_rail_receipts(receipts, {})
        result["fields"].update(aggregate)
        result["raw"].update(_json_safe(aggregate))
        result["raw"]["source_coupon_pages"] = len(coupon_pages)
        result["raw"]["parsed_coupon_pages"] = len(receipts)
        result["status"] = "parsed"
        result["confidence"] = Decimal("0.995")
        result["warnings"] = [
            f"Проверено ЖД-бланков: {len(receipts)} из {len(coupon_pages)}. "
            "Каждая страница сохранена как отдельный билет с пассажиром, "
            "номером билета, поездом, вагоном, местом, маршрутом и стоимостью."
        ]
        return result

    joined = "\n".join(page for page in pages if page.strip())
    if joined:
        s7 = _parse_s7_ticket(joined)
        if s7:
            return _replace_result(
                _base_result(),
                s7,
                warning=(
                    f"S7 распознан полностью до запуска общего OCR: {len(s7['segments'])} сегм.; "
                    "пассажир, билет, маршрут, рейсы, тариф, таксы и итог проверены."
                ),
            )
    return None


def _parser_failure(exc: Exception) -> dict:
    result = _base_result()
    result["status"] = "manual_review"
    result["confidence"] = Decimal("0")
    result["warnings"] = [
        "Общий распознаватель не смог обработать файл автоматически. "
        "Файл сохранён и доступен для ручной проверки; повторная загрузка не требуется."
    ]
    result["raw"]["parser_exception"] = exc.__class__.__name__
    return result


def install_receipt_preflight_patch() -> None:
    from documents import services

    if getattr(services.extract_receipt_fields, "_receipt_preflight_patch", False):
        return
    original = services.extract_receipt_fields

    def wrapped(content: bytes, *, mime: str = "", name: str = ""):
        is_pdf = mime == "application/pdf" or content.startswith(b"%PDF")
        if is_pdf:
            pages = _pdf_pages(content)
            known = _parse_known_pages(pages) if pages else None
            if known is not None:
                return known

        try:
            return original(content, mime=mime, name=name)
        except Exception as exc:
            # A parser bug must never turn a successfully uploaded supplier PDF
            # into a fatal API error. Preserve the document for manual review.
            return _parser_failure(exc)

    wrapped._receipt_preflight_patch = True
    services.extract_receipt_fields = wrapped
