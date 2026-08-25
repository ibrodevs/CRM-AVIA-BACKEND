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
