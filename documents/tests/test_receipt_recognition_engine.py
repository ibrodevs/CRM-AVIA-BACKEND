from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from pypdf import PdfWriter

from documents.receipt_ocr_fallback import (
    _ocr_one_image,
    _render_pdf_with_pdfium,
    _should_ocr,
)
from documents.receipt_quality_guard import apply_receipt_quality_guard
from documents.receipt_recognition_engine import (
    _compact_route_recovery,
    _consistency_warnings,
    _merge_dict,
    _repair_fields,
    _result_score,
)


def test_merge_keeps_good_primary_values_and_fills_missing_fields():
    primary = {
        "service_kind": "avia",
        "passenger_name": "TEST PASSENGER",
        "ticket_number": "421 1234567890",
        "segments": [{"fromCode": "SVO", "toCode": "KUF", "flightNo": "SU1604"}],
    }
    secondary = {
        "service_kind": "avia",
        "passenger_name": "WRONG NAME",
        "reference": "ABC123",
        "segments": [
            {
                "from": "Москва",
                "fromCode": "SVO",
                "to": "Самара",
                "toCode": "KUF",
                "flightNo": "SU1604",
                "date": "29.01.2026",
                "dep": "10:10",
                "arr": "12:55",
            }
        ],
    }

    result = _merge_dict(primary, secondary)

    assert result["passenger_name"] == "TEST PASSENGER"
    assert result["reference"] == "ABC123"
    assert result["segments"][0]["date"] == "29.01.2026"
    assert result["segments"][0]["dep"] == "10:10"


def test_merge_accepts_complete_hotel_stay_from_secondary_extractor():
    primary = {
        "service_kind": "hotel",
        "passenger_name": "АЛЕКСАНДР ЧИЧЕВ",
        "segments": [],
    }
    secondary = {
        "service_kind": "hotel",
        "segments": [{
            "from": "",
            "fromCode": "",
            "to": "Лесная Сафмар",
            "toCode": "",
            "date": "28.01.2026",
            "endDate": "29.01.2026",
            "dep": "14:00",
            "arr": "12:00",
            "flightNo": "Представительский номер",
        }],
    }

    result = _merge_dict(primary, secondary)

    assert result["segments"][0]["date"] == "28.01.2026"
    assert result["segments"][0]["endDate"] == "29.01.2026"


def test_compact_airline_endorsement_recovers_route_and_flight():
    rows = _compact_route_recovery(
        "Передаточные надписи: SU-1604 SVO - KUF (UCOR) далее S7-5019 OVB - SVX"
    )

    assert rows == [
        {
            "from": "SVO",
            "fromCode": "SVO",
            "to": "KUF",
            "toCode": "KUF",
            "date": "",
            "dep": "",
            "arr": "",
            "flightNo": "SU-1604",
            "dir": "out",
        },
        {
            "from": "OVB",
            "fromCode": "OVB",
            "to": "SVX",
            "toCode": "SVX",
            "date": "",
            "dep": "",
            "arr": "",
            "flightNo": "S7-5019",
            "dir": "seg",
        },
    ]


def test_nested_passenger_ticket_is_promoted_to_top_level():
    fields = {
        "service_kind": "avia",
        "passengers": [
            {
                "name": "TEST PASSENGER",
                "ticketNo": "555 1234567890",
                "document": "4519000000",
            }
        ],
        "segments": [
            {
                "fromCode": "SVO",
                "toCode": "KUF",
                "flightNo": "SU1604",
                "date": "29.01.2026",
            }
        ],
        "fare": Decimal("25880"),
        "taxes": Decimal("693"),
        "fees": Decimal("400"),
        "total": Decimal("26973"),
    }
    warnings = []

    _repair_fields(fields, "SU-1604 SVO - KUF", warnings)

    assert fields["passenger_name"] == "TEST PASSENGER"
    assert fields["ticket_number"] == "555 1234567890"
    assert warnings == []
    guarded = apply_receipt_quality_guard(
        {"fields": fields, "status": "parsed", "confidence": Decimal("0.95"), "warnings": []}
    )
    assert guarded["status"] == "parsed"


def test_missing_fare_is_reconciled_from_supplier_total():
    fields = {
        "service_kind": "avia",
        "passenger_name": "TEST PASSENGER",
        "ticket_number": "555 1234567890",
        "segments": [{"fromCode": "SVO", "toCode": "KUF", "flightNo": "SU1604"}],
        "fare": None,
        "taxes": Decimal("693"),
        "fees": Decimal("400"),
        "total": Decimal("26973"),
    }
    warnings = []

    _repair_fields(fields, "", warnings)

    assert fields["fare"] == Decimal("25880")
    assert not warnings


