from __future__ import annotations

from decimal import Decimal

from documents.receipt_multiform_patch import _aggregate_rail_receipts, _best_pages, _page_texts
from documents.receipt_parser_patch_safe import _json_safe, _rail
from documents.receipt_pdf_grouping import _aliases, _clean_child
from documents.receipt_quality_guard import apply_receipt_quality_guard

RZD_COUPON_MARKER = "КОНТРОЛЬНЫЙ КУПОН"


def _compact_passenger_summary(fields: dict, max_length: int = 255) -> None:
    """Keep the DB summary bounded while preserving every passenger in JSON.

    ReceiptDraft.passenger_name is a 255-char compatibility field. Group RZD
    files can contain far more passengers, so the full list must live in
    ``passengers``/``receipts`` while the summary remains safely persistable.
    """

    value = str(fields.get("passenger_name") or "").strip()
    if len(value) <= max_length:
        return

    names = [
        str(row.get("name") or "").strip()
        for row in (fields.get("passengers") or [])
        if isinstance(row, dict) and str(row.get("name") or "").strip()
    ]
    if not names:
        fields["passenger_name"] = value[: max_length - 1].rstrip(" ,") + "…"
        return

    selected: list[str] = []
    for index, name in enumerate(names):
        remaining = len(names) - index - 1
        suffix = f" +{remaining}" if remaining else ""
        candidate = ", ".join([*selected, name]) + suffix
        if len(candidate) > max_length:
            break
        selected.append(name)

    remaining = len(names) - len(selected)
    if selected:
        summary = ", ".join(selected)
        if remaining:
            summary += f" +{remaining}"
        fields["passenger_name"] = summary[:max_length]
    else:
        fields["passenger_name"] = names[0][: max_length - 1].rstrip() + "…"


def recognize_rzd_coupon_pages(pages: list[str]) -> dict | None:
    """Recognize RZD graphical control coupons without the generic OCR stack.

    pypdf duplicates some visible words in this layout, but the deterministic
    per-page RZD parser handles that representation reliably. Parsing each page
    independently is both more accurate and much cheaper than repeatedly
    running every PDF extractor against a multi-page group file.
    """

    coupon_pages = [
        (page_number, page)
        for page_number, page in enumerate(pages, start=1)
        if RZD_COUPON_MARKER in (page or "")
    ]
    if not coupon_pages:
        return None

    receipts: list[dict] = []
    failed_pages: list[int] = []
    for page_number, page in coupon_pages:
        try:
            receipt = _rail(page)
        except Exception:
            receipt = None
        if receipt:
            receipts.append(
                _clean_child(
                    receipt,
                    page_number=page_number,
                    index=len(receipts),
                )
            )
        else:
            failed_pages.append(page_number)

    if not receipts:
        return None

    fields = _aggregate_rail_receipts(receipts, {})
    items = [
        _clean_child(
            receipt,
            page_number=int(receipt.get("sourcePage") or receipt.get("receiptPage") or index + 1),
            index=index,
        )
        for index, receipt in enumerate(fields.get("receipts") or receipts)
    ]
    _aliases(fields, items)
    fields["source_coupon_pages"] = len(coupon_pages)
    _compact_passenger_summary(fields)

    complete = len(receipts) == len(coupon_pages)
    warnings = []
    if complete:
        warnings.append(
            f"Распознано ЖД-бланков: {len(receipts)} из {len(coupon_pages)}. "
            "Каждая страница сохранена как отдельный билет."
        )
    else:
        warnings.append(
            f"Нужно проверить вручную: распознано ЖД-бланков {len(receipts)} "
            f"из {len(coupon_pages)}; страницы без результата: "
            + ", ".join(str(value) for value in failed_pages)
            + "."
        )

    raw = _json_safe(fields)
    _aliases(raw, _json_safe(items))
    raw.update(
        {
            "rzd_fastpath": True,
            "source_coupon_pages": len(coupon_pages),
            "parsed_coupon_pages": len(receipts),
            "failed_coupon_pages": failed_pages,
        }
    )
    result = {
        "fields": fields,
        "raw": raw,
        "warnings": warnings,
        "status": "parsed" if complete else "manual_review",
        "confidence": Decimal("0.995") if complete else Decimal("0.490"),
    }
    return apply_receipt_quality_guard(result)


def _safe_parser_failure(exc: Exception) -> dict:
    return {
        "fields": {"service_kind": "other", "service_type": "Прочее"},
        "raw": {
            "outer_parser_exception": exc.__class__.__name__,
            "outer_parser_guard": True,
        },
        "warnings": [
            "Автоматическое распознавание не завершилось, но файл сохранён. "
            "Повторная загрузка не требуется — документ доступен для ручной проверки."
        ],
        "status": "manual_review",
        "confidence": Decimal("0"),
    }


def install_receipt_rzd_fastpath() -> None:
    from documents import services

    if getattr(services.extract_receipt_fields, "_rzd_fastpath", False):
        return
    original = services.extract_receipt_fields

    def wrapped(content: bytes, *, mime: str = "", name: str = ""):
        is_pdf = mime == "application/pdf" or content.startswith(b"%PDF")
        if is_pdf:
            try:
                pypdf_pages, pdfminer_pages = _page_texts(content)
                pages = _best_pages(pypdf_pages, pdfminer_pages)
                fast_result = recognize_rzd_coupon_pages(pages)
                if fast_result is not None:
                    return fast_result
            except Exception:
                # The fast path is an optimization. If it cannot inspect the
                # file, let the normal recognition stack try before giving up.
                pass

        try:
            return original(content, mime=mime, name=name)
        except Exception as exc:
            # Parser bugs/timeouts must not turn a valid uploaded supplier file
            # into a fatal API response. Keep it available for manual review.
            return _safe_parser_failure(exc)

    wrapped._rzd_fastpath = True
    services.extract_receipt_fields = wrapped
