"""Распознавание документа личности для карточки физлица.

OCR здесь — альтернатива ручному заполнению анкеты, а не отдельная функция
в конце формы: оператор загружает скан паспорта или ID-карты, backend
извлекает текст и разбирает машиночитаемую зону (MRZ, ICAO 9303), после чего
фронт подставляет готовые поля в карточку.

MRZ выбран основным источником осознанно: это фиксированный формат с
контрольными цифрами, поэтому результат проверяем, а не «на глаз». Если MRZ
нет или он не читается, применяется текстовый разбор (кириллические паспорта
СНГ), и тогда результат помечается пониженной уверенностью.
"""

from __future__ import annotations

import re
from datetime import date
from io import BytesIO

MRZ_TD3_LINE = 44
MRZ_TD2_LINE = 36
MRZ_TD1_LINE = 30

# Код страны MRZ → название гражданства в справочнике карточки физлица.
MRZ_COUNTRY_NAMES = {
    "KGZ": "Кыргызстан", "KAZ": "Казахстан", "RUS": "Россия", "UZB": "Узбекистан",
    "TJK": "Таджикистан", "TKM": "Туркменистан", "AZE": "Азербайджан", "ARM": "Армения",
    "BLR": "Беларусь", "GEO": "Грузия", "MDA": "Молдова", "UKR": "Украина",
    "TUR": "Турция", "DEU": "Германия", "CHN": "Китай", "ARE": "ОАЭ",
}

DOCUMENT_KIND_BY_MRZ = {
    "P": "foreign_passport",
    "I": "id_card",
    "A": "id_card",
    "C": "id_card",
    "V": "visa",
}

DOCUMENT_LABELS = {
    "foreign_passport": "Загранпаспорт",
    "national_passport": "Общегражданский паспорт",
    "id_card": "ID-карта",
    "birth_certificate": "Свидетельство о рождении",
    "visa": "Виза",
    "other": "Другое",
}


def _mrz_check_digit(value: str) -> int:
    weights = (7, 3, 1)
    total = 0
    for index, char in enumerate(value):
        if char.isdigit():
            digit = int(char)
        elif char.isalpha():
            digit = ord(char.upper()) - 55
        else:
            digit = 0
        total += digit * weights[index % 3]
    return total % 10


def _mrz_date(value: str, *, future: bool) -> str:
    """YYMMDD из MRZ → ISO. Век выбирается по смыслу поля."""
    if not re.fullmatch(r"\d{6}", value or ""):
        return ""
    year, month, day = int(value[:2]), int(value[2:4]), int(value[4:6])
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    current = date.today().year % 100
    if future:
        century = 2000 if year <= current + 50 else 1900
    else:
        century = 1900 if year > current else 2000
    try:
        return date(century + year, month, day).isoformat()
    except ValueError:
        return ""


def _mrz_names(field: str) -> tuple[str, str, str]:
    surname, _, given = field.partition("<<")
    surname = surname.replace("<", " ").strip()
    parts = [part for part in given.replace("<", " ").split() if part]
    return surname, (parts[0] if parts else ""), " ".join(parts[1:])


def _mrz_lines(text: str) -> list[str]:
    """Строки-кандидаты MRZ: только допустимые символы и типовая длина."""
    lines = []
    for raw in (text or "").splitlines():
        candidate = re.sub(r"[^A-Z0-9<]", "", raw.upper().replace(" ", ""))
        if len(candidate) in {MRZ_TD1_LINE, MRZ_TD2_LINE, MRZ_TD3_LINE} and "<" in candidate:
            lines.append(candidate)
    return lines


