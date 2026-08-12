from documents.receipt_client_pdf_finalizer import _hotel_deposit, _modern_route_codes


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
