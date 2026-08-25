from __future__ import annotations

import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any

from documents.receipt_quality_guard import (
    apply_receipt_quality_guard,
    plausible_avia_location,
)

ENGINE_VERSION = "2026.08.25-v3"


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _clean_text(value: str) -> str:
    value = (value or "").replace("\x00", "").replace("\xa0", " ").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def _text_score(value: str) -> int:
    text = _clean_text(value)
    if len(text) < 20:
        return -10000
    letters = sum(char.isalpha() for char in text)
    labels = len(
        re.findall(
            r"пассажир|гость|билет|брон|маршрут|поезд|вагон|место|рейс|заезд|выезд|"
            r"итого|тариф|tax|passenger|ticket|hotel|flight",
            text,
            flags=re.IGNORECASE,
        )
    )
    return letters + labels * 120 + min(text.count("\n"), 400) * 2


def _pdf_text_candidates(content: bytes) -> list[tuple[str, str]]:
    """Extract independent PDF text views instead of trusting one extractor.

    Supplier PDFs frequently expose different useful fields through pypdf and
    pdfminer.  The old implementation selected one text representation early;
    this engine keeps every meaningful view and lets field quality decide.
    """

    candidates: list[tuple[str, str]] = []
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content), strict=False)
        pages: list[str] = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                pages.append("")
        joined = _clean_text("\n".join(pages))
        if joined:
            candidates.append(("pypdf", joined))
    except Exception:
        pass

    try:
        from pdfminer.high_level import extract_text

        text = _clean_text(extract_text(BytesIO(content)) or "")
        if text:
            candidates.append(("pdfminer", text))
    except Exception:
        pass

    try:
        from documents import services

        text = _clean_text(services._extract_pdf_text(content))
        if text:
            candidates.append(("legacy_best", text))
    except Exception:
        pass

    # Deduplicate near-identical candidates while preserving the richer one.
    unique: list[tuple[str, str]] = []
    fingerprints: set[str] = set()
    for source, text in sorted(candidates, key=lambda item: _text_score(item[1]), reverse=True):
        fingerprint = re.sub(r"\W+", "", text).lower()[:12000]
        if fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        unique.append((source, text))
    return unique


def _passenger_rows(fields: dict) -> list[dict]:
    rows = fields.get("passengers") or []
    return [row for row in rows if isinstance(row, dict) and _present(row.get("name"))]


def _segments(fields: dict) -> list[dict]:
    rows = fields.get("segments") or fields.get("legs") or []
    return [row for row in rows if isinstance(row, dict)]


def _ticket_number(fields: dict) -> str:
    top = str(fields.get("ticket_number") or fields.get("ticketNo") or "").strip()
    if top:
        return top
    for passenger in _passenger_rows(fields):
        value = str(passenger.get("ticketNo") or passenger.get("ticket_number") or "").strip()
        if value:
            return value
    return ""


def _has_route(fields: dict) -> bool:
    is_avia = str(fields.get("service_kind") or "").lower() == "avia"
    return any(
        _present(segment.get("from") or segment.get("fromCode"))
        and _present(segment.get("to") or segment.get("toCode"))
        and (
            not is_avia
            or (
                plausible_avia_location(segment.get("from"), segment.get("fromCode"))
                and plausible_avia_location(segment.get("to"), segment.get("toCode"))
            )
        )
        for segment in _segments(fields)
    )


def _has_transport_number(fields: dict) -> bool:
    return any(_present(segment.get("flightNo")) for segment in _segments(fields))


