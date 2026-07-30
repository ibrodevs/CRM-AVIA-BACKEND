import re
from decimal import Decimal, InvalidOperation


_CURRENCY = r"(?:USD|EUR|RUB|KGS|KZT|сом|руб\.?|₽|\$|€)"
_CURRENCY_ALIASES = {
    "СОМ": "KGS",
    "РУБ": "RUB",
    "РУБ.": "RUB",
    "₽": "RUB",
    "$": "USD",
    "€": "EUR",
}
_FINANCIAL_LABELS = (
    ("fare", re.compile(r"^ТАРИФ$", re.IGNORECASE)),
    ("taxes", re.compile(r"^(?:СБОР/TAX|TAX(?:ES)?|ТАКСЫ)$", re.IGNORECASE)),
    ("ticket_total", re.compile(r"^(?:ИТОГО ПО БИЛЕТУ|TICKET TOTAL)$", re.IGNORECASE)),
    ("sa_fee", re.compile(r"^СБОР СА$", re.IGNORECASE)),
    ("asb_fee", re.compile(r"^СБОР АСБ$", re.IGNORECASE)),
    ("total", re.compile(r"^(?:ВСЕГО К ОПЛАТЕ|GRAND TOTAL|AMOUNT DUE)$", re.IGNORECASE)),
)


def _clean_line(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip(" :;\t")


def _currency(value: str) -> str:
    normalized = (value or "").upper()
    return _CURRENCY_ALIASES.get(normalized, normalized)


def _money_line(value: str) -> tuple[str, Decimal] | None:
    match = re.search(
        rf"(?:(?P<currency_before>{_CURRENCY})\s*)?"
        r"(?P<amount>-?\d[\d\s]*(?:[,.]\d{1,2})?)\s*"
        rf"(?P<currency_after>{_CURRENCY})?",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    try:
        amount = Decimal(match.group("amount").replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return _currency(match.group("currency_before") or match.group("currency_after") or ""), amount


def _label_key(value: str) -> str:
    for key, pattern in _FINANCIAL_LABELS:
        if pattern.fullmatch(value):
            return key
    return ""


def _tax_components(value: str, *, expected: Decimal | None, default_currency: str) -> list[dict]:
    rows = []
    for code, amount, currency in re.findall(
        rf"\b([A-ZА-Я]{{2,4}})\s*(\d[\d\s]*(?:[,.]\d{{1,2}})?)\s*({_CURRENCY})\b",
        value,
        flags=re.IGNORECASE,
    ):
        code = code.upper()
        if code in {"RUB", "USD", "EUR", "KGS", "KZT", "TAX", "FARE", "TOTAL", "NUC", "ROE", "END"}:
            continue
        try:
            parsed = Decimal(amount.replace(" ", "").replace(",", "."))
        except (InvalidOperation, ValueError):
            continue
        rows.append(
            {
                "code": code,
                "label": code,
                "amount": str(parsed),
                "currency": _currency(currency) or default_currency,
            }
        )

    if expected is None:
        return rows
    component_total = sum((Decimal(row["amount"]) for row in rows), Decimal("0"))
    residual = expected - component_total
    if rows and residual > Decimal("0"):
        rows.append(
            {
                "code": "OTHER",
                "label": "Прочие таксы",
                "amount": str(residual),
                "currency": default_currency,
            }
        )
    if not rows or component_total > expected:
        return [
            {
                "code": "TAX",
                "label": "Таксы",
                "amount": str(expected),
                "currency": default_currency,
            }
        ]
    return rows


def column_ordered_avia_financials(text: str) -> dict:
    """Parse PDFs whose text layer returns all captions before all money values."""
    lines = [_clean_line(line) for line in (text or "").splitlines()]
    lines = [line for line in lines if line]

    for start, line in enumerate(lines):
        if _label_key(line) != "fare":
            continue

        labels = []
        cursor = start
        while cursor < min(len(lines), start + 12):
            key = _label_key(lines[cursor])
            if key:
                labels.append((key, cursor))
            elif labels:
                break
            cursor += 1

        keys = [key for key, _ in labels]
        if "taxes" not in keys or len(labels) < 3:
            continue

        values = []
        value_cursor = labels[-1][1] + 1
        value_limit = min(len(lines), labels[-1][1] + 20)
        while value_cursor < value_limit and len(values) < len(labels):
            parsed = _money_line(lines[value_cursor])
            has_explicit_money_shape = bool(
                re.search(_CURRENCY, lines[value_cursor], flags=re.IGNORECASE)
                or lines[value_cursor].lstrip().startswith(":")
            )
            if parsed and has_explicit_money_shape:
                values.append((*parsed, value_cursor))
            value_cursor += 1

        if len(values) < len(labels):
            continue

        mapped = {
            key: {"currency": currency, "amount": amount}
            for (key, _), (currency, amount, _) in zip(labels, values)
        }
        fare = mapped.get("fare", {}).get("amount")
        taxes = mapped.get("taxes", {}).get("amount")
        sa_fee = mapped.get("sa_fee", {}).get("amount", Decimal("0"))
        asb_fee = mapped.get("asb_fee", {}).get("amount", Decimal("0"))
        total = mapped.get("total", {}).get("amount") or mapped.get("ticket_total", {}).get("amount")
        default_currency = next(
            (entry["currency"] for entry in mapped.values() if entry.get("currency")),
            "RUB",
        )

        section_end = min(len(lines), values[-1][2] + 10)
        section = "\n".join(lines[start:section_end])
        tax_breakdown = _tax_components(section, expected=taxes, default_currency=default_currency)
        fee_breakdown = []
        if "sa_fee" in mapped:
            fee_breakdown.append(
                {"code": "SA", "label": "Сбор СА", "amount": str(sa_fee), "currency": default_currency}
            )
        if "asb_fee" in mapped:
            fee_breakdown.append(
                {"code": "ASB", "label": "Сбор АСБ", "amount": str(asb_fee), "currency": default_currency}
            )

        return {
            "fare": fare,
            "taxes": taxes,
            "fees": sa_fee + asb_fee,
            "total": total,
            "currency": default_currency,
            "tax_breakdown": tax_breakdown,
            "fee_breakdown": fee_breakdown,
        }
    return {}


def _json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def install_receipt_tax_columns_patch():
    from documents import services

    if getattr(services.extract_receipt_fields, "_tax_columns_patch", False):
        return
    original = services.extract_receipt_fields

    def wrapped(content: bytes, *, mime: str = "", name: str = ""):
        result = original(content, mime=mime, name=name)
        if not (mime == "application/pdf" or content.startswith(b"%PDF")):
            return result

        text = services._extract_pdf_text(content)
        financials = column_ordered_avia_financials(text)
        if not financials:
            return result

        fields = result.setdefault("fields", {})
        if fields.get("service_kind") not in {None, "", "avia"}:
            return result
        fields.update(financials)
        result.setdefault("raw", {}).update(_json_safe(financials))
        result["status"] = "parsed"
        result["confidence"] = max(
            Decimal(str(result.get("confidence") or 0)),
            Decimal("0.970"),
        )
        return result

    wrapped._tax_columns_patch = True
    if getattr(original, "_safe_layout_patch", False):
        wrapped._safe_layout_patch = True
    services.extract_receipt_fields = wrapped
