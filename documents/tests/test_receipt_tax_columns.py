from decimal import Decimal

from documents.receipt_tax_columns_patch import column_ordered_avia_financials


def test_column_ordered_aeroflot_financial_block_extracts_tax_total_and_components():
    text = """
    РАСЧЕТ ТАРИФА:
    ТАРИФ
    СБОР/TAX
    ИТОГО ПО БИЛЕТУ
    СБОР СА
    СБОР АСБ
    ВСЕГО К ОПЛАТЕ
    : RUB79200
    : RUB823
    : RUB80023
    : RUB0
    : RUB550
    : RUB80573
    ЭКВИВ. В ВАЛ. ПЛ: RUB79200
    RI703RUB YR120RUB
    (включая НДС 5% - RUB6.19)
    """

    fields = column_ordered_avia_financials(text)

    assert fields["fare"] == Decimal("79200")
    assert fields["taxes"] == Decimal("823")
    assert fields["fees"] == Decimal("550")
    assert fields["total"] == Decimal("80573")
    assert fields["currency"] == "RUB"
    assert fields["tax_breakdown"] == [
        {"code": "RI", "label": "RI", "amount": "703", "currency": "RUB"},
        {"code": "YR", "label": "YR", "amount": "120", "currency": "RUB"},
    ]
    assert fields["fee_breakdown"] == [
        {"code": "SA", "label": "Сбор СА", "amount": "0", "currency": "RUB"},
        {"code": "ASB", "label": "Сбор АСБ", "amount": "550", "currency": "RUB"},
    ]


def test_column_parser_does_not_replace_normal_non_column_text():
    text = "ТАРИФ: RUB1000\nСБОР/TAX: RUB100\nВСЕГО К ОПЛАТЕ: RUB1100"

    assert column_ordered_avia_financials(text) == {}
