#!/usr/bin/env python3
"""Prepare a self-contained bilingual OCR runtime for PythonAnywhere."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGET_DIR = Path(
    os.getenv("RECEIPT_OCR_TESSDATA_DIR", PROJECT_ROOT / ".runtime" / "tessdata")
).expanduser()
TESSDATA_REVISION = "87416418657359cb625c412a48b6e1d6d41c29bd"
LANGUAGES = {
    "eng": 1_000_000,
    "rus": 1_000_000,
}


def tesseract_binary() -> str:
    configured = os.getenv("RECEIPT_OCR_TESSERACT", "").strip()
    binary = shutil.which(configured or "tesseract")
    if not binary and not configured and Path("/usr/bin/tesseract").is_file():
        binary = "/usr/bin/tesseract"
    if not binary:
        raise SystemExit(
            "Tesseract не найден. Обновите system image PythonAnywhere и проверьте `which tesseract`."
        )
    return binary


def ensure_supported_version(binary: str) -> str:
    proc = subprocess.run(
        [binary, "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
        check=False,
    )
    first_line = proc.stdout.splitlines()[0] if proc.stdout else ""
    match = re.search(r"tesseract\s+(\d+)", first_line, re.IGNORECASE)
    if proc.returncode != 0 or not match or int(match.group(1)) < 4:
        raise SystemExit(
            f"Нужен Tesseract 4 или новее; найдено: {first_line or 'неизвестная версия'}. "
            "Обновите system image в настройках аккаунта PythonAnywhere."
        )
    return first_line


def download_language(language: str, minimum_size: int) -> None:
    target = TARGET_DIR / f"{language}.traineddata"
    if target.is_file() and target.stat().st_size >= minimum_size:
        return
    url = (
        "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/"
        f"{TESSDATA_REVISION}/{language}.traineddata"
    )
    request = Request(url, headers={"User-Agent": "TravelHub-Receipt-OCR/1.0"})
    temporary_path = None
    try:
        with urlopen(request, timeout=90) as response, tempfile.NamedTemporaryFile(
            dir=TARGET_DIR,
            prefix=f".{language}-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            shutil.copyfileobj(response, temporary)
        if temporary_path.stat().st_size < minimum_size:
            raise RuntimeError(f"загружен неполный файл ({temporary_path.stat().st_size} байт)")
        temporary_path.replace(target)
    except Exception as error:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()
        raise SystemExit(
            f"Не удалось загрузить {language}.traineddata: {error}. "
            f"Скачайте файл вручную из {url} в {TARGET_DIR}."
        ) from error


def main() -> int:
    binary = tesseract_binary()
    version = ensure_supported_version(binary)
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    for language, minimum_size in LANGUAGES.items():
        download_language(language, minimum_size)

    proc = subprocess.run(
        [binary, "--tessdata-dir", str(TARGET_DIR), "--list-langs"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
        check=False,
    )
    available = set(proc.stdout.split())
    missing = set(LANGUAGES) - available
    if proc.returncode != 0 or missing:
        raise SystemExit(
            f"OCR-модели не прошли проверку; отсутствуют: {', '.join(sorted(missing)) or 'неизвестно'}. "
            f"Ответ Tesseract: {proc.stdout.strip()}"
        )

    print(f"OCR готов: {version}; языки rus+eng; каталог {TARGET_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
