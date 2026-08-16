from documents.receipt_client_pdf_finalizer import _hotel_deposit, _modern_route_codes
from documents.receipt_metadata import receipt_verified_data


def test_modern_fare_calculation_extracts_codes_joined_to_amounts():
    text = (
        "Расчет тарифа/Fare calculation "
        "KZN SU MOW9180SU CSY6800RUB15980END "
        "Тариф/Fare 15980.00РУБ"
    )

    assert _modern_route_codes(text) == ["KZN", "MOW", "CSY"]


def test_partner_hotel_accepts_one_line_deposit_from_pdfminer():
    lines = [
        "Meal type",
        "Breakfast included",
        "Deposit",
        "The guest can be also asked to provide a credit card or cash deposit as a guarantee.",
        "Deposit",
        "1000 CNY per room for the night",
        "Agency is not responsible for the quality of services provided by the hotel.",
        "GPS 22.726416 114.06274",
    ]

    assert _hotel_deposit(lines) == "1000 CNY per room for the night"


def test_partner_hotel_accepts_split_deposit_columns():
    lines = ["Deposit", "1000", "CNY", "per room for the night", "GPS 22.726416 114.06274"]

    assert _hotel_deposit(lines) == "1000 CNY per room for the night"


def test_receipt_metadata_keeps_carry_on_separate_from_checked_baggage():
    verified = receipt_verified_data({
        "service_kind": "avia",
        "segments": [{"cls": "ECONOMY", "baggage": "1PC", "hand_baggage": "8KG"}],
    }, parser_status="parsed")

    assert verified["legs"][0]["baggage"] == "1PC"
    assert verified["legs"][0]["handBaggage"] == "8KG"
    assert verified["legs"][0]["cabin"] == "ECONOMY"


def test_receipt_metadata_does_not_guess_cabin_from_one_letter_booking_code():
    verified = receipt_verified_data({
        "service_kind": "avia",
        "segments": [{"cls": "P", "cabin": ""}],
    }, parser_status="parsed")

    assert verified["legs"][0]["cabin"] == ""
