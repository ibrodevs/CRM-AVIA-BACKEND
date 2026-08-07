from decimal import Decimal

from documents.receipt_recognition_performance import strong_receipt_result


def test_complete_avia_result_uses_fast_path():
    result = {
        "status": "parsed",
        "confidence": Decimal("0.990"),
        "warnings": [],
        "fields": {
            "service_kind": "avia",
            "passenger_name": "TEST PASSENGER",
            "ticket_number": "421 1234567890",
            "total": Decimal("25000"),
            "segments": [
                {
                    "fromCode": "SVO",
                    "toCode": "KUF",
                    "date": "29.01.2026",
                    "flightNo": "SU-1604",
                }
            ],
        },
    }
    assert strong_receipt_result(result) is True


def test_complete_group_rail_result_uses_fast_path():
    receipt = {
        "passenger": "ИВАНОВ ИВАН ИВАНОВИЧ",
        "ticketNo": "12345678901234",
        "total": Decimal("2431.00"),
        "legs": [
            {
                "from": "НИЖНИЙ НОВГОРОД",
                "to": "МОСКВА",
                "date": "02.02.2026",
                "flightNo": "719",
                "coach": "04",
                "seat": "091",
            }
        ],
    }
    result = {
        "status": "parsed",
        "confidence": Decimal("0.995"),
        "warnings": [],
        "fields": {
            "service_kind": "rail",
            "receipts": [receipt, {**receipt, "ticketNo": "12345678901235", "passenger": "ПЕТРОВ ПЕТР ПЕТРОВИЧ", "legs": [{**receipt["legs"][0], "seat": "092"}]}],
            "source_coupon_pages": 2,
        },
    }
    assert strong_receipt_result(result) is True


def test_incomplete_or_suspicious_result_still_uses_deep_recognition():
    incomplete = {
        "status": "parsed",
        "confidence": Decimal("0.990"),
        "warnings": [],
        "fields": {
            "service_kind": "avia",
            "passenger_name": "TEST PASSENGER",
            "ticket_number": "421 1234567890",
            "total": Decimal("25000"),
            "segments": [{"fromCode": "SVO", "toCode": "KUF"}],
        },
    }
    suspicious = {
        **incomplete,
        "fields": {
            **incomplete["fields"],
            "segments": [{"fromCode": "SVO", "toCode": "KUF", "date": "29.01.2026", "flightNo": "SU-1604"}],
        },
        "warnings": ["Нужно проверить стоимость"],
    }
    assert strong_receipt_result(incomplete) is False
    assert strong_receipt_result(suspicious) is False
