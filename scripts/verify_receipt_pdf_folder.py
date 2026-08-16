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

from documents.services import extract_receipt_fields  # noqa: E402


def present(value) -> bool:
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return bool(str(value or "").strip())


def baggage_allowance(value) -> bool:
    return bool(re.fullmatch(r"\d+(?:[.,]\d+)?\s*(?:PC|KG|КГ|КМ)", str(value or "").strip(), re.IGNORECASE))


def receipt_issues(fields: dict, status: str) -> list[str]:
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
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify every receipt PDF in a folder with the production parser.")
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    paths = sorted(args.folder.glob("*.pdf"), key=lambda path: path.name.casefold())
    if not paths:
        print(f"PDF files not found: {args.folder}")
        return 2

    failures = 0
    for index, path in enumerate(paths, 1):
        try:
            result = extract_receipt_fields(path.read_bytes(), mime="application/pdf", name=path.name)
            fields = result.get("fields") if isinstance(result, dict) and isinstance(result.get("fields"), dict) else {}
            status = str(result.get("status") or "") if isinstance(result, dict) else ""
            issues = receipt_issues(fields, status)
            failures += bool(issues)
            kind = fields.get("service_kind") or "—"
            blanks = len(fields.get("receipt_items") or fields.get("receipts") or []) or 1
            print(f"[{index:02d}/{len(paths):02d}] {'FAIL' if issues else 'OK  '} | {kind:8} | {blanks:2} blank(s) | {path.name}", flush=True)
            if issues:
                print("       missing: " + ", ".join(issues), flush=True)
        except Exception as error:  # keep auditing the rest of the folder
            failures += 1
            print(f"[{index:02d}/{len(paths):02d}] ERROR | {path.name} | {type(error).__name__}: {error}", flush=True)

    print(f"SUMMARY: {len(paths) - failures}/{len(paths)} passed; {failures} failed", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
