from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from django.http import FileResponse
from rest_framework.views import APIView

from accounts.permissions import require
from documents.models import ReceiptImportJob
from documents.receipt_metadata import json_safe, receipt_verified_data
from documents.selectors import get_document_or_404


@dataclass(frozen=True)
class AmountTarget:
    key: str
    old: Decimal
    new: Decimal | str
    aliases: tuple[str, ...]
    page_index: int | None = None
    page_markers: tuple[str, ...] = ()


_FINANCIAL_FIELDS = (
    ("fare", ("ТАРИФ", "FARE")),
    ("taxes", ("СБОР/TAX", "TAX", "ТАКС")),
    ("fees", ("СБОР АСБ", "СБОР СА", "SERVICE FEE", "FEE", "СБОР")),
    ("total", (
        "ВСЕГО К ОПЛАТЕ",
        "ИТОГО ПО ТАРИФУ/СБОРАМ",
        "СУММА ПЛАТЕЖА",
        "ИТОГО",
        "TOTAL",
        "PAYMENT AMOUNT",
        "AMOUNT",
    )),
    ("ticketCost", ("БИЛЕТ", "TICKET")),
    ("reservedSeatCost", ("ПЛАЦКАРТА", "RESERVED SEAT")),
    ("agencyServiceFee", ("СЕРВИСНЫЙ СБОР", "SERVICE FEE", "СБОР")),
    ("additionalFees", ("ДОПОЛНИТЕЛЬНЫЕ СБОРЫ", "ADDITIONAL", "СБОР")),
)
_BREAKDOWNS = (
    ("fareBreakdown", ("ТАРИФ", "FARE")),
    ("taxBreakdown", ("TAX", "ТАКС", "СБОР")),
    ("feeBreakdown", ("FEE", "СБОР")),
)
_GROUP_KEYS = ("groupTickets", "receiptItems", "receipt_items", "receipts", "railTickets")


def _decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).replace("\u00a0", " ").replace(" ", "").replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _value(data: dict, key: str):
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
    return data.get(key, data.get(snake))


def _first_group(data: dict) -> list:
    for key in _GROUP_KEYS:
        value = data.get(key)
        if isinstance(value, list) and value:
            return value
    return []


def _ticket_page_markers(data: dict) -> tuple[str, ...]:
    if not isinstance(data, dict):
        return ()
    values = (
        _value(data, "blankId"),
        _value(data, "ticketNo"),
        data.get("ticket_number"),
        _value(data, "docNo"),
        data.get("document_number"),
        data.get("passenger"),
        data.get("passenger_name"),
    )
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value or "").strip()))


def _page_marker_token(value) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def _target_matches_page(target: AmountTarget, page_index: int, page) -> bool:
    """Match grouped tickets by their identity, not by their ordinal position.

    A single ticket can span several physical PDF pages. The editor's second
    blank is therefore not necessarily PDF page two (Aeroflot commonly uses
    pages 1–2 for ticket one and 3–4 for ticket two). Ticket/document markers
    locate the actual financial page; the ordinal index remains a fallback for
    older payloads that do not contain identity fields.
    """

    if target.page_markers:
        try:
            page_text = _page_marker_token(page.extract_text() or "")
        except Exception:
            page_text = ""
        return any(
            marker and marker in page_text
            for marker in (_page_marker_token(value) for value in target.page_markers)
        )
    return target.page_index is None or target.page_index == page_index


