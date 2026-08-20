import json
from decimal import Decimal

import pytest

from documents.receipt_structural_hardening import (
    _clean_passenger_name,
    _fare_calculation_codes,
    _sync_raw,
    harden_avia_fields,
)


@pytest.mark.parametrize(
    ("fare_calculation", "expected"),
    [
        ("HMA UT TJMRUBEND", ["HMA", "TJM"]),
        ("KZN SU MOW9180SU CSY6800RUB15980END", ["KZN", "MOW", "CSY"]),
        ("FRU YK OSS12500KGS12500END", ["FRU", "OSS"]),
    ],
)
def test_route_codes_do_not_depend_on_prices_or_carrier(fare_calculation, expected):
    text = f"Расчет тарифа/Fare calculation {fare_calculation} Тариф/Fare"

    assert _fare_calculation_codes(text) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("ПЕТРОВ/ИВАН И Г-Н", "ПЕТРОВ ИВАН И"),
        ("SMITH/ANNA MRS", "SMITH ANNA"),
        ("ИВАНОВА/МАРИЯ Г-ЖА", "ИВАНОВА МАРИЯ"),
    ],
)
def test_passenger_titles_are_not_saved_as_name_parts(title, expected):
    assert _clean_passenger_name(title) == expected


@pytest.mark.parametrize(
    ("issuer", "passenger", "fare_calculation", "fare", "total", "tax_rows", "expected_codes"),
    [
        (
            "ПАО АВИАКОМПАНИЯ ЮТЭЙР",
            "ПЕТРОВ/ИВАН И Г-Н",
            "HMA UT TJMRUBEND",
            "10300.00",
            "11722",
            "YQ1000РУБ YR4220РУБ",
            ("HMA", "TJM"),
        ),
        (
            "ООО СЕВЕРНЫЙ ВЕТЕР",
            "SMITH/ANNA MRS",
            "KZN N4 AER15500RUBEND",
            "15500",
            "16840",
            "YQ840РУБ ZZ500РУБ",
            ("KZN", "AER"),
        ),
    ],
)
def test_same_visual_template_uses_values_from_each_document(
    issuer,
    passenger,
    fare_calculation,
    fare,
    total,
    tax_rows,
    expected_codes,
):
    text = f"""
    Electronic ticket (itinerary/receipt)
    Номер билета Ticket number 298 6000000001
    Выдан от/Issued by {issuer}
    Дата выдачи Date of issue 26 ноября 2024
    Рейс/Flight
    Расчет тарифа/Fare calculation {fare_calculation}
    Тариф/Fare {fare}РУБ
    Сбор/Tax/fee/charge {tax_rows}
    Итого/Total {total}РУБ
    """
    fields = {
        "service_kind": "avia",
        "issuer": "АЭРОФЛОТ",
        "passenger_name": passenger,
        "passengers": [{"name": passenger}],
        "fare": Decimal(fare),
        "taxes": Decimal("0"),
        "fees": Decimal("0"),
        "total": Decimal(fare),
        "segments": [{"from": "Город А", "to": "Город Б", "fromCode": "", "toCode": ""}],
    }

    changed = harden_avia_fields(fields, text)

    assert fields["issuer"] == issuer
    assert fields["passenger_name"] not in {passenger, ""}
    assert fields["passengers"][0]["name"] == fields["passenger_name"]
    assert (fields["segments"][0]["fromCode"], fields["segments"][0]["toCode"]) == expected_codes
    assert fields["fare"] == Decimal(fare)
    assert fields["total"] == Decimal(total)
    assert fields["taxes"] == Decimal(total) - Decimal(fare)
    assert sum(Decimal(row["amount"]) for row in fields["tax_breakdown"]) == fields["taxes"]
    assert {"issuer", "passenger_name", "segments", "finances"} <= changed


def test_hand_baggage_uses_the_ticket_cabin_rule():
    text = """
    Electronic ticket Номер билета Ticket number Рейс/Flight Тариф/Fare
    Нормы провоза ручной клади: класс Эконом - одно место весом не более 10 кг;
    класс Бизнес - одно место весом не более 15 кг.
    Габариты одного места ручной клади не должны превышать: 55 см в длину,
    40 см в ширину, 25 см в высоту.
    """
    fields = {
        "service_kind": "avia",
        "booking_class": "Бизнес",
        "segments": [{"from": "Москва", "to": "Сочи", "cabin": "Бизнес"}],
    }

    harden_avia_fields(fields, text)

    assert fields["hand_baggage"] == "1 место до 15 кг (55×40×25 см)"
    assert fields["segments"][0]["handBaggage"] == fields["hand_baggage"]


def test_diagnostics_are_json_safe_for_receipt_import_storage():
    result = {"raw": {}}
    fields = {
        "fare": Decimal("10300.00"),
        "taxes": Decimal("1422.00"),
        "total": Decimal("11722.00"),
        "tax_breakdown": [{"code": "YR", "amount": Decimal("422.00")}],
    }

    _sync_raw(result, fields, {"finances"})

    json.dumps(result["raw"])
