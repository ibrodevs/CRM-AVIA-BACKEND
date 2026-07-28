from decimal import Decimal

from documents.receipt_parser_patch_safe import _rail


def test_rzd_page_parses_when_pdf_joins_time_and_date():
    text = """
    ЭЛЕКТРОННЫЙ БИЛЕТ. КОНТРОЛЬНЫЙ КУПОН
    ПОЕЗД ВАГОН МЕСТО 755 755 02 02 048 048
    № 70 713 634 630 285
    06:40 06:4028.01.2026 ср
    Санкт-Петербург-Главн.
    10:51 10:5128.01.2026 ср
    Москва Октябрьская
    ПАСПОРТ РФ 4003617920 12.02.1981 RUS М
    НАГОРНЫЙ КОНСТАНТИН ДМИТРИЕВИЧ
    Посадка в поезд осуществляется
    Заказ: 70713634630263
    БИЗНЕС КЛАСС 1С
    Итого Вкл. НДС 11 587,80 ₽
    """

    fields = _rail(text)

    assert fields is not None
    assert fields["passenger_name"] == "НАГОРНЫЙ КОНСТАНТИН ДМИТРИЕВИЧ"
    assert fields["total"] == Decimal("11587.80")
    assert fields["segments"] == [
        {
            "from": "Санкт-Петербург-Главн.",
            "fromCode": "",
            "to": "Москва Октябрьская",
            "toCode": "",
            "date": "28.01.2026",
            "dep": "06:40",
            "arr": "10:51",
            "endDate": "28.01.2026",
            "flightNo": "755",
            "coach": "02",
            "seat": "048",
            "dir": "out",
        }
    ]
