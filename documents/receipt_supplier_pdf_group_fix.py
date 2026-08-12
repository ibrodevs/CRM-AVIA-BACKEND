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
    are never used as source-PDF targets.
    """

    from documents import receipt_supplier_pdf_patch as supplier_pdf

    if getattr(supplier_pdf._collect_targets, "_group_source_fix", False):
        return

    def collect_targets(before: dict, after: dict, *, page_index: int | None = None, prefix: str = ""):
        before = before if isinstance(before, dict) else {}
        after = after if isinstance(after, dict) else {}

        old_group = supplier_pdf._first_group(before)
        new_group = supplier_pdf._first_group(after)
        if old_group or new_group:
            # A grouped PDF has no real aggregate amount printed in the source.
            # Patch only matching child tickets on their own pages.
            targets = []
            for index, (old_child, new_child) in enumerate(zip(old_group, new_group)):
                if isinstance(old_child, dict) and isinstance(new_child, dict):
                    targets.extend(
                        collect_targets(
                            old_child,
                            new_child,
                            page_index=index,
                            prefix=f"{prefix}receipt[{index}].",
                        )
                    )
            deduped = {
                (target.key, target.old, target.new, target.page_index): target
                for target in targets
            }
            return list(deduped.values())

        is_rail = _looks_like_rail_ticket(before) or _looks_like_rail_ticket(after)
        if is_rail:
            allowed = {"ticketCost", "reservedSeatCost", "total"}
            financial_fields = [row for row in supplier_pdf._FINANCIAL_FIELDS if row[0] in allowed]
            breakdowns = ()
        else:
            # Generic supplier PDFs (primarily aviation) use fare/tax/fee/total.
            # Do not let rail-only compatibility fields become accidental targets.
            allowed = {"fare", "taxes", "fees", "total"}
            financial_fields = [row for row in supplier_pdf._FINANCIAL_FIELDS if row[0] in allowed]
            breakdowns = supplier_pdf._BREAKDOWNS

        targets = []
        for key, aliases in financial_fields:
            old = supplier_pdf._decimal(_value(before, key))
            new = supplier_pdf._decimal(_value(after, key))
            if old is None or new is None or old == new:
                continue
            targets.append(supplier_pdf.AmountTarget(f"{prefix}{key}", old, new, aliases, page_index))

        for breakdown_key, fallback_aliases in breakdowns:
            old_rows = _value(before, breakdown_key)
            new_rows = _value(after, breakdown_key)
            if not isinstance(old_rows, list) or not isinstance(new_rows, list):
                continue
            for index, (old_row, new_row) in enumerate(zip(old_rows, new_rows)):
                if not isinstance(old_row, dict) or not isinstance(new_row, dict):
                    continue
                old = supplier_pdf._decimal(old_row.get("amount"))
                new = supplier_pdf._decimal(new_row.get("amount"))
                if old is None or new is None or old == new:
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
                    )
                )

        deduped = {
            (target.key, target.old, target.new, target.page_index): target
            for target in targets
        }
        return list(deduped.values())

    collect_targets._group_source_fix = True
    supplier_pdf._collect_targets = collect_targets
