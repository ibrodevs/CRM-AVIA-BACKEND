import hashlib
import re
import zlib
from decimal import Decimal, InvalidOperation

from django.core.files.base import ContentFile

from common.errors import ApiError
from documents.models import Document, DocumentVersion

ALLOWED_FILE_SIGNATURES: dict[str, list[bytes]] = {
    "application/pdf": [b"%PDF"],
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/png": [b"\x89PNG"],
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": [b"PK\x03\x04"],
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [b"PK\x03\x04"],
    "text/csv": [],
    "text/plain": [],
}
MAX_FILE_SIZE = 25 * 1024 * 1024
SERVICE_KIND_LABELS = {
    "avia": "Авиа",
    "rail": "ЖД",
    "hotel": "Гостиница",
    "transfer": "Трансфер",
    "other": "Прочее",
}
RU_MONTHS = {
    "ЯНВ": 1,
    "ФЕВ": 2,
    "МАР": 3,
    "АПР": 4,
    "МАЙ": 5,
    "ИЮН": 6,
    "ИЮЛ": 7,
    "АВГ": 8,
    "СЕН": 9,
    "ОКТ": 10,
    "НОЯ": 11,
    "ДЕК": 12,
}


def _detect_service_kind(text: str, *, name: str = "") -> str:
    haystack = re.sub(r"[_\-/]+", " ", f"{name}\n{text}".lower())
    service_line = _first_match(text, [r"(?:service|услуга|тип услуги)\s*[:\-]\s*([^\n\r]+)"]).lower()
    if service_line:
        if re.search(r"\b(?:hotel|room)\b|гостиниц|отел|проживан", service_line):
            return "hotel"
        if re.search(r"\b(?:transfer|pickup|driver|car)\b|трансфер|водител|встреч", service_line):
            return "transfer"
        if re.search(r"\b(?:rail|train)\b|ржд|\bжд\b|ж/д|поезд", service_line):
            return "rail"
        if re.search(r"\b(?:air|avia|flight)\b|авиа|рейс", service_line):
            return "avia"
        if re.search(r"\bother\b|проч", service_line):
            return "other"
    rules = {
        "hotel": [
            r"\bhotel\b", r"\broom\b", r"\bcheck[\s-]?in\b", r"\bcheck[\s-]?out\b",
            r"\bservice\s*:\s*hotel\b", r"гостиниц", r"отел", r"проживан", r"бронь\s+отел", r"номер\s+в\s+отел",
        ],
        "transfer": [
            r"\btransfer\b", r"\bpickup\b", r"\bpick[\s-]?up\b", r"\bdriver\b", r"\bcar\b",
            r"\bservice\s*:\s*transfer\b", r"трансфер", r"водител", r"встреч", r"аэропорт\s*[-–—]\s*отел", r"машин",
        ],
        "rail": [
            r"\brail\b", r"\btrain\b", r"\brzd\b", r"ржд", r"\bжд\b", r"ж/д", r"поезд",
            r"\bservice\s*:\s*rail\b", r"вагон", r"место", r"станци", r"электронный\s+проездной",
        ],
        "avia": [
            r"\bavia\b", r"\bflight\b", r"\bairline\b", r"\bair\s*ticket\b", r"\bservice\s*:\s*air\b",
            r"\bpnr\b", r"\bticket\b", r"авиа", r"рейс", r"перевозчик", r"маршрут[-\s]?квитанц",
            r"электронн(?:ый|ого)\s+билет",
        ],
    }
    scores = {}
    for kind, patterns in rules.items():
        score = 0
        for pattern in patterns:
            if re.search(pattern, haystack, flags=re.IGNORECASE):
                score += 3 if kind != "avia" else 1
        scores[kind] = score
    if re.search(r"\b(?:flight|airline|air\s*ticket|service\s*:\s*air)\b|авиа|рейс", haystack):
        scores["avia"] += 4
    kind, score = max(scores.items(), key=lambda item: item[1])
    return kind if score else "other"


