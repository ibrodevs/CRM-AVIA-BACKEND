from decimal import Decimal, InvalidOperation
from functools import wraps

from documents.models import Document, ReceiptImportJob
from documents.receipt_metadata import json_safe, receipt_verified_data


RECEIPT_ITEM_KEYS = (
    "receipt_items",
    "receiptItems",
    "groupTickets",
    "receipts",
    "railTickets",
)


def _as_dict(value):
    return value if isinstance(value, dict) else {}


def _service_kind(value):
    raw = str(value or "").strip().lower()
    if raw in {"rail", "train", "жд", "ж/д"}:
        return "rail"
    if raw in {"avia", "flight", "авиа"}:
        return "avia"
    if raw in {"hotel", "гостиница", "отель"}:
        return "hotel"
    if raw in {"transfer", "трансфер"}:
        return "transfer"
    return raw or "other"


def receipt_items_from(source):
    """Find child tickets in API payloads, parser output or verified metadata."""
    source = _as_dict(source)
    candidates = [source]
    supplier_original = _as_dict(source.get("supplier_original"))
    candidates.append(_as_dict(supplier_original.get("verified_data")))
    candidates.append(_as_dict(source.get("verified_data")))
    receipt_import = _as_dict(source.get("receipt_import"))
    candidates.append(_as_dict(receipt_import.get("verified_data")))

    for candidate in candidates:
        for key in RECEIPT_ITEM_KEYS:
            rows = candidate.get(key)
            if isinstance(rows, list) and rows:
                return rows
    return []


