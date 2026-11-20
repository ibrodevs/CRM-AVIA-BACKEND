"""Persist uploads before recognition; long OCR runs outside HTTP workers."""
from hashlib import sha256

from django.db import transaction
from rest_framework.response import Response

from common.errors import ApiError
from common.jobs import enqueue, job_handler
from common.models import BackgroundJob
from documents.models import Document, ReceiptDraft, ReceiptImportJob
from documents.receipt_metadata import json_safe, receipt_document_metadata
from documents.services import add_document_version

KIND = 'documents.receipt.recognize'


def create_receipt_import(request, file, content):
    key = request.headers.get('Idempotency-Key', '').strip()
    if len(key) > 255:
        raise ApiError(code='VALIDATION_ERROR', message='Слишком длинный ключ загрузки', status_code=400)
    digest = sha256(content).hexdigest()
    with transaction.atomic():
        # Serialise only persistence for this uploader, never recognition.
        type(request.user).objects.select_for_update().get(pk=request.user.pk)
        existing = BackgroundJob.objects.filter(kind=KIND, tenant_id=request.user.tenant_id,
            initiated_by=request.user, correlation_id=key).first() if key else None
        if existing:
            if existing.payload.get('sha256') != digest:
                raise ApiError(code='IDEMPOTENCY_CONFLICT', message='Ключ загрузки уже использован для другого файла', status_code=409)
            imported = ReceiptImportJob.objects.get(job=existing, tenant_id=request.user.tenant_id)
            if not imported.file_version_id:
                raise ApiError(code='UPLOAD_REMOVED', message='Оригинал был удалён. Загрузите файл заново', status_code=409)
            return Response({'id': str(imported.id), 'document_id': str(imported.file_version.document_id)}, status=202)
        document = Document.objects.create(tenant_id=request.user.tenant_id, kind='itinerary_receipt',
            title=file.name, source='supplier', created_by=request.user,
            metadata={'supplier_original': {'name': file.name, 'mime': file.content_type, 'size': file.size},
                      'receipt_import': {'stage': 'uploaded'}})
        version = add_document_version(document, content=content, mime=file.content_type, name=file.name,
            user=request.user, origin='uploaded')
        imported = ReceiptImportJob.objects.create(tenant_id=request.user.tenant_id, created_by=request.user,
            file_version=version, parser_status='queued')
        ReceiptDraft.objects.create(tenant_id=request.user.tenant_id, created_by=request.user, import_job=imported)
        job = enqueue(KIND, {'import_id': str(imported.id), 'sha256': digest}, request=request,
            tenant_id=request.user.tenant_id, correlation_id=key, priority=40)
        imported.job = job
        imported.save(update_fields=['job'])
    return Response({'id': str(imported.id), 'document_id': str(document.id)}, status=202)


@job_handler(KIND, max_attempts=1, user_cancellable=False)
def recognize_receipt(job):
    from documents import services
    from documents.receipt_ticket_level_patch import _save_draft_items, normalize_receipt_items

    imported = ReceiptImportJob.objects.select_related('file_version__document').get(
        pk=job.payload['import_id'], tenant_id=job.tenant_id)
    if imported.parser_status not in {'queued', 'processing'}:
        return {'import_id': str(imported.id), 'status': imported.parser_status}
    imported.parser_status = 'processing'
    imported.save(update_fields=['parser_status'])
    try:
        version = imported.file_version
        with version.file.open('rb') as stream:
            content = stream.read()
        result = services.extract_receipt_fields(content, mime=version.mime_type, name=version.original_name)
        fields = result['fields']
        with transaction.atomic():
            imported.guessed_type = fields.get('service_kind') or 'other'
            imported.parser_status = result['status']
            imported.confidence = result['confidence']
            imported.raw_extraction = json_safe(result['raw'])
            imported.warnings = result['warnings']
            imported.save(update_fields=['guessed_type', 'parser_status', 'confidence', 'raw_extraction', 'warnings'])
            draft = imported.draft
            for field in ['issuer', 'passenger_name', 'currency', 'trip_type']:
                setattr(draft, field, fields.get(field) or '')
            for field in ['fare', 'taxes', 'fees', 'total']:
                setattr(draft, field, fields.get(field))
            for field in ['fare_breakdown', 'tax_breakdown', 'fee_breakdown', 'segments']:
                setattr(draft, field, json_safe(fields.get(field) or []))
            draft.save()
            items = normalize_receipt_items(imported.raw_extraction, parser_status=imported.parser_status,
                service_kind=imported.guessed_type)
            if items:
                _save_draft_items(imported, items)
            document = version.document
            document.metadata = receipt_document_metadata(document.metadata, import_id=imported.id,
                extraction=result, file_name=version.original_name, mime=version.mime_type, size=len(content))
            document.save(update_fields=['metadata'])
        return {'import_id': str(imported.id), 'status': imported.parser_status}
    except Exception:
        ReceiptImportJob.objects.filter(pk=imported.id, tenant_id=job.tenant_id).update(
            parser_status='failed', warnings=['Распознавание не завершено. Оригинал сохранён, документ можно проверить вручную.'])
        raise