def _manual_receipt_result(*, name: str, mime: str, warning: str) -> dict:
    service_kind = _detect_service_kind("", name=name)
    return {
        "status": "manual_review",
        "confidence": Decimal("0.000"),
        "fields": {"service_kind": service_kind, "service_type": SERVICE_KIND_LABELS[service_kind]},
        "raw": {
            "file_name": name,
            "mime": mime,
            "text_available": False,
            "service_kind": service_kind,
            "service_type": SERVICE_KIND_LABELS[service_kind],
        },
        "warnings": [warning],
    }


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8", "cp1251", "latin-1"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        return ""
    return re.sub(r"[ \t]+", " ", text.replace("\r", "\n"))


def _decode_pdf_literal(value: str) -> str:
    value = re.sub(r"\\([nrtbf()\\])", lambda m: {
        "n": "\n", "r": "\n", "t": "\t", "b": "\b", "f": "\f",
        "(": "(", ")": ")", "\\": "\\",
    }[m.group(1)], value)
    return re.sub(r"\\([0-7]{1,3})", lambda m: chr(int(m.group(1), 8)), value)


def _decode_pdf_hex(value: str) -> str:
    hex_value = re.sub(r"\s+", "", value)
    if not hex_value:
        return ""
    if len(hex_value) % 2:
        hex_value += "0"
    try:
        raw = bytes.fromhex(hex_value)
    except ValueError:
        return ""
    candidates = []
    if raw.startswith(b"\xfe\xff"):
        candidates.append(raw[2:].decode("utf-16-be", errors="ignore"))
    if len(raw) % 2 == 0:
        candidates.append(raw.decode("utf-16-be", errors="ignore"))
    candidates.extend(
        [
            raw.decode("utf-8", errors="ignore"),
            raw.decode("latin-1", errors="ignore"),
        ]
    )

    def score(text: str) -> int:
        return sum(
            1
            for ch in text
            if ch.isalnum() or ch.isspace() or ch in "№:;.,/\\-–—()\"'«»$€₽"
        )

    return max(candidates, key=score).replace("\x00", "")


def _extract_pdf_text_fragments(text: str) -> list[str]:
    chunks: list[str] = []
    for literal in re.findall(r"\((?:\\.|[^\\()])*\)\s*Tj", text):
        chunks.append(_decode_pdf_literal(literal[1 : literal.rfind(")")]))
    for hex_text in re.findall(r"<([0-9A-Fa-f\s]+)>\s*Tj", text):
        decoded = _decode_pdf_hex(hex_text)
        if decoded:
            chunks.append(decoded)
    for array in re.findall(r"\[(.*?)\]\s*TJ", text, flags=re.DOTALL):
        parts = []
        for match in re.finditer(r"\((?:\\.|[^\\()])*\)|<([0-9A-Fa-f\s]+)>", array):
            token = match.group(0)
            if token.startswith("("):
                parts.append(_decode_pdf_literal(token[1:-1]))
            else:
                parts.append(_decode_pdf_hex(token[1:-1]))
        joined = "".join(parts).strip()
        if joined:
            chunks.append(joined)
    return chunks


def _extract_pdf_text(content: bytes) -> str:
    chunks: list[str] = []
    try:
        from pypdf import PdfReader  # type: ignore

        from io import BytesIO

        reader = PdfReader(BytesIO(content))
        chunks.extend((page.extract_text() or "") for page in reader.pages)
    except Exception:
        pass

    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", content, flags=re.DOTALL):
        raw = match.group(1).strip(b"\r\n")
        candidates = [raw]
        try:
            candidates.append(zlib.decompress(raw))
        except zlib.error:
            pass
        for candidate in candidates:
            text = candidate.decode("latin-1", errors="ignore")
            extracted = _extract_pdf_text_fragments(text)
            if extracted:
                chunks.append("\n".join(extracted))
    return "\n".join(chunk for chunk in chunks if chunk)


def _first_match(text: str, patterns: list[str]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).strip(" :;,\n\t")
    return ""