def _collect_targets(
    before: dict,
    after: dict,
    *,
    page_index: int | None = None,
    prefix: str = "",
    page_markers: tuple[str, ...] = (),
) -> list[AmountTarget]:
    targets: list[AmountTarget] = []
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    output = _value(after, "output")
    price_mode = str(output.get("priceMode") or output.get("price_mode") or "").strip().lower() if isinstance(output, dict) else ""
    for key, aliases in _FINANCIAL_FIELDS:
        old = _decimal(_value(before, key))
        new = _decimal(_value(after, key))
        if key == "fare" and price_mode in {"it", "закрыть как it", "closed_it"}:
            if old is not None:
                targets.append(AmountTarget(f"{prefix}fare.it", old, "IT", aliases, page_index, page_markers))
            continue
        if old is None or new is None or old == new:
            continue
        # A zero-valued component is usually absent from the supplier blank.
        # Replacing every printed zero is unsafe; the changed payable total is
        # still patched and reflects the new cost.
        if old == 0 and key != "total":
            continue
        targets.append(AmountTarget(f"{prefix}{key}", old, new, aliases, page_index, page_markers))
    for breakdown_key, fallback_aliases in _BREAKDOWNS:
        old_rows = _value(before, breakdown_key)
        new_rows = _value(after, breakdown_key)
        if not isinstance(old_rows, list) or not isinstance(new_rows, list):
            continue
        for index, (old_row, new_row) in enumerate(zip(old_rows, new_rows, strict=False)):
            if not isinstance(old_row, dict) or not isinstance(new_row, dict):
                continue
            old = _decimal(old_row.get("amount"))
            new = _decimal(new_row.get("amount"))
            if old is None or new is None or old == new:
                continue
            if old == 0:
                continue
            row_aliases = tuple(
                str(value).strip()
                for value in (old_row.get("code"), old_row.get("label"), new_row.get("code"), new_row.get("label"))
                if str(value or "").strip()
            )
            targets.append(AmountTarget(
                f"{prefix}{breakdown_key}[{index}]",
                old,
                new,
                row_aliases or fallback_aliases,
                page_index,
                page_markers,
            ))
    old_group = _first_group(before)
    new_group = _first_group(after)
    if old_group and new_group:
        for index, (old_child, new_child) in enumerate(
            zip(old_group, new_group, strict=False)
        ):
            if isinstance(old_child, dict) and isinstance(new_child, dict):
                targets.extend(_collect_targets(
                    old_child,
                    new_child,
                    page_index=index,
                    prefix=f"{prefix}receipt[{index}].",
                    page_markers=_ticket_page_markers(old_child) or _ticket_page_markers(new_child),
                ))
    deduped: dict[tuple, AmountTarget] = {}
    for target in targets:
        deduped[(target.key, target.old, target.new, target.page_index)] = target
    return list(deduped.values())


def _amount_variants(value: Decimal) -> list[str]:
    result: set[str] = set()
    absolute = abs(value)
    for decimals in (0, 2):
        if decimals == 0 and absolute != absolute.to_integral_value():
            continue
        rendered = f"{absolute:.{decimals}f}"
        whole, _, fraction = rendered.partition(".")
        grouped = f"{int(whole):,}".replace(",", " ")
        result.update((whole, grouped))
        if decimals:
            result.update((f"{whole}.{fraction}", f"{whole},{fraction}", f"{grouped}.{fraction}", f"{grouped},{fraction}"))
    if value < 0:
        result = {"-" + item for item in result}
    return sorted(result, key=len, reverse=True)


def _format_like(template: str, value: Decimal | str) -> str:
    if isinstance(value, str):
        return value
    normalized = template.replace("\u00a0", " ")
    sign = "-" if value < 0 else ""
    value = abs(value)
    decimal_match = re.search(r"([.,])(\d+)$", normalized)
    decimals = len(decimal_match.group(2)) if decimal_match else 0
    decimal_separator = decimal_match.group(1) if decimal_match else ""
    if decimals:
        raw = f"{value:.{decimals}f}"
        whole, fraction = raw.split(".")
    else:
        whole = str(int(value.to_integral_value()))
        fraction = ""
    integer_template = re.sub(r"[.,]\d+$", "", normalized).lstrip("-")
    if " " in integer_template:
        whole = f"{int(whole):,}".replace(",", " ")
    return sign + whole + (decimal_separator + fraction if decimals else "")


def _font_codec(font):
    try:
        from pypdf._cmap import build_char_map_from_dict
        _subtype, _space, encoding, char_map = build_char_map_from_dict(200, font)
    except Exception:
        return None
    if not isinstance(encoding, str) or not isinstance(char_map, dict):
        return None
    inverse = {
        unicode_char: encoded_char
        for encoded_char, unicode_char in char_map.items()
        if isinstance(encoded_char, str) and isinstance(unicode_char, str) and len(unicode_char) == 1
    }
    return encoding, char_map, inverse


def _original_bytes(value) -> bytes:
    raw = getattr(value, "original_bytes", None)
    if raw is not None:
        return bytes(raw)
    try:
        return bytes(value)
    except Exception:
        return str(value).encode("latin1", errors="ignore")


def _decode_text(value, codec) -> str:
    if codec is None:
        return str(value)
    encoding, char_map, _inverse = codec
    try:
        encoded_text = _original_bytes(value).decode(encoding)
    except Exception:
        return str(value)
    return "".join(char_map.get(char, char) for char in encoded_text)


