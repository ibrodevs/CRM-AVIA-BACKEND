import hashlib
import re
import unicodedata
import zlib
from decimal import Decimal, InvalidOperation
from io import BytesIO

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
    "JAN": 1,
    "ЯНВ": 1,
    "FEB": 2,
    "ФЕВ": 2,
    "MAR": 3,
    "МАР": 3,
    "APR": 4,
    "АПР": 4,
    "MAY": 5,
    "МАЙ": 5,
    "МАЯ": 5,
    "JUN": 6,
    "ИЮН": 6,
    "JUL": 7,
    "ИЮЛ": 7,
    "AUG": 8,
    "АВГ": 8,
    "SEP": 9,
    "СЕН": 9,
    "OCT": 10,
    "ОКТ": 10,
    "NOV": 11,
    "НОЯ": 11,
    "DEC": 12,
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


def _clean_extracted_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\x00", "").replace("\xa0", " ").replace("\r", "\n").replace("\f", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _pdf_text_score(text: str) -> int:
    if not text:
        return -10_000
    controls = sum(1 for char in text if ord(char) < 32 and char not in "\n\t")
    letters = sum(1 for char in text if char.isalpha())
    labels = len(
        re.findall(
            r"пассажир|билет|брон|маршрут|заезд|отправлен|стоимост|итого|total|passenger|ticket",
            text,
            flags=re.IGNORECASE,
        )
    )
    return letters + labels * 80 + min(text.count("\n"), 250) * 2 - controls * 100


def _extract_pdf_text(content: bytes) -> str:
    candidates: list[str] = []
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(BytesIO(content), strict=False)
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                continue
        candidates.append("\n".join(pages))
    except Exception:
        pass

    try:
        from pdfminer.high_level import extract_text  # type: ignore

        candidates.append(extract_text(BytesIO(content)))
    except Exception:
        pass

    raw_chunks: list[str] = []
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", content, flags=re.DOTALL):
        raw = match.group(1).strip(b"\r\n")
        stream_candidates = [raw]
        try:
            stream_candidates.append(zlib.decompress(raw))
        except zlib.error:
            pass
        for candidate in stream_candidates:
            text = candidate.decode("latin-1", errors="ignore")
            extracted = _extract_pdf_text_fragments(text)
            if extracted:
                raw_chunks.append("\n".join(extracted))
    candidates.append("\n".join(raw_chunks))

    cleaned = [_clean_extracted_text(candidate) for candidate in candidates]
    return max(cleaned, key=_pdf_text_score, default="")


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
            # Some PDF generators split a label, colon and amount across three
            # separate text lines (for example: "СБОР АСБ", ":", "RUB100").
            joined = " ".join(lines[index : index + 3])
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
            r"\b(сом|руб\.?)\b",
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
    match = re.search(r"\b(\d{1,2})\s*([А-ЯЁA-Z]{3,9})\s*(\d{2,4})?\b", value, flags=re.IGNORECASE)
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


def _date_parts(value: str, *, default_year: str = "") -> str:
    normalized = _normalize_date(value)
    if default_year and re.fullmatch(r"\d{2}\.\d{2}", normalized):
        normalized = f"{normalized}.{default_year}"
    return normalized


def _first_labeled_value(
    text: str,
    labels: list[str],
    *,
    lookahead_lines: int = 6,
    reject: str = "",
) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        for label in labels:
            match = re.search(label, line, flags=re.IGNORECASE)
            if not match:
                continue
            inline = line[match.end() :].strip(" :;-")
            values = ([inline] if inline else []) + lines[index + 1 : index + 1 + lookahead_lines]
            for value in values:
                value = value.strip(" :;-")
                if not value or re.search(
                    r"^(?:name(?: of (?:passenger|guest))?|passenger|traveller|guest|фамилия|имя|пассажир|гост[ьи]|номер|"
                    r"ticket|document|e-ticket|order|заказ|выдан|issued|carrier|перевозчик)$",
                    value,
                    flags=re.IGNORECASE,
                ):
                    continue
                if reject and re.search(reject, value, flags=re.IGNORECASE):
                    continue
                return value
    return ""


def _money_near_label(
    text: str,
    labels: list[str],
    *,
    window: int = 700,
    allow_bare: bool = True,
    prefer_max: bool = False,
) -> tuple[Decimal | None, str]:
    currency_pattern = r"USD|EUR|RUB|KGS|KZT|СОМ|РУБ\.?|₽|\$|€|CNY"
    amount_pattern = r"-?\d[\d ]*(?:[,.]\s*\d{1,2})?"
    for label in labels:
        match = re.search(label, text, flags=re.IGNORECASE)
        if not match:
            continue
        fragment = text[match.end() : match.end() + window]
        currency_matches = list(re.finditer(
            rf"(?:(?P<before>{currency_pattern})\s*)?(?P<amount>{amount_pattern})\s*(?P<after>{currency_pattern})",
            fragment,
            flags=re.IGNORECASE,
        ))
        if not currency_matches:
            currency_matches = list(re.finditer(
                rf"(?P<before>{currency_pattern})\s*(?P<amount>{amount_pattern})",
                fragment,
                flags=re.IGNORECASE,
            ))
        parsed_matches = []
        for currency_match in currency_matches:
            amount = currency_match.group("amount").replace(" ", "").replace(",.", ".").replace(", ", ",")
            try:
                value = Decimal(amount.replace(",", "."))
            except InvalidOperation:
                continue
            currency = _first_currency(currency_match.group("before") or currency_match.groupdict().get("after") or "")
            parsed_matches.append((value, currency))
        if parsed_matches:
            return max(parsed_matches, key=lambda item: item[0]) if prefer_max else parsed_matches[0]
        if allow_bare:
            for line in fragment.splitlines()[:8]:
                number = re.search(r"-?\d[\d ]*(?:[,.]\d{1,2})?", line)
                if not number or re.search(r"\d{1,2}[.:]\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", line):
                    continue
                try:
                    return Decimal(number.group(0).replace(" ", "").replace(",", ".")), ""
                except InvalidOperation:
                    continue
    return None, ""


def _normalize_person(value: str) -> str:
    value = re.sub(r"\s+(?:MR|MRS|MS|Г-Н|Г-ЖА)$", "", value.strip(), flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value.replace("/", " ")).strip(" ,")