def _result_score(result: dict) -> int:
    fields = result.get("fields") if isinstance(result, dict) else {}
    if not isinstance(fields, dict):
        return -1000
    kind = str(fields.get("service_kind") or "other").lower()
    score = 0
    if kind != "other":
        score += 12
    if _present(fields.get("passenger_name")) or _passenger_rows(fields):
        score += 18
    if _has_route(fields):
        score += 18
    if _has_transport_number(fields) and kind in {"avia", "rail"}:
        score += 12
    if _ticket_number(fields) and kind in {"avia", "rail"}:
        score += 12
    if _present(fields.get("reference")):
        score += 3
    if _present(fields.get("issue_date")):
        score += 3
    if _decimal(fields.get("total")) not in (None, Decimal("0")):
        score += 8
    if _present(fields.get("currency")):
        score += 2

    segments = _segments(fields)
    score += min(len(segments), 6) * 3
    score += min(len(_passenger_rows(fields)), 8) * 2

    receipts = fields.get("receipts") or fields.get("railTickets") or []
    if isinstance(receipts, list) and receipts:
        score += min(len(receipts), 12) * 5
        good = sum(
            1
            for receipt in receipts
            if isinstance(receipt, dict)
            and _present(receipt.get("passenger") or receipt.get("passenger_name"))
            and _present(receipt.get("ticketNo") or receipt.get("ticket_number"))
            and _has_route(receipt)
        )
        score += good * 5

    if result.get("status") == "parsed":
        score += 5
    if result.get("status") in {"error", "failed"}:
        score -= 30
    return score


def _specialized_result(text: str) -> dict | None:
    """Try deterministic supplier parsers against every text representation."""

    parsers = []
    try:
        from documents.receipt_red_wings_patch import _parse_red_wings

        parsers.append(("red_wings", _parse_red_wings))
    except Exception:
        pass
    try:
        from documents.receipt_problem_formats_patch import _parse_s7_ticket

        parsers.append(("s7", _parse_s7_ticket))
    except Exception:
        pass
    try:
        from documents.receipt_multiform_patch import _parse_psc_air, _parse_psc_hotel

        parsers.extend((("psc_air", _parse_psc_air), ("psc_hotel", _parse_psc_hotel)))
    except Exception:
        pass
    try:
        from documents.receipt_parser_patch_safe import _hotel, _hotel_details, _rail

        parsers.append(("rzd", _rail))

        def parse_partner_hotel(value: str) -> dict | None:
            fields = _hotel(value)
            if not fields or fields.get("service_kind") != "hotel":
                return None
            fields.update(_hotel_details(value, fields))
            return fields

        parsers.append(("partner_hotel", parse_partner_hotel))
    except Exception:
        pass

    best: dict | None = None
    best_score = -1000
    for parser_name, parser in parsers:
        try:
            fields = parser(text)
        except Exception:
            continue
        if not isinstance(fields, dict) or not fields:
            continue
        result = {
            "fields": fields,
            "raw": {"recognition_specialized_parser": parser_name},
            "warnings": [],
            "status": "parsed",
            "confidence": Decimal("0.970"),
        }
        score = _result_score(result)
        if score > best_score:
            best = result
            best_score = score
    return best


def _merge_passengers(primary: list, secondary: list) -> list:
    result = [deepcopy(row) for row in primary if isinstance(row, dict)]
    by_name = {str(row.get("name") or "").strip().casefold(): row for row in result if row.get("name")}
    for row in secondary:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        key = name.casefold()
        if key not in by_name:
            cloned = deepcopy(row)
            result.append(cloned)
            by_name[key] = cloned
            continue
        target = by_name[key]
        for field, value in row.items():
            if not _present(target.get(field)) and _present(value):
                target[field] = deepcopy(value)
    return result


def _segment_completeness(segment: dict, *, service_kind: str = "") -> int:
    keys = ("from", "fromCode", "to", "toCode", "date", "dep", "arr", "flightNo", "coach", "seat", "status")
    score = sum(_present(segment.get(key)) for key in keys)
    origin = segment.get("from") or segment.get("fromCode")
    destination = segment.get("to") or segment.get("toCode")
    # Only apply aviation semantics to aviation data. Hotel stays reuse the
    # same segment shape, but an empty origin and a hotel name as destination
    # are perfectly valid there.
    has_airport_code = _present(segment.get("fromCode")) or _present(segment.get("toCode"))
    validate_as_avia = service_kind == "avia" or has_airport_code
    if validate_as_avia and (origin or destination) and not (
        plausible_avia_location(segment.get("from"), segment.get("fromCode"))
        and plausible_avia_location(segment.get("to"), segment.get("toCode"))
    ):
        score -= 20
    return score


def _segments_score(rows: list, *, service_kind: str = "") -> int:
    return sum(
        _segment_completeness(row, service_kind=service_kind)
        for row in rows
        if isinstance(row, dict)
    ) + len(rows) * 3


