from copy import deepcopy
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from pypdf import PdfReader

from documents.receipt_client_pdf_requirements import _parse_russian_aeroflot_group
from documents.receipt_financial_rows import additional_rows, insert_financial_rows
from documents.receipt_supplier_pdf_patch import SUPPLIER_PDF_SYNC_JOB
from documents.receipt_supplier_pdf_writer_fix import patch_supplier_pdf
from documents.tests.test_receipt_supplier_pdf_patch import _simple_supplier_pdf

TEXT = Path(__file__).with_name('fixtures').joinpath('aeroflot_connection.txt').read_text()


def test_connection_and_payment_continuation_are_one_ticket_with_two_complete_segments():
    result = _parse_russian_aeroflot_group(TEXT)
    assert result['ticket_number'] == '5550000000001'
    assert result.get('receipt_count', 1) == 1
    assert result['fare'] == Decimal('22940')
    assert result['taxes'] == Decimal('1729')
    assert result['total'] == Decimal('24669')
    assert [(r['code'], Decimal(r['amount'])) for r in result['tax_breakdown']] == [('YR', Decimal('244')), ('XT', Decimal('1485'))]
    assert [(s['from'], s['fromCode'], s['to'], s['toCode'], s['flightNo'], s['dep'], s['arr']) for s in result['segments']] == [
        ('Томск', 'TOF', 'Москва', 'SVO', 'SU1531', '09:00', '09:45'),
        ('Москва', 'SVO', 'Нижний Новгород', 'GOJ', 'SU6221', '13:10', '14:30'),
    ]
    assert all(s['date'] == s['endDate'] == '13.11.2026' for s in result['segments'])
    assert [s['cls'] for s in result['segments']] == ['N', 'R']
    assert [s['fareBasis'] for s in result['segments']] == ['NCOENR', 'RCOENR']
    assert [s['carrier'] for s in result['segments']] == ['Аэрофлот', 'Россия']
    assert all(s['baggage'] == '1 • до 23 кг' for s in result['segments'])
    assert result['booking_class'] == 'N / R'
    assert result['fare_basis'] == 'NCOENR / RCOENR'
    assert result['trip_type'] == 'complex'
    assert result['date_of_birth'] == '01.01.1990'


def test_payment_continuation_does_not_leak_between_passengers():
    second = TEXT.replace('5550000000001', '5550000000002').replace('TESTOV TEST TESTOVICH', 'SECOND TEST PASSENGER')
    result = _parse_russian_aeroflot_group(TEXT + '\f' + second)
    assert result['receipt_count'] == 2
    assert result['total'] == Decimal('49338')
    assert [r['total'] for r in result['receipt_items']] == [Decimal('24669')] * 2
    assert [len(r['segments']) for r in result['receipt_items']] == [2, 2]
    assert [r['sourcePage'] for r in result['receipt_items']] == [1, 3]


def test_adding_tax_prints_label_and_amount_in_working_copy_and_updates_total():
    source = _simple_supplier_pdf()
    before = {'total': '25470', 'taxes': '0', 'taxBreakdown': []}
    after = {'total': '25970', 'taxes': '500', 'taxBreakdown': [
        {'code': 'ZZ', 'label': 'Дополнительная такса', 'amount': '500', 'currency': 'RUB'},
    ]}
    corrected, report = patch_supplier_pdf(source, before, after)
    assert corrected is not None
    reader = PdfReader(BytesIO(corrected))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text()
    assert 'TOTAL 25970 RUB' in text
    assert 'ZZ · Дополнительная такса' in text
    assert '500.00 RUB' in text
    assert report['added_rows'] == 1
    assert report['unapplied'] == []
    assert 'TOTAL 25470 RUB' in PdfReader(BytesIO(source)).pages[0].extract_text()
    assert 'Дополнительная такса' not in PdfReader(BytesIO(source)).pages[0].extract_text()


