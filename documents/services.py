import hashlib

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