def _first_value(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return ""


def normalize_receipt_items(source, *, parser_status="parsed", service_kind=""):
    """Canonicalize every supplier ticket independently without re-aggregating it."""
    rows = receipt_items_from(source)
    normalized = []
    parent_kind = _service_kind(service_kind)
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        item = dict(row)
        for key in RECEIPT_ITEM_KEYS:
            item.pop(key, None)
        item_kind = _service_kind(item.get("service_kind") or parent_kind)
        item["service_kind"] = item_kind
        item.setdefault(
            "service_type",
            "ЖД" if item_kind == "rail" else item.get("service_type") or "",
        )
        item["passenger"] = _first_value(item.get("passenger"), item.get("passenger_name"))
        item["passenger_name"] = item["passenger"]
        item["ticketNo"] = _first_value(
            item.get("ticketNo"), item.get("ticket_number"), item.get("ticket_no")
        )
        item["ticket_number"] = item["ticketNo"]
        verified = receipt_verified_data(item, parser_status=parser_status)
        rail_component_keys = {
            "ticketCost",
            "ticket_cost",
            "reservedSeatCost",
            "reserved_seat_cost",
            "agencyServiceFee",
            "agency_service_fee",
            "additionalFees",
            "additional_fees",
        }
        if item_kind == "rail" and any(key in item for key in rail_component_keys):
            ticket_cost, reserved, agency_fee, additional_fees = _rail_item_financials(item)
            verified.update(
                {
                    "ticketCost": str(ticket_cost),
                    "reservedSeatCost": str(reserved),
                    "agencyServiceFee": str(agency_fee),
                    "additionalFees": str(additional_fees),
                    "fare": str(ticket_cost + reserved),
                    "fees": str(agency_fee + additional_fees),
                    "total": str(ticket_cost + reserved + agency_fee + additional_fees),
                }
            )
        try:
            receipt_index = int(
                _first_value(item.get("receiptIndex"), item.get("receipt_index"), index + 1)
            )
        except (TypeError, ValueError):
            receipt_index = index + 1
        verified["receiptIndex"] = receipt_index
        verified["receiptCount"] = 1
        # A child ticket must never recursively contain the whole group.
        verified["receiptItems"] = []
        verified["groupTickets"] = []
        verified["receipts"] = []
        verified["railTickets"] = []
        normalized.append(json_safe(verified))
    return normalized


def _decimal(value):
    try:
        if value in (None, ""):
            return Decimal("0")
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _rail_item_financials(item):
    """Return the corrected per-ticket rail amounts without using parent totals."""
    ticket_cost = _decimal(_first_value(item.get("ticketCost"), item.get("ticket_cost")))
    reserved = _decimal(
        _first_value(item.get("reservedSeatCost"), item.get("reserved_seat_cost"))
    )
    agency_fee = _decimal(
        _first_value(item.get("agencyServiceFee"), item.get("agency_service_fee"))
    )
    additional_fees = _decimal(
        _first_value(item.get("additionalFees"), item.get("additional_fees"))
    )
    return ticket_cost, reserved, agency_fee, additional_fees


def receipt_item_total(item):
    rail_keys = {
        "ticketCost",
        "ticket_cost",
        "reservedSeatCost",
        "reserved_seat_cost",
        "agencyServiceFee",
        "agency_service_fee",
        "additionalFees",
        "additional_fees",
    }
    if _service_kind(item.get("service_kind")) == "rail" and any(key in item for key in rail_keys):
        return sum(_rail_item_financials(item), Decimal("0"))
    explicit = _first_value(item.get("total"), item.get("originalTotal"), item.get("original_total"))
    if explicit not in (None, ""):
        return _decimal(explicit)
    return _decimal(item.get("fare")) + _decimal(item.get("taxes")) + _decimal(item.get("fees"))


def _rail_aggregate(items):
    fare = Decimal("0")
    taxes = Decimal("0")
    fees = Decimal("0")
    total = Decimal("0")
    for item in items:
        ticket_cost = _first_value(item.get("ticketCost"), item.get("ticket_cost"))
        reserved = _first_value(item.get("reservedSeatCost"), item.get("reserved_seat_cost"))
        if ticket_cost not in (None, "") or reserved not in (None, ""):
            fare += _decimal(ticket_cost) + _decimal(reserved)
        else:
            fare += _decimal(item.get("fare"))
        taxes += _decimal(item.get("taxes"))
        item_fees = (
            _decimal(_first_value(item.get("agencyServiceFee"), item.get("agency_service_fee")))
            + _decimal(_first_value(item.get("additionalFees"), item.get("additional_fees")))
        )
        if item_fees == 0 and item.get("fees") not in (None, ""):
            item_fees = _decimal(item.get("fees"))
        fees += item_fees
        total += receipt_item_total(item)
    return fare, taxes, fees, total


def _with_item_aliases(target, items):
    if not isinstance(target, dict):
        return target
    target["receipt_items"] = items
    target["receiptItems"] = items
    target["groupTickets"] = items
    target["receipts"] = items
    target["railTickets"] = items
    target["receiptCount"] = len(items)
    target["receipt_count"] = len(items)
    return target


def _save_draft_items(import_job, items):
    draft = getattr(import_job, "draft", None)
    if draft is None:
        return None
    draft.receipt_items = json_safe(items)
    draft.save(update_fields=["receipt_items"])
    return draft


def _store_items_in_document(document, items, *, service_kind=""):
    if document is None or not items:
        return
    service_kind = _service_kind(service_kind)
    metadata = document.metadata or {}
    supplier_original = dict(metadata.get("supplier_original") or {})
    supplier_verified = dict(supplier_original.get("verified_data") or {})
    _with_item_aliases(supplier_verified, items)
    supplier_original["verified_data"] = supplier_verified

    receipt_import = dict(metadata.get("receipt_import") or {})
    receipt_verified = dict(receipt_import.get("verified_data") or {})
    corrected_fields = dict(receipt_import.get("corrected_fields") or {})
    _with_item_aliases(receipt_verified, items)
    _with_item_aliases(corrected_fields, items)
    receipt_import["verified_data"] = receipt_verified
    receipt_import["corrected_fields"] = corrected_fields
    receipt_import["receipt_items"] = items

    update_fields = ["metadata"]
    if service_kind == "rail":
        fare, taxes, fees, total = _rail_aggregate(items)
        corrected_fields.update(
            {
                "fare": str(fare),
                "taxes": str(taxes),
                "fees": str(fees),
                "total": str(total),
            }
        )
        receipt_import["supplier_items_total"] = str(total)
        document.amount = total
        update_fields.append("amount")

    document.metadata = {
        **metadata,
        "supplier_original": supplier_original,
        "receipt_import": receipt_import,
    }
    document.save(update_fields=update_fields)


def install_receipt_ticket_level_patch():
    """Make the import API treat a multi-ticket PDF as a container of editable tickets."""
    from documents.views import (
        DocumentReceiptUpdateView,
        ReceiptImportConfirmView,
        ReceiptImportCreateView,
        ReceiptImportResultView,
    )

    if getattr(ReceiptImportCreateView.post, "_ticket_level_patch", False):
        return

    original_create = ReceiptImportCreateView.post

    @wraps(original_create)
    def create(self, request):
        response = original_create(self, request)
        if getattr(response, "status_code", 500) < 400:
            import_id = _as_dict(getattr(response, "data", {})).get("id")
            import_job = ReceiptImportJob.objects.filter(pk=import_id).first() if import_id else None
            if import_job is not None:
                items = normalize_receipt_items(
                    import_job.raw_extraction or {},
                    parser_status=import_job.parser_status,
                    service_kind=import_job.guessed_type,
                )
                if items:
                    _save_draft_items(import_job, items)
        return response

    create._ticket_level_patch = True
    ReceiptImportCreateView.post = create

    original_result = ReceiptImportResultView.get

    @wraps(original_result)
    def result(self, request, import_id):
        response = original_result(self, request, import_id)
        if getattr(response, "status_code", 500) >= 400:
            return response
        import_job = ReceiptImportJob.objects.filter(pk=import_id, tenant_id=request.user.tenant_id).first()
        if import_job is None:
            return response
        draft = getattr(import_job, "draft", None)
        items = list(getattr(draft, "receipt_items", None) or [])
        if not items:
            items = normalize_receipt_items(
                import_job.raw_extraction or {},
                parser_status=import_job.parser_status,
                service_kind=import_job.guessed_type,
            )
            if items and draft is not None:
                _save_draft_items(import_job, items)
        if not items:
            return response

        data = response.data
        data["receipt_items"] = items
        _with_item_aliases(data.setdefault("extracted", {}), items)
        _with_item_aliases(data.setdefault("verified_data", {}), items)
        if isinstance(data.get("draft"), dict):
            _with_item_aliases(data["draft"], items)
        return response

    result._ticket_level_patch = True
    ReceiptImportResultView.get = result

    original_confirm = ReceiptImportConfirmView.post

    @wraps(original_confirm)
    def confirm(self, request, import_id):
        import_job = ReceiptImportJob.objects.filter(pk=import_id, tenant_id=request.user.tenant_id).first()
        parser_status = import_job.parser_status if import_job is not None else "parsed"
        service_kind = _service_kind(import_job.guessed_type if import_job is not None else "")
        items = normalize_receipt_items(
            request.data,
            parser_status=parser_status,
            service_kind=service_kind,
        )
        response = original_confirm(self, request, import_id)
        if getattr(response, "status_code", 500) >= 400 or import_job is None or not items:
            return response

        import_job.refresh_from_db()
        draft = _save_draft_items(import_job, items)
        if draft is not None and service_kind == "rail":
            fare, taxes, fees, total = _rail_aggregate(items)
            draft.fare = fare
            draft.taxes = taxes
            draft.fees = fees
            draft.total = total
            draft.save(update_fields=["fare", "taxes", "fees", "total", "receipt_items"])
            if isinstance(response.data, dict):
                response.data["total"] = str(total)

        document = getattr(draft, "result_document", None) if draft is not None else None
        _store_items_in_document(document, items, service_kind=service_kind)
        return response

    confirm._ticket_level_patch = True
    ReceiptImportConfirmView.post = confirm

    original_update = DocumentReceiptUpdateView.post

    @wraps(original_update)
    def update_document(self, request, document_id):
        verified_input = request.data.get("verified_data") if hasattr(request.data, "get") else None
        items = normalize_receipt_items(verified_input or {}, parser_status="parsed")
        response = original_update(self, request, document_id)
        if getattr(response, "status_code", 500) < 400 and items:
            document = Document.objects.filter(pk=document_id, tenant_id=request.user.tenant_id).first()
            service_kind = (
                _as_dict(_as_dict(document.metadata).get("receipt_import")).get("service_kind", "")
                if document is not None
                else ""
            )
            _store_items_in_document(document, items, service_kind=service_kind)
        return response

    update_document._ticket_level_patch = True
    DocumentReceiptUpdateView.post = update_document
