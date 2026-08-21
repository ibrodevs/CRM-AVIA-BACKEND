from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from documents.receipt_parser_patch_safe import _json_safe

HARDENING_VERSION = "2026.08.21-v3"
_CURRENCIES = {"RUB", "USD", "EUR", "KGS", "CNY", "РУБ"}
_PASSENGER_TITLE = re.compile(
    r"\s+(?:MR|MRS|MS|MSTR|Г-Н|Г-ЖА|Г-Ж|ГОСПОДИН|ГОСПОЖА)$",
    re.IGNORECASE,
)


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(re.sub(r"\s+", "", str(value)).replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _pdf_pages(content: bytes) -> list[str]:
    # Some client PDFs contain malformed text arrays: viewers and pdfminer can
    # read them, while pypdf raises during ``extract_text``. Reuse the same
    # resilient per-page selection as the grouping engine so final hardening is
    # never silently skipped for exactly those supplier files.
    try:
        from documents.receipt_multiform_patch import _page_texts

        pypdf_pages, pdfminer_pages = _page_texts(content)
        count = max(len(pypdf_pages), len(pdfminer_pages))
        # Prefer pypdf whenever it successfully exposes the page because its
        # row order is better for route columns. Use pdfminer only for pages
        # whose malformed stream makes pypdf return no text.
        return [
            (pypdf_pages[index] if index < len(pypdf_pages) else "")
            or (pdfminer_pages[index] if index < len(pdfminer_pages) else "")
            for index in range(count)
        ]
    except Exception:
        return []


def _page_token(value: Any) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def _receipt_source_page(item: dict, pages: list[str], fallback: int) -> int:
    page_tokens = [_page_token(page) for page in pages]
    markers = (
        item.get("blankId"),
        item.get("ticketNo"),
        item.get("ticket_number"),
        item.get("docNo"),
        item.get("document_number"),
        item.get("passenger"),
        item.get("passenger_name"),
    )
    for marker in markers:
        token = _page_token(marker)
        if len(token) < 5:
            continue
        for page_index, page_token in enumerate(page_tokens):
            if token in page_token:
                return page_index
    return min(max(fallback, 0), max(len(pages) - 1, 0))


def _is_bilingual_eticket(text: str) -> bool:
    flat = _clean(text).casefold()
    return all(
        marker in flat
        for marker in (
            "electronic ticket",
            "номер билета",
            "ticket number",
            "рейс/flight",
            "тариф/fare",
        )
    )


def _source_issuer(text: str) -> str:
    flat = _clean(text)
    match = re.search(
        r"(?:Выдан\s+от\s*/?\s*Issued\s+by|Issued\s+by)\s*:?[ ]*"
        r"(.+?)(?=\s+(?:Дата\s+выдачи|Date\s+of\s+issue)\b)",
        flat,
        re.IGNORECASE,
    )
    if match:
        return _clean(match.group(1))
    # Older TCH-style receipts print four labels first and their values below:
    # ``ВЫДАН ОТ / НОМЕР БИЛЕТА / ... / : RYANAIR / : 1111484695``.
    old_form = re.search(
        r"ВЫДАН\s+ОТ\s+НОМЕР\s+БИЛЕТА\s+В\s+ОБМЕН\s+НА\s+"
        r"ПЕРВОН\.?\s+ВЫДАН\s*:\s*(.+?)\s*:\s*(?:\d{3}\s+)?\d{10}\b",
        flat,
        re.IGNORECASE,
    )
    return _clean(old_form.group(1)).strip(":") if old_form else ""


def _source_ticket_number(text: str) -> str:
    flat = _clean(text)
    old_form = re.search(
        r"ВЫДАН\s+ОТ\s+НОМЕР\s+БИЛЕТА\s+В\s+ОБМЕН\s+НА\s+"
        r"ПЕРВОН\.?\s+ВЫДАН\s*:\s*.+?\s*:\s*((?:\d{3}\s+)?\d{10})\b",
        flat,
        re.IGNORECASE,
    )
    if old_form:
        return _clean(old_form.group(1))
    labeled = re.search(
        r"(?:НОМЕР\s+БИЛЕТА|TICKET\s+NUMBER)\s*:?\s*((?:\d{3}\s+)?\d{10})\b",
        flat,
        re.IGNORECASE,
    )
    return _clean(labeled.group(1)) if labeled else ""


def _source_document_number(text: str) -> str:
    flat = _clean(text)
    old_form = re.search(
        r"ФАМИЛИЯ\s*:.+?\b([A-ZА-ЯЁ]{1,4}\s*\d{6,14})\b.+?ОТПРВ/НАЗН",
        flat,
        re.IGNORECASE,
    )
    if old_form:
        return re.sub(r"\s+", "", old_form.group(1))
    labeled = re.search(
        r"(?:ДОКУМЕНТ|ПАСПОРТ|DOCUMENT)\s*(?:№|NO|NUMBER)?\s*:?\s*"
        r"([A-ZА-ЯЁ]{0,4}\s*\d{6,14})\b",
        flat,
        re.IGNORECASE,
    )
    return re.sub(r"\s+", "", labeled.group(1)) if labeled else ""


def _clean_passenger_name(value: Any) -> str:
    name = _PASSENGER_TITLE.sub("", _clean(value))
    return _clean(name.replace("/", " "))


def _fare_calculation_codes(text: str) -> list[str]:
    flat = _clean(text)
    match = re.search(
        r"Расчет\s+тарифа\s*/?\s*Fare\s+calculation\s+(.+?)\s+Тариф\s*/?\s*Fare",
        flat,
        re.IGNORECASE,
    )
    if not match:
        return []
    codes = re.findall(
        r"(?<![A-Z])([A-Z]{3})(?=(?:\s|\d|RUB|USD|EUR|KGS|CNY|END|$))",
        match.group(1).upper(),
    )
    return [code for code in codes if code not in _CURRENCIES and code != "END"]


def _label_amount(text: str, label: str) -> Decimal | None:
    flat = _clean(text)
    match = re.search(
        label
        + r"\s*(?:þÿ)?\s*(\d[\d ]*(?:[,.]\d{1,2})?)\s*(?:RUB|РУБ)\b",
        flat,
        re.IGNORECASE,
    )
    return _decimal(match.group(1)) if match else None


def _tax_rows(text: str) -> list[dict]:
    flat = _clean(text)
    section = re.search(
        r"Сбор\s*/?\s*Tax\s*/?\s*fee\s*/?\s*(?:charge)?\s+(.+?)\s+Итого\s*/?\s*Total",
        flat,
        re.IGNORECASE,
    )
    if not section:
        return []
    rows: list[dict] = []
    for code, amount in re.findall(
        r"\b([A-Z]{2,3})\s*(\d[\d ]*(?:[,.]\d{1,2})?)\s*(?:RUB|РУБ)\b",
        section.group(1),
        re.IGNORECASE,
    ):
        parsed = _decimal(amount)
        if parsed is None:
            continue
        rows.append(
            {
                "code": code.upper(),
                "label": code.upper(),
                "amount": str(parsed),
                "currency": "RUB",
            }
        )
    return rows


def _reconciled_tax_rows(rows: list[dict], expected: Decimal) -> list[dict]:
    if expected <= 0:
        return []
    if not rows:
        return [
            {
                "code": "TAX",
                "label": "Таксы и сборы",
                "amount": str(expected),
                "currency": "RUB",
            }
        ]
    amounts = [_decimal(row.get("amount")) or Decimal("0") for row in rows]
    if abs(sum(amounts, Decimal("0")) - expected) <= Decimal("0.01"):
        return rows
    previous = sum(amounts[:-1], Decimal("0"))
    if previous <= expected:
        rows[-1]["amount"] = str(expected - previous)
        return rows
    return [
        {
            "code": "TAX",
            "label": "Таксы и сборы",
            "amount": str(expected),
            "currency": "RUB",
        }
    ]


def _hand_baggage(text: str, fields: dict) -> str:
    flat = _clean(text)
    if not re.search(r"ручн(?:ая|ой)\s+клад", flat, re.IGNORECASE):
        return ""
    cabin = _clean(fields.get("booking_class"))
    segments = fields.get("segments") if isinstance(fields.get("segments"), list) else []
    for segment in segments:
        if isinstance(segment, dict) and segment.get("cabin"):
            cabin = _clean(segment["cabin"])
            break
    class_weights: dict[str, str] = {}
    for class_name, weight in re.findall(
        r"класс\s+(Эконом|Комфорт|Бизнес).{0,100}?не\s+более\s+(\d+)\s*кг",
        flat,
        re.IGNORECASE,
    ):
        class_weights[class_name.casefold()] = weight
    cabin_key = cabin.casefold()
    weight = ""
    for class_name, value in class_weights.items():
        if class_name in cabin_key:
            weight = value
            break
    if not weight:
        explicit = re.search(
            r"ручн(?:ая|ой)\s+клад[^.;]{0,140}?(\d+)\s*(?:кг|kg)\b",
            flat,
            re.IGNORECASE,
        )
        weight = explicit.group(1) if explicit else ""
    if not weight:
        return ""
    value = f"1 место до {weight} кг"
    dimensions = re.search(
        r"(\d+)\s*см\s+в\s+длину,?\s*(\d+)\s*см\s+в\s+ширину,?\s*"
        r"(\d+)\s*см\s+в\s+высоту",
        flat,
        re.IGNORECASE,
    )
    if dimensions:
        value += f" ({dimensions.group(1)}×{dimensions.group(2)}×{dimensions.group(3)} см)"
    return value


def _repair_finances(fields: dict, text: str) -> bool:
    output = fields.get("output") if isinstance(fields.get("output"), dict) else {}
    if str(output.get("priceMode") or "").lower() == "it":
        return False
    fare = _label_amount(text, r"Тариф\s*/?\s*Fare")
    total = _label_amount(text, r"Итого\s*/?\s*Total")
    if fare is None or total is None or total < fare:
        return False
    taxes = total - fare
    tax_rows = _reconciled_tax_rows(_tax_rows(text), taxes)
    fields.update(
        {
            "fare": fare,
            "taxes": taxes,
            "fees": Decimal("0"),
            "total": total,
            "originalTotal": total,
            "currency": "RUB",
            "fare_breakdown": [
                {
                    "code": "FARE",
                    "label": "Тариф",
                    "amount": str(fare),
                    "currency": "RUB",
                }
            ],
            "tax_breakdown": tax_rows,
            "fee_breakdown": [],
        }
    )
    return True


def harden_rail_fields(fields: dict, text: str) -> set[str]:
    """Read the printed ticket/reserved-seat split from modern RZD coupons."""

    changed: set[str] = set()
    if not isinstance(fields, dict) or str(fields.get("service_kind") or "").lower() != "rail":
        return changed
    flat = _clean(text)
    ticket_match = re.search(
        r"Тариф\s+билета\s*,?\s*руб\.?\s*"
        r"(?:Fare\s+ticket\s*,?\s*RUB\s*)?(\d[\d ]*(?:[,.]\d{1,2})?)",
        flat,
        re.IGNORECASE,
    )
    reserved_match = re.search(
        r"Тариф\s+плацкарт[аы]\s*,?\s*руб\.?\s*"
        r"(?:Fare\s+reservation\s*,?\s*RUB\s*)?(\d[\d ]*(?:[,.]\d{1,2})?)",
        flat,
        re.IGNORECASE,
    )
    total_match = re.search(
        r"Цена\s*,?\s*руб\.?\s*(?:Price\s*,?\s*RUB\s*)?"
        r"(\d[\d ]*(?:[,.]\d{1,2})?)",
        flat,
        re.IGNORECASE,
    )
    combined_match = re.search(
        r"Тариф\s*\(\s*билет\s*,\s*плацкарт[аы]?\s*\)"
        r".{0,320}?(\d[\d ]*(?:[,.]\d{1,2})?)\s*/\s*"
        r"(\d[\d ]*(?:[,.]\d{1,2})?)",
        flat,
        re.IGNORECASE,
    )
    ticket = _decimal(ticket_match.group(1)) if ticket_match else None
    reserved = _decimal(reserved_match.group(1)) if reserved_match else None
    if combined_match and (ticket is None or reserved is None):
        ticket = _decimal(combined_match.group(1))
        reserved = _decimal(combined_match.group(2))
    total = _decimal(total_match.group(1)) if total_match else None
    if ticket is None or reserved is None:
        return changed
    calculated = ticket + reserved
    # Printed components are authoritative. Preserve a separately printed total
    # only when it reconciles; otherwise surface the mathematically safe sum.
    payable = total if total is not None and abs(total - calculated) <= Decimal("0.01") else calculated
    values = {
        "ticketCost": ticket,
        "reservedSeatCost": reserved,
        "fare": ticket,
        "fees": reserved,
        "total": payable,
        "originalTotal": payable,
        "currency": "RUB",
    }
    for key, value in values.items():
        if fields.get(key) != value:
            fields[key] = value
            changed.add(key)
    return changed


def harden_avia_fields(fields: dict, text: str) -> set[str]:
    """Repair values by document labels, independently of passenger-specific data."""

    changed: set[str] = set()
    if not isinstance(fields, dict) or str(fields.get("service_kind") or "").lower() != "avia":
        return changed

    issuer = _source_issuer(text)
    if issuer and issuer != fields.get("issuer"):
        fields["issuer"] = issuer
        changed.add("issuer")

    ticket_number = _source_ticket_number(text)
    if ticket_number and ticket_number != fields.get("ticket_number"):
        fields["ticket_number"] = ticket_number
        changed.add("ticket_number")

    document_number = _source_document_number(text)
    if document_number and document_number != fields.get("document_number"):
        fields["document_number"] = document_number
        changed.add("document_number")

    passenger = _clean_passenger_name(fields.get("passenger_name"))
    if passenger and passenger != fields.get("passenger_name"):
        fields["passenger_name"] = passenger
        changed.add("passenger_name")
    passengers = fields.get("passengers") if isinstance(fields.get("passengers"), list) else []
    for index, row in enumerate(passengers):
        if not isinstance(row, dict):
            continue
        clean_name = _clean_passenger_name(row.get("name"))
        if clean_name and clean_name != row.get("name"):
            row["name"] = clean_name
            changed.add("passengers")
        if index == 0 and ticket_number and row.get("ticketNo") != ticket_number:
            row["ticketNo"] = ticket_number
            changed.add("passengers")
        if index == 0 and document_number and row.get("document") != document_number:
            row["document"] = document_number
            changed.add("passengers")

    if not _is_bilingual_eticket(text):
        return changed

    segments = fields.get("segments") if isinstance(fields.get("segments"), list) else []
    route_codes = _fare_calculation_codes(text)
    if len(route_codes) >= len(segments) + 1:
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            if not _clean(segment.get("fromCode")):
                segment["fromCode"] = route_codes[index]
                changed.add("segments")
            if not _clean(segment.get("toCode")):
                segment["toCode"] = route_codes[index + 1]
                changed.add("segments")

    if _repair_finances(fields, text):
        changed.add("finances")

    hand_baggage = _hand_baggage(text, fields)
    if hand_baggage and hand_baggage != fields.get("hand_baggage"):
        fields["hand_baggage"] = hand_baggage
        changed.add("hand_baggage")
    if hand_baggage:
        for segment in segments:
            if isinstance(segment, dict) and not segment.get("handBaggage"):
                segment["handBaggage"] = hand_baggage
                changed.add("segments")
    return changed


def _sync_raw(result: dict, fields: dict, changed: set[str]) -> None:
    raw = result.setdefault("raw", {})
    if not isinstance(raw, dict):
        raw = {}
        result["raw"] = raw
    raw["structural_hardening_version"] = HARDENING_VERSION
    raw["structural_hardening_fields"] = sorted(changed)
    for key in (
        "issuer",
        "passenger_name",
        "passengers",
        "ticket_number",
        "document_number",
        "segments",
        "fare",
        "taxes",
        "fees",
        "total",
        "currency",
        "hand_baggage",
        "fare_breakdown",
        "tax_breakdown",
        "fee_breakdown",
        "ticketCost",
        "reservedSeatCost",
        "agencyServiceFee",
        "additionalFees",
    ):
        if key in fields:
            raw[key] = _json_safe(fields[key])


def install_receipt_structural_hardening() -> None:
    from documents import services

    if getattr(services.extract_receipt_fields, "_receipt_structural_hardening", False):
        return
    original = services.extract_receipt_fields

    def wrapped(content: bytes, *, mime: str = "", name: str = ""):
        result = original(content, mime=mime, name=name)
        if not isinstance(result, dict) or not (mime == "application/pdf" or content.startswith(b"%PDF")):
            return result
        fields = result.get("fields")
        if not isinstance(fields, dict):
            return result
        service_kind = str(fields.get("service_kind") or "").lower()
        if service_kind not in {"avia", "rail"}:
            return result
        pages = _pdf_pages(content)
        if not pages:
            return result

        hardener = harden_rail_fields if service_kind == "rail" else harden_avia_fields
        changed = hardener(fields, "\n".join(pages))
        items = fields.get("receipt_items") or fields.get("receipts") or []
        if isinstance(items, list) and items:
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                try:
                    fallback_page_index = max(int(item.get("receiptPage") or index + 1) - 1, 0)
                except (TypeError, ValueError):
                    fallback_page_index = index
                page_index = _receipt_source_page(item, pages, fallback_page_index)
                item["receiptPage"] = page_index + 1
                item["receipt_page"] = page_index + 1
                child_text = pages[page_index] if page_index < len(pages) else "\n".join(pages)
                item_kind = str(item.get("service_kind") or service_kind).lower()
                item_hardener = harden_rail_fields if item_kind == "rail" else harden_avia_fields
                changed.update(item_hardener(item, child_text))

        _sync_raw(result, fields, changed)
        return result

    wrapped._receipt_structural_hardening = True
    services.extract_receipt_fields = wrapped