def _extract_passenger(text: str, *, service_kind: str) -> str:
    if service_kind == "transfer":
        match = re.search(
            r"Примечания\s*\n([А-ЯЁA-Z][А-ЯЁA-Za-z'-]+(?:\s+[А-ЯЁA-Z][А-ЯЁA-Za-z'-]+){1,3})\s*\n\+?\d",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return _normalize_person(match.group(1))

    if service_kind == "hotel":
        guests = re.search(r"Гости\s*:\s*\n(?P<body>.*?)(?=\nВажная информация)", text, flags=re.IGNORECASE | re.DOTALL)
        if guests:
            names = [
                line.strip(" ,")
                for line in guests.group("body").splitlines()
                if re.search(r"[A-Za-zА-ЯЁа-яё]", line)
            ]
            if names:
                return ", ".join(names)

    passenger = _first_match(
        text,
        [
            r"(?:^|\n)\s*(?:passenger|traveller|фио)\b\s*[:\-]?\s*([^\n]+)",
            r"(?:^|\n)\s*фамилия\s*[:\-]\s*([^\n]+)",
            r"(?:^|\n)\s*(?:имя гостя|guest name)\s*[:\-]?\s*([^\n]+)",
        ],
    )
    if passenger:
        return _normalize_person(passenger)

    passenger = _first_labeled_value(
        text,
        [
            r"^(?:пассажир|passenger)$",
            r"^фамилия пассажира$",
            r"^имя гостя\s*:?$",
            r"^гости\s*:?$",
        ],
        lookahead_lines=8,
        reject=r"телефон|таблич|примечан|данные|авиакомпан|кроват|важная|^[+\d]",
    )
    if passenger:
        return _normalize_person(passenger)

    if service_kind == "rail":
        patterns = [
            r"\n([А-ЯЁ]{2,}(?:\s+[А-ЯЁ]{2,}){2,3})\s*\n(?:Посадка|ПН)",
            r"\n([А-ЯЁ]{2,}\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.)\s*\nПН",
        ]
        passenger = _first_match(text, patterns)
        if passenger:
            return _normalize_person(passenger)
    return ""


def _extract_reference(text: str, *, service_kind: str) -> str:
    reference = _first_match(
        text,
        [
            r"\bкод бронирования\s*[:\-]?\s*([A-Z0-9А-Я-]{5,16})",
            r"\bbooking\s*(?:ref(?:erence)?|reference)\s*(?:/\s*PNR)?\s*[:\-]?\s*([A-Z0-9А-Я-]{5,16})",
            r"\bPNR\s*[:\-]?\s*([A-Z0-9А-Я-]{5,16})",
            r"\bбронирование\s+([0-9]{6,16})",
            r"\bзаказ\b\s*(?:№|No)?\s*[:\-]?\s*([A-ZА-Я0-9-]*\d[A-ZА-Я0-9-]{4,19})",
            r"\bномер заказа\b(?:\s*Order number)?\s*([0-9]{6,20})",
        ],
    )
    if reference:
        return reference
    if service_kind == "transfer":
        reference = _first_labeled_value(text, [r"^Заказ$"], lookahead_lines=5, reject=r"^Заказ$")
        if re.search(r"\d", reference):
            return reference
    if service_kind == "avia":
        marker = re.search(r"КОД БРОНИРОВАНИЯ(?P<body>.{0,500})", text, flags=re.IGNORECASE | re.DOTALL)
        if marker:
            codes = re.findall(r"(?:^|\n)\s*:?\s*([A-Z0-9]{6})\s*(?:\n|$)", marker.group("body"))
            if codes:
                return codes[0]
    return ""


def _extract_ticket_number(text: str, *, service_kind: str) -> str:
    if service_kind == "avia":
        patterns = [
            r"(?:номер билета|ticket number)\s*[:\-]?\s*(?:Ticket number\s*)?(\d{3}\s+\d{10}|\d{10,13})",
            r"\b(\d{3}\s+\d{10})\b",
        ]
    elif service_kind == "rail":
        patterns = [
            r"(?:электронный билет\s*\(номер\)|e-ticket number)\s*(?:E-ticket number\s*)?(\d{13,16})",
            r"(?:номер квитанции|receipt number)\s*(\d{13,16})",
            r"(?:№|No)\s*([\d ]{13,20})",
        ]
    else:
        patterns = [r"(?:ваучер|voucher)\s*(?:№|number)?\s*[:\-]?\s*([A-ZА-Я0-9-]{5,20})"]
    ticket = _first_match(text, patterns)
    if ticket:
        return re.sub(r"\s+", " ", ticket).strip()

    marker = re.search(r"НОМЕР БИЛЕТА(?P<body>.{0,400})", text, flags=re.IGNORECASE | re.DOTALL)
    if marker:
        values = re.findall(r"(?:^|\n)\s*:?\s*(\d{10,13}|\d{3}\s+\d{10})\s*(?:\n|$)", marker.group("body"))
        if values:
            return values[0]
    return ""


def _city_code(value: str) -> tuple[str, str]:
    match = re.match(r"(.+?),\s*([A-Z]{3})$", value.strip())
    return (match.group(1).strip(), match.group(2)) if match else (value.strip(), "")


def _s7_rub_amount(text: str, start: str, end: str, *, stop_at_tax_code: bool = False) -> Decimal | None:
    block = re.search(rf"{start}(?P<body>.*?)(?={end})", text, re.IGNORECASE | re.DOTALL)
    if not block:
        return None
    value = block.group("body")
    rub = re.search(r"RUB", value, re.IGNORECASE)
    if not rub:
        return None
    value = value[rub.end() :]
    if stop_at_tax_code:
        value = re.split(r"[A-Z]{2,3}\s*\d", value, maxsplit=1)[0]
    digits = re.findall(r"\d+", value)
    return Decimal("".join(digits)) if digits else None


def _s7_line_segments(text: str) -> tuple[list[dict], str, str, str, list[str]]:
    route_start = re.search(r"Рейс под брендом авиакомпании\s*S7 Airlines", text, re.IGNORECASE)
    route_end = re.search(r"РАСЧЕТ ТАРИФА", text, re.IGNORECASE)
    if not route_start or not route_end:
        return [], "", "", "", []
    lines = [line.strip() for line in text[route_start.end() : route_end.start()].splitlines() if line.strip()]
    city_pattern = re.compile(r"^(?P<name>.+?),\s*(?P<code>[A-Z]{3})(?:\s+\([^)]*\))?$")
    starts = [
        index
        for index in range(len(lines) - 1)
        if city_pattern.match(lines[index]) and city_pattern.match(lines[index + 1])
    ]
    segments = []
    booking_class = ""
    baggage = ""
    fare_bases = []
    for offset, start_index in enumerate(starts):
        end_index = starts[offset + 1] if offset + 1 < len(starts) else len(lines)
        from_match = city_pattern.match(lines[start_index])
        to_match = city_pattern.match(lines[start_index + 1])
        body = "\n".join(lines[start_index + 2 : end_index])
        times = re.findall(r"\d{1,2}:\d{2}", body)
        dates = re.findall(
            r"(?:Пн|Вт|Ср|Чт|Пт|Сб|Вс),?\s*(\d{1,2}\s+[А-ЯЁа-яё]+\s+20\d{2})",
            body,
            flags=re.IGNORECASE,
        )
        flight = _first_match(body, [r"(S7-\d{2,5})"])
        if not from_match or not to_match or not flight or len(times) < 2 or not dates:
            continue
        segment = {
            "from": from_match.group("name").strip(" ,"),
            "fromCode": from_match.group("code"),
            "to": to_match.group("name").strip(" ,"),
            "toCode": to_match.group("code"),
            "date": _normalize_date(dates[0]),
            "dep": times[0],
            "arr": times[1],
            "flightNo": flight,
            "dir": "out",
        }
        if segments:
            segment["dir"] = "back" if segment["toCode"] == segments[0]["fromCode"] else "seg"
        segments.append(segment)
        body_lines = [line.strip() for line in body.splitlines() if line.strip()]
        booking_class = booking_class or next(
            (line for line in body_lines if line in {"ECONOMY", "BUSINESS", "ЭКОНОМ", "БИЗНЕС"}),
            "",
        )
        flight_index = body_lines.index(flight) if flight in body_lines else -1
        if flight_index >= 0:
            tail = body_lines[flight_index + 1 :]
            fare_basis = next(
                (
                    line
                    for line in tail
                    if re.fullmatch(r"[A-Z0-9-]{2,32}", line)
                    and line not in {"ECONOMY", "BUSINESS", "OK"}
                    and not re.fullmatch(r"\d+(?:PC|KG)", line)
                ),
                "",
            )
            if fare_basis:
                fare_bases.append(fare_basis)
        baggage = baggage or next((line for line in body_lines if re.fullmatch(r"\d+\s*PC", line)), "")
    hand_baggage = _first_match(text, [r"Ручная кладь\s*(\d+\s*кг)"])
    return segments, booking_class, baggage, hand_baggage, fare_bases


def _s7_compact_fields(text: str) -> dict:
    """Parse S7 receipts whose PDF text is compacted or splits amount digits."""
    flat = re.sub(r"\s+", " ", text or "").strip()
    if not re.search(r"ЭЛЕКТРОННЫЙ БИЛЕТ.*?S7 Airlines", flat, re.IGNORECASE):
        return {}

    passenger = ""
    dob = ""
    document_number = ""
    ticket_number = ""
    issue_date = ""
    passenger_block = re.search(
        r"Пассажир\s*(?:Дата рождения\s*)?Номер документа\s*Номер билета\s*Бонусная карта\s*Продажа"
        r"(?P<body>.+?)Рейс под брендом авиакомпании",
        flat,
        re.IGNORECASE,
    )
    if passenger_block:
        body = passenger_block.group("body").strip()
        ticket_match = re.search(r"421\s*\d{10}", body)
        if ticket_match:
            ticket_number = re.sub(r"\s+", " ", ticket_match.group(0))
            issue_match = re.search(r"\d{2}[./-]\d{2}[./-]\d{4}", body[ticket_match.end() :])
            issue_date = _normalize_date(issue_match.group(0)) if issue_match else ""
            prefix = body[: ticket_match.start()].strip()
            dob_match = re.search(r"\d{2}[./-]\d{2}[./-]\d{4}", prefix)
            document_match = re.search(r"(?:ПС\s*)?\d{10}\s*$", prefix)
            name_end = min(
                [
                    match.start()
                    for match in (dob_match, document_match)
                    if match is not None
                ]
                or [len(prefix)]
            )
            passenger = _normalize_person(prefix[:name_end])
            dob = _normalize_date(dob_match.group(0)) if dob_match else ""
            if document_match:
                document_number = re.sub(r"\s+", " ", document_match.group(0)).strip()

    segments, booking_class, baggage, hand_baggage, fare_bases = _s7_line_segments(text)
    route_start = re.search(r"Рейс под брендом авиакомпании\s*S7 Airlines", flat, re.IGNORECASE)
    route_end = re.search(r"РАСЧЕТ ТАРИФА", flat, re.IGNORECASE)
    route_text = flat[route_start.end() : route_end.start()] if route_start and route_end else ""
    header_pattern = re.compile(
        r"(?P<fromCode>[A-Z]{3})(?P<from>[^\d]{2,80}?)"
        r"(?P<toCode>[A-Z]{3})(?P<to>[^\d]{2,80}?)"
        r"ПеревозчикРейсТарифБагаж(?:Ручнаякладь)?Статус",
    )
    headers = list(header_pattern.finditer(route_text))
    for index, header in enumerate(headers if not segments else []):
        body_end = headers[index + 1].start() if index + 1 < len(headers) else len(route_text)
        body = route_text[header.end() : body_end]
        times = re.findall(r"\d{1,2}:\d{2}", body)
        dates = re.findall(
            r"(?:Пн|Вт|Ср|Чт|Пт|Сб|Вс),?\s*(\d{1,2}\s+[А-ЯЁа-яё]+\s+20\d{2})",
            body,
            flags=re.IGNORECASE,
        )
        flight = _first_match(body, [r"(S7-\d{2,5})"])
        if not flight or len(times) < 2 or not dates:
            continue
        segment = {
            "from": header.group("from").strip(" ,"),
            "fromCode": header.group("fromCode").upper(),
            "to": header.group("to").strip(" ,"),
            "toCode": header.group("toCode").upper(),
            "date": _normalize_date(dates[0]),
            "dep": times[0],
            "arr": times[1],
            "flightNo": flight,
            "dir": "out",
        }
        if segments:
            segment["dir"] = "back" if segment["toCode"] == segments[0]["fromCode"] else "seg"
        segments.append(segment)
        booking_class = booking_class or _first_match(body, [r"(ECONOMY|BUSINESS|ЭКОНОМ|БИЗНЕС)"])
        fare_basis = _first_match(
            body,
            [
                rf"{re.escape(flight)}(?:ECONOMY|BUSINESS|ЭКОНОМ|БИЗНЕС)"
                r"([A-Z0-9-]{2,32}?)(?=\d+\s*(?:PC|KG)|OK|$)"
            ],
        )
        if fare_basis:
            fare_bases.append(fare_basis)
        baggage = baggage or _first_match(body, [r"(\d+\s*PC)"])
        hand_baggage = hand_baggage or _first_match(body, [r"(\d+\s*KG)"])

    fare = _s7_rub_amount(text, r"РАСЧЕТ ТАРИФА:\s*ТАРИФ", r"ЭКВИВ\.|СБОР/TAX")
    taxes = _s7_rub_amount(text, r"СБОР/TAX", r"ВСЕГО К ОПЛАТЕ", stop_at_tax_code=True)
    main_total = _s7_rub_amount(
        text,
        r"ВСЕГО К ОПЛАТЕ",
        r"В Т\.Ч\.|КВИТАНЦИЯ РАЗНЫХ СБОРОВ",
    )
    tax_breakdown = []
    tax_block = re.search(r"СБОР/TAX\s*:.*?(?=ВСЕГО К ОПЛАТЕ)", flat, re.IGNORECASE)
    if tax_block:
        tax_breakdown = [
            {"code": code.upper(), "label": code.upper(), "amount": str(_decimal_amount), "currency": "RUB"}
            for code, amount in re.findall(
                r"(?<![A-Z])([A-Z]{2,3})\s*(\d[\d ]*(?:[,.]\d{1,2})?)\s*RUB\b",
                tax_block.group(0),
            )
            if (_decimal_amount := _amount_and_currency(f"RUB {amount}")[1]) is not None
        ]

    fee_breakdown = []
    fee_section = re.search(r"КВИТАНЦИЯ РАЗНЫХ СБОРОВ(?P<body>.+)", flat, re.IGNORECASE)
    if fee_section:
        for code, amount in re.findall(
            r"СБОР\s+(АСБ|СА)\s*(\d[\d ]*(?:[,.]\d{1,2})?)\s*РУБ",
            fee_section.group("body"),
            flags=re.IGNORECASE,
        ):
            parsed = _amount_and_currency(f"RUB {amount}")
            if parsed:
                fee_breakdown.append(
                    {"code": code.upper(), "label": f"Сбор {code.upper()}", "amount": str(parsed[1]), "currency": "RUB"}
                )
    fees = sum((Decimal(row["amount"]) for row in fee_breakdown), Decimal("0")) if fee_breakdown else None
    total = (main_total + fees) if main_total is not None and fees is not None else main_total

    reference = _first_match(flat, [r"код бронирования\s*:\s*([A-Z0-9]{5,12})"])
    supplier_order = _first_match(flat, [r"Заказ\s*(?:№|No)\s*(\d{5,20})"])
    return {
        "issuer": "S7 Airlines",
        "passenger_name": passenger,
        "reference": reference,
        "supplier_order_number": supplier_order,
        "ticket_number": ticket_number,
        "document_number": document_number,
        "date_of_birth": dob,
        "issue_date": issue_date,
        "booking_class": booking_class,
        "fare_basis": " / ".join(dict.fromkeys(fare_bases)),
        "baggage": baggage,
        "hand_baggage": hand_baggage,
        "fare": fare,
        "taxes": taxes,
        "fees": fees,
        "total": total,
        "currency": "RUB",
        "segments": segments,
        "tax_breakdown": tax_breakdown,
        "fee_breakdown": fee_breakdown,
    }


def _rossiya_itinerary_fields(text: str) -> dict:
    """Parse multi-page Aeroflot/Rossiya itinerary receipts."""
    receipt_pattern = re.compile(
        r"(?P<issue>\d{1,2}\s+[А-ЯЁа-яё]+\s+20\d{2})\s*\n"
        r"\s*Маршрутная квитанция электронного билета\s*\n"
        r"(?P<body>.*?)(?="
        r"\n\s*\d{1,2}\s+[А-ЯЁа-яё]+\s+20\d{2}\s*\n"
        r"\s*Маршрутная квитанция электронного билета|\Z)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    receipts = list(receipt_pattern.finditer(text or ""))
    if not receipts or not re.search(r"Перевозчик:\s*Россия", text, re.IGNORECASE):
        return {}

    passengers = []
    segments = []
    segment_keys = set()
    fares = []
    totals = []
    references = []
    ticket_numbers = []
    document_numbers = []
    fare_bases = []
    booking_classes = []
    baggage_values = []
    issuer = "Россия"

    for receipt in receipts:
        body = "\n".join(line.strip() for line in receipt.group("body").splitlines())
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        passenger_name = _normalize_person(lines[0]) if lines else ""
        document_number = _first_match(body, [r"Документ:\s*\n\s*([A-ZА-Я0-9 -]{5,24})"])
        ticket_number = _first_match(body, [r"(?:N[oо]|№)\s*эл\.билета:\s*\n\s*(\d{10,16})"])
        reference = _first_match(
            body,
            [r"Код бронирования\*?\s*\n\s*([A-Z0-9]{5,12})"],
        )
        dob_code = _first_match(body, [r"/DOB(\d{2}[A-Z]{3}\d{2})\b"])
        dob = ""
        if dob_code:
            day, month_name, year = dob_code[:2], dob_code[2:5], int(dob_code[5:])
            century = 1900 if year > 25 else 2000
            dob = _normalize_date(f"{day} {month_name} {century + year}")

        if passenger_name:
            passengers.append(
                {
                    "name": passenger_name,
                    "dob": dob,
                    "document": document_number,
                    "ticketNo": ticket_number,
                }
            )
        if reference:
            references.append(reference)
        if ticket_number:
            ticket_numbers.append(ticket_number)
        if document_number:
            document_numbers.append(document_number)

        route = re.search(
            r"Код бронирования\*?\s*\n\s*[A-Z0-9]{5,12}\s*\n"
            r"(?P<from>[^\n]+)\s*\n(?P<to>[^\n]+)\s*\n"
            r"Рейс:\s*(?P<flight>[A-ZА-Я0-9 -]{3,12})"
            r"(?P<schedule>.*?)(?=\nКласс:)",
            body,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if route:
            schedule = route.group("schedule")
            dates = re.findall(
                r"\b(\d{1,2}\s+[А-ЯЁа-яё]{3,9}\.?\s+20\d{2})\b",
                schedule,
                flags=re.IGNORECASE,
            )
            code_times = []
            for line in schedule.splitlines():
                value = line.strip()
                dep = re.search(r"\b(\d{1,2}:\d{2})\s+([A-Z]{3})\b", value)
                arr = re.search(r"\b([A-Z]{3})(?:\s+[A-Z0-9])?\s+(\d{1,2}:\d{2})\b", value)
                if dep:
                    code_times.append((dep.group(2), dep.group(1)))
                elif arr:
                    code_times.append((arr.group(1), arr.group(2)))
            if len(code_times) >= 2:
                (from_code, dep_time), (to_code, arr_time) = code_times[:2]
                segment = {
                    "from": route.group("from").strip(),
                    "fromCode": from_code,
                    "to": route.group("to").strip(),
                    "toCode": to_code,
                    "date": _normalize_date(dates[0].replace(".", "")) if dates else "",
                    "dep": dep_time,
                    "arr": arr_time,
                    "flightNo": re.sub(r"\s+", "", route.group("flight")),
                    "dir": "out",
                }
                key = tuple(segment.get(field, "") for field in ("fromCode", "toCode", "date", "dep", "flightNo"))
                if key not in segment_keys:
                    segment_keys.add(key)
                    segments.append(segment)

        booking_class = _first_match(body, [r"Класс:\s*([^\n/]+(?:\s*/\s*[A-ZА-Я0-9]+)?)"])
        fare_basis = _first_match(body, [r"Вид тарифа:\s*([A-ZА-Я0-9-]{2,32})"])
        baggage = _first_match(body, [r"Провоз багажа:\s*([^\n]+)"])
        if booking_class:
            booking_classes.append(booking_class)
        if fare_basis:
            fare_bases.append(fare_basis)
        if baggage:
            baggage_values.append(baggage)

        fare = _money(body, [r"(?m)^Тариф\s*\n\s*([^\n]+)"])
        total = _money(body, [r"Итого по тарифу/сборам\s*\n\s*([^\n]+)"])
        if fare is not None:
            fares.append(fare)
        if total is not None:
            totals.append(total)

    if not passengers or not segments:
        return {}
    passenger_names = [passenger["name"] for passenger in passengers]
    return {
        "issuer": issuer,
        "passenger_name": ", ".join(dict.fromkeys(passenger_names)),
        "passengers": passengers,
        "reference": ", ".join(dict.fromkeys(references)),
        "ticket_number": ", ".join(dict.fromkeys(ticket_numbers)),
        "document_number": ", ".join(dict.fromkeys(document_numbers)),
        "date_of_birth": passengers[0]["dob"],
        "issue_date": _normalize_date(receipts[0].group("issue")),
        "booking_class": " / ".join(dict.fromkeys(booking_classes)),
        "fare_basis": " / ".join(dict.fromkeys(fare_bases)),
        "baggage": " / ".join(dict.fromkeys(baggage_values)),
        "hand_baggage": "",
        "fare": sum(fares, Decimal("0")) if fares else None,
        "taxes": Decimal("0"),
        "fees": Decimal("0"),
        "total": sum(totals, Decimal("0")) if totals else None,
        "currency": "RUB",
        "segments": segments,
        "tax_breakdown": [],
        "fee_breakdown": [],
    }


def _avia_compact_table_segments(text: str) -> list[dict]:
    """Parse compact airline tables where every connection is a separate row."""
    if "МАРШРУТ/ПЕРЕВОЗЧИК" not in text or "ОТПРВ/НАЗН" not in text:
        return []

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    try:
        start = next(index for index, line in enumerate(lines) if "МАРШРУТ/ПЕРЕВОЗЧИК" in line)
    except StopIteration:
        return []
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if re.search(r"ПЕРЕДАТ\.?\s+НАДПИСИ|РАСЧЕТ\s+ТАРИФА", lines[index], flags=re.IGNORECASE)
        ),
        len(lines),
    )
    table = lines[start + 1 : end]
    origin_rows: list[tuple[int, str, str, str]] = []
    for index, line in enumerate(table):
        match = re.fullmatch(r"([A-Z]{3})(?:\s+[A-Z0-9])?\s*/\s*(.+)", line)
        if not match or index == 0:
            continue
        city = table[index - 1].strip()
        if re.fullmatch(
            r"(?:РЕЙС|КЛАСС|ДАТА|ВРЕМЯ\s+(?:ОТПР|ПРИБ)|СТАТУС|БАЗОВЫЙ\s+ТАРИФ|БАГ)",
            city,
            flags=re.IGNORECASE,
        ):
            continue
        origin_rows.append((index, match.group(1), city, match.group(2).strip()))
    if not origin_rows:
        return []

    last_origin_index = origin_rows[-1][0]
    destination: tuple[str, str] | None = None
    for index in range(last_origin_index + 1, len(table)):
        match = re.fullmatch(r"([A-Z]{3})(?:\s+[A-Z0-9])?", table[index])
        if match and index:
            destination = (match.group(1), table[index - 1].strip())
            break
    if destination is None:
        return []

    stops = [(code, city) for _, code, city, _ in origin_rows] + [destination]
    issue_year = _extract_issue_year(text)
    segments: list[dict] = []
    for leg_index, (row_index, from_code, from_city, carrier) in enumerate(origin_rows):
        if leg_index + 1 >= len(stops):
            break
        next_row_index = origin_rows[leg_index + 1][0] - 1 if leg_index + 1 < len(origin_rows) else len(table)
        block = table[row_index + 1 : next_row_index]
        flight_index = next(
            (
                index
                for index, value in enumerate(block)
                if re.fullmatch(r"[A-Z0-9]{2}-?\d{2,5}", value)
            ),
            None,
        )
        if flight_index is None:
            continue
        flight = block[flight_index].replace("-", "")
        schedule = block[flight_index + 1 :]
        date_index = next(
            (
                index
                for index, value in enumerate(schedule)
                if re.fullmatch(r"\d{1,2}[А-ЯЁA-Z]{3}(?:\d{2,4})?", value)
            ),
            None,
        )
        date = schedule[date_index] if date_index is not None else ""
        cls = schedule[date_index - 1] if date_index and re.fullmatch(r"[A-ZА-Я0-9]{1,3}", schedule[date_index - 1]) else ""
        time_indexes = [
            index
            for index, value in enumerate(schedule)
            if date_index is not None and index > date_index and re.fullmatch(r"\d{3,4}", value)
        ]
        times = [schedule[index] for index in time_indexes[:2]]
        details_start = time_indexes[1] + 1 if len(time_indexes) > 1 else len(schedule)
        details = schedule[details_start:]
        status = details[0] if details and re.fullmatch(r"[A-ZА-Я]{2,12}", details[0]) else ""
        fare_basis = details[1] if len(details) > 1 and re.fullmatch(r"[A-ZА-Я0-9-]{2,32}", details[1]) else ""
        cabin = details[2] if len(details) > 2 and re.fullmatch(r"[A-ZА-Я -]{3,24}", details[2]) else ""
        baggage = next(
            (value for value in details if re.fullmatch(r"\d+\s*(?:PC|KG|КМ)", value)),
            "",
        )
        to_code, to_city = stops[leg_index + 1]
        segments.append(
            {
                "from": re.sub(r"\s+", " ", from_city),
                "fromCode": from_code,
                "to": re.sub(r"\s+", " ", to_city),
                "toCode": to_code,
                "date": _date_parts(date, default_year=issue_year),
                "dep": _format_time(times[0]) if times else "",
                "arr": _format_time(times[1]) if len(times) > 1 else "",
                "flightNo": flight,
                "carrier": carrier,
                "cls": cls,
                "status": status,
                "fareBasis": fare_basis,
                "cabin": cabin,
                "baggage": baggage,
                "dir": (
                    "out"
                    if leg_index == 0
                    else ("back" if to_code == origin_rows[0][1] else "seg")
                ),
            }
        )
    return segments


def _avia_segments(text: str) -> list[dict]:
    compact_segments = _avia_compact_table_segments(text)
    if compact_segments:
        return compact_segments

    route_codes = _first_match(text, [r"ОТПРВ/НАЗН\s*:\s*([A-Z]{6})"])
    if route_codes:
        from_code, to_code = route_codes[:3], route_codes[3:]
        lines = text.splitlines()
        from_name = from_code
        to_name = to_code
        for index, line in enumerate(lines):
            if re.match(rf"^{from_code}\s*/", line):
                names = [value for value in lines[max(0, index - 2) : index] if not re.search(r"МАРШРУТ", value)]
                from_name = " ".join(names)
            if line == to_code and index:
                to_name = lines[index - 1]
        flight_match = re.search(r"\b([A-Z0-9]{2})-\s*\n?\s*(\d{2,5})\b", text)
        schedule = re.search(
            r"ОТПР\s*\n?\s*(\d{1,2}[А-ЯЁ]{3})\s+(\d{3,4}).*?ПРИБ\s*\n?\s*(\d{3,4})",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        date = schedule.group(1) if schedule else ""
        year = _extract_issue_year(text)
        if from_code == to_code:
            leg_rows = re.findall(
                r"\b([A-Z0-9]{2})-\s*\n?\s*(\d{2,5})\s*\n\s*([A-Z])\s*\n\s*"
                r"(\d{1,2}[А-ЯЁ]{3})\s*\n\s*(\d{3,4})\s*\n\s*(\d{3,4})",
                text,
            )
            intermediate_codes = [
                code
                for code in re.findall(r"(?m)^([A-Z]{3})\s*/", text)
                if code != from_code
            ]
            if len(leg_rows) >= 2 and intermediate_codes:
                intermediate_code = intermediate_codes[0]
                return [
                    {
                        "from": from_name if index == 0 else intermediate_code,
                        "fromCode": from_code if index == 0 else intermediate_code,
                        "to": intermediate_code if index == 0 else from_name,
                        "toCode": intermediate_code if index == 0 else from_code,
                        "date": _date_parts(row[3], default_year=year),
                        "dep": _format_time(row[4]),
                        "arr": _format_time(row[5]),
                        "flightNo": f"{row[0]}{row[1]}",
                        "dir": "out" if index == 0 else "back",
                    }
                    for index, row in enumerate(leg_rows[:2])
                ]
        return [{
            "from": re.sub(r"\s+", " ", from_name),
            "fromCode": from_code,
            "to": re.sub(r"\s+", " ", to_name),
            "toCode": to_code,
            "date": _date_parts(date, default_year=year),
            "dep": _format_time(schedule.group(2)) if schedule else "",
            "arr": _format_time(schedule.group(3)) if schedule else "",
            "flightNo": "".join(flight_match.groups()) if flight_match else "",
            "dir": "out",
        }]

    city_blocks = re.findall(
        r"(?m)^([^\n]{2,80},\s*[A-Z]{3})\s*\n(\d{1,2}:\d{2})\s*\n(?:[^\n]*,\s*)?"
        r"(\d{1,2}\s+[А-ЯЁA-Z]{3,8}\s+20\d{2})$",
        text,
        flags=re.IGNORECASE,
    )
    if len(city_blocks) >= 2:
        by_date: dict[str, list[tuple[str, str]]] = {}
        for city, time, date in city_blocks:
            by_date.setdefault(_normalize_date(date), []).append((city, time))
        flight_by_route = {
            (from_code, to_code): flight
            for flight, from_code, to_code in re.findall(
                r"\b([A-Z0-9]{2}-\d{2,5})\s+([A-Z]{3})\s*[-–—]\s*([A-Z]{3})\b", text
            )
        }
        segments = []
        for date, stops in by_date.items():
            if len(stops) < 2:
                continue
            (from_value, dep), (to_value, arr) = stops[:2]
            from_name, from_code = _city_code(from_value)
            to_name, to_code = _city_code(to_value)
            segments.append({
                "from": from_name,
                "fromCode": from_code,
                "to": to_name,
                "toCode": to_code,
                "date": date,
                "dep": dep,
                "arr": arr,
                "flightNo": flight_by_route.get((from_code, to_code), ""),
                "dir": "out" if not segments else ("back" if to_code == segments[0]["fromCode"] else "seg"),
            })
        if segments:
            return segments

    if "Рейс/Flight" in text and "Номер билета" in text:
        prefix = text[: text.find("Номер билета")]
        names = [
            line for line in prefix.splitlines()
            if re.fullmatch(r"[А-ЯЁ][А-ЯЁа-яё .'-]{2,50}", line)
            and not re.search(r"Электрон|Фамилия|Документ|Маршрут|Билет", line, flags=re.IGNORECASE)
        ]
        flight = re.search(r"\b([A-ZА-Я]{2})\s*[- ]?\s*(\d{2,5})\b", text)
        dates = re.findall(r"\b(\d{1,2}[./-]\d{1,2}[./-]\d{4})\b", text)
        times = re.findall(r"\b(\d{1,2}:\d{2})\b", text)
        if len(names) >= 2 and flight and dates and len(times) >= 2:
            return [{
                "from": names[-2],
                "fromCode": "",
                "to": names[-1],
                "toCode": "",
                "date": _normalize_date(dates[0]),
                "dep": times[0],
                "arr": times[1],
                "flightNo": "".join(flight.groups()),
                "dir": "out",
            }]

    route = re.search(r"(?m)^([А-ЯЁA-Z][^\n]{2,40})\s+([А-ЯЁA-Z][^\n]{2,40})\s*$", text)
    flight = re.search(r"\b([A-ZА-Я]{2})\s*[- ]?\s*(\d{2,5})\b", text)
    dates = re.findall(r"\b(\d{1,2}[./-]\d{1,2}[./-]\d{4})\b", text)
    times = re.findall(r"\b(\d{1,2}:\d{2})\b", text)
    if route and flight and dates and len(times) >= 2:
        return [{
            "from": route.group(1).strip(),
            "fromCode": "",
            "to": route.group(2).strip(),
            "toCode": "",
            "date": _normalize_date(dates[0]),
            "dep": times[-2],
            "arr": times[-1],
            "flightNo": "".join(flight.groups()),
            "dir": "out",
        }]
    return []


def _rail_segments(text: str) -> list[dict]:
    train = _first_match(text, [
        r"(?:Поезд|Поезда)\s*(?:\n|:)\s*(?:Train\s*\n)?([0-9][0-9A-ZА-Я]{2,11})",
        r"\b([0-9]{3,4}[A-ZА-Я]{0,2})\s+\d{1,2}[./]\d{1,2}[./]\d{4}\s+\d{1,2}:\d{2}",
    ])
    if len(train) % 2 == 0 and train[: len(train) // 2] == train[len(train) // 2 :]:
        train = train[: len(train) // 2]
    route = re.search(
        r"(\d{1,2}[./]\d{1,2}(?:[./]\d{4})?)\s*\n(\d{1,2}:\d{2})\s*\n"
        r"([А-ЯЁA-Z][А-ЯЁA-Z0-9 .'-]{2,50})\s*\n(?:->\s*)?"
        r"([А-ЯЁA-Z][А-ЯЁA-Z0-9 .'-]{2,50})\s*\n"
        r"(\d{1,2}[./]\d{1,2}(?:[./]\d{4})?)\s*\n(\d{1,2}:\d{2})",
        text,
        flags=re.IGNORECASE,
    )
    if route:
        date, dep, from_name, to_name, _arrival_date, arr = route.groups()
        return [{
            "from": from_name,
            "fromCode": "",
            "to": to_name,
            "toCode": "",
            "date": _date_parts(
                date,
                default_year=_first_match(text, [r"Год совершения поездки\s*:\s*(20\d{2})"]) or _extract_issue_year(text),
            ),
            "dep": dep,
            "arr": arr,
            "flightNo": train,
            "dir": "out",
        }]

    departure = re.search(
        r"(?:Отправление|Departure)(?P<body>.{0,350}?)(?:Прибытие|Arrival)(?P<arrival>.{0,350})",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if departure:
        body, arrival = departure.group("body"), departure.group("arrival")
        from_name = _first_match(body, [r"\n([А-ЯЁ][А-ЯЁA-Z0-9 .'-]{3,50})\n\d{1,2}[./]\d{1,2}[./]\d{4}"])
        to_name = _first_match(arrival, [r"\n([А-ЯЁ][А-ЯЁA-Z0-9 .'-]{3,50})\n\d{1,2}[./]\d{1,2}[./]\d{4}"])
        dep_date = _first_match(body, [r"(\d{1,2}[./]\d{1,2}[./]\d{4})"])
        dep_time = _first_match(body, [r"(\d{1,2}:\d{2})"])
        arr_time = _first_match(arrival, [r"(\d{1,2}:\d{2})"])
        if from_name and to_name:
            return [{
                "from": from_name,
                "fromCode": "",
                "to": to_name,
                "toCode": "",
                "date": _normalize_date(dep_date),
                "dep": dep_time,
                "arr": arr_time,
                "flightNo": train,
                "dir": "out",
            }]

    stations = []
    for pattern in [r"(?i)(?:М|M)?ОСКВА[^\n;]*", r"(?i)(?:Р|R)?ЯЗАНЬ[^\n;]*"]:
        match = re.search(pattern, text)
        if match:
            stations.append(match.group(0).strip())
    leading = text[: text.find("Возврат онлайн") if "Возврат онлайн" in text else len(text)]
    times = list(dict.fromkeys(re.findall(r"\b(\d{1,2}:\d{2})\b", leading)))
    dates = re.findall(r"\b(\d{1,2}[./]\d{1,2}[./]\d{4})\b", leading)
    if len(stations) >= 2 and len(times) >= 2 and dates:
        return [{
            "from": re.sub(r"^(?:ММ|MМ|M)?(?=осква)", "М", stations[0], flags=re.IGNORECASE),
            "fromCode": "",
            "to": re.sub(r"^(?:РР|RР|R)?(?=язань)", "Р", stations[1], flags=re.IGNORECASE),
            "toCode": "",
            "date": _normalize_date(dates[0]),
            "dep": times[0],
            "arr": times[1],
            "flightNo": train,
            "dir": "out",
        }]
    return []


def _hotel_segment(text: str) -> tuple[list[dict], str, str]:
    hotel = _first_match(text, [
        r"Название отеля\s*:\s*([^\n]+)",
        r"Размещение забронировано нашим партнером\s*\n(?:Пассажирский Сервисный Центр\s*\n)?"
        r"(?:\+?[\d ]+\s*\n)?([^\n]+)",
    ])
    if not hotel:
        hotel = _first_labeled_value(text, [r"^Название отеля\s*:?$"], lookahead_lines=3)
    checkin = _first_match(text, [
        r"Дата заезда\s*/\s*Дата\s*\n?выезда\s*:\s*(\d{1,2}[./]\d{1,2}[./]\d{4}\s+\d{1,2}:\d{2})",
        r"Заезд\s*\n(\d{1,2}[./]\d{1,2}[./]\d{4}(?:,\s*с)?\s*\n?\d{1,2}:\d{2}(?::\d{2})?)",
    ])
    checkout = _first_match(text, [
        r"Дата заезда\s*/\s*Дата\s*\n?выезда\s*:\s*[^\n/]+\s*/\s*(\d{1,2}[./]\d{1,2}[./]\d{4}\s+\d{1,2}:\d{2})",
        r"Выезд\s*\n(\d{1,2}[./]\d{1,2}[./]\d{4}(?:,\s*до)?\s*\n?\d{1,2}:\d{2}(?::\d{2})?)",
    ])

    def split_stay(value: str) -> tuple[str, str]:
        date = _first_match(value, [r"(\d{1,2}[./]\d{1,2}[./]\d{4})"])
        time = _first_match(value, [r"(\d{1,2}:\d{2})"])
        return _normalize_date(date), time

    start_date, start_time = split_stay(checkin)
    end_date, end_time = split_stay(checkout)
    room = _first_match(text, [
        r"Категория номера\s*:\s*([^\n]+)",
        r"(Стандартный номер[^\n]+)",
        r"(Двухместный люкс[^\n]+)",
    ])
    if hotel and start_date:
        return [{
            "from": "",
            "fromCode": "",
            "to": hotel.strip(" «»"),
            "toCode": "",
            "date": start_date,
            "endDate": end_date,
            "dep": start_time,
            "arr": end_time,
            "flightNo": room,
            "dir": "out",
        }], hotel.strip(" «»"), room
    return [], hotel.strip(" «»"), room


def _transfer_segments(text: str) -> list[dict]:
    blocks = re.findall(
        r"\+?\d[\d -]{8,}\s*\n(?P<route>.*?)\nСтоимость\s*\n(?P<details>.*?)(?=\nУсловия изменения|\nПассажиры|\nИтого)",
        text,
        flags=re.DOTALL,
    )
    segments = []
    for index, (route_block, details) in enumerate(blocks[:2]):
        route_lines = []
        for line in route_block.splitlines():
            value = line.strip().rstrip("–— ").strip()
            if value and (not route_lines or value != route_lines[-1]):
                route_lines.append(value)
        if len(route_lines) < 2:
            continue
        dates = re.findall(r"\b(\d{1,2}[./]\d{1,2}[./]\d{4})\b", details)
        times = list(dict.fromkeys(re.findall(r"(?m)^(\d{1,2}:\d{2})\s*$", details)))
        segments.append({
            "from": route_lines[0],
            "fromCode": "",
            "to": route_lines[-1],
            "toCode": "",
            "date": _normalize_date(dates[0] if dates else ""),
            "dep": times[0] if times else "",
            "arr": "",
            "flightNo": "",
            "dir": "out" if index == 0 else "back",
        })
    return segments


def _structured_receipt_fields(text: str, *, service_kind: str) -> dict:
    passenger = _extract_passenger(text, service_kind=service_kind)
    reference = _extract_reference(text, service_kind=service_kind)
    ticket = _extract_ticket_number(text, service_kind=service_kind)
    document_number = _first_match(text, [
        r"(?:Документ\s*\n/Document|Документ/Document|Номер документа|ПАСПОРТ РФ|ПСП)"
        r"\s*[:\-]?\s*([A-ZА-Я]{0,3}\s*\d{6,14})",
        r"\b(ПСП\d{6,14})\b",
    ])
    dob = _first_match(text, [
        r"(?:дата рождения|dob|date of birth)\s*[:\-]?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
        r"(?:ПН[^\n]*[/\s])(\d{1,2}[./-]\d{1,2}[./-]\d{4})",
        r"(?:ПАСПОРТ РФ[^\n]*\n)(\d{1,2}[./-]\d{1,2}[./-]\d{4})",
    ])
    issue_date = _normalize_date(_first_match(text, [
        r"(?:Date of issue|Дата выдачи|Дата оформления|ДАТА)\s*[:\-]?\s*(\d{1,2}(?:[./-]\d{1,2}[./-]\d{2,4}|[А-ЯЁ]{3}\d{2,4}))",
        r"(?:Продажа|Забронировано)\s*[:\-]?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
    ]))
    if not issue_date:
        issue_candidate = _first_labeled_value(
            text,
            [r"^Дата выдачи$", r"^Date of issue$"],
            lookahead_lines=4,
            reject=r"^Date of issue$",
        )
        if re.search(r"\d", issue_candidate):
            issue_date = _normalize_date(issue_candidate)
    booking_class = _first_match(text, [
        r"(?:Класс/Class|Class|Класс обслуживания)\s*[:\-]?\s*([A-ZА-Я0-9]{1,8})",
        r"(?m)^\s*([A-Z])\s*\n\s*ВРЕМЯ ПРИБ",
    ])
    if service_kind == "rail":
        booking_class = _first_match(text, [r"(?m)^([123][A-ZА-Я])$"]) or booking_class
    fare_basis = _first_match(text, [
        r"(?:Вид тарифа/Fare basis|Fare basis|Тарифный код)\s*[:\-]?\s*([A-ZА-Я0-9-]{2,32})",
        r"(?m)^ТАРИФ\s*\n((?!СБОР|БАГАЖ|СТАТУС)[A-ZА-Я0-9-]{3,32})$",
    ])
    baggage = _first_match(text, [
        r"(?:Багаж\s*\n/Baggage allow:|Baggage allow|Baggage|Багаж)\s*[:\-]?\s*([^\n]+)",
        r"(?m)^БАГ\s*\n([0-9]+\s*(?:PC|KG|КМ))$",
        r"(?m)^([0-9]+\s*(?:PC|KG|КМ))$",
    ])
    hand_baggage = _first_match(text, [r"Ручная кладь\s*[:\-]?\s*([^\n.]+)"])
    if service_kind == "avia":
        standalone_baggage = _first_match(text, [r"(?m)^([0-9]+\s*(?:PC|KG|КМ))$"])
        baggage = standalone_baggage or baggage
    else:
        baggage = ""
        hand_baggage = ""

    issuer = _first_match(text, [
        r"(?:Выдан от/Issued by)\s*[:\-]?\s*([^\n]+)",
        r"(?m)^(?:Carrier|Перевозчик)(?:\s*\([^)]*\))?\s*:\s*([^\n]+)",
        r"Рейс под брендом авиакомпании\s+([^\n]+)",
    ])
    if not issuer and service_kind == "avia":
        issuer = _first_labeled_value(text, [r"^ВЫДАН ОТ$"], lookahead_lines=8, reject=r"номер|обмен|первон")
    if not issuer and service_kind == "rail":
        issuer = _first_labeled_value(
            text,
            [r"^Перевозчик(?:\s*\([^)]*\))?\s*:?$"],
            lookahead_lines=5,
            reject=r"^[CС]arrier",
        )
    if service_kind == "avia":
        fare_codes = re.findall(r"\b[A-Z0-9]{2}-\d{2,5}\s+[A-Z]{3}\s*[-–—]\s*[A-Z]{3}\s*\(([A-Z0-9-]+)\)", text)
        if fare_codes:
            fare_basis = " / ".join(dict.fromkeys(fare_codes))
        if not booking_class:
            booking_class = _first_match(text, [r"(?m)^(ECONOMY|BUSINESS|ЭКОНОМ|БИЗНЕС)$"])

    if service_kind == "hotel":
        segments, hotel, _room = _hotel_segment(text)
        issuer = hotel or issuer
    elif service_kind == "transfer":
        segments = _transfer_segments(text)
    elif service_kind == "rail":
        segments = _rail_segments(text)
    else:
        segments = _avia_segments(text)

    total_labels = [
        r"ВСЕГО К ОПЛАТЕ",
        r"\bИтого\b",
        r"\bTotal\b",
        r"\bСТОИМОСТЬ\s*:",
        r"(?:Цена|Price),?\s*(?:Руб|RUB)",
    ]
    total, total_currency = _money_near_label(text, total_labels, prefer_max=True)
    fare, fare_currency = _money_near_label(
        text,
        [r"\bТариф/Fare\b", r"\bFare\b", r"Тариф\s*\(билет", r"Тариф билета", r"\bТАРИФ\b"],
        window=500,
    )
    taxes, tax_currency = _money_near_label(text, [r"СБОР/TAX", r"\bTaxes\b", r"\bТаксы\b"], window=400)
    fee_breakdown = _fee_breakdown(text)
    included_fee_layout = re.search(
        r"СТОИМОСТЬ:.*?СБОР АСБ:.*?СБОР СА:(?P<body>.{0,1000})",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if included_fee_layout:
        amounts = re.findall(
            r"(?m)^(\d[\d ]*(?:[,.]\d{1,2})?)\s*РУБ",
            included_fee_layout.group("body"),
            flags=re.IGNORECASE,
        )
        if len(amounts) >= 3:
            fee_breakdown = [
                {"code": "ASB", "label": "Сбор АСБ", "amount": str(Decimal(amounts[1].replace(" ", "").replace(",", "."))), "currency": "RUB"},
                {"code": "SA", "label": "Сбор СА", "amount": str(Decimal(amounts[2].replace(" ", "").replace(",", "."))), "currency": "RUB"},
            ]
    fees = sum((Decimal(row["amount"]) for row in fee_breakdown), Decimal("0")) if fee_breakdown else None
    currency = total_currency or fare_currency or tax_currency or _first_currency(text)

    calculation_layout = re.search(
        r"РАСЧЕТ ТАРИФА:.*?ТАРИФ.*?СБОР/TAX.*?ВСЕГО К ОПЛАТЕ(?P<body>.{0,1000})",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if calculation_layout:
        amounts = re.findall(r":\s*RUB\s*(\d[\d ]*(?:[,.]\d{1,2})?)", calculation_layout.group("body"))
        if len(amounts) >= 3:
            fare, taxes, total = [Decimal(value.replace(" ", "").replace(",", ".")) for value in amounts[:3]]
            currency = "RUB"

    if service_kind in {"rail", "transfer"} and total is not None:
        fare, taxes, fees = total, Decimal("0"), Decimal("0")
    elif included_fee_layout and total is not None:
        fare = max(total - (fees or Decimal("0")), Decimal("0"))
        taxes = taxes or Decimal("0")
    elif total is not None and fare is None:
        included = (taxes or Decimal("0")) + (fees or Decimal("0"))
        fare = max(total - included, Decimal("0"))

    fields = {
        "issuer": issuer,
        "passenger_name": passenger,
        "fare": fare,
        "taxes": taxes,
        "fees": fees,
        "total": total,
        "currency": currency,
        "segments": segments,
        "reference": reference,
        "ticket_number": ticket,
        "document_number": document_number,
        "date_of_birth": dob,
        "issue_date": issue_date,
        "booking_class": booking_class,
        "fare_basis": fare_basis,
        "baggage": baggage,
        "hand_baggage": hand_baggage,
        "tax_breakdown": [],
        "fee_breakdown": fee_breakdown,
    }
    if service_kind == "avia":
        compact_s7 = _s7_compact_fields(text)
        if compact_s7 and compact_s7.get("passenger_name") and compact_s7.get("segments"):
            fields.update(compact_s7)
        rossiya = _rossiya_itinerary_fields(text)
        if rossiya and rossiya.get("passenger_name") and rossiya.get("segments"):
            fields.update(rossiya)
    return fields


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
    generic_fare, generic_taxes, generic_total = fare, taxes, total
    segments = _extract_segments(text, service_kind=service_kind)
    generic_passenger = passenger
    generic_segments = segments
    structured = _structured_receipt_fields(text, service_kind=service_kind)
    # A service-specific parser knows the supplier layouts better, but it may
    # legitimately return an empty value for a compact/generic receipt.  Keep
    # the universal parser result as a fallback instead of erasing it.
    issuer = structured["issuer"] or issuer
    passenger_on_next_line = bool(re.search(r"(?mi)^\s*ФАМИЛИЯ\s*:\s*$", text))
    passenger = (
        generic_passenger
        if passenger_on_next_line and "/" in (generic_passenger or "")
        else structured["passenger_name"] or generic_passenger
    )
    fare = structured["fare"] if structured["fare"] is not None else fare
    taxes = structured["taxes"] if structured["taxes"] is not None else taxes
    fees = structured["fees"] if structured["fees"] is not None else fees
    total = structured["total"] if structured["total"] is not None else total
    compact_financial_block = bool(
        re.search(r"(?mi)^\s*ТАРИФ\s*$", text)
        and re.search(r"(?mi)^\s*ВСЕГО К ОПЛАТЕ\s*$", text)
        and not structured.get("supplier_order_number")
    )
    if compact_financial_block:
        fare = generic_fare if generic_fare is not None else fare
        taxes = generic_taxes if generic_taxes is not None else taxes
        total = generic_total if generic_total is not None else total
    # Supplier PDFs often place fare/tax captions in a column and their values
    # several lines below.  A generic regex can then pick a nearby time or a
    # tax amount as the fare (for example, ``8`` instead of ``53 545``).  The
    # printed total is authoritative, so keep the extracted taxes/fees and
    # reconcile the base fare whenever the components do not add up.
    if service_kind == "avia" and total is not None:
        additions = (taxes or Decimal("0")) + (fees or Decimal("0"))
        if fare is None or abs((fare + additions) - total) > Decimal("0.01"):
            fare = max(total - additions, Decimal("0"))
    currency = structured["currency"] or currency
    explicit_route = bool(re.search(r"(?mi)^\s*Route\s*:", text))
    segments = generic_segments if explicit_route and generic_segments else structured["segments"] or generic_segments
    reference = structured["reference"] or reference
    ticket = structured["ticket_number"] or ticket
    document_number = structured["document_number"] or document_number
    dob = structured["date_of_birth"] or dob
    issue_date = structured["issue_date"] or issue_date
    booking_class = structured["booking_class"] or booking_class
    fare_basis = structured["fare_basis"] or fare_basis
    baggage = structured["baggage"] or baggage
    hand_baggage = structured["hand_baggage"] or hand_baggage
    if service_kind == "avia" and segments:
        segment_classes = [segment.get("cls") for segment in segments if segment.get("cls")]
        segment_fares = [segment.get("fareBasis") for segment in segments if segment.get("fareBasis")]
        segment_baggage = [segment.get("baggage") for segment in segments if segment.get("baggage")]
        if segment_classes:
            booking_class = " / ".join(dict.fromkeys(segment_classes))
        if segment_fares:
            fare_basis = " / ".join(dict.fromkeys(segment_fares))
        if segment_baggage:
            baggage = " / ".join(dict.fromkeys(segment_baggage))
    tax_breakdown = structured.get("tax_breakdown") or tax_breakdown
    fee_breakdown = structured["fee_breakdown"] or fee_breakdown
    supplier_order_number = structured.get("supplier_order_number") or ""
    trip_type = _guess_trip_type(segments, service_kind)
    route_code = _first_match(text, [r"ОТПРВ/НАЗН\s*:\s*([A-Z]{6})"])
    if service_kind == "avia" and len(route_code) == 6 and route_code[:3] == route_code[-3:]:
        trip_type = "roundtrip"

    fields = {
        "issuer": issuer,
        "passenger_name": passenger,
        "passengers": structured.get("passengers") or [],
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
        "supplier_order_number": supplier_order_number,
        "service_kind": service_kind,
        "service_type": SERVICE_KIND_LABELS[service_kind],
    }
    warnings = []
    if not passenger:
        warnings.append("Пассажир или гость не найден в текстовом слое.")
    if not segments:
        warnings.append("Маршрут или размещение не найдены в текстовом слое.")
    if service_kind != "hotel" and total is None:
        warnings.append("Итоговая сумма не найдена в текстовом слое.")
    if service_kind == "hotel" and total is None:
        warnings.append("В исходном ваучере стоимость не указана.")

    has_route_date = bool(
        segments
        and any(segment.get("date") or segment.get("dep") or segment.get("arr") for segment in segments)
        and any(segment.get("from") or segment.get("to") for segment in segments)
    )
    financial_ok = total is not None or service_kind == "hotel"
    # Some valid supplier receipts contain only a passenger, booking/ticket
    # reference and the financial block.  Such documents are useful and were
    # supported before route extraction was added, so absence of a route alone
    # must not downgrade them to manual review.
    itinerary_ok = has_route_date or bool(reference or ticket)
    status = "parsed" if passenger and itinerary_ok and financial_ok else "manual_review"
    required_found = sum((bool(passenger), has_route_date, financial_ok, bool(reference or ticket)))
    confidence = Decimal("0.920") if required_found == 4 and status == "parsed" else (
        Decimal("0.820") if status == "parsed" else Decimal("0.350")
    )
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
            "supplier_order_number": supplier_order_number,
            "passengers": structured.get("passengers") or [],
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