def _merge_dict(primary: dict, secondary: dict, *, service_kind: str = "") -> dict:
    result = deepcopy(primary)
    active_service_kind = str(
        primary.get("service_kind")
        or secondary.get("service_kind")
        or service_kind
        or "other"
    ).lower()
    for key, value in secondary.items():
        current = result.get(key)
        if key == "passengers" and isinstance(value, list):
            result[key] = _merge_passengers(current if isinstance(current, list) else [], value)
            continue
        if key in {"segments", "legs"} and isinstance(value, list):
            current_rows = current if isinstance(current, list) else []
            if _segments_score(value, service_kind=active_service_kind) > _segments_score(
                current_rows,
                service_kind=active_service_kind,
            ):
                result[key] = deepcopy(value)
            continue
        if key in {"receipts", "railTickets", "groupTickets"} and isinstance(value, list):
            current_rows = current if isinstance(current, list) else []
            if len(value) > len(current_rows):
                result[key] = deepcopy(value)
            continue
        if key in {"fare_breakdown", "tax_breakdown", "fee_breakdown"} and isinstance(value, list):
            current_rows = current if isinstance(current, list) else []
            if len(value) > len(current_rows):
                result[key] = deepcopy(value)
            continue
        if isinstance(value, dict):
            result[key] = _merge_dict(
                current if isinstance(current, dict) else {},
                value,
                service_kind=active_service_kind,
            )
            continue
        if not _present(current) and _present(value):
            result[key] = deepcopy(value)
    return result


def _compact_route_recovery(text: str) -> list[dict]:
    """Recover routes printed in transfer/endorsement lines of air tickets."""

    rows: list[dict] = []
    for flight, origin, destination in re.findall(
        r"\b([A-Z0-9]{2,3}\s*[- ]?\s*\d{2,5})\s+([A-Z]{3})\s*[-–—]\s*([A-Z]{3})\b",
        text,
        flags=re.IGNORECASE,
    ):
        normalized_flight = re.sub(r"\s+", "", flight).upper()
        key = (normalized_flight, origin.upper(), destination.upper())
        if any((row["flightNo"], row["fromCode"], row["toCode"]) == key for row in rows):
            continue
        rows.append(
            {
                "from": origin.upper(),
                "fromCode": origin.upper(),
                "to": destination.upper(),
                "toCode": destination.upper(),
                "date": "",
                "dep": "",
                "arr": "",
                "flightNo": normalized_flight,
                "dir": "out" if not rows else "seg",
            }
        )
    if len(rows) > 1 and rows[-1]["toCode"] == rows[0]["fromCode"]:
        rows[-1]["dir"] = "back"
    return rows


def _repair_finances(fields: dict, warnings: list[str]) -> None:
    total = _decimal(fields.get("total"))
    fare = _decimal(fields.get("fare"))
    taxes = _decimal(fields.get("taxes")) or Decimal("0")
    fees = _decimal(fields.get("fees")) or Decimal("0")
    if total is None or total < 0:
        return
    if fare is None and total >= taxes + fees:
        fields["fare"] = total - taxes - fees
        fare = _decimal(fields.get("fare"))
    if fare is None:
        return
    delta = abs(total - (fare + taxes + fees))
    if delta > Decimal("1.00"):
        warnings.append(
            "Нужно проверить стоимость: тариф + таксы + сборы не совпадают с итогом поставщика."
        )


