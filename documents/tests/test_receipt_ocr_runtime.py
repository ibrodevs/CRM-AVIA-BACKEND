from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from documents import receipt_ocr_fallback


def test_pythonanywhere_ocr_runtime_is_ready_with_local_languages(monkeypatch, tmp_path):
    tessdata = tmp_path / "tessdata"
    tessdata.mkdir()
    (tessdata / "eng.traineddata").write_bytes(b"eng")
    (tessdata / "rus.traineddata").write_bytes(b"rus")
    monkeypatch.setattr(receipt_ocr_fallback, "_ocr_tools", lambda: ("/usr/bin/tesseract", None))
    monkeypatch.setattr(receipt_ocr_fallback, "_tessdata_dir", lambda: tessdata)
    monkeypatch.setattr(
        receipt_ocr_fallback,
        "_available_tesseract_languages",
        lambda *args, **kwargs: {"eng", "rus"},
    )
    monkeypatch.setattr(receipt_ocr_fallback.importlib.util, "find_spec", lambda name: object())

    status = receipt_ocr_fallback.receipt_ocr_runtime_status()

    assert status == {
        "ready": True,
        "tesseract": "/usr/bin/tesseract",
        "tessdata_dir": str(tessdata),
        "languages": ["eng", "rus"],
        "pdf_renderer": "pypdfium2",
    }


def test_check_receipt_ocr_fails_deploy_when_runtime_is_missing(monkeypatch):
    monkeypatch.setattr(
        "documents.management.commands.check_receipt_ocr.receipt_ocr_runtime_status",
        lambda: {
            "ready": False,
            "tesseract": "",
            "tessdata_dir": "",
            "languages": [],
            "pdf_renderer": "pypdfium2",
        },
    )

    with pytest.raises(CommandError, match="setup_pythonanywhere_ocr.py"):
        call_command("check_receipt_ocr", stdout=StringIO())


def test_completed_text_parse_does_not_run_ocr(monkeypatch):
    from documents import services

    result = {'status': 'parsed', 'fields': {'total': '100'}}
    monkeypatch.setattr(services, 'extract_receipt_fields', lambda *a, **kw: result)
    monkeypatch.setattr(receipt_ocr_fallback, '_ocr_content', lambda *a, **kw: pytest.fail('Text PDF must not use OCR'))
    receipt_ocr_fallback.install_receipt_ocr_fallback()
    assert services.extract_receipt_fields(b'%PDF text', mime='application/pdf') is result


def test_scan_still_uses_ocr_fallback(monkeypatch):
    from documents import services

    calls = []
    initial = {'status': 'failed', 'fields': {}}
    recognized = {'status': 'parsed', 'fields': {'total': '100'}}

    def parser(content, **kwargs):
        calls.append(kwargs['mime'])
        return recognized if kwargs['mime'] == 'text/plain' else initial

    monkeypatch.setattr(services, 'extract_receipt_fields', parser)
    monkeypatch.setattr(receipt_ocr_fallback, '_ocr_content', lambda *a, **kw: ('Recognized scanned ticket content', {}))
    monkeypatch.setattr(receipt_ocr_fallback, '_merge_ocr_result', lambda *a: recognized)
    receipt_ocr_fallback.install_receipt_ocr_fallback()
    assert services.extract_receipt_fields(b'%PDF scan', mime='application/pdf') is recognized
    assert calls == ['application/pdf', 'text/plain']