def _money(text: str, patterns: list[str]) -> Decimal | None:
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
            value = match.group(1).strip(" :;,\n\t")
            amount = re.search(r"-?\d[\d\s]*(?:[,.]\d{1,2})?", value)
            if not amount:
                continue
            normalized = amount.group(0).replace(" ", "").replace(",", ".")
            try:
                return Decimal(normalized)
            except (InvalidOperation, ValueError):
                continue
    return None


def _amount_and_currency(value: str) -> tuple[str, Decimal] | None:
    match = re.search(
        r"(?:(USD|EUR|RUB|KGS|KZT|сом|руб\.?|₽|\$|€)\s*)?(-?\d[\d\s]*(?:[,.]\d{1,2})?)\s*(USD|EUR|RUB|KGS|KZT|сом|руб\.?|₽|\$|€)?",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    cur = (match.group(1) or match.group(3) or "").upper()
    normalized = match.group(2).replace(" ", "").replace(",", ".")
    try:
        amount = Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None
    return {"СОМ": "KGS", "РУБ": "RUB", "РУБ.": "RUB", "₽": "RUB", "$": "USD", "€": "EUR"}.get(cur, cur), amount


def _money_breakdown(text: str, labels: list[tuple[str, str]]) -> list[dict]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    rows: list[dict] = []
    for index, line in enumerate(lines):
        for code, pattern in labels:
            if not re.search(pattern, line, flags=re.IGNORECASE):
                continue
            joined = " ".join(lines[index : index + 2])
            parsed = _amount_and_currency(joined)
            if parsed:
                currency, amount = parsed
                rows.append({"code": code, "label": line.strip(" :"), "amount": str(amount), "currency": currency})
                break
    return rows


def _tax_breakdown(text: str) -> list[dict]:
    rows = _money_breakdown(text, [("TAX", r"\b(?:tax|такс|сбор/tax|аэропортовые сборы)\b")])
    components = []
    for code, amount, currency in re.findall(
        r"\b([A-ZА-Я]{2,4})\s*(\d[\d\s]*(?:[,.]\d{1,2})?)\s*(USD|EUR|RUB|KGS|KZT|сом|руб\.?|₽|\$|€)\b",
        text,
        flags=re.IGNORECASE,
    ):
        try:
            value = Decimal(amount.replace(" ", "").replace(",", "."))
        except (InvalidOperation, ValueError):
            continue
        components.append({"code": code.upper(), "label": code.upper(), "amount": str(value), "currency": _first_currency(currency) or currency.upper()})
    return components or rows


def _fee_breakdown(text: str) -> list[dict]:
    return _money_breakdown(
        text,
        [
            ("SA", r"\bсбор\s+са\b|\bservice\s+fee\b|сервисн(?:ый|ого)\s+сбор"),
            ("ASB", r"\bсбор\s+асб\b|\bагентск(?:ий|ого)\s+сбор"),
        ],
    )


def _first_currency(text: str) -> str:
    currency = _first_match(
        text,
        [
            r"(?:валюта|currency)\s*[:\-]?\s*(USD|EUR|RUB|KGS|KZT|сом|руб\.?|₽|\$|€)",
            r"\b(USD|EUR|RUB|KGS|KZT)\b",
            r"\b(USD|EUR|RUB|KGS|KZT)(?=\d)",
            r"([₽$€])",
        ],
    ).upper()
    return {"СОМ": "KGS", "РУБ": "RUB", "РУБ.": "RUB", "₽": "RUB", "$": "USD", "€": "EUR"}.get(currency, currency)


def _normalize_date(value: str) -> str:
    value = (value or "").strip()
    match = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", value)
    if match:
        day, month, year = match.groups()
        if len(year) == 2:
            year = "20" + year
        return f"{int(day):02d}.{int(month):02d}.{year}"
    match = re.search(r"\b(\d{1,2})\s*([А-ЯЁA-Z]{3})\s*(\d{2,4})?\b", value, flags=re.IGNORECASE)
    if not match:
        return value
    day, month_name, year = match.groups()
    month = RU_MONTHS.get(month_name.upper().replace("Ё", "Е")[:3])
    if not month:
        return value
    year = year or ""
    if len(year) == 2:
        year = "20" + year
    return f"{int(day):02d}.{month:02d}.{year}" if year else f"{int(day):02d}.{month:02d}"


def _format_time(value: str) -> str:
    value = (value or "").strip()
    if re.fullmatch(r"\d{3,4}", value):
        value = value.zfill(4)
        return f"{value[:2]}:{value[2:]}"
    return value


def _date_with_year(value: str, year: str = "") -> str:
    normalized = _normalize_date(value)
    if year and re.fullmatch(r"\d{2}\.\d{2}", normalized):
        return f"{normalized}.{year}"
    return normalized


def _extract_issue_year(text: str) -> str:
    value = _first_match(text, [r"(?:дата|issued date|issue date|дата выписки|дата оформления)\s*[:\-]?\s*([^\n\r]+)"])
    normalized = _normalize_date(value)
    match = re.search(r"\b(20\d{2})\b", normalized)
    return match.group(1) if match else ""


def _guess_trip_type(segments: list[dict], service_kind: str) -> str:
    if service_kind == "hotel":
        return "stay"
    if len(segments) >= 2 and segments[0].get("fromCode") == segments[-1].get("toCode"):
        return "roundtrip"
    if len(segments) > 1:
        return "complex"
    return "oneway"


def _extract_transport_table_segments(text: str, *, service_kind: str) -> list[dict]:
    if service_kind not in {"avia", "rail"}:
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    stops = []
    for index in range(len(lines) - 1):
        code_line = lines[index + 1]
        code_match = re.match(r"^([A-ZА-Я]{3})(?:\s+[A-Z0-9])?(?:\s*/.*)?$", code_line)
        if not code_match:
            continue
        if (
            not re.search(r"[А-ЯA-Z]", lines[index])
            or not re.search(r",|аэропорт|airport|вокзал|станци", lines[index], flags=re.IGNORECASE)
            or re.search(r"маршрут|рейс|класс|статус|тариф|баг", lines[index], flags=re.IGNORECASE)
        ):
            continue
        stops.append({"name": lines[index], "code": code_match.group(1), "city_index": index, "code_index": index + 1})
    if len(stops) < 2:
        return []
    year = _extract_issue_year(text)
    segments = []
    for index in range(len(stops) - 1):
        block = lines[stops[index]["code_index"] + 1 : stops[index + 1]["city_index"]]
        flight = next((line for line in block if re.fullmatch(r"[A-ZА-Я]{2}\s?-?\d{2,5}", line)), "")
        date = next((line for line in block if re.fullmatch(r"\d{1,2}[А-ЯЁA-Z]{3}(?:\d{2,4})?|\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?", line, flags=re.IGNORECASE)), "")
        times = [_format_time(line) for line in block if re.fullmatch(r"\d{3,4}", line)]
        segments.append(
            {
                "from": stops[index]["name"],
                "fromCode": stops[index]["code"],
                "to": stops[index + 1]["name"],
                "toCode": stops[index + 1]["code"],
                "date": _date_with_year(date, year),
                "dep": times[0] if times else "",
                "arr": times[1] if len(times) > 1 else "",
                "flightNo": flight.replace(" ", ""),
                "dir": "back" if index and stops[0]["code"] == stops[index + 1]["code"] else ("out" if index == 0 else "seg"),
            }
        )
    return segments


def _extract_segments(text: str, *, service_kind: str) -> list[dict]:
    table_segments = _extract_transport_table_segments(text, service_kind=service_kind)
    if table_segments:
        return table_segments
    route = _first_match(text, [
        r"(?:route|маршрут|направление)\s*[:\-]\s*([^\n\r]+)",
        r"(?:from|откуда)\s*[:\-]\s*([A-ZА-ЯЁ0-9 .'-]{2,40})\s+(?:to|куда|[-–—>→]+)\s*([A-ZА-ЯЁ0-9 .'-]{2,40})",
    ])
    if route and "  " in route:
        route = re.sub(r"\s{2,}", " ", route)
    date = _normalize_date(_first_match(text, [
        r"(?:departure date|travel date|route date|дата выезда|дата отправления|дата поездки)\s*[:\-]\s*([^\n\r]+)",
        r"^date\s*[:\-]\s*([^\n\r]+)",
    ]))
    dep = _first_match(text, [r"(?:departure|depart|dep|вылет|отправление|заезд|check[\s-]?in)\s*[:\-]\s*(\d{1,2}:\d{2})"])
    arr = _first_match(text, [r"(?:arrival|arr|прил[её]т|прибытие|выезд|check[\s-]?out)\s*[:\-]\s*(\d{1,2}:\d{2})"])
    flight = _first_match(text, [
        r"(?:flight|рейс)\s*(?:no|№|number)?\s*[:\-]?\s*([A-ZА-Я0-9]{2,4}\s?-?\d{1,5})",
        r"(?:train|поезд)\s*(?:no|№|number)?\s*[:\-]?\s*([A-ZА-Я0-9 -]{1,12})",
        r"(?:room|номер)\s*[:\-]\s*([^\n\r]+)",
        r"(?:car|vehicle|машина|авто)\s*[:\-]\s*([^\n\r]+)",
    ])
    if not route and service_kind == "hotel":
        route = _first_match(text, [r"(?:hotel|отель|гостиница)\s*[:\-]\s*([^\n\r]+)"])
    if not route and service_kind == "transfer":
        route = _first_match(text, [r"(?:pickup|встреча|маршрут трансфера)\s*[:\-]\s*([^\n\r]+)"])
    if not route:
        return []

    parts = [p.strip(" .") for p in re.split(r"\s*(?:→|->|[-–—])\s*", route) if p.strip(" .")]
    if len(parts) >= 2:
        legs = []
        for index in range(len(parts) - 1):
            legs.append({
                "from": parts[index],
                "fromCode": parts[index] if re.fullmatch(r"[A-ZА-ЯЁ]{2,5}", parts[index]) else "",
                "to": parts[index + 1],
                "toCode": parts[index + 1] if re.fullmatch(r"[A-ZА-ЯЁ]{2,5}", parts[index + 1]) else "",
                "date": date,
                "dep": dep if index == 0 else "",
                "arr": arr if index == len(parts) - 2 else "",
                "flightNo": flight,
                "dir": "back" if len(parts) == 3 and index == 1 and parts[0] == parts[-1] else ("out" if index == 0 else "seg"),
            })
        return legs
    return [{
        "from": "" if service_kind == "hotel" else route,
        "fromCode": "",
        "to": route if service_kind == "hotel" else "",
        "toCode": "",
        "date": date,
        "dep": dep,
        "arr": arr,
        "flightNo": flight,
        "dir": "out",
    }]


def extract_receipt_fields(content: bytes, *, mime: str = "", name: str = "") -> dict:
    """Best-effort extraction from a text layer; scanned images still require OCR/manual entry."""
    is_pdf = mime == "application/pdf" or content.startswith(b"%PDF")
    if mime in {"image/jpeg", "image/png"}:
        return _manual_receipt_result(
            name=name,
            mime=mime,
            warning="Файл является изображением. Для распознавания фото/сканов нужен OCR или ручное заполнение.",
        )
    text = _extract_pdf_text(content) if is_pdf else _decode_text(content)
    text = text.replace("\\n", "\n").replace("\\r", "\n")
    visible_text = re.sub(r"\s+", " ", text).strip()
    if len(visible_text) < 20:
        return _manual_receipt_result(
            name=name,
            mime=mime,
            warning="Не удалось извлечь текст из файла. Для сканов нужен OCR или ручное заполнение.",
        )

    service_kind = _detect_service_kind(text, name=name)
    currency = _first_currency(text)
    fare = _money(text, [
        r"(?:base fare|fare|тариф|стоимость тарифа)\s*[:\-]?\s*([^\n\r]+)",
        r"(?:стоимость перевозки|базовый тариф)\s*[:\-]?\s*([^\n\r]+)",
    ])
    taxes = _money(text, [
        r"(?:taxes|таксы|аэропортовые сборы|сбор/tax|tax)\s*[:\-]?\s*([^\n\r]+)",
        r"(?:таксы и сборы)\s*[:\-]?\s*([^\n\r]+)",
    ])
    total = _money(text, [
        r"(?:total|итого по билету|итого|всего к оплате|к оплате|amount|общая стоимость|всего)\s*[:\-]?\s*([^\n\r]+)",
        r"(?:сумма)\s*[:\-]?\s*([^\n\r]+)",
    ])
    payable_total = _money(text, [r"(?:всего к оплате|grand total|amount due)\s*[:\-]?\s*([^\n\r]+)"])
    if payable_total is not None:
        total = payable_total
    tax_breakdown = _tax_breakdown(text)
    fee_breakdown = _fee_breakdown(text)
    fees = sum((Decimal(row["amount"]) for row in fee_breakdown), Decimal("0")) if fee_breakdown else None
    passenger = _first_match(
        text,
        [
            r"(?:^|\n)\s*(?:passenger|пассажир|traveller)\s*[:\-]?\s*([^\n]+)",
            r"(?:^|\n)\s*(?:фамилия\s*/?\s*имя|фамилия|пассажир\(ы\))\s*[:\-]?\s*([^\n]+)",
            r"(?:^|\n)\s*(?:name|фио)\s*[:\-]?\s*([^\n]+)",
        ],
    )
    issuer = _first_match(
        text,
        [
            r"(?:^|\n)\s*(?:carrier|airline|перевозчик|поставщик)\s*[:\-]\s*([^\n]+)",
            r"(?:^|\n)\s*(?:issued by|выдано|выдан от)\s*[:\-]?\s*([^\n]+)",
        ],
    )
    reference = _first_match(text, [
        r"\bbooking\s*(?:ref(?:erence)?|reference)\s*(?:/\s*PNR)?\s*[:\-]?\s*([A-Z0-9А-Я-]{5,12})",
        r"\bPNR\s*[:\-]?\s*([A-Z0-9А-Я-]{5,12})",
        r"\b(?:бронь|код бронирования|номер брони)\s*[:\-]?\s*([A-Z0-9А-Я-]{5,12})",
    ])
    ticket = _first_match(
        text,
        [r"(?:ticket|билет|номер билета|электронный билет)\s*(?:no|№|number)?\s*[:\-]?\s*([0-9A-ZА-Я -]{6,24})"],
    )
    document_number = _first_match(text, [
        r"(?:^|\n)\s*(?:документ|паспорт|document)\s*(?:№|no|number)?\s*[:\-]\s*([0-9A-ZА-Я -]{5,24})",
    ])
    dob = _first_match(text, [
        r"(?:дата рождения|dob|date of birth)\s*[:\-]?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
    ])
    issue_date = _normalize_date(_first_match(text, [
        r"(?:issued date|issue date|дата выписки|дата оформления|дата|выдано)\s*[:\-]\s*([^\n\r]+)",
    ]))
    booking_class = _first_match(text, [r"(?:class|класс)\s*[:\-]\s*([A-ZА-Я0-9 -]{1,20})"])
    fare_basis = _first_match(text, [r"(?:fare basis|тарифный код|код тарифа)\s*[:\-]\s*([A-ZА-Я0-9 -]{2,32})"])
    baggage = _first_match(text, [r"(?:baggage|багаж)\s*[:\-]\s*([^\n\r]+)"])
    hand_baggage = _first_match(text, [r"(?:hand baggage|ручная кладь|cabin baggage)\s*[:\-]\s*([^\n\r]+)"])
    segments = _extract_segments(text, service_kind=service_kind)
    trip_type = _guess_trip_type(segments, service_kind)

    fields = {
        "issuer": issuer,
        "passenger_name": passenger,
        "fare": fare,
        "taxes": taxes,
        "fees": fees,
        "total": total,
        "currency": currency,
        "segments": segments,
        "trip_type": trip_type,
        "tax_breakdown": tax_breakdown,
        "fee_breakdown": fee_breakdown,
        "reference": reference,
        "ticket_number": ticket,
        "document_number": document_number,
        "date_of_birth": dob,
        "issue_date": issue_date,
        "booking_class": booking_class,
        "fare_basis": fare_basis,
        "baggage": baggage,
        "hand_baggage": hand_baggage,
        "service_kind": service_kind,
        "service_type": SERVICE_KIND_LABELS[service_kind],
    }
    filled = sum(1 for key in ("passenger_name", "fare", "taxes", "total", "currency") if fields.get(key))
    warnings = []
    if not passenger:
        warnings.append("Пассажир не найден в текстовом слое.")
    if fare is None and total is None:
        warnings.append("Суммы не найдены в текстовом слое.")
    status = "parsed" if filled >= 3 and not warnings else "manual_review"
    confidence = Decimal("0.850") if status == "parsed" else Decimal("0.350")
    return {
        "status": status,
        "confidence": confidence,
        "fields": fields,
        "raw": {
            "file_name": name,
            "mime": mime,
            "text_available": True,
            "reference": reference,
            "ticket_number": ticket,
            "document_number": document_number,
            "date_of_birth": dob,
            "issue_date": issue_date,
            "booking_class": booking_class,
            "fare_basis": fare_basis,
            "baggage": baggage,
            "hand_baggage": hand_baggage,
            "tax_breakdown": tax_breakdown,
            "fee_breakdown": fee_breakdown,
            "service_kind": service_kind,
            "service_type": SERVICE_KIND_LABELS[service_kind],
            "segments": segments,
            "trip_type": trip_type,
            "text_sample": visible_text[:1000],
        },
        "warnings": warnings or ["Проверьте распознанные поля перед подтверждением документа."],
    }


def validate_upload(file) -> None:
    if file.size > MAX_FILE_SIZE:
        raise ApiError(code="FILE_TOO_LARGE", message="Максимальный размер 25 МБ", status_code=400)
    if file.content_type not in ALLOWED_FILE_SIGNATURES:
        raise ApiError(
            code="UNSUPPORTED_FILE_TYPE",
            message=f"Тип {file.content_type} запрещён",
            status_code=400,
        )
    signatures = ALLOWED_FILE_SIGNATURES[file.content_type]
    if not signatures:
        return
    header = file.read(8)
    file.seek(0)
    if not any(header.startswith(signature) for signature in signatures):
        raise ApiError(
            code="FILE_SIGNATURE_MISMATCH",
            message="Содержимое не соответствует заявленному типу",
            status_code=400,
        )


def add_document_version(
    document: Document,
    *,
    content: bytes,
    mime: str,
    name: str,
    user,
    origin: str = "uploaded",
    template_version: str = "",
    correction_reason: str = "",
    correction_diff=None,
) -> DocumentVersion:
    version = DocumentVersion(
        document=document,
        version=document.current_version + 1,
        checksum_sha256=hashlib.sha256(content).hexdigest(),
        mime_type=mime,
        size_bytes=len(content),
        original_name=name,
        origin=origin,
        template_version=template_version,
        scan_status="clean",
        correction_reason=correction_reason,
        correction_diff=correction_diff,
        created_by=user,
    )
    version.file.save(name or f"v{version.version}", ContentFile(content), save=False)
    version.save()
    document.current_version = version.version
    if document.status == Document.Status.DRAFT and origin == "generated":
        document.status = Document.Status.GENERATED
    document.save(update_fields=["current_version", "status"])
    return version
