#!/usr/bin/env python3
"""Exercise every editable source-price field against real supplier PDFs.

The verifier uses the same parser and PDF writer as production.  Every
recognized non-zero component is changed independently together with its
dependent aggregate and payable total.  A corrected sample per source PDF can
optionally be written for render/visual checks.
"""

from __future__ import annotations

import argparse
import copy
import logging
import os
import re
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")

import django  # noqa: E402

django.setup()
logging.getLogger("pypdf").setLevel(logging.ERROR)
logging.getLogger("pdfminer").setLevel(logging.ERROR)

from pypdf import PdfReader  # noqa: E402

from documents import receipt_supplier_pdf_patch as supplier_pdf  # noqa: E402
from documents.services import extract_receipt_fields  # noqa: E402

GROUP_KEYS = ("groupTickets", "receiptItems", "receipt_items", "receipts", "railTickets")
BREAKDOWN_ALIASES = {
    "fare": ("fareBreakdown", "fare_breakdown"),
    "taxes": ("taxBreakdown", "tax_breakdown"),
    "fees": ("feeBreakdown", "fee_breakdown"),
}
RAIL_COMPONENT_ROWS = {
    "ticketCost": "TICKET",
    "reservedSeatCost": "RESERVED_SEAT",
}


@dataclass(frozen=True)
class Scenario:
    scope_index: int | None
    field: str
    row_index: int | None = None

    @property
    def label(self) -> str:
        scope = f"blank[{self.scope_index}]" if self.scope_index is not None else "document"
        row = f"[{self.row_index}]" if self.row_index is not None else ""
        return f"{scope}.{self.field}{row}"


def decimal(value) -> Decimal | None:
    return supplier_pdf._decimal(value)


def get_value(data: dict, key: str):
    return supplier_pdf._value(data, key)


def set_value(data: dict, key: str, value: Decimal) -> None:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
    rendered = format(value, "f")
    present = False
    for candidate in (key, snake):
        if candidate in data:
            data[candidate] = rendered
            present = True
    if not present:
        data[key] = rendered


def group_key(data: dict) -> str | None:
    for key in GROUP_KEYS:
        if isinstance(data.get(key), list) and data[key]:
            return key
    return None


def scopes(data: dict) -> list[tuple[int | None, dict]]:
    key = group_key(data)
    if key is None:
        return [(None, data)]
    return [(index, child) for index, child in enumerate(data[key]) if isinstance(child, dict)]


def breakdown(data: dict, aggregate: str) -> tuple[str | None, list]:
    for key in BREAKDOWN_ALIASES[aggregate]:
        if isinstance(data.get(key), list) and data[key]:
            return key, data[key]
    return None, []


def scenarios_for(fields: dict) -> list[Scenario]:
    kind = str(get_value(fields, "serviceKind") or get_value(fields, "serviceType") or "").casefold()
    is_rail = kind in {"rail", "жд", "железнодорожный билет"}
    result: list[Scenario] = []
    for scope_index, data in scopes(fields):
        if is_rail:
            for key in ("ticketCost", "reservedSeatCost", "agencyServiceFee", "additionalFees"):
                value = decimal(get_value(data, key))
                if value is not None and value > 0:
                    result.append(Scenario(scope_index, key))
            continue

        for aggregate in ("fare", "taxes", "fees"):
            _key, rows = breakdown(data, aggregate)
            positive_rows = [
                index for index, row in enumerate(rows)
                if isinstance(row, dict) and (decimal(row.get("amount")) or Decimal("0")) > 0
            ]
            if positive_rows:
                result.extend(Scenario(scope_index, aggregate, index) for index in positive_rows)
                continue
            value = decimal(get_value(data, aggregate))
            if value is not None and value > 0:
                result.append(Scenario(scope_index, aggregate))

        # Vouchers can expose the supplier amount through service-specific
        # fields instead of fare/tax/fee.
        if not any(item.scope_index == scope_index for item in result):
            for key in ("supplierCost", "agencyServiceFee", "additionalFees"):
                value = decimal(get_value(data, key))
                if value is not None and value > 0:
                    result.append(Scenario(scope_index, key))
    if not result and kind in {"hotel", "transfer"}:
        # Some vouchers contain booking details but no supplier price at all.
        # The editor still allows a price to be entered manually; production
        # must publish that change in the working PDF copy as an appendix.
        result.append(Scenario(None, "supplierCost"))
    return result


def selected_scope(data: dict, scope_index: int | None) -> dict:
    if scope_index is None:
        return data
    key = group_key(data)
    if key is None:
        raise ValueError("group scope disappeared")
    return data[key][scope_index]


def update_breakdown_compatibility_rows(data: dict, component: str, value: Decimal) -> None:
    expected_code = RAIL_COMPONENT_ROWS.get(component)
    if not expected_code:
        return
    for key in ("costBreakdown", "cost_breakdown", "fareBreakdown", "fare_breakdown"):
        rows = data.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and str(row.get("code") or "").upper() == expected_code:
                row["amount"] = format(value, "f")