def _encode_text(text: str, codec):
    from pypdf.generic import ByteStringObject
    if codec is None:
        return None
    encoding, _char_map, inverse = codec
    encoded_chars: list[str] = []
    for char in text:
        encoded = inverse.get(char)
        if encoded is None:
            if ord(char) < 128:
                encoded = char
            else:
                return None
        encoded_chars.append(encoded)
    try:
        return ByteStringObject("".join(encoded_chars).encode(encoding))
    except Exception:
        return None


def _replace_combined_text(array, codec, target: AmountTarget, context: str) -> int:
    from pypdf.generic import ByteStringObject, TextStringObject
    positions = [index for index, item in enumerate(array) if isinstance(item, (TextStringObject, ByteStringObject))]
    if not positions:
        return 0
    chunks = [_decode_text(array[index], codec) for index in positions]
    combined = "".join(chunks)
    upper_context = (context + " " + combined).upper()
    if target.aliases and not any(alias.upper() in upper_context for alias in target.aliases):
        return 0
    for variant in _amount_variants(target.old):
        match = re.search(r"(?<!\d)" + re.escape(variant) + r"(?!\d)", combined)
        if not match:
            continue
        replacement = _format_like(match.group(0), target.new)
        updated = combined[: match.start()] + replacement + combined[match.end() :]
        if len(updated) == len(combined):
            offset = 0
            for array_index, chunk in zip(positions, chunks, strict=True):
                replacement_chunk = updated[offset : offset + len(chunk)]
                offset += len(chunk)
                encoded = _encode_text(replacement_chunk, codec)
                if encoded is None:
                    return 0
                array[array_index] = encoded
            return 1
        char_cursor = 0
        first_pos = last_pos = None
        prefix = suffix = ""
        for position_index, chunk in enumerate(chunks):
            start = char_cursor
            end = start + len(chunk)
            if first_pos is None and match.start() < end:
                first_pos = position_index
                prefix = chunk[: max(0, match.start() - start)]
            if match.end() <= end and last_pos is None:
                last_pos = position_index
                suffix = chunk[max(0, match.end() - start) :]
                break
            char_cursor = end
        if first_pos is None:
            continue
        if last_pos is None:
            last_pos = len(chunks) - 1
        new_first = prefix + replacement + (suffix if first_pos == last_pos else "")
        encoded = _encode_text(new_first, codec)
        if encoded is None:
            return 0
        array[positions[first_pos]] = encoded
        for position_index in range(first_pos + 1, last_pos + 1):
            if position_index == last_pos and first_pos != last_pos and suffix:
                tail = _encode_text(suffix, codec)
                if tail is None:
                    return 0
                array[positions[position_index]] = tail
            else:
                array[positions[position_index]] = ByteStringObject(b"")
        return 1
    return 0


def patch_supplier_pdf(content: bytes, before: dict, after: dict) -> tuple[bytes | None, dict]:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, ByteStringObject, ContentStream, TextStringObject
    targets = _collect_targets(before, after)
    report = {"requested": len(targets), "applied": 0, "unapplied": [], "font_preserved": True, "source_immutable": True}
    if not targets:
        return None, report
    reader = PdfReader(BytesIO(content))
    applied_keys: set[str] = set()
    for page_index, page in enumerate(reader.pages):
        resources = page.get("/Resources") or {}
        font_resources = resources.get("/Font") or {}
        codecs = {str(name): _font_codec(reference.get_object()) for name, reference in font_resources.items()}
        stream = ContentStream(page.get_contents(), reader)
        active_font = None
        recent_text: list[str] = []
        page_changed = False
        for operands, operator in stream.operations:
            if operator == b"Tf" and operands:
                active_font = str(operands[0])
                continue
            codec = codecs.get(active_font)
            if operator == b"TJ" and operands and isinstance(operands[0], ArrayObject):
                array = operands[0]
                visible = "".join(_decode_text(item, codec) for item in array if isinstance(item, (TextStringObject, ByteStringObject)))
                context = " ".join(recent_text[-12:])
                for target in targets:
                    if target.key in applied_keys or not _target_matches_page(target, page_index, page):
                        continue
                    if _replace_combined_text(array, codec, target, context):
                        applied_keys.add(target.key)
                        report["applied"] += 1
                        page_changed = True
                        visible = "".join(_decode_text(item, codec) for item in array if isinstance(item, (TextStringObject, ByteStringObject)))
                if visible:
                    recent_text.append(visible)
            elif operator in (b"Tj", b"'", b'"') and operands:
                item = operands[-1]
                if not isinstance(item, (TextStringObject, ByteStringObject)):
                    continue
                visible = _decode_text(item, codec)
                context = " ".join(recent_text[-12:]) + " " + visible
                updated = visible
                changed_targets: list[str] = []
                for target in targets:
                    if target.key in applied_keys or not _target_matches_page(target, page_index, page):
                        continue
                    if target.aliases and not any(alias.upper() in context.upper() for alias in target.aliases):
                        continue
                    for variant in _amount_variants(target.old):
                        match = re.search(r"(?<!\d)" + re.escape(variant) + r"(?!\d)", updated)
                        if match:
                            replacement = _format_like(match.group(0), target.new)
                            updated = updated[: match.start()] + replacement + updated[match.end() :]
                            changed_targets.append(target.key)
                            break
                if changed_targets:
                    encoded = _encode_text(updated, codec)
                    if encoded is not None:
                        operands[-1] = encoded
                        page_changed = True
                        for key in changed_targets:
                            applied_keys.add(key)
                            report["applied"] += 1
                if updated:
                    recent_text.append(updated)
        if page_changed:
            page.replace_contents(stream)
    report["unapplied"] = [target.key for target in targets if target.key not in applied_keys]
    if report["unapplied"]:
        return None, report
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    output = BytesIO()
    writer.write(output)
    return output.getvalue(), report