def _parse_td3(first: str, second: str) -> dict | None:
    if len(first) != MRZ_TD3_LINE or len(second) != MRZ_TD3_LINE:
        return None
    surname, given, middle = _mrz_names(first[5:])
    number = second[0:9].replace("<", "")
    checks = {
        "number": _mrz_check_digit(second[0:9]) == int(second[9]) if second[9].isdigit() else False,
        "birth": _mrz_check_digit(second[13:19]) == int(second[19]) if second[19].isdigit() else False,
        "expiry": _mrz_check_digit(second[21:27]) == int(second[27]) if second[27].isdigit() else False,
    }
    return {
        "format": "TD3",
        "document_kind": DOCUMENT_KIND_BY_MRZ.get(first[0], "foreign_passport"),
        "issuing_country": first[2:5].replace("<", ""),
        "surname": surname, "given_name": given, "middle_name": middle,
        "number": number,
        "nationality": second[10:13].replace("<", ""),
        "birth_date": _mrz_date(second[13:19], future=False),
        "sex": second[20],
        "expiry_date": _mrz_date(second[21:27], future=True),
        "checks": checks,
    }


def _parse_td2(first: str, second: str) -> dict | None:
    if len(first) != MRZ_TD2_LINE or len(second) != MRZ_TD2_LINE:
        return None
    surname, given, middle = _mrz_names(first[5:])
    checks = {
        "number": _mrz_check_digit(second[0:9]) == int(second[9]) if second[9].isdigit() else False,
        "birth": _mrz_check_digit(second[13:19]) == int(second[19]) if second[19].isdigit() else False,
        "expiry": _mrz_check_digit(second[21:27]) == int(second[27]) if second[27].isdigit() else False,
    }
    return {
        "format": "TD2",
        "document_kind": DOCUMENT_KIND_BY_MRZ.get(first[0], "id_card"),
        "issuing_country": first[2:5].replace("<", ""),
        "surname": surname, "given_name": given, "middle_name": middle,
        "number": second[0:9].replace("<", ""),
        "nationality": second[10:13].replace("<", ""),
        "birth_date": _mrz_date(second[13:19], future=False),
        "sex": second[20],
        "expiry_date": _mrz_date(second[21:27], future=True),
        "checks": checks,
    }


def _parse_td1(first: str, second: str, third: str) -> dict | None:
    if len({len(first), len(second), len(third)}) != 1 or len(first) != MRZ_TD1_LINE:
        return None
    surname, given, middle = _mrz_names(third)
    checks = {
        "number": _mrz_check_digit(first[5:14]) == int(first[14]) if first[14].isdigit() else False,
        "birth": _mrz_check_digit(second[0:6]) == int(second[6]) if second[6].isdigit() else False,
        "expiry": _mrz_check_digit(second[8:14]) == int(second[14]) if second[14].isdigit() else False,
    }
    return {
        "format": "TD1",
        "document_kind": DOCUMENT_KIND_BY_MRZ.get(first[0], "id_card"),
        "issuing_country": first[2:5].replace("<", ""),
        "surname": surname, "given_name": given, "middle_name": middle,
        "number": first[5:14].replace("<", ""),
        "nationality": second[15:18].replace("<", ""),
        "birth_date": _mrz_date(second[0:6], future=False),
        "sex": second[7],
        "expiry_date": _mrz_date(second[8:14], future=True),
        "checks": checks,
    }


def parse_mrz(text: str) -> dict | None:
    """Разбирает первую найденную машиночитаемую зону TD1/TD2/TD3."""
    lines = _mrz_lines(text)
    for index in range(len(lines) - 2):
        parsed = _parse_td1(lines[index], lines[index + 1], lines[index + 2])
        if parsed:
            return parsed
    for index in range(len(lines) - 1):
        parsed = _parse_td3(lines[index], lines[index + 1]) or _parse_td2(lines[index], lines[index + 1])
        if parsed:
            return parsed
    return None


CYRILLIC_NAME = r"[А-ЯЁ][А-ЯЁа-яё-]+"


