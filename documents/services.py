import hashlib
import re
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


def _first_match(text: str, patterns: list[str]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).strip(" :;,\n\t")
    return ""


def _money(text: str, patterns: list[str]) -> Decimal | None:
    value = _first_match(text, patterns)
    if not value:
        return None
    normalized = re.sub(r"[^\d,.-]", "", value).replace(" ", "").replace(",", ".")
    try:
        return Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None


def extract_receipt_fields(content: bytes, *, mime: str = "", name: str = "") -> dict:
    """Best-effort extraction from a text layer; scanned images still require OCR/manual entry."""
    text = _decode_text(content)
    visible_text = re.sub(r"\s+", " ", text).strip()
    if len(visible_text) < 20:
        return {
            "status": "manual_review",
            "confidence": Decimal("0.000"),
            "fields": {},
            "raw": {"file_name": name, "mime": mime, "text_available": False},
            "warnings": ["Не удалось извлечь текст из файла. Для сканов нужен OCR или ручное заполнение."],
        }

    currency = _first_match(
        text,
        [
            r"\b(USD|EUR|RUB|KGS|KZT)\b",
            r"(?:валюта|currency)\s*[:\-]?\s*([A-Z]{3})",
        ],
    )
    fare = _money(text, [r"(?:fare|тариф|base fare)\s*[:\-]?\s*([0-9][0-9\s.,-]*)"])
    taxes = _money(text, [r"(?:taxes|таксы|сборы|tax)\s*[:\-]?\s*([0-9][0-9\s.,-]*)"])
    total = _money(text, [r"(?:total|итого|к оплате|amount)\s*[:\-]?\s*([0-9][0-9\s.,-]*)"])
    passenger = _first_match(
        text,
        [
            r"(?:passenger|пассажир|traveller)\s*[:\-]?\s*([^\n]+)",
            r"(?:name|фио)\s*[:\-]?\s*([^\n]+)",
        ],
    )
    issuer = _first_match(
        text,
        [
            r"(?:carrier|airline|перевозчик|поставщик)\s*[:\-]?\s*([^\n]+)",
            r"(?:issued by|выдано)\s*[:\-]?\s*([^\n]+)",
        ],
    )
    reference = _first_match(text, [r"\b(?:PNR|booking ref|бронь)\s*[:\-]?\s*([A-Z0-9]{5,8})"])
    ticket = _first_match(
        text,
        [r"(?:ticket|билет|номер билета)\s*(?:no|№|number)?\s*[:\-]?\s*([0-9A-ZА-Я -]{6,24})"],
    )

    fields = {
        "issuer": issuer,
        "passenger_name": passenger,
        "fare": fare,
        "taxes": taxes,
        "total": total,
        "currency": currency,
        "segments": [],
        "reference": reference,
        "ticket_number": ticket,
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
