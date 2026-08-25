from __future__ import annotations

import shutil
import subprocess
import tempfile
from copy import deepcopy
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from documents.receipt_quality_guard import apply_receipt_quality_guard
from documents.receipt_recognition_engine import _merge_dict, _repair_fields, _result_score

MAX_OCR_PAGES = 10


def _ocr_tools() -> tuple[str | None, str | None]:
    return shutil.which("tesseract"), shutil.which("pdftoppm")


def _should_ocr(result: dict) -> bool:
    if not isinstance(result, dict):
        return True
    if result.get("status") in {"error", "failed"}:
        return True
    if result.get("status") != "manual_review":
        return False
    return _result_score(result) < 70


def _tesseract_languages(binary: str) -> str:
    try:
        proc = subprocess.run(
            [binary, "--list-langs"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=4,
            check=False,
        )
        available = set(proc.stdout.split())
    except Exception:
        available = set()
    if {"rus", "eng"}.issubset(available):
        return "rus+eng"
    if "rus" in available:
        return "rus"
    return "eng"


def _ocr_one_image(tesseract: str, path: Path, *, language: str) -> str:
    try:
        proc = subprocess.run(
            [
                tesseract,
                str(path),
                "stdout",
                "-l",
                language,
                "--psm",
                "6",
                "-c",
                "preserve_interword_spaces=1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=12,
            check=False,
        )
    except Exception:
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _ocr_content(content: bytes, *, mime: str) -> tuple[str, dict]:
    tesseract, pdftoppm = _ocr_tools()
    diagnostics = {
        "ocr_tesseract_available": bool(tesseract),
        "ocr_pdftoppm_available": bool(pdftoppm),
        "ocr_pages": 0,
    }
    if not tesseract:
        return "", diagnostics
    language = _tesseract_languages(tesseract)
    diagnostics["ocr_language"] = language

    with tempfile.TemporaryDirectory(prefix="receipt-ocr-") as tmp:
        tmpdir = Path(tmp)
        if mime in {"image/jpeg", "image/png"}:
            suffix = ".jpg" if mime == "image/jpeg" else ".png"
            image_path = tmpdir / f"source{suffix}"
            image_path.write_bytes(content)
            text = _ocr_one_image(tesseract, image_path, language=language)
            diagnostics["ocr_pages"] = 1 if text.strip() else 0
            return text, diagnostics

        if mime != "application/pdf" and not content.startswith(b"%PDF"):
            return "", diagnostics
        if not pdftoppm:
            return "", diagnostics

        source = tmpdir / "source.pdf"
        source.write_bytes(content)
        page_count = MAX_OCR_PAGES
        try:
            from pypdf import PdfReader

            page_count = min(len(PdfReader(BytesIO(content), strict=False).pages), MAX_OCR_PAGES)
        except Exception:
            pass
        if page_count <= 0:
            return "", diagnostics

        prefix = tmpdir / "page"
        try:
            subprocess.run(
                [
                    pdftoppm,
                    "-f",
                    "1",
                    "-l",
                    str(page_count),
                    "-r",
                    "220",
                    "-png",
                    str(source),
                    str(prefix),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=40,
                check=False,
            )
        except Exception:
            return "", diagnostics

        chunks: list[str] = []
        for image_path in sorted(tmpdir.glob("page-*.png"))[:MAX_OCR_PAGES]:
            text = _ocr_one_image(tesseract, image_path, language=language)
            if text.strip():
                chunks.append(text)
        diagnostics["ocr_pages"] = len(chunks)
        diagnostics["ocr_truncated"] = page_count >= MAX_OCR_PAGES
        return "\n".join(chunks), diagnostics


def _merge_ocr_result(initial: dict, ocr_result: dict, ocr_text: str, diagnostics: dict) -> dict:
    initial_score = _result_score(initial)
    ocr_score = _result_score(ocr_result)
    primary, secondary = (ocr_result, initial) if ocr_score > initial_score else (initial, ocr_result)
    final = deepcopy(primary)
    primary_fields = final.get("fields") if isinstance(final.get("fields"), dict) else {}
    secondary_fields = secondary.get("fields") if isinstance(secondary.get("fields"), dict) else {}
    fields = _merge_dict(primary_fields, secondary_fields)
    final["fields"] = fields

    warnings = [str(item) for item in (final.get("warnings") or []) if str(item).strip()]
    _repair_fields(fields, ocr_text, warnings)
    final["warnings"] = list(dict.fromkeys(warnings))
    final = apply_receipt_quality_guard(final)

    raw = final.setdefault("raw", {})
    if not isinstance(raw, dict):
        raw = {}
        final["raw"] = raw
    raw.update(diagnostics)
    raw["ocr_used"] = True
    raw["ocr_result_score"] = ocr_score
    raw["pre_ocr_result_score"] = initial_score
    if final.get("status") == "parsed":
        final["confidence"] = min(max(final.get("confidence") or Decimal("0"), Decimal("0.780")), Decimal("0.920"))
    return final


def install_receipt_ocr_fallback() -> None:
    from documents import services

    if getattr(services.extract_receipt_fields, "_receipt_ocr_fallback", False):
        return
    original = services.extract_receipt_fields

    def wrapped(content: bytes, *, mime: str = "", name: str = ""):
        result = original(content, mime=mime, name=name)
        is_supported = mime in {"image/jpeg", "image/png", "application/pdf"} or content.startswith(b"%PDF")
        if not is_supported or not _should_ocr(result):
            return result

        ocr_text, diagnostics = _ocr_content(content, mime=mime or ("application/pdf" if content.startswith(b"%PDF") else ""))
        raw = result.setdefault("raw", {}) if isinstance(result, dict) else {}
        if isinstance(raw, dict):
            raw.update(diagnostics)
            raw["ocr_used"] = False
        if len(ocr_text.strip()) < 20:
            return result

        try:
            ocr_result = original(ocr_text.encode("utf-8"), mime="text/plain", name=name)
        except Exception:
            return result
        if not isinstance(ocr_result, dict):
            return result
        return _merge_ocr_result(result, ocr_result, ocr_text, diagnostics)

    wrapped._receipt_ocr_fallback = True
    services.extract_receipt_fields = wrapped