def apply_scenario(before: dict, scenario: Scenario, delta: Decimal) -> dict:
    after = copy.deepcopy(before)
    data = selected_scope(after, scenario.scope_index)
    old_total = decimal(get_value(data, "total"))
    if old_total is None and scenario.field == "supplierCost":
        set_value(data, "supplierCost", delta)
        set_value(data, "fare", delta)
        set_value(data, "total", delta)
        return after
    if old_total is None:
        raise ValueError("recognized component has no payable total")

    if scenario.row_index is not None:
        row_key, rows = breakdown(data, scenario.field)
        if row_key is None:
            raise ValueError("breakdown disappeared")
        row = rows[scenario.row_index]
        old = decimal(row.get("amount"))
        if old is None:
            raise ValueError("breakdown amount disappeared")
        row["amount"] = format(old + delta, "f")
        aggregate = decimal(get_value(data, scenario.field))
        if aggregate is not None:
            set_value(data, scenario.field, aggregate + delta)
    else:
        old = decimal(get_value(data, scenario.field))
        if old is None:
            raise ValueError("financial value disappeared")
        set_value(data, scenario.field, old + delta)
        update_breakdown_compatibility_rows(data, scenario.field, old + delta)
        if scenario.field in {"ticketCost", "reservedSeatCost"}:
            fare = decimal(get_value(data, "fare"))
            if fare is not None:
                set_value(data, "fare", fare + delta)

    set_value(data, "total", old_total + delta)
    return after


def pdf_page_count(content: bytes) -> int:
    from io import BytesIO

    return len(PdfReader(BytesIO(content), strict=False).pages)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--delta", type=Decimal, default=Decimal("101.00"))
    args = parser.parse_args()

    paths = sorted(args.folder.rglob("*.pdf"), key=lambda path: str(path).casefold())
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)

    failed_files = 0
    total_scenarios = 0
    failed_scenarios = 0
    no_source_price = 0
    for file_index, path in enumerate(paths, 1):
        content = path.read_bytes()
        extraction = extract_receipt_fields(content, mime="application/pdf", name=path.name)
        before = extraction.get("fields") or {}
        source_price_missing = not any(
            decimal(get_value(scope, key)) is not None
            for _scope_index, scope in scopes(before)
            for key in ("fare", "supplierCost", "ticketCost", "total")
        )
        cases = scenarios_for(before)
        total_scenarios += len(cases)
        errors = []
        representative = None
        representative_report = None
        if source_price_missing:
            no_source_price += 1
        if cases:
            try:
                after = copy.deepcopy(before)
                for case_index, case in enumerate(cases, 1):
                    after = apply_scenario(
                        after,
                        case,
                        args.delta + Decimal(case_index) / Decimal("100"),
                    )
                corrected, report = supplier_pdf.patch_supplier_pdf(content, before, after)
                if corrected is None or report.get("unapplied"):
                    errors.append(
                        f"{report.get('strategy', 'none')} "
                        f"unapplied={report.get('unapplied', [])}"
                    )
                elif corrected == content:
                    errors.append("output bytes unchanged")
                elif report.get("strategy") == "financial_correction_appendix":
                    expected_pages = pdf_page_count(content) + report.get("appended_pages", 0)
                    if pdf_page_count(corrected) != expected_pages:
                        errors.append("correction appendix page count is invalid")
                elif pdf_page_count(corrected) != pdf_page_count(content):
                    errors.append("page count changed")
                if not errors:
                    representative = corrected
                    representative_report = report
            except Exception as error:
                errors.append(f"{type(error).__name__}: {error}")

        failed_scenarios += len(cases) if errors else 0
        failed_files += bool(errors)
        status = "FAIL" if errors else "OK"
        kind = before.get("service_kind") or "—"
        relative = path.relative_to(args.folder)
        print(
            f"[{file_index:02d}/{len(paths):02d}] {status:8} | {kind:8} | "
            f"{len(cases):2} edit(s) | {relative}",
            flush=True,
        )
        for error in errors:
            print(f"       {error}", flush=True)
        if args.output_dir and representative is not None:
            output = args.output_dir / f"{file_index:02d}-{path.stem}-corrected.pdf"
            output.write_bytes(representative)
            print(
                f"       sample: {output.name} ({representative_report.get('strategy')})",
                flush=True,
            )

    print(
        f"SUMMARY: files={len(paths)} failed_files={failed_files} "
        f"scenarios={total_scenarios} failed_scenarios={failed_scenarios} "
        f"no_source_price={no_source_price}",
        flush=True,
    )
    return 1 if failed_scenarios else 0


if __name__ == "__main__":
    raise SystemExit(main())