def test_grouped_rail_receipts_keep_each_ticket_and_rebuild_parent_summary():
    receipts = [
        {
            "passenger": "PASSENGER ONE",
            "ticketNo": "70000000000001",
            "total": Decimal("2431.00"),
            "segments": [
                {
                    "from": "Нижний Новгород",
                    "to": "Москва",
                    "date": "02.02.2026",
                    "dep": "09:30",
                    "arr": "13:42",
                    "flightNo": "719",
                    "coach": "04",
                    "seat": "091",
                }
            ],
        },
        {
            "passenger": "PASSENGER TWO",
            "ticketNo": "70000000000002",
            "total": Decimal("2431.00"),
            "segments": [
                {
                    "from": "Нижний Новгород",
                    "to": "Москва",
                    "date": "02.02.2026",
                    "dep": "09:30",
                    "arr": "13:42",
                    "flightNo": "719",
                    "coach": "04",
                    "seat": "092",
                }
            ],
        },
    ]
    fields = {"service_kind": "rail", "receipts": receipts}
    warnings = []

    _repair_fields(fields, "", warnings)

    assert fields["receipt_count"] == 2
    assert len(fields["passengers"]) == 2
    assert fields["ticket_number"] == "70000000000001, 70000000000002"
    assert fields["total"] == Decimal("4862.00")
    assert fields["segments"][0]["seat"] == "091"
    assert fields["segments"][1]["seat"] == "092"
    assert warnings == []


def test_inconsistent_segment_is_sent_to_manual_review_instead_of_false_success():
    fields = {
        "service_kind": "avia",
        "passenger_name": "TEST PASSENGER",
        "ticket_number": "421 1234567890",
        "segments": [{"fromCode": "SVO", "toCode": "SVO", "flightNo": "SU100"}],
    }

    warnings = _consistency_warnings(fields)

    assert warnings
    assert "совпадает" in warnings[0]


def test_more_complete_candidate_has_higher_score():
    weak = {
        "fields": {"service_kind": "avia", "passenger_name": "TEST PASSENGER"},
        "status": "manual_review",
    }
    strong = {
        "fields": {
            "service_kind": "avia",
            "passenger_name": "TEST PASSENGER",
            "ticket_number": "421 1234567890",
            "currency": "RUB",
            "total": Decimal("29153"),
            "segments": [
                {
                    "fromCode": "OVB",
                    "toCode": "DME",
                    "flightNo": "S7-2508",
                    "date": "01.02.2026",
                    "dep": "06:50",
                    "arr": "07:20",
                }
            ],
        },
        "status": "parsed",
    }

    assert _result_score(strong) > _result_score(weak)


def test_ocr_runs_only_for_weak_manual_review_results():
    weak = {
        "fields": {"service_kind": "avia"},
        "status": "manual_review",
        "confidence": Decimal("0.10"),
    }
    strong = {
        "fields": {
            "service_kind": "avia",
            "passenger_name": "TEST PASSENGER",
            "ticket_number": "421 1234567890",
            "segments": [{"fromCode": "OVB", "toCode": "DME", "flightNo": "S7-2508"}],
        },
        "status": "parsed",
        "confidence": Decimal("0.90"),
    }

    assert _should_ocr(weak) is True
    assert _should_ocr(strong) is False


def test_ocr_keeps_receipt_columns_for_russian_and_english_text(monkeypatch, tmp_path):
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="Passenger Иванов", stderr="")

    monkeypatch.setattr("documents.receipt_ocr_fallback.subprocess.run", fake_run)

    result = _ocr_one_image(
        "/usr/bin/tesseract",
        tmp_path / "receipt.png",
        language="rus+eng",
    )

    assert result == "Passenger Иванов"
    assert observed["command"][-2:] == ["-c", "preserve_interword_spaces=1"]
    assert observed["command"][observed["command"].index("-l") + 1] == "rus+eng"


def test_pdfium_renders_pdf_without_system_pdftoppm(tmp_path):
    source = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=100)
    writer.write(source)

    pages = _render_pdf_with_pdfium(source.getvalue(), tmp_path, 1)

    assert len(pages) == 1
    assert pages[0].is_file()
    assert pages[0].stat().st_size > 0


def test_pythonanywhere_deploy_prepares_and_checks_ocr_runtime():
    project_root = Path(__file__).resolve().parents[2]
    pyproject = (project_root / "pyproject.toml").read_text(encoding="utf-8")
    deploy = (project_root / "scripts" / "pythonanywhere_deploy.sh").read_text(encoding="utf-8")

    assert '"pillow>=10.4"' in pyproject
    assert '"pypdfium2>=4.30"' in pyproject
    assert "python scripts/setup_pythonanywhere_ocr.py" in deploy
    assert "python manage.py check_receipt_ocr" in deploy


def test_valid_airports_win_over_supplier_details_during_candidate_merge():
    wrong = {
        "service_kind": "avia",
        "segments": [{
            "from": "TRANS SERVICE GROUP, LLC",
            "to": "TIN 3907209514",
            "flightNo": "WZ1339",
            "date": "10.10.2026",
            "dep": "09:30",
            "arr": "13:45",
        }],
    }
    correct = {
        "service_kind": "avia",
        "segments": [{
            "from": "Nizhny Novgorod",
            "to": "Tbilisi",
            "flightNo": "WZ1339",
            "date": "10.10.2026",
            "dep": "09:30",
            "arr": "13:45",
        }],
    }

    assert _merge_dict(wrong, correct)["segments"] == correct["segments"]
