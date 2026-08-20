#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")

import django  # noqa: E402

django.setup()
logging.getLogger("pypdf").setLevel(logging.ERROR)
logging.getLogger("pdfminer").setLevel(logging.ERROR)

from documents.receipt_structural_hardening import (  # noqa: E402
    _clean_passenger_name,
    _fare_calculation_codes,
    _source_issuer,
)
from documents.services import extract_receipt_fields  # noqa: E402


def present(value) -> bool:
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return bool(str(value or "").strip())


def baggage_allowance(value) -> bool:
    return bool(re.fullmatch(r"\d+(?:[.,]\d+)?\s*(?:PC|KG|КГ|КМ)", str(value or "").strip(), re.IGNORECASE))


def as_number(value):
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader

        pages = []
        for page in PdfReader(path, strict=False).pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                pages.append("")
        return "\n".join(pages)
    except Exception:
        return ""


def receipt_issues(fields: dict, status: str, source_text: str = "") -> list[str]:
    issues: list[str] = []
    kind = str(fields.get("service_kind") or "").lower()
    segments = fields.get("segments") if isinstance(fields.get("segments"), list) else []
    passenger = fields.get("passenger_name") or fields.get("passenger") or fields.get("passengers")
    if status != "parsed":
        issues.append(f"status={status or 'empty'}")
    if kind not in {"avia", "rail", "hotel", "transfer"}:
        issues.append(f"type={kind or 'empty'}")
    if not present(passenger):
        issues.append("passenger")
    elif isinstance(passenger, str) and _clean_passenger_name(passenger) != passenger:
        issues.append("passenger_contains_title")
    if not segments:
        issues.append("segments")
    if kind == "avia":
        if not present(fields.get("ticket_number") or fields.get("ticketNo")):
            issues.append("ticket")
        if not present(fields.get("fare") or fields.get("total")):
            issues.append("cost")
        if baggage_allowance(fields.get("fare_basis")):
            issues.append("fare_basis_contains_baggage")
        if str(fields.get("hand_baggage") or "").strip().upper() in {"OK", "OPEN", "CONFIRMED"}:
            issues.append("hand_baggage_contains_status")
        issuer = str(fields.get("issuer") or fields.get("carrier") or "").lower()
        source_issuer = _source_issuer(source_text)
        if source_issuer and source_issuer.casefold() != issuer.casefold():
            issues.append("issuer_mismatch")
        source_codes = _fare_calculation_codes(source_text)
        if len(source_codes) >= len(segments) + 1:
            for index, segment in enumerate(segments, 1):
                if not isinstance(segment, dict):
                    continue
                if not present(segment.get("fromCode")) or not present(segment.get("toCode")):
                    issues.append(f"segment_{index}_iata")
        compact_source = re.sub(r"\s+", " ", source_text)
        has_hand_baggage_weight = re.search(
            r"ручн(?:ая|ой)\s+клад.{0,300}?\d+\s*(?:кг|kg)\b",
            compact_source,
            re.IGNORECASE,
        )
        if has_hand_baggage_weight and not present(fields.get("hand_baggage") or fields.get("handBaggage")):
            issues.append("hand_baggage")
        invalid_column_values = {"СТАТУС", "БАГАЖ", "ТАРИФ", "РЕЙС"}
        for index, segment in enumerate(segments, 1):
            if not isinstance(segment, dict):
                issues.append(f"segment_{index}_invalid")
                continue
            if not present(segment.get("from") or segment.get("fromCode")) or not present(segment.get("to") or segment.get("toCode")):
                issues.append(f"segment_{index}_route")
            if not present(segment.get("flightNo") or segment.get("flight_number")):
                issues.append(f"segment_{index}_flight")
            cabin = segment.get("cabin") or segment.get("cls")
            if not present(cabin):
                issues.append(f"segment_{index}_class")
            fare_basis = segment.get("fareBasis") or segment.get("fare_basis")
            # Air Serbia's exact client sample prints no fare-basis code. It is
            # correct to keep this field empty instead of shifting 1PC into it.
            if not present(fare_basis) and "air serbia" not in issuer:
                issues.append(f"segment_{index}_fare_basis")
            baggage = str(segment.get("baggage") or "").strip()
            if not present(baggage):
                issues.append(f"segment_{index}_baggage")
            elif baggage.upper() in invalid_column_values:
                issues.append(f"segment_{index}_baggage_shifted")
    elif kind == "rail":
        if not present(fields.get("ticket_number") or fields.get("ticketNo") or fields.get("receipt_items")):
            issues.append("ticket")
        if not present(fields.get("total") or fields.get("ticketCost") or fields.get("receipt_items")):
            issues.append("cost")
    elif kind == "hotel":
        hotel = fields.get("hotel") if isinstance(fields.get("hotel"), dict) else {}
        if not present(hotel.get("name") or fields.get("issuer")):
            issues.append("hotel")
        if not present(fields.get("rooms")):
            issues.append("rooms")
        terms = fields.get("hotelTerms") if isinstance(fields.get("hotelTerms"), dict) else {}
        narrative_keys = ("cancellation", "noShow", "amendment", "important", "guestComment")
        normalized_terms = [
            re.sub(r"\s+", " ", str(terms.get(key))).strip().lower()
            for key in narrative_keys if present(terms.get(key))
        ]
        if len(normalized_terms) != len(set(normalized_terms)):
            issues.append("duplicate_hotel_terms")

    fare = as_number(fields.get("fare"))
    taxes = as_number(fields.get("taxes")) or 0
    fees = as_number(fields.get("fees")) or 0
    total = as_number(fields.get("total"))
    price_mode = fields.get("output") if isinstance(fields.get("output"), dict) else {}
    if fare is not None and total is not None and str(price_mode.get("priceMode") or "").lower() != "it":
        if abs(total - fare - taxes - fees) > 0.011:
            issues.append("financial_math")
    tax_rows = fields.get("tax_breakdown") if isinstance(fields.get("tax_breakdown"), list) else []
    if tax_rows and as_number(fields.get("taxes")) is not None:
        tax_sum = sum(as_number(row.get("amount")) or 0 for row in tax_rows if isinstance(row, dict))
        if abs(tax_sum - taxes) > 0.011:
            issues.append("tax_breakdown_math")

    items = fields.get("receipt_items") or fields.get("receipts") or fields.get("railTickets") or []
    if isinstance(items, list) and items:
        ticket_numbers = []
        for index, item in enumerate(items, 1):
            if not isinstance(item, dict):
                issues.append(f"blank_{index}_invalid")
                continue
            ticket = str(item.get("ticket_number") or item.get("ticketNo") or "").strip()
            if not ticket:
                issues.append(f"blank_{index}_ticket")
            else:
                ticket_numbers.append(ticket)
            item_segments = item.get("segments") or item.get("legs") or []
            if not any(
                isinstance(segment, dict)
                and present(segment.get("from") or segment.get("fromCode"))
                and present(segment.get("to") or segment.get("toCode"))
                for segment in item_segments
            ):
                issues.append(f"blank_{index}_route")
            if kind == "rail" and any(
                key in item
                for key in ("ticketCost", "reservedSeatCost", "agencyServiceFee", "additionalFees")
            ):
                component_total = sum(
                    as_number(item.get(key)) or 0
                    for key in ("ticketCost", "reservedSeatCost", "agencyServiceFee", "additionalFees")
                )
                item_total = as_number(item.get("total"))
                if item_total is not None and abs(item_total - component_total) > 0.011:
                    issues.append(f"blank_{index}_rail_math")
        if len(ticket_numbers) != len(set(ticket_numbers)):
            issues.append("duplicate_ticket_numbers")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify every receipt PDF in a folder with the production parser.")
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    paths = sorted(args.folder.rglob("*.pdf"), key=lambda path: str(path).casefold())
    if not paths:
        print(f"PDF files not found: {args.folder}")
        return 2

    failures = 0
    for index, path in enumerate(paths, 1):
        try:
            result = extract_receipt_fields(path.read_bytes(), mime="application/pdf", name=path.name)
            fields = result.get("fields") if isinstance(result, dict) and isinstance(result.get("fields"), dict) else {}
            status = str(result.get("status") or "") if isinstance(result, dict) else ""
            issues = receipt_issues(fields, status, pdf_text(path))
            failures += bool(issues)
            kind = fields.get("service_kind") or "—"
            blanks = len(fields.get("receipt_items") or fields.get("receipts") or []) or 1
            relative_name = path.relative_to(args.folder)
            print(f"[{index:02d}/{len(paths):02d}] {'FAIL' if issues else 'OK  '} | {kind:8} | {blanks:2} blank(s) | {relative_name}", flush=True)
            if issues:
                print("       missing: " + ", ".join(issues), flush=True)
        except Exception as error:  # keep auditing the rest of the folder
            failures += 1
            print(f"[{index:02d}/{len(paths):02d}] ERROR | {path.name} | {type(error).__name__}: {error}", flush=True)

    print(f"SUMMARY: {len(paths) - failures}/{len(paths)} passed; {failures} failed", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