def _draft_base_verified(draft, parser_status: str) -> dict:
    if draft is None:
        return {}
    return receipt_verified_data({
        "issuer": draft.issuer,
        "passenger_name": draft.passenger_name,
        "segments": draft.segments,
        "fare": str(draft.fare) if draft.fare is not None else None,
        "taxes": str(draft.taxes) if draft.taxes is not None else None,
        "fees": str(draft.fees) if draft.fees is not None else None,
        "total": str(draft.total) if draft.total is not None else None,
        "currency": draft.currency,
        "fare_breakdown": draft.fare_breakdown,
        "tax_breakdown": draft.tax_breakdown,
        "fee_breakdown": draft.fee_breakdown,
        "receipt_items": draft.receipt_items,
    }, parser_status=parser_status)


def _base_verified_from_document(document) -> dict:
    metadata = document.metadata or {}
    supplier = metadata.get("supplier_original") or {}
    cached = supplier.get("base_verified_data")
    if isinstance(cached, dict) and cached:
        return cached
    original = document.versions.filter(mime_type="application/pdf").order_by("version").first()
    if original is None:
        return {}
    try:
        from documents.services import extract_receipt_fields
        with original.file.open("rb") as source:
            extraction = extract_receipt_fields(source.read(), mime=original.mime_type, name=original.original_name or document.title)
        return receipt_verified_data(extraction.get("fields") or {}, parser_status=extraction.get("status") or "parsed")
    except Exception:
        return {}


def _confirmed_verified_data(document, submitted: dict, parser_status: str) -> dict:
    """Use the financial values actually accepted by the confirm endpoint.

    The import UI stores pricing edits separately from the parsed receipt.  Its
    confirm request therefore contains the new fee/total in the top-level
    payload while ``supplier_original.verified_data`` can still contain the
    recognized source amounts.  ``ReceiptImportConfirmView`` persists the
    accepted values in ``receipt_import.corrected_fields``; those values must
    win when producing the corrected supplier PDF.
    """

    receipt_import = (document.metadata or {}).get("receipt_import") or {}
    confirmed = receipt_import.get("corrected_fields") or {}
    source = {
        **(submitted if isinstance(submitted, dict) else {}),
        **(confirmed if isinstance(confirmed, dict) else {}),
    }
    return receipt_verified_data(source, parser_status=parser_status)


