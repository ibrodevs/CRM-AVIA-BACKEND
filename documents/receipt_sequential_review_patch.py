"""Sequential review state for child tickets inside grouped supplier receipts.

The ticket-level patch owns extraction/confirmation. This module runs after it and
adds a small, backward-compatible review state to every canonical receipt item.
No extra table is required: the state lives together with the child ticket JSON
and is copied into the resulting document metadata.
"""

from documents import receipt_ticket_level_patch as ticket_level
from documents.receipt_metadata import json_safe


_REVIEWED_VALUES = {"reviewed", "checked", "done", "complete", "completed"}


def receipt_item_review_status(item):
    item = item if isinstance(item, dict) else {}
    if item.get("reviewed") is True:
        return "reviewed"
    raw = item.get("reviewStatus", item.get("review_status", "pending"))
    value = str(raw or "pending").strip().lower()
    return "reviewed" if value in _REVIEWED_VALUES else "pending"


def receipt_review_progress(items):
    rows = [row for row in (items or []) if isinstance(row, dict)]
    total = len(rows)
    reviewed = sum(1 for row in rows if receipt_item_review_status(row) == "reviewed")
    next_index = next(
        (index + 1 for index, row in enumerate(rows) if receipt_item_review_status(row) != "reviewed"),
        None,
    )
    return {
        "total": total,
        "reviewed": reviewed,
        "complete": bool(total) and reviewed == total,
        "next_index": next_index,
    }


def apply_review_state(items, source_rows):
    """Copy review state from incoming child items to their canonical forms."""
    rows = list(source_rows or [])
    normalized = []
    for index, item in enumerate(items or []):
        canonical = dict(item or {})
        source = rows[index] if index < len(rows) and isinstance(rows[index], dict) else {}
        status = receipt_item_review_status(source)
        canonical["reviewStatus"] = status
        canonical["review_status"] = status

        reviewed_at = source.get("reviewedAt", source.get("reviewed_at", ""))
        reviewed_by = source.get("reviewedBy", source.get("reviewed_by", ""))
        if status == "reviewed":
            if reviewed_at:
                canonical["reviewedAt"] = reviewed_at
                canonical["reviewed_at"] = reviewed_at
            if reviewed_by:
                canonical["reviewedBy"] = reviewed_by
                canonical["reviewed_by"] = reviewed_by
        else:
            canonical.pop("reviewedAt", None)
            canonical.pop("reviewed_at", None)
            canonical.pop("reviewedBy", None)
            canonical.pop("reviewed_by", None)
        normalized.append(json_safe(canonical))
    return normalized


def install_receipt_sequential_review_patch():
    """Install review-state preservation after the ticket-level receipt patch."""
    if getattr(ticket_level, "_sequential_review_patch_installed", False):
        return

    original_normalize = ticket_level.normalize_receipt_items
    original_aliases = ticket_level._with_item_aliases
    original_store = ticket_level._store_items_in_document

    def normalize_with_review(source, *, parser_status="parsed", service_kind=""):
        source_rows = ticket_level.receipt_items_from(source)
        items = original_normalize(
            source,
            parser_status=parser_status,
            service_kind=service_kind,
        )
        return apply_review_state(items, source_rows)

    def aliases_with_review(target, items):
        target = original_aliases(target, items)
        if isinstance(target, dict):
            progress = receipt_review_progress(items)
            target["reviewProgress"] = progress
            target["review_progress"] = progress
        return target

    def store_with_review(document, items, *, service_kind=""):
        original_store(document, items, service_kind=service_kind)
        if document is None or not items:
            return
        metadata = dict(document.metadata or {})
        receipt_import = dict(metadata.get("receipt_import") or {})
        receipt_import["review_progress"] = receipt_review_progress(items)
        metadata["receipt_import"] = receipt_import
        document.metadata = metadata
        document.save(update_fields=["metadata"])

    ticket_level.normalize_receipt_items = normalize_with_review
    ticket_level._with_item_aliases = aliases_with_review
    ticket_level._store_items_in_document = store_with_review
    ticket_level._sequential_review_patch_installed = True
