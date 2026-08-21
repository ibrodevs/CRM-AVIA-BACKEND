from __future__ import annotations

import re


def _value(data: dict, key: str):
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
    return data.get(key, data.get(snake))


def _looks_like_rail_ticket(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    return any(
        _value(data, key) not in (None, "")
        for key in ("ticketCost", "reservedSeatCost", "agencyServiceFee", "additionalFees")
    )


def _dedupe_source_targets(targets):
    """Collapse aggregate/detail aliases that point to one printed amount.

    The receipt editor keeps a top-level financial value and its breakdown row
    in sync.  In supplier PDFs such as dompdf/CPDF Aeroflot receipts, ``fees``
    and ``feeBreakdown[ASB]`` are two CRM representations of the same single
    ``СБОР АСБ`` text object.  Requiring two replacements makes the all-or-
    nothing guard reject an otherwise safe correction after the first target
    consumes that one source occurrence.

    Only collapse a top-level target with a breakdown target when page, old and
    new amounts match and their labels overlap.  Independent breakdown rows
    with equal numbers and distinct codes remain separate targets.
    """

    merged = []
    for target in targets:
        is_breakdown = "Breakdown[" in target.key or "_breakdown[" in target.key
        match_index = None
        if is_breakdown:
            target_aliases = {
                str(alias).strip().upper()
                for alias in target.aliases
                if str(alias).strip()
            }
            for index, existing in enumerate(merged):
                existing_is_top_level = "[" not in existing.key
                if not existing_is_top_level:
                    continue
                if (existing.page_index, existing.old, existing.new) != (
                    target.page_index,
                    target.old,
                    target.new,
                ):
                    continue
                if existing.page_markers != target.page_markers:
                    continue
                existing_aliases = {
                    str(alias).strip().upper()
                    for alias in existing.aliases
                    if str(alias).strip()
                }
                if existing_aliases & target_aliases:
                    match_index = index
                    break
        if match_index is None:
            merged.append(target)
            continue
        existing = merged[match_index]
        aliases = tuple(dict.fromkeys((*existing.aliases, *target.aliases)))
        merged[match_index] = existing.__class__(
            existing.key,
            existing.old,
            existing.new,
            aliases,
            existing.page_index,
            existing.page_markers,
        )
    return merged


def install_receipt_supplier_pdf_group_fix() -> None:
    """Make corrected supplier PDFs target real source amounts, not CRM aggregates.

    Grouped rail PDFs are containers: every child ticket belongs to one source
    page, while the parent ``fare``/``fees``/``total`` values are calculated CRM
    summaries and do not exist anywhere in the supplier PDF. Rail children also
    carry compatibility aliases (``fare``/``fees``) that are derived from
    ``ticketCost``/``reservedSeatCost`` and likewise are not separate source
    values. Treating those aliases as PDF targets makes the all-or-nothing guard
    reject the corrected copy and the UI silently falls back to the unchanged
    source PDF.

    This patch keeps generic air-ticket behavior intact, but for grouped/source
    rail tickets it edits only the amounts that actually exist on the ticket:
    ticket cost, reserved-seat cost and the ticket total. Parent group summaries
    are never used as source-PDF targets. The supplier-PDF endpoint is also
    marked no-store so a browser cannot keep showing the source response that it
    cached before a corrected version was created.
    """

    from documents import receipt_supplier_pdf_patch as supplier_pdf

    if not getattr(supplier_pdf._collect_targets, "_group_source_fix", False):

        def collect_targets(
            before: dict,
            after: dict,
            *,
            page_index: int | None = None,
            prefix: str = "",
            page_markers: tuple[str, ...] = (),
        ):
            before = before if isinstance(before, dict) else {}
            after = after if isinstance(after, dict) else {}

            old_group = supplier_pdf._first_group(before)
            new_group = supplier_pdf._first_group(after)
            if old_group or new_group:
                # A grouped PDF has no real aggregate amount printed in the source.
                # Patch only matching child tickets on their own pages.
                targets = []
                for index, (old_child, new_child) in enumerate(
                    zip(old_group, new_group, strict=False)
                ):
                    if isinstance(old_child, dict) and isinstance(new_child, dict):
                        targets.extend(
                            collect_targets(
                                old_child,
                                new_child,
                                page_index=index,
                                prefix=f"{prefix}receipt[{index}].",
                                page_markers=(
                                    supplier_pdf._ticket_page_markers(old_child)
                                    or supplier_pdf._ticket_page_markers(new_child)
                                ),
                            )
                        )
                deduped = {
                    (target.key, target.old, target.new, target.page_index): target
                    for target in targets
                }
                return list(deduped.values())

            is_rail = _looks_like_rail_ticket(before) or _looks_like_rail_ticket(after)
            if is_rail:
                # Patch every printed rail component, not only the payable
                # total. Zero-valued components are still skipped by the safe
                # collector because inserting a field absent from the source
                # layout would be ambiguous.
                allowed = {
                    "ticketCost",
                    "reservedSeatCost",
                    "agencyServiceFee",
                    "additionalFees",
                    "total",
                }
                financial_fields = [row for row in supplier_pdf._FINANCIAL_FIELDS if row[0] in allowed]
                breakdowns = ()
            else:
                # Generic supplier PDFs (primarily aviation) use fare/tax/fee/total.
                # Do not let rail-only compatibility fields become accidental targets.
                allowed = {"fare", "taxes", "fees", "total"}
                financial_fields = [row for row in supplier_pdf._FINANCIAL_FIELDS if row[0] in allowed]
                breakdowns = supplier_pdf._BREAKDOWNS

            targets = []
            output = _value(after, "output")
            price_mode = str(output.get("priceMode") or output.get("price_mode") or "").strip().lower() if isinstance(output, dict) else ""
            for key, aliases in financial_fields:
                if is_rail and key == "total":
                    # Railway coupons often print both ``Цена`` and ``Итого``.
                    # Both are payable-total representations and must change in
                    # the corrected copy together.
                    aliases = tuple(dict.fromkeys((*aliases, "ЦЕНА")))
                old = supplier_pdf._decimal(_value(before, key))
                new = supplier_pdf._decimal(_value(after, key))
                if key == "fare" and price_mode in {"it", "закрыть как it", "closed_it"}:
                    if old is not None:
                        targets.append(supplier_pdf.AmountTarget(
                            f"{prefix}fare.it", old, "IT", aliases, page_index, page_markers
                        ))
                    continue
                if old is None or new is None or old == new:
                    continue
                if old == 0 and key != "total":
                    continue
                targets.append(supplier_pdf.AmountTarget(
                    f"{prefix}{key}", old, new, aliases, page_index, page_markers
                ))

            for breakdown_key, fallback_aliases in breakdowns:
                old_rows = _value(before, breakdown_key)
                new_rows = _value(after, breakdown_key)
                if not isinstance(old_rows, list) or not isinstance(new_rows, list):
                    continue
                for index, (old_row, new_row) in enumerate(
                    zip(old_rows, new_rows, strict=False)
                ):
                    if not isinstance(old_row, dict) or not isinstance(new_row, dict):
                        continue
                    old = supplier_pdf._decimal(old_row.get("amount"))
                    new = supplier_pdf._decimal(new_row.get("amount"))
                    if old is None or new is None or old == new:
                        continue
                    if old == 0:
                        continue
                    row_aliases = tuple(
                        str(value).strip()
                        for value in (
                            old_row.get("code"),
                            old_row.get("label"),
                            new_row.get("code"),
                            new_row.get("label"),
                        )
                        if str(value or "").strip()
                    )
                    targets.append(
                        supplier_pdf.AmountTarget(
                            f"{prefix}{breakdown_key}[{index}]",
                            old,
                            new,
                            row_aliases or fallback_aliases,
                            page_index,
                            page_markers,
                        )
                    )

            deduped = {
                (target.key, target.old, target.new, target.page_index): target
                for target in targets
            }
            return _dedupe_source_targets(list(deduped.values()))

        collect_targets._group_source_fix = True
        supplier_pdf._collect_targets = collect_targets

    # The same authenticated URL can first serve the immutable source and later
    # serve a newly-created corrected version. Explicitly disable browser/proxy
    # caching so opening it again always asks the backend which version is current.
    from documents import views

    supplier_pdf_view = getattr(views, "DocumentSupplierPdfView", None)
    if supplier_pdf_view is not None and not getattr(supplier_pdf_view.get, "_supplier_pdf_no_cache", False):
        original_get = supplier_pdf_view.get

        def get_no_cache(self, request, document_id):
            response = original_get(self, request, document_id)
            response["Cache-Control"] = "private, no-store, no-cache, must-revalidate, max-age=0"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"
            return response

        get_no_cache._supplier_pdf_no_cache = True
        supplier_pdf_view.get = get_no_cache