def _sync_supplier_pdf(document, base_verified: dict, corrected_verified: dict, user) -> dict:
    metadata = document.metadata or {}
    supplier = {**(metadata.get("supplier_original") or {})}
    if base_verified:
        supplier.setdefault("base_verified_data", json_safe(base_verified))
    original = document.versions.filter(mime_type="application/pdf").order_by("version").first()
    result = {"status": "source", "source_version": original.version if original else None, "corrected_version": None, "requested": 0, "applied": 0, "font_preserved": True}
    if original is None or not base_verified:
        result["status"] = "unsupported"
    else:
        with original.file.open("rb") as source:
            corrected_content, patch_report = patch_supplier_pdf(source.read(), base_verified, corrected_verified)
        result.update(patch_report)
        if patch_report["requested"] == 0:
            receipt_import = {**(metadata.get("receipt_import") or {})}
            receipt_import.pop("supplier_corrected_version", None)
            metadata = {**metadata, "supplier_original": supplier, "receipt_import": receipt_import}
        elif corrected_content is None:
            result["status"] = "manual_required"
            metadata = {**metadata, "supplier_original": supplier}
        else:
            from documents.services import add_document_version
            original_name = original.original_name or document.title or "supplier.pdf"
            version = add_document_version(
                document,
                content=corrected_content,
                mime="application/pdf",
                name=f"{Path(original_name).stem}-corrected.pdf",
                user=user,
                origin="supplier_fix",
                correction_reason="Финансовые корректировки перенесены в копию оригинала поставщика",
                correction_diff=json_safe(patch_report),
            )
            result.update({"status": "corrected", "corrected_version": version.version})
            receipt_import = {**(document.metadata or {}).get("receipt_import", {}), "supplier_corrected_version": version.version}
            metadata = {**(document.metadata or {}), "supplier_original": supplier, "receipt_import": receipt_import}
    metadata = {**metadata, "supplier_pdf_correction": json_safe(result)}
    document.metadata = metadata
    document.save(update_fields=["metadata"])
    return result


def install_receipt_supplier_pdf_patch() -> None:
    from documents import views
    if getattr(views.DocumentReceiptUpdateView.post, "_supplier_pdf_patch", False):
        return
    original_update = views.DocumentReceiptUpdateView.post
    original_confirm = views.ReceiptImportConfirmView.post

    def update_post(self, request, document_id):
        document_before = get_document_or_404(request.user, document_id)
        base_verified = _base_verified_from_document(document_before)
        response = original_update(self, request, document_id)
        document = get_document_or_404(request.user, document_id)
        verified_input = request.data.get("verified_data")
        if isinstance(verified_input, dict):
            corrected = receipt_verified_data(verified_input, parser_status="manual_review" if request.data.get("draft") else "parsed")
            result = _sync_supplier_pdf(document, base_verified, corrected, request.user)
            try:
                from documents.serializers import DocumentSerializer
                response.data = DocumentSerializer(document).data
                response.data["supplier_pdf_correction"] = result
            except Exception:
                pass
        return response

    update_post._supplier_pdf_patch = True
    views.DocumentReceiptUpdateView.post = update_post

    def confirm_post(self, request, import_id):
        import_job = ReceiptImportJob.objects.filter(pk=import_id, tenant_id=request.user.tenant_id).first()
        draft = getattr(import_job, "draft", None) if import_job else None
        base_verified = _draft_base_verified(draft, import_job.parser_status if import_job else "parsed")
        response = original_confirm(self, request, import_id)
        document_id = response.data.get("document_id") if isinstance(response.data, dict) else None
        submitted_supplier = request.data.get("supplier_original")
        corrected_input = submitted_supplier.get("verified_data") if isinstance(submitted_supplier, dict) else None
        if document_id and isinstance(corrected_input, dict):
            document = get_document_or_404(request.user, document_id)
            corrected = _confirmed_verified_data(
                document,
                corrected_input,
                import_job.parser_status if import_job else "parsed",
            )
            result = _sync_supplier_pdf(document, base_verified, corrected, request.user)
            response.data["supplier_pdf_correction"] = result
        return response

    confirm_post._supplier_pdf_patch = True
    views.ReceiptImportConfirmView.post = confirm_post

    class DocumentSupplierPdfView(APIView):
        permission_classes = [require("documents.view")]

        def get(self, request, document_id):
            document = get_document_or_404(request.user, document_id)
            versions = document.versions.filter(mime_type="application/pdf").order_by("version")
            source = versions.first()
            if source is None:
                from common.errors import ApiError
                raise ApiError(code="NO_PDF_VERSION", message="PDF поставщика не найден", status_code=404)
            version = source
            corrected = (document.metadata or {}).get("receipt_import", {}).get("supplier_corrected_version")
            if not request.query_params.get("source") and corrected:
                candidate = versions.filter(version=corrected).first()
                if candidate is not None:
                    version = candidate
            response = FileResponse(version.file.open("rb"), content_type="application/pdf")
            disposition = "attachment" if request.query_params.get("disposition") == "attachment" else "inline"
            response["Content-Disposition"] = f'{disposition}; filename="{version.original_name or document.title}"'
            response["X-Content-Type-Options"] = "nosniff"
            response["X-Supplier-PDF-Mode"] = "source" if version.version == source.version else "corrected"
            response["X-Supplier-Source-Version"] = str(source.version)
            response["X-Supplier-Display-Version"] = str(version.version)
            return response

    views.DocumentSupplierPdfView = DocumentSupplierPdfView