def _repair_grouped_rail(fields: dict, warnings: list[str]) -> None:
    receipts = fields.get("receipts") or fields.get("railTickets") or []
    if not isinstance(receipts, list) or not receipts:
        return
    fields["receipt_count"] = len(receipts)
    ticket_numbers: list[str] = []
    passengers: list[dict] = []
    seen_passengers: set[tuple[str, str]] = set()
    all_segments: list[dict] = []
    seen_segments: set[tuple] = set()
    total = Decimal("0")
    for receipt in receipts:
        if not isinstance(receipt, dict):
            continue
        passenger_name = str(receipt.get("passenger") or receipt.get("passenger_name") or "").strip()
        ticket = str(receipt.get("ticketNo") or receipt.get("ticket_number") or "").strip()
        if ticket:
            ticket_numbers.append(ticket)
        if passenger_name:
            key = (passenger_name.casefold(), ticket)
            if key not in seen_passengers:
                seen_passengers.add(key)
                passengers.append(
                    {
                        "name": passenger_name,
                        "dob": receipt.get("date_of_birth") or "",
                        "document": receipt.get("document_number") or "",
                        "ticketNo": ticket,
                    }
                )
        for segment in receipt.get("segments") or receipt.get("legs") or []:
            if not isinstance(segment, dict):
                continue
            key = (
                segment.get("from"), segment.get("to"), segment.get("date"), segment.get("dep"),
                segment.get("arr"), segment.get("flightNo"), segment.get("coach"), segment.get("seat"),
            )
            if key not in seen_segments:
                seen_segments.add(key)
                all_segments.append(deepcopy(segment))
        value = _decimal(receipt.get("total"))
        if value is not None:
            total += value
    if passengers:
        fields["passengers"] = passengers
        fields["passenger_name"] = ", ".join(dict.fromkeys(row["name"] for row in passengers))
    if ticket_numbers:
        fields["ticket_number"] = ", ".join(ticket_numbers)
    if all_segments:
        fields["segments"] = all_segments
    if total:
        fields["total"] = total
        fields.setdefault("currency", "RUB")

    source_count = fields.get("source_coupon_pages")
    if source_count is not None and int(source_count) != len(receipts):
        warnings.append(
            f"Нужно проверить ЖД PDF: распознано {len(receipts)} бланков из {source_count} страниц-купонов."
        )
    duplicate_tickets = len(ticket_numbers) != len(set(ticket_numbers))
    if duplicate_tickets:
        warnings.append("Нужно проверить ЖД PDF: обнаружены повторяющиеся номера билетов.")


def _repair_fields(fields: dict, text: str, warnings: list[str]) -> None:
    kind = str(fields.get("service_kind") or "other").lower()
    passengers = _passenger_rows(fields)
    if not _present(fields.get("passenger_name")) and passengers:
        fields["passenger_name"] = ", ".join(row["name"] for row in passengers)
    if not _present(fields.get("ticket_number")):
        ticket = _ticket_number(fields)
        if ticket:
            fields["ticket_number"] = ticket

    if kind == "avia":
        recovered = _compact_route_recovery(text)
        current = _segments(fields)
        current_is_plausible = all(
            plausible_avia_location(row.get("from"), row.get("fromCode"))
            and plausible_avia_location(row.get("to"), row.get("toCode"))
            for row in current
        ) if current else False
        if recovered and (
            not current_is_plausible
            or _segments_score(recovered, service_kind="avia")
            > _segments_score(current, service_kind="avia")
        ):
            # Preserve dates/times from existing rows when route recovery only
            # contributes flight and airport identities.
            if len(recovered) == len(current):
                for index, row in enumerate(recovered):
                    for key in ("date", "endDate", "dep", "arr", "carrier", "cls", "status", "fareBasis", "cabin", "baggage"):
                        if _present(current[index].get(key)) and not _present(row.get(key)):
                            row[key] = current[index][key]
            fields["segments"] = recovered
    if kind == "rail":
        _repair_grouped_rail(fields, warnings)
    _repair_finances(fields, warnings)


def _consistency_warnings(fields: dict) -> list[str]:
    warnings: list[str] = []
    kind = str(fields.get("service_kind") or "other").lower()
    if kind in {"avia", "rail"}:
        for index, segment in enumerate(_segments(fields)):
            origin = str(segment.get("fromCode") or segment.get("from") or "").strip()
            destination = str(segment.get("toCode") or segment.get("to") or "").strip()
            if origin and destination and origin.casefold() == destination.casefold():
                warnings.append(f"Нужно проверить сегмент {index + 1}: пункт отправления совпадает с пунктом назначения.")
            if kind == "avia" and not (
                plausible_avia_location(segment.get("from"), segment.get("fromCode"))
                and plausible_avia_location(segment.get("to"), segment.get("toCode"))
            ):
                warnings.append(
                    f"Нужно проверить сегмент {index + 1}: вместо авиационной локации распознаны реквизиты или служебный текст."
                )
            dep = str(segment.get("dep") or "").strip()
            arr = str(segment.get("arr") or "").strip()
            for label, value in (("время отправления", dep), ("время прибытия", arr)):
                if value and not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", value):
                    warnings.append(f"Нужно проверить сегмент {index + 1}: некорректное {label} «{value}».")
    return warnings


