from __future__ import annotations

import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from documents.receipt_metadata import json_safe
from documents.receipt_multiform_patch import _aggregate_rail_receipts, _page_texts
from documents.receipt_parser_patch_safe import _rail
from documents.receipt_structural_hardening import harden_rail_fields

GROUPING_VERSION = "2026.08.21-v1"
_GROUP_KEYS = ("receipt_items", "receiptItems", "groupTickets", "receipts", "railTickets")


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _decimal(value: Any) -> Decimal:
    try:
        if value in (None, ""):
            return Decimal("0")
        return Decimal(str(value).replace("\u00a0", "").replace(" ", "").replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _kind(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"rail", "train", "жд", "ж/д"}:
        return "rail"
    if raw in {"avia", "flight", "авиа"}:
        return "avia"
    return raw or "other"


def _best_page_texts(content: bytes) -> list[tuple[int, str]]:
    pypdf_pages, pdfminer_pages = _page_texts(content)
    count = max(len(pypdf_pages), len(pdfminer_pages))
    selected: list[tuple[int, str]] = []
    for index in range(count):
        pypdf_text = pypdf_pages[index] if index < len(pypdf_pages) else ""
        miner_text = pdfminer_pages[index] if index < len(pdfminer_pages) else ""
        if "КОНТРОЛЬНЫЙ КУПОН" in pypdf_text and len(pypdf_text) > 500:
            text = pypdf_text
        elif len(miner_text.strip()) >= len(pypdf_text.strip()) * 0.8:
            text = miner_text
        else:
            text = pypdf_text or miner_text
        if text.strip():
            selected.append((index, text))
    return selected


def _split_ticket_blocks(text: str) -> list[str]:
    """Split several printable blanks that happen to share one physical page."""

    markers = (
        r"(?=Электронный билет\s*\(маршрут/квитанция для пассажира\))",
        r"(?=\bКОНТРОЛЬНЫЙ КУПОН\b)",
    )
    for marker in markers:
        starts = [match.start() for match in re.finditer(marker, text, re.IGNORECASE)]
        if len(starts) < 2:
            continue
        prefix = text[: starts[0]]
        blocks = []
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else len(text)
            block = (prefix if index == 0 else "") + text[start:end]
            if block.strip():
                blocks.append(block)
        return blocks
    return [text]


def _evidence(text: str) -> tuple[int, int]:
    flat = re.sub(r"\s+", " ", text or "").upper()
    rail = sum(
        weight
        for marker, weight in (
            ("КОНТРОЛЬНЫЙ КУПОН", 8),
            ("ПЛАЦКАРТА", 4),
            ("ПОЕЗД", 2),
            ("ВАГОН", 2),
            ("МЕСТО", 2),
            ("ПАСПОРТ РФ", 2),
            ("ОАО РЖД", 5),
            ("АО ФПК", 5),
            ("ЦППК", 5),
        )
        if marker in flat
    )
    avia = sum(
        weight
        for marker, weight in (
            ("МАРШРУТ/ПЕРЕВОЗЧИК", 6),
            ("ОТПРВ/НАЗН", 5),
            ("МАРШРУТ-КВИТАНЦ", 4),
            ("ITINERARY RECEIPT", 5),
            ("FLIGHT", 2),
            ("РЕЙС", 2),
            ("БАГАЖ", 1),
        )
        if marker in flat
    )
    return rail, avia


def _first_group(source: dict) -> list[dict]:
    for key in _GROUP_KEYS:
        rows = source.get(key)
        if isinstance(rows, list) and rows:
            return [row for row in rows if isinstance(row, dict)]
    return []


def _ticket_number(item: dict) -> str:
    return str(item.get("ticketNo") or item.get("ticket_number") or item.get("ticket_no") or "").strip()


def _passenger(item: dict) -> str:
    return str(item.get("passenger") or item.get("passenger_name") or "").strip()


def _identity_part(value: Any) -> str:
    return re.sub(r"\W+", "", str(value or "")).casefold()


def _first_segment(item: dict) -> dict:
    for key in ("segments", "legs"):
        values = item.get(key)
        if isinstance(values, list):
            for value in values:
                if isinstance(value, dict):
                    return value
    return {}


def _route_identity(item: dict) -> str:
    segment = _first_segment(item)
    fields = (
        "from",
        "fromCode",
        "to",
        "toCode",
        "date",
        "dep",
        "arr",
        "flightNo",
        "train",
        "trainNo",
        "train_number",
        "coach",
        "wagon",
        "car",
        "seat",
        "place",
    )
    return "|".join(_identity_part(segment.get(field) or item.get(field)) for field in fields)


def _identity(item: dict) -> str:
    ticket = _identity_part(_ticket_number(item))
    passenger = _identity_part(_passenger(item))
    document = _identity_part(item.get("document_number") or item.get("docNo"))
    route = _route_identity(item)
    total = _decimal(item.get("total"))
    if _kind(item.get("service_kind")) == "rail":
        # Grouped railway PDFs can print the same order/control number on many
        # coupons. Keep coach/seat/passenger data in the key so neighbours are
        # not collapsed into a false duplicate.
        if any((ticket, passenger, document, route)):
            return f"rail:{ticket}|{passenger}|{document}|{route}|{total}"
        return ""
    if ticket:
        return f"ticket:{ticket}"
    if any((passenger, document, route)):
        return f"fields:{passenger}|{document}|{route}|{total}"
    return ""


def _merge_item(current: dict, incoming: dict) -> dict:
    result = deepcopy(current)
    for key, value in incoming.items():
        if key in _GROUP_KEYS:
            continue
        if not _present(result.get(key)) and _present(value):
            result[key] = deepcopy(value)
        elif isinstance(value, list) and len(value) > len(result.get(key) or []):
            result[key] = deepcopy(value)
    pages = []
    for source in (current, incoming):
        values = source.get("sourcePages") or source.get("source_pages") or []
        if not values and source.get("sourcePage") is not None:
            values = [source.get("sourcePage")]
        for page in values:
            if page not in pages:
                pages.append(page)
    if pages:
        result["sourcePages"] = pages
        result["source_pages"] = pages
        result["sourcePage"] = pages[0]
        result["source_page"] = pages[0]
    return result


def _dedupe_items(items: list[dict]) -> tuple[list[dict], int]:
    deduped: list[dict] = []
    positions: dict[str, int] = {}
    duplicate_count = 0
    for item in items:
        identity = _identity(item)
        if identity and identity in positions:
            index = positions[identity]
            deduped[index] = _merge_item(deduped[index], item)
            duplicate_count += 1
            continue
        if identity:
            positions[identity] = len(deduped)
        deduped.append(item)
    return deduped, duplicate_count


def _clean_child(fields: dict, *, page_number: int, index: int) -> dict:
    child = deepcopy(fields)
    for key in _GROUP_KEYS:
        child.pop(key, None)
    service_kind = _kind(child.get("service_kind"))
    child["service_kind"] = service_kind
    child["service_type"] = "ЖД" if service_kind == "rail" else "Авиа"
    child["passenger"] = _passenger(child)
    child["passenger_name"] = child["passenger"]
    child["ticketNo"] = _ticket_number(child)
    child["ticket_number"] = child["ticketNo"]
    child["receiptIndex"] = index + 1
    child["receipt_index"] = index + 1
    child["receiptCount"] = 1
    child["receipt_count"] = 1
    child["sourcePage"] = page_number
    child["source_page"] = page_number
    child["sourcePages"] = [page_number]
    child["source_pages"] = [page_number]
    return child


def _parse_block(
    original: Callable,
    text: str,
    *,
    name: str,
    page_number: int,
    index: int,
) -> dict | None:
    rail = _rail(text)
    if rail:
        return _clean_child(rail, page_number=page_number, index=index)
    rail_score, avia_score = _evidence(text)
    try:
        parsed = original(text.encode("utf-8"), mime="text/plain", name=name)
    except Exception:
        return None
    fields = parsed.get("fields") if isinstance(parsed, dict) else None
    if not isinstance(fields, dict):
        return None
    parsed_kind = _kind(fields.get("service_kind"))
    # The full parser stack contains the supplier-specific RZD fast paths that
    # understand modern and legacy coupon layouts.  A direct ``_rail`` call is
    # intentionally cheap, but is not sufficient for every real client page.
    if rail_score >= 4 and rail_score > avia_score and parsed_kind == "rail":
        harden_rail_fields(fields, text)
        has_identity = bool(_ticket_number(fields) or _passenger(fields))
        has_route = bool(fields.get("segments") or fields.get("legs"))
        if has_identity and has_route:
            return _clean_child(fields, page_number=page_number, index=index)
        return None
    if avia_score < 4 or rail_score > avia_score or parsed_kind != "avia":
        return None
    has_identity = bool(_ticket_number(fields) or _passenger(fields))
    has_route = bool(fields.get("segments") or fields.get("legs"))
    if not has_identity or not has_route:
        return None
    return _clean_child(fields, page_number=page_number, index=index)


def _aliases(target: dict, items: list[dict]) -> None:
    for key in _GROUP_KEYS:
        target[key] = items
    target["receiptCount"] = len(items)
    target["receipt_count"] = len(items)
    target["sourceBlankCount"] = len(items)
    target["source_blank_count"] = len(items)
    target["documentIsContainer"] = len(items) > 1
    target["document_is_container"] = len(items) > 1


def _aggregate_avia(items: list[dict], current: dict) -> dict:
    first = deepcopy(items[0])
    passengers: list[dict] = []
    seen_passengers: set[tuple[str, str]] = set()
    segments: list[dict] = []
    seen_segments: set[tuple] = set()
    for item in items:
        item_passengers = item.get("passengers") or []
        if not item_passengers and _passenger(item):
            item_passengers = [{
                "name": _passenger(item),
                "document": item.get("document_number") or item.get("docNo") or "",
                "ticketNo": _ticket_number(item),
            }]
        for passenger in item_passengers:
            if not isinstance(passenger, dict):
                continue
            key = (
                str(passenger.get("name") or "").strip().casefold(),
                re.sub(r"\W+", "", str(passenger.get("ticketNo") or passenger.get("ticket_number") or "")),
            )
            if key not in seen_passengers:
                seen_passengers.add(key)
                passengers.append(deepcopy(passenger))
        for segment in item.get("segments") or item.get("legs") or []:
            if not isinstance(segment, dict):
                continue
            key = tuple(
                str(segment.get(field) or "").strip().casefold()
                for field in ("from", "fromCode", "to", "toCode", "date", "dep", "arr", "flightNo")
            )
            if key not in seen_segments:
                seen_segments.add(key)
                segments.append(deepcopy(segment))
    aggregate = {
        **current,
        **first,
        "passenger_name": ", ".join(dict.fromkeys(_passenger(item) for item in items if _passenger(item))),
        "passengers": passengers,
        "ticket_number": ", ".join(_ticket_number(item) for item in items if _ticket_number(item)),
        "segments": segments,
        "fare": sum((_decimal(item.get("fare")) for item in items), Decimal("0")),
        "taxes": sum((_decimal(item.get("taxes")) for item in items), Decimal("0")),
        "fees": sum((_decimal(item.get("fees")) for item in items), Decimal("0")),
        "total": sum((_decimal(item.get("total")) for item in items), Decimal("0")),
        "currency": first.get("currency") or current.get("currency") or "RUB",
        "service_kind": "avia",
        "service_type": "Авиа",
    }
    _aliases(aggregate, items)
    return aggregate


def apply_pdf_grouping(
    original: Callable,
    content: bytes,
    *,
    mime: str,
    name: str,
    result: dict,
) -> dict:
    if not isinstance(result, dict) or not (mime == "application/pdf" or content.startswith(b"%PDF")):
        return result
    fields = result.get("fields")
    if not isinstance(fields, dict):
        return result

    page_texts = _best_page_texts(content)
    if not page_texts:
        return result

    existing = _first_group(fields) or _first_group(result.get("raw") or {})
    candidates: list[dict] = []
    ticket_like_blocks = 0
    if len(existing) > 1:
        for index, item in enumerate(existing):
            source_page = int(
                item.get("sourcePage")
                or item.get("source_page")
                or item.get("receiptPage")
                or item.get("receipt_page")
                or index + 1
            )
            candidates.append(_clean_child(item, page_number=source_page, index=index))
        ticket_like_blocks = len(existing)
    else:
        candidate_index = 0
        for page_index, page_text in page_texts:
            blocks = _split_ticket_blocks(page_text)
            parsed_on_page = 0
            page_has_ticket_evidence = False
            for block in blocks:
                rail_score, avia_score = _evidence(block)
                if max(rail_score, avia_score) >= 4:
                    page_has_ticket_evidence = True
                child = _parse_block(
                    original,
                    block,
                    name=name,
                    page_number=page_index + 1,
                    index=candidate_index,
                )
                if child is not None:
                    candidates.append(child)
                    candidate_index += 1
                    parsed_on_page += 1
            # pypdf sometimes duplicates the words "КОНТРОЛЬНЫЙ КУПОН"
            # inside one graphical page. Count multiple same-page blanks only
            # when multiple complete children were actually parsed; otherwise
            # the physical page contributes at most one expected blank.
            ticket_like_blocks += max(parsed_on_page, 1 if page_has_ticket_evidence else 0)

    items, duplicate_count = _dedupe_items(candidates)
    kinds = {_kind(item.get("service_kind")) for item in items}
    warnings = [str(value) for value in (result.get("warnings") or []) if str(value).strip()]

    if len(items) >= 2 and len(kinds) == 1 and kinds <= {"rail", "avia"}:
        item_kind = next(iter(kinds))
        if item_kind == "rail":
            aggregate = _aggregate_rail_receipts(items, fields)
            items = [
                _clean_child(
                    item,
                    page_number=int(
                        item.get("sourcePage")
                        or item.get("source_page")
                        or item.get("receiptPage")
                        or item.get("receipt_page")
                        or index + 1
                    ),
                    index=index,
                )
                for index, item in enumerate(_first_group(aggregate) or items)
            ]
            _aliases(aggregate, items)
        else:
            aggregate = _aggregate_avia(items, fields)
        fields.clear()
        fields.update(aggregate)
        raw = result.setdefault("raw", {})
        if not isinstance(raw, dict):
            raw = {}
            result["raw"] = raw
        raw.update(json_safe(aggregate))
        _aliases(raw, json_safe(items))
        raw["pdf_grouping_version"] = GROUPING_VERSION
        raw["source_page_count"] = len(page_texts)
        raw["deduplicated_blank_count"] = duplicate_count
        warnings = [warning for warning in warnings if "отдельных" not in warning.lower()]
        warnings.append(
            f"Распознано отдельных {'ЖД' if item_kind == 'rail' else 'авиа'}-бланков: {len(items)}. "
            "Каждый билет сохранён и редактируется отдельно."
        )
        if duplicate_count:
            warnings.append(f"Удалено повторных страниц одного и того же билета: {duplicate_count}.")
        result["warnings"] = list(dict.fromkeys(warnings))
        result["status"] = "parsed"
        result["confidence"] = min(result.get("confidence") or Decimal("0.990"), Decimal("0.995"))
        return result

    # A railway blank must never stay classified as aviation merely because it
    # contains the generic phrase "electronic ticket". A direct rail parse is
    # decisive even for a one-page document.
    direct_rail = None
    for _page, text in page_texts:
        direct_rail = _rail(text)
        if direct_rail:
            break
    if direct_rail and _kind(fields.get("service_kind")) != "rail":
        fields.clear()
        fields.update(direct_rail)
        result.setdefault("raw", {}).update(json_safe(direct_rail))
        warnings.append("Тип исправлен на ЖД по полям поезд, вагон, место и плацкарта.")
        result["warnings"] = list(dict.fromkeys(warnings))
        result["status"] = "parsed"
        result["confidence"] = Decimal("0.995")
        return result

    if ticket_like_blocks >= 2 and len(items) < ticket_like_blocks:
        fields["sourceBlankCount"] = ticket_like_blocks
        fields["source_blank_count"] = ticket_like_blocks
        raw = result.setdefault("raw", {})
        raw["sourceBlankCount"] = ticket_like_blocks
        raw["source_blank_count"] = ticket_like_blocks
        raw["pdf_grouping_version"] = GROUPING_VERSION
        warnings.append(
            f"Нужно проверить групповой PDF: найдено бланков {ticket_like_blocks}, "
            f"надёжно распознано {len(items)}. Файл не объединён в один билет."
        )
        result["warnings"] = list(dict.fromkeys(warnings))
        result["status"] = "manual_review"
        result["confidence"] = min(result.get("confidence") or Decimal("0"), Decimal("0.490"))
    return result


def install_receipt_pdf_grouping() -> None:
    from documents import services

    if getattr(services.extract_receipt_fields, "_pdf_grouping_v1", False):
        return
    original = services.extract_receipt_fields

    def wrapped(content: bytes, *, mime: str = "", name: str = ""):
        result = original(content, mime=mime, name=name)
        return apply_pdf_grouping(
            original,
            content,
            mime=mime,
            name=name,
            result=result,
        )

    wrapped._pdf_grouping_v1 = True
    services.extract_receipt_fields = wrapped
