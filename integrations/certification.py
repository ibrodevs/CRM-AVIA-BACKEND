"""Evidence derived exclusively from recorded HBPRO calls; never invent successful steps."""

import io
import json
from collections import defaultdict
from copy import copy
from datetime import date
from math import ceil
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from django.core.serializers.json import DjangoJSONEncoder

from integrations.hotelbook_observability import sanitize

OPERATIONS = {
    "search": ("a", "Search"),
    "search_results": ("b", "getSearchResults"),
    "revalidate": ("c", "getSearchDetails"),
    "create_order": ("d", "createOrder"),
    "book": ("e", "createBooking"),
    "search_orders": ("f", "searchOrders"),
    "order_info": ("f", "OrderInfo"),
    "retrieve_item": ("g", "HotelOnlineItemInfo"),
    "cancel": ("h", "cancellation"),
}


def summary(run):
    calls = list(run.calls.order_by("created_at", "id"))
    ids = defaultdict(list)
    for call in calls:
        for key, values in call.external_ids.items():
            for value in values:
                if value not in ids[key]:
                    ids[key].append(value)
    snapshot = run.service.provider_snapshot if run.service_id else run.snapshot
    booking = (snapshot or {}).get("hotelbook_booking", {})
    # Recovery/diagnostic calls are optional in the supplier's streamlined flow.
    required = OPERATIONS.keys() - {"search_orders", "order_info"}
    missing = [
        op
        for op in OPERATIONS
        if op in required and not any(c.operation == op and c.result == "success" for c in calls)
    ]
    items = booking.get("items", [])
    cancelled = bool(items) and all(i.get("status") == "CANCELED" for i in items)
    searches = [c.request_sanitized for c in calls if c.operation == "search" and c.result == "success"]
    books = [c for c in calls if c.operation == "book" and c.result == "success"]
    expected_mode = "ON_REQUEST" if run.scenario == 1 else "ONLINE"
    expected_rooms = 1 if run.scenario == 1 else 2
    search = searches[0] if searches else {}
    try:
        nights = (date.fromisoformat(search["checkOut"]) - date.fromisoformat(search["checkIn"])).days
    except (KeyError, ValueError, TypeError):
        nights = 0
    book = books[0].request_sanitized if books else {}
    booked_rooms = book.get("rooms", [])
    checks = {
        "certification_hotel": search.get("hotels") in ([1251201], [131687])
        and search.get("hotels") == [(snapshot or {}).get("hotelbook", {}).get("hotel_id")],
        "nights": nights == (3 if run.scenario == 1 else 2),
        "confirmation_mode": book.get("confirmationMode") == expected_mode,
        "one_booking_call": len(books) == 1,
        "room_count": len(booked_rooms) == expected_rooms,
        "guest_count": len(booked_rooms) == expected_rooms
        and all(len(r.get("guests", [])) == (1 if run.scenario == 1 else 2) for r in booked_rooms),
        "adult_guests": bool(booked_rooms)
        and all(g.get("isChild") is False for r in booked_rooms for g in r.get("guests", [])),
        "identifiers": all(
            ids.get(k) for k in ("searchId", "offerId", "orderId", "providerReferenceId", "itemId")
        ),
    }
    if run.scenario == 2:
        checks["different_genders_per_room"] = bool(booked_rooms) and all(
            {g.get("gender") for g in r.get("guests", [])} == {"M", "F"} for r in booked_rooms
        )
    else:
        states = [
            c.supplier_status
            for c in calls
            if c.operation in ("book", "retrieve_item") and c.result == "success"
        ]
        checks["pending_then_confirmed"] = any("CONFIRM_IN_PROGRESS" in state for state in states) and any(
            state == "CONFIRMED" for state in states
        )
    supplier_updated = max((str(i["updatedAt"]) for i in items if i.get("updatedAt")), default=None)
    return {
        "certification_run_id": str(run.id),
        "scenario": run.scenario,
        "environment": run.environment,
        "search_ids": ids.get("searchId", []),
        "offer_count": len(ids.get("offerId", [])),
        "selected_offer_id": (snapshot or {}).get("hotelbook", {}).get("offer_id"),
        "hotel_id": (snapshot or {}).get("hotelbook", {}).get("hotel_id"),
        "order_ids": ids.get("orderId", []),
        "provider_reference_ids": ids.get("providerReferenceId", []),
        "item_ids": ids.get("itemId", []),
        "booking_status": booking.get("normalized_status", "not_booked"),
        "supplier_statuses": booking.get("supplier_statuses", []),
        "created_at": run.created_at.isoformat(),
        "updated_at": booking.get("updated_at", run.updated_at.isoformat()),
        "cancellation_status": "cancelled" if cancelled else "not_confirmed",
        "supplier_updated_at": supplier_updated,
        "scenario_checks": checks,
        "call_count": len(calls),
        "missing_operations": missing,
        "readiness": "PASS" if not missing and cancelled and all(checks.values()) else "PARTIAL",
    }


def _json(value):
    return json.dumps(sanitize(value), cls=DjangoJSONEncoder, ensure_ascii=False, indent=2).encode()