def _mark_diagnostics(result: dict, *, sources: list[str], candidate_count: int, chosen_score: int) -> None:
    raw = result.setdefault("raw", {})
    if not isinstance(raw, dict):
        raw = {}
        result["raw"] = raw
    raw["recognition_engine_version"] = ENGINE_VERSION
    raw["recognition_text_sources"] = sources
    raw["recognition_candidate_count"] = candidate_count
    raw["recognition_chosen_score"] = chosen_score


def _enhance_pdf_result(original, content: bytes, *, mime: str, name: str, initial: dict) -> dict:
    candidates = _pdf_text_candidates(content)
    parsed_candidates: list[tuple[str, dict, str]] = [("current_pipeline", initial, "")]

    for source, text in candidates:
        specialized = _specialized_result(text)
        if specialized is not None:
            parsed_candidates.append((f"{source}:specialized", specialized, text))
        try:
            generic = original(text.encode("utf-8"), mime="text/plain", name=name)
        except Exception:
            generic = None
        if isinstance(generic, dict):
            parsed_candidates.append((f"{source}:generic", generic, text))

    parsed_candidates.sort(key=lambda item: _result_score(item[1]), reverse=True)
    chosen_source, chosen, chosen_text = parsed_candidates[0]
    final = deepcopy(chosen)
    final_fields = final.setdefault("fields", {})
    if not isinstance(final_fields, dict):
        final_fields = {}
        final["fields"] = final_fields

    for _source, candidate, _text in parsed_candidates[1:]:
        fields = candidate.get("fields") if isinstance(candidate, dict) else None
        if not isinstance(fields, dict):
            continue
        candidate_kind = str(fields.get("service_kind") or "other").lower()
        final_kind = str(final_fields.get("service_kind") or "other").lower()
        if final_kind != "other" and candidate_kind not in {"other", final_kind}:
            continue
        final_fields = _merge_dict(final_fields, fields)
        final["fields"] = final_fields

    richest_text = max((text for _source, text in candidates), key=_text_score, default=chosen_text)
    warnings = [str(item) for item in (final.get("warnings") or []) if str(item).strip()]
    _repair_fields(final_fields, richest_text, warnings)
    warnings.extend(_consistency_warnings(final_fields))
    warnings = list(dict.fromkeys(warnings))
    final["warnings"] = warnings

    serious = [warning for warning in warnings if warning.startswith("Нужно проверить")]
    final = apply_receipt_quality_guard(final)
    if serious:
        final["status"] = "manual_review"
        final["confidence"] = min(final.get("confidence") or Decimal("0"), Decimal("0.690"))
    elif final.get("status") == "parsed":
        # Do not claim perfect certainty for heuristic merging, but preserve a
        # high confidence for deterministic supplier-specific results.
        final["confidence"] = min(max(final.get("confidence") or Decimal("0"), Decimal("0.850")), Decimal("0.995"))

    _mark_diagnostics(
        final,
        sources=[source for source, _text in candidates] + [chosen_source],
        candidate_count=len(parsed_candidates),
        chosen_score=_result_score(chosen),
    )
    return final


def install_receipt_recognition_engine() -> None:
    from documents import services

    if getattr(services.extract_receipt_fields, "_recognition_engine_v2", False):
        return
    original = services.extract_receipt_fields

    def wrapped(content: bytes, *, mime: str = "", name: str = ""):
        try:
            initial = original(content, mime=mime, name=name)
        except Exception as exc:
            initial = {
                "fields": {},
                "raw": {"parser_exception": exc.__class__.__name__},
                "warnings": ["Автоматический парсер завершился с ошибкой; документ сохранён для повторного анализа."],
                "status": "manual_review",
                "confidence": Decimal("0"),
            }

        is_pdf = mime == "application/pdf" or content.startswith(b"%PDF")
        if not is_pdf:
            result = apply_receipt_quality_guard(initial)
            _mark_diagnostics(result, sources=["non_pdf"], candidate_count=1, chosen_score=_result_score(result))
            return result
        return _enhance_pdf_result(original, content, mime=mime, name=name, initial=initial)

    wrapped._recognition_engine_v2 = True
    services.extract_receipt_fields = wrapped
