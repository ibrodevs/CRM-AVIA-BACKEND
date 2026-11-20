from decimal import Decimal
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from pypdf import PdfWriter

from documents.models import ReceiptImportJob
from documents.receipt_async import recognize_receipt

pytestmark = pytest.mark.django_db


def pdf_upload():
    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    writer.write(stream)
    return SimpleUploadedFile('async-test.pdf', stream.getvalue(), content_type='application/pdf')


def test_upload_returns_before_analysis_and_worker_persists_result(admin_client, monkeypatch, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    def parser(*a, **kw):
        pytest.fail('HTTP request must not run OCR')
    monkeypatch.setattr('documents.services.extract_receipt_fields', parser)
    response = admin_client.post('/api/v1/receipt-imports/?async=1', {'file': pdf_upload()}, format='multipart', HTTP_IDEMPOTENCY_KEY='upload-1')
    assert response.status_code == 202, response.data
    imported = ReceiptImportJob.objects.get(pk=response.data['id'])
    assert imported.parser_status == 'queued'
    assert imported.file_version.file.storage.exists(imported.file_version.file.name)
    assert imported.job.kind == 'documents.receipt.recognize'
    repeated = admin_client.post('/api/v1/receipt-imports/?async=1', {'file': pdf_upload()}, format='multipart', HTTP_IDEMPOTENCY_KEY='upload-1')
    assert repeated.data['id'] == response.data['id']
    fields = {'service_kind':'avia', 'issuer':'TEST', 'passenger_name':'TEST PERSON', 'total':Decimal('100'), 'fare':Decimal('100'), 'currency':'RUB', 'segments':[]}
    monkeypatch.setattr('documents.services.extract_receipt_fields', lambda *a, **kw: {'status':'parsed','confidence':Decimal('.990'),'fields':fields,'raw':fields,'warnings':[]})
    recognize_receipt(imported.job)
    imported.refresh_from_db()
    assert imported.parser_status == 'parsed'
    assert imported.draft.total == Decimal('100')
    result = admin_client.get(f'/api/v1/receipt-imports/{imported.pk}/result/')
    assert result.status_code == 200
    assert result.data['parser_status'] == 'parsed'


def test_failed_recognition_keeps_original_and_returns_explicit_failure(admin_client, monkeypatch, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    response = admin_client.post('/api/v1/receipt-imports/?async=1', {'file': pdf_upload()}, format='multipart')
    imported = ReceiptImportJob.objects.get(pk=response.data['id'])
    def broken(*a, **kw):
        raise ValueError('broken parser')
    monkeypatch.setattr('documents.services.extract_receipt_fields', broken)
    with pytest.raises(ValueError):
        recognize_receipt(imported.job)
    imported.refresh_from_db()
    assert imported.parser_status == 'failed'
    assert imported.file_version.file.storage.exists(imported.file_version.file.name)
    assert imported.warnings