def parse_cyrillic_document(text: str) -> dict:
    """Запасной разбор кириллических паспортов, когда MRZ не читается."""
    flat = re.sub(r"[ \t]+", " ", (text or "").upper())
    result: dict = {}
    number = re.search(r"\b([A-ZА-Я]{2}\s?\d{6,9})\b", flat) or re.search(r"\b(\d{2}\s?\d{2}\s?\d{6})\b", flat)
    if number:
        result["number"] = re.sub(r"\s+", "", number.group(1))
    dates = re.findall(r"\b(\d{2})[.\-/](\d{2})[.\-/](\d{4})\b", flat)
    isos = []
    for day, month, year in dates:
        try:
            isos.append(date(int(year), int(month), int(day)).isoformat())
        except ValueError:
            continue
    isos.sort()
    today = date.today().isoformat()
    past = [value for value in isos if value < today]
    future = [value for value in isos if value >= today]
    if past:
        result["birth_date"] = past[0]
    if future:
        result["expiry_date"] = future[-1]
    names = re.findall(rf"\b{CYRILLIC_NAME}\b", (text or ""))
    stop = {"ПАСПОРТ", "РОССИЙСКОЙ", "ФЕДЕРАЦИИ", "КЫРГЫЗСКОЙ", "РЕСПУБЛИКИ", "ГРАЖДАНИНА", "ВЫДАН"}
    candidates = [name for name in names if name.upper() not in stop and len(name) > 2]
    if len(candidates) >= 2:
        result["surname"] = candidates[0]
        result["given_name"] = candidates[1]
        if len(candidates) >= 3:
            result["middle_name"] = candidates[2]
    if "ПАСПОРТ" in flat:
        result["document_kind"] = "national_passport"
    return result


def _pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content), strict=False)
        return "\n".join((page.extract_text() or "") for page in reader.pages[:5])
    except Exception:
        return ""


def _document_text(content: bytes, *, mime: str) -> tuple[str, dict]:
    """Текст документа: слой PDF, затем OCR тем же движком, что и квитанции."""
    diagnostics: dict = {}
    text = ""
    if mime == "application/pdf" or content.startswith(b"%PDF"):
        text = _pdf_text(content)
        diagnostics["pdf_text_layer"] = bool(text.strip())
    if len(_mrz_lines(text)) < 2:
        try:
            from documents.receipt_ocr_fallback import _ocr_content

            ocr_text, ocr_diagnostics = _ocr_content(content, mime=mime)
            diagnostics.update(ocr_diagnostics)
            if ocr_text.strip():
                text = f"{text}\n{ocr_text}" if text.strip() else ocr_text
        except Exception as error:  # pragma: no cover - зависит от окружения
            diagnostics["ocr_error"] = str(error)
    return text, diagnostics


def recognize_person_document(content: bytes, *, mime: str) -> dict:
    """Возвращает поля карточки физлица, распознанные из документа."""
    text, diagnostics = _document_text(content, mime=mime)
    mrz = parse_mrz(text)
    if mrz:
        confirmed = sum(1 for value in mrz["checks"].values() if value)
        fields = {
            "surname": mrz["surname"],
            "given_name": mrz["given_name"],
            "middle_name": mrz["middle_name"],
            "latin_surname": mrz["surname"],
            "latin_given_name": mrz["given_name"],
            "latin_middle_name": mrz["middle_name"],
            "birth_date": mrz["birth_date"],
            "sex": {"M": "Мужской", "F": "Женский"}.get(mrz["sex"], ""),
            "document_kind": mrz["document_kind"],
            "document_label": DOCUMENT_LABELS.get(mrz["document_kind"], "Другое"),
            "number": mrz["number"],
            "expiry_date": mrz["expiry_date"],
            "issuing_country": mrz["issuing_country"],
            "nationality": mrz["nationality"],
            "citizenship": MRZ_COUNTRY_NAMES.get(mrz["nationality"] or mrz["issuing_country"], ""),
        }
        return {
            "status": "recognized",
            "source": f"mrz_{mrz['format'].lower()}",
            "confidence": 60 + confirmed * 13,
            "checks": mrz["checks"],
            "fields": {key: value for key, value in fields.items() if value},
            "diagnostics": diagnostics,
        }

    fallback = parse_cyrillic_document(text)
    if fallback:
        fallback.setdefault("document_kind", "national_passport")
        fallback["document_label"] = DOCUMENT_LABELS.get(fallback["document_kind"], "Другое")
        return {
            "status": "partial",
            "source": "text",
            "confidence": 45,
            "checks": {},
            "fields": fallback,
            "diagnostics": diagnostics,
        }

    return {
        "status": "manual_required",
        "source": "none",
        "confidence": 0,
        "checks": {},
        "fields": {},
        "diagnostics": diagnostics,
    }
