from decimal import Decimal

from documents.receipt_quality_guard import (
    apply_receipt_quality_guard,
    plausible_avia_location,
)


def test_partial_avia_is_reviewable_not_terminal_error():
    result = apply_receipt_quality_guard({
        "status": "error",
        "confidence": Decimal("0"),
        "fields": {
            "service_kind": "avia",
            "service_type": "Авиа",
            "passenger_name": "",
            "segments": [],
            "total": Decimal("0"),
        },
        "warnings": [],
    })

    assert result["status"] == "manual_review"
    assert result["fields"]["total"] is None
    assert result["raw"]["quality_review_required"] is True
    assert set(result["raw"]["quality_missing_fields"]) == {
        "пассажир", "маршрут", "номер рейса", "номер билета"
    }


def test_hotel_without_supplier_price_can_be_valid():
    result = apply_receipt_quality_guard({
        "status": "parsed",
        "confidence": Decimal("0.99"),
        "fields": {
            "service_kind": "hotel",
            "issuer": "Фридом",
            "passenger_name": "Бакаев Максим Юрьевич",
            "passengers": [{"name": "Бакаев Максим Юрьевич"}],
            "segments": [{"date": "02.02.2026", "endDate": "15.02.2026", "to": "Фридом"}],
            "fare": Decimal("0"),
            "taxes": Decimal("0"),
            "fees": Decimal("0"),
            "total": Decimal("0"),
        },
        "warnings": [],
    })

    assert result["status"] == "parsed"
    assert result["fields"]["total"] is None
    assert result["raw"]["quality_missing_fields"] == []


def test_multi_rail_keeps_complete_child_ticket_identity():
    def receipt(ticket, seat):
        return {
            "ticketNo": ticket,
            "passenger": "ИВАНОВ ИВАН ИВАНОВИЧ",
            "segments": [{
                "from": "МОСКВА",
                "to": "ТВЕРЬ",
                "date": "01.02.2026",
                "flightNo": "755",
                "seat": seat,
            }],
        }
    result = apply_receipt_quality_guard({
        "status": "parsed",
        "confidence": Decimal("0.99"),
        "fields": {
            "service_kind": "rail",
            "passenger_name": "ИВАНОВ ИВАН ИВАНОВИЧ, ПЕТРОВ ПЕТР ПЕТРОВИЧ",
            "segments": [{"from": "МОСКВА", "to": "ТВЕРЬ", "date": "01.02.2026", "flightNo": "755"}],
            "receipts": [receipt("70000000000001", "001"), receipt("70000000000002", "002")],
            "ticket_number": "70000000000001, 70000000000002",
            "total": Decimal("2000"),
        },
        "warnings": [],
    })

    assert result["status"] == "parsed"
    assert result["raw"]["quality_missing_fields"] == []
    assert len(result["fields"]["receipts"]) == 2


def test_incomplete_child_rail_ticket_requests_review():
    result = apply_receipt_quality_guard({
        "status": "parsed",
        "confidence": Decimal("0.95"),
        "fields": {
            "service_kind": "rail",
            "passenger_name": "ИВАНОВ ИВАН ИВАНОВИЧ",
            "segments": [{"from": "МОСКВА", "to": "ТВЕРЬ", "flightNo": "755"}],
            "receipts": [{
                "ticketNo": "70000000000001",
                "passenger": "ИВАНОВ ИВАН ИВАНОВИЧ",
                "segments": [{"from": "МОСКВА", "to": "ТВЕРЬ", "flightNo": "755", "seat": ""}],
            }],
        },
        "warnings": [],
    })

    assert result["status"] == "manual_review"
    assert "номер билета / поезд / место" in result["raw"]["quality_missing_fields"]


def test_avia_legal_entity_and_tax_number_are_not_accepted_as_route():
    result = apply_receipt_quality_guard({
        "status": "parsed",
        "confidence": Decimal("0.99"),
        "fields": {
            "service_kind": "avia",
            "passenger_name": "KIGHURADZE OTAR",
            "ticket_number": "309 6112781636",
            "segments": [{
                "from": "TRANS SERVICE GROUP, LLC",
                "to": "TIN 3907209514",
                "flightNo": "WZ1339",
            }],
        },
        "warnings": [],
    })

    assert plausible_avia_location("Nizhny Novgorod") is True
    assert plausible_avia_location("Tbilisi", "TBS") is True
    assert plausible_avia_location("Москва, Шереметьево", "SVO B") is True
    assert plausible_avia_location("Санкт-Петербург, Пулково", "LED 1") is True
    assert plausible_avia_location("TRANS SERVICE GROUP, LLC") is False
    assert plausible_avia_location("TIN 3907209514") is False
    assert result["status"] == "manual_review"
    assert "маршрут" in result["raw"]["quality_missing_fields"]