def test_reordering_existing_rows_does_not_insert_them_again_but_zero_row_is_added():
    before = {'taxBreakdown': [{'code': 'YR', 'amount': '100'}, {'code': 'ZZ', 'amount': '0'}]}
    after = deepcopy(before)
    after['taxBreakdown'].reverse()
    after['taxBreakdown'][0]['amount'] = '200'
    assert [r['label'] for r in additional_rows(before, after)] == ['ZZ']
    after['taxBreakdown'][0]['amount'] = '0'
    assert additional_rows(before, after) == []


def test_missing_whitespace_adds_readable_page_without_covering_source(monkeypatch):
    from documents import receipt_supplier_pdf_writer_fix as writer
    monkeypatch.setattr(writer, '_layout_lines', lambda content: [])
    source = _simple_supplier_pdf()
    corrected, report = insert_financial_rows(source, [{'label': 'Тестовая такса', 'amount': '500.00', 'currency': 'RUB', 'page': None}])
    reader = PdfReader(BytesIO(corrected))
    assert len(reader.pages) == 2
    assert 'TOTAL 25470 RUB' in reader.pages[0].extract_text()
    assert 'Тестовая такса' in reader.pages[1].extract_text()
    assert '500.00 RUB' in reader.pages[1].extract_text()
    assert report['breakdown_appended_pages'] == 1


def _connection_pdf():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    font = 'ReceiptRegressionSans'
    if font not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font, str(Path(__file__).parents[1] / 'fonts' / 'NotoSans-Regular.ttf')))
    stream = BytesIO()
    pdf = canvas.Canvas(stream, pagesize=(595, 842))
    for page in TEXT.split('\f'):
        block = pdf.beginText(25, 815)
        block.setFont(font, 8)
        block.setLeading(10)
        for line in page.splitlines():
            block.textLine(line)
        pdf.drawText(block)
        pdf.showPage()
    pdf.save()
    return stream.getvalue()


@pytest.mark.django_db
def test_api_import_and_preview_preserve_connections_and_added_tax(admin_client):
    source = _connection_pdf()
    imported = admin_client.post('/api/v1/receipt-imports/', {'file': SimpleUploadedFile('sample.pdf', source, content_type='application/pdf')}, format='multipart')
    assert imported.status_code == 201, imported.content
    result = admin_client.get(f"/api/v1/receipt-imports/{imported.json()['id']}/result/")
    assert result.status_code == 200, result.content
    body = result.json()
    verified = deepcopy(body['verified_data'])
    assert len(verified['legs']) == 2, verified
    assert [(s['fromCode'], s['toCode']) for s in verified['legs']] == [('TOF', 'SVO'), ('SVO', 'GOJ')]
    assert float(verified['total']) == 24669
    assert float(verified['fare']) == 22940
    assert float(verified['taxes']) == 1729
    verified['taxBreakdown'].append({'code': 'ZZ', 'label': 'Дополнительная такса', 'amount': '500', 'currency': 'RUB'})
    verified['taxes'] = '2229'
    verified['total'] = '25169'
    with override_settings(SYNC_JOB_KINDS=(SUPPLIER_PDF_SYNC_JOB,)):
        saved = admin_client.post(f"/api/v1/documents/{body['source_document_id']}/receipt/", {'draft': True, 'preview_sync': True, 'verified_data': verified}, format='json')
    assert saved.status_code == 200, saved.content
    assert saved.json()['supplier_pdf_correction']['status'] == 'corrected', saved.json()
    preview = admin_client.get(f"/api/v1/documents/{body['source_document_id']}/supplier-pdf/?disposition=inline")
    assert preview.status_code == 200
    assert preview['X-Supplier-PDF-Mode'] == 'corrected'
    pdf = b''.join(preview.streaming_content)
    pages = PdfReader(BytesIO(pdf)).pages
    assert len(pages) == 2
    text = pages[1].extract_text()
    assert 'Дополнительная такса' in text
    assert '500.00 RUB' in text
    assert '25169' in text
    assert '24669' not in text