def export_runs(runs):
    """Accepted .json files, every pagination/retry retained; unchanged template copied."""
    runs = list(runs)
    if not runs or len({r.scenario for r in runs}) != len(runs):
        raise ValueError("Choose one run per scenario")
    data = io.BytesIO()
    reports = []
    workbook_path = Path(__file__).resolve().parent.parent / "docs/hbpro/certification-template.xlsx"
    import openpyxl

    workbook = openpyxl.load_workbook(workbook_path)
    sheet = workbook["Ru"]
    # Remove supplier sample answers and filenames, including sample order 414209.
    labels = {
        3: "Search ID: ",
        4: "number of OfferIDs: ",
        5: "Offer ID: ",
        6: "Order ID: ",
        7: "providerReferenceId: ",
        8: "ItemId: ",
        10: "Order ID: ",
        11: "ItemId: ",
        14: "Search ID: ",
        15: "number of OfferIDs: ",
        16: "Offer ID: ",
        17: "Order ID: ",
        18: "providerReferenceId: ",
        19: "ItemId: ",
        21: "Order ID: ",
        22: "ItemId: ",
        23: "Status: ",
        24: "Updated at: ",
    }
    for row, label in labels.items():
        sheet.cell(row, 2).value = label
    for row in range(3, 25):
        if row not in (12, 13) and not isinstance(sheet.cell(row, 3), openpyxl.cell.cell.MergedCell):
            sheet.cell(row, 3).value = None
    with ZipFile(data, "w", ZIP_DEFLATED) as archive:
        for run in runs:
            report = summary(run)
            reports.append(report)
            counts = defaultdict(int)
            files = defaultdict(list)
            for call in run.calls.order_by("created_at", "id"):
                letter, name = OPERATIONS.get(call.operation, ("extra", call.operation))
                counts[call.operation] += 1
                suffix = "" if counts[call.operation] == 1 else f".{counts[call.operation]:03d}"
                filename = f"{run.scenario}.{letter}.{name}{suffix}.json"
                files[call.operation].append(filename)
                archive.writestr(
                    f"hbpro-certification/scenario-{run.scenario}/{filename}",
                    _json(
                        {
                            "operation": call.operation,
                            "timestamp": call.created_at,
                            "url": call.url,
                            "http_method": call.http_method,
                            "request_headers": call.request_headers,
                            "request": call.request_sanitized,
                            "http_status": call.http_status,
                            "response": call.response_sanitized,
                            "duration_ms": call.duration_ms,
                            "external_ids": call.external_ids,
                            "correlation_id": call.correlation_id,
                            "environment": call.environment,
                            "order_id": call.order_id,
                            "error_code": call.error_code,
                            "error": call.provider_error,
                        }
                    ),
                )
            report["files"] = dict(files)

            def filenames(operation, manifest=files):
                names = manifest[operation]
                return (
                    "\n".join(names[:3]) + (f"\n+{len(names) - 3}: summary.json" if len(names) > 3 else "")
                    if names
                    else None
                )

            offset = 0 if run.scenario == 1 else 11
            answers = {
                3: report["search_ids"],
                4: report["offer_count"],
                5: report["selected_offer_id"],
                6: report["order_ids"],
                7: report["provider_reference_ids"],
                8: report["item_ids"],
                10: report["order_ids"],
                11: report["item_ids"],
            }
            for row, value in answers.items():
                cell = sheet.cell(row + offset, 2)
                cell.value = labels[row + offset] + (
                    ", ".join(map(str, value))
                    if isinstance(value, list)
                    else str(value if value is not None else "")
                )
            for row, op in {
                3: "search",
                4: "search_results",
                5: "revalidate",
                6: "create_order",
                7: "book",
                8: "search_orders",
                9: "order_info",
                10: "retrieve_item",
            }.items():
                sheet.cell(row + offset, 3).value = filenames(op)
            if run.scenario == 2:
                sheet["B23"] = labels[23] + report["cancellation_status"]
                sheet["B24"] = labels[24] + (report["supplier_updated_at"] or "")
                sheet["C23"] = filenames("cancel")
        # Keep the original template untouched. Only the filled copy wraps long IDs
        # and filenames; the full pagination manifest is available in summary.json.
        for row in sheet:
            lines = 1
            for cell in row[1:3]:
                if isinstance(cell, openpyxl.cell.cell.MergedCell) or not cell.value:
                    continue
                alignment = copy(cell.alignment)
                alignment.wrap_text = True
                cell.alignment = alignment
                width = max(1, sheet.column_dimensions[cell.column_letter].width - 2)
                lines = max(
                    lines, sum(max(1, ceil(len(part) / width)) for part in str(cell.value).split("\n"))
                )
            sheet.row_dimensions[row[0].row].height = max(
                sheet.row_dimensions[row[0].row].height or 15, min(400, 15 * lines + 8)
            )
        xlsx = io.BytesIO()
        workbook.save(xlsx)
        archive.writestr("hbpro-certification/certification-filled.xlsx", xlsx.getvalue())
        archive.writestr("hbpro-certification/summary.json", _json(reports))
    return data.getvalue()
