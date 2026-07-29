from decimal import Decimal

from documents.receipt_parser_patch_safe import _hotel_details, _rail, _transfer_details
from documents.services import (
    _avia_segments,
    _fare_breakdown,
    _fee_breakdown,
    _rail_cost_components,
    _rossiya_itinerary_fields,
    _s7_compact_fields,
    _tax_breakdown,
    _transfer_segments,
    extract_receipt_fields,
)


def test_central_ppk_coupon_splits_ticket_and_reserved_seat_costs():
    text = """
    Тариф (билет,плацкарта), Руб
    Fare(ticket,reservation), RUB
    875.00/0.00
    Цена, Руб
    875.00
    Итого, Руб
    875.00
    """

    assert _rail_cost_components(text, Decimal("875.00")) == {
        "ticketCost": Decimal("875.00"),
        "reservedSeatCost": Decimal("0.00"),
        "agencyServiceFee": Decimal("0"),
        "additionalFees": Decimal("0"),
    }


def test_rail_cost_components_fall_back_to_total_when_coupon_has_no_split():
    assert _rail_cost_components("Итого, Руб 1175.00", Decimal("1175.00")) == {
        "ticketCost": Decimal("1175.00"),
        "reservedSeatCost": Decimal("0"),
        "agencyServiceFee": Decimal("0"),
        "additionalFees": Decimal("0"),
    }


def test_fare_calculation_is_split_into_route_components_and_roe():
    text = (
        "Расчет тарифа/Fare calculation "
        "EVN WZ GOJ131.11 NUC131.11END ROE0.915261"
    )

    assert _fare_breakdown(text) == [
        {
            "code": "WZ",
            "label": "EVN → GOJ",
            "amount": "131.11",
            "currency": "NUC",
            "from": "EVN",
            "to": "GOJ",
            "carrier": "WZ",
        },
        {
            "code": "ROE",
            "label": "Курс пересчёта",
            "amount": "0.915261",
            "currency": "",
        },
    ]


def test_fare_calculation_uses_previous_destination_for_next_component():
    text = "Fare calculation SVO SU LED100.00 SU MMK50.00NUC150.00END"

    rows = _fare_breakdown(text)

    assert [row["label"] for row in rows] == ["SVO → LED", "LED → MMK"]
    assert [row["amount"] for row in rows] == ["100.00", "50.00"]


def test_fare_and_rate_values_do_not_leak_into_tax_breakdown():
    text = """
    Расчет тарифа/Fare calculation EVN WZ GOJ131.11NUC131.11END ROE0.915261
    Тариф/Fare 12060РУБ
    Эквив. тарифа/Equivalent fare paid
    Сбор/Tax/fee/charge ZZ185РУБ KC2714РУБ AM2400РУБ SA250РУБ XQ1090РУБ
    Итого/Total 18699РУБ
    """

    assert [row["code"] for row in _tax_breakdown(text)] == ["ZZ", "KC", "AM", "SA", "XQ"]


def test_fare_calculation_and_all_tax_components_reconcile_ticket_total():
    text = """
    Маршрутная квитанция пассажира
    Пассажир: SARGSIAN SIRANUSH B
    PNR: 02K4NC
    Маршрут: EVN → GOJ
    Дата отправления: 24.09.2024
    Рейс WZ1348
    Расчет тарифа/Fare calculation EVN WZ GOJ131.11NUC131.11END ROE0.915261
    Тариф/Fare 12060РУБ
    Сбор/Tax/fee/charge ZZ185РУБ KC2714РУБ AM2400РУБ SA250РУБ XQ1090РУБ
    Итого/Total 18699РУБ
    """

    fields = extract_receipt_fields(
        text.encode(),
        mime="text/plain",
        name="wz-receipt.txt",
    )["fields"]

    assert fields["fare"] == Decimal("12060")
    assert fields["taxes"] == Decimal("6639")
    assert fields["total"] == Decimal("18699")
    assert [row["code"] for row in fields["fare_breakdown"]] == ["WZ", "ROE"]


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
    755АА 28.01.2026 06:40 02С 048 САНКТ-ПЕТЕРБУРГ-ГЛАВН. - МОСКВА ОКТЯБРЬСКАЯ
    ПН4003617920 НАГОРНЫЙ-КД 120281
    Заказ: 70713634630263
    Перевозчик: ДОСС РЖД / ДОСС ИНН 7708503727
    БИЗНЕС КЛАСС 1С 1С
    Оплата наличными Билет Плацкарта НДС 0% НДС 22%
    1 436,30 ₽ 10 151,50 ₽ 0,00 ₽ 581,72 ₽
    Итого Вкл. НДС 11 587,80 ₽
    """

    fields = _rail(text)

    assert fields is not None
    assert fields["passenger_name"] == "НАГОРНЫЙ КОНСТАНТИН ДМИТРИЕВИЧ"
    assert fields["total"] == Decimal("11587.80")
    assert fields["ticketCost"] == Decimal("1436.30")
    assert fields["reservedSeatCost"] == Decimal("10151.50")
    assert fields["issuer"] == "ДОСС РЖД / ДОСС"
    assert fields["booking_class"] == "1С"
    assert fields["segments"][0] == {
        "from": "САНКТ-ПЕТЕРБУРГ-ГЛАВН.",
        "fromCode": "",
        "to": "МОСКВА ОКТЯБРЬСКАЯ",
        "toCode": "",
        "date": "28.01.2026",
        "dep": "06:40",
        "arr": "10:51",
        "endDate": "28.01.2026",
        "flightNo": "755АА",
        "coach": "02",
        "seat": "048",
        "dir": "out",
    }


def test_rzd_card_payment_layout_deduplicates_name_and_splits_full_cost():
    text = """
    ЭЛЕКТРОННЫЙ БИЛЕТ. КОНТРОЛЬНЫЙ КУПОН
    ПОЕЗД ВАГОН МЕСТО 021 021 11 11 032 032
    № 71 853 989 048 044
    01:05 01:05 15.03.2025
    09:37 09:37 15.03.2025
    ПАСПОРТ РФ 4023479540 12.05.2003 RUS М
    КЛЮШНИЧЕНКО АЛЕКСАНДР КЛЮШНИЧЕНКО АЛЕКСАНДР ЮРЬЕВИЧ ЮРЬЕВИЧ
    Посадка в поезд осуществляется
    021АА 15.03.2025 01:05 11К 032 САНКТ-ПЕТЕРБУРГ-ГЛАВНЫЙ - МОСКВА ОКТЯБРЬСКАЯ
    ПН4023479540 КЛЮШНИЧЕНКО-АЮ 120503
    Заказ: 71853989048011
    Перевозчик: ТВЕРСКОЙ ЭКСПР / ТВЕРСК ИНН 7705506536
    2Ф 2Ф Тариф: Полный
    Оплата банковской картой ****1314
    Билет Плацкарта НДС 0% НДС 20%
    1 796,40 ₽ 2 068,50 ₽ 0,00 ₽ 141,67 ₽
    Итого Вкл. НДС 3 864,90 ₽
    """

    fields = _rail(text)

    assert fields is not None
    assert fields["passenger_name"] == "КЛЮШНИЧЕНКО АЛЕКСАНДР ЮРЬЕВИЧ"
    assert fields["ticket_number"] == "71853989048044"
    assert fields["ticketCost"] == Decimal("1796.40")
    assert fields["reservedSeatCost"] == Decimal("2068.50")
    assert fields["total"] == Decimal("3864.90")
    assert fields["costBreakdown"] == [
        {"code": "TICKET", "label": "Билет", "amount": "1796.40", "currency": "RUB"},
        {"code": "RESERVED_SEAT", "label": "Плацкарта", "amount": "2068.50", "currency": "RUB"},
    ]
    assert fields["includedTaxBreakdown"] == [
        {"code": "VAT0", "label": "НДС 0% (включён)", "amount": "0.00", "currency": "RUB"},
        {"code": "VAT", "label": "НДС (включён)", "amount": "141.67", "currency": "RUB"},
    ]
    assert fields["segments"][0]["coach"] == "11"
    assert fields["segments"][0]["seat"] == "032"


def test_rzd_pages_override_incorrect_generic_avia_classification(monkeypatch):
    from documents import receipt_parser_patch_safe, services

    first_page = """
    ЭЛЕКТРОННЫЙ БИЛЕТ. КОНТРОЛЬНЫЙ КУПОН
    ПОЕЗД ВАГОН МЕСТО 755 755 02 02 046 046
    № 70 713 634 630 263
    06:40 06:40 28.01.2026
    10:51 10:51 28.01.2026
    ПАСПОРТ РФ 4024774038 11.03.1979 RUS М
    ЧИРКОВ ТАРАС АЛЕКСАНДРОВИЧ
    Посадка в поезд осуществляется
    755АА 28.01.2026 06:40 02С 046 САНКТ-ПЕТЕРБУРГ-ГЛАВН. - МОСКВА ОКТЯБРЬСКАЯ
    ПН4024774038 ЧИРКОВ-ТА 110379
    Заказ: 70713634630263
    Перевозчик: ДОСС РЖД / ДОСС ИНН 7708503727
    БИЗНЕС КЛАСС 1С 1С
    Оплата наличными Билет Плацкарта НДС 0% НДС 22%
    1 436,30 ₽ 9 521,90 ₽ 0,00 ₽ 581,72 ₽
    Итого Вкл. НДС 10 958,20 ₽
    """
    second_page = (
        first_page
        .replace("046 046", "047 047")
        .replace("630 263", "630 274")
        .replace("4024774038", "4010265111")
        .replace("11.03.1979", "28.03.1991")
        .replace("ЧИРКОВ ТАРАС АЛЕКСАНДРОВИЧ", "ЧИЧЕВ АЛЕКСАНДР СЕРГЕЕВИЧ")
        .replace("046 САНКТ", "047 САНКТ")
        .replace("ЧИРКОВ-ТА 110379", "ЧИЧЕВ-АС 280391")
    )

    def incorrectly_classified(*_args, **_kwargs):
        return {
            "status": "manual_review",
            "confidence": Decimal("0.200"),
            "fields": {"service_kind": "avia", "service_type": "Авиа"},
            "raw": {"service_kind": "avia", "service_type": "Авиа"},
            "warnings": ["Тип требует проверки."],
        }

    monkeypatch.setattr(services, "extract_receipt_fields", incorrectly_classified)
    monkeypatch.setattr(receipt_parser_patch_safe, "_pages", lambda _content: [first_page, second_page])
    receipt_parser_patch_safe.install_receipt_parser_patch()

    result = services.extract_receipt_fields(b"%PDF group", mime="application/pdf", name="group.pdf")

    assert result["status"] == "parsed"
    assert result["fields"]["service_kind"] == "rail"
    assert result["fields"]["service_type"] == "ЖД"
    assert result["fields"]["receipt_count"] == 2
    assert len(result["fields"]["receipts"]) == 2
    assert result["fields"]["passenger_name"] == (
        "ЧИРКОВ ТАРАС АЛЕКСАНДРОВИЧ, ЧИЧЕВ АЛЕКСАНДР СЕРГЕЕВИЧ"
    )
    assert result["fields"]["total"] == Decimal("21916.40")


def test_rzd_page_parses_arbitrary_route_from_control_line():
    text = """
    ЭЛЕКТРОННЫЙ БИЛЕТ. КОНТРОЛЬНЫЙ КУПОН
    ПОЕЗД ВАГОН МЕСТО
    124 124 05 05 012 012
    № 70 763 636 259 211
    06:18 06:18 30.01.2026
    13:02 13:02 30.01.2026
    ПАСПОРТ РФ 7515715595 10.11.1995 RUS М
    БОРИСЕНКОВ АЛЕКСАНДР ВЛАДИМИРОВИЧ
    Посадка в поезд осуществляется
    124ВА 30.01.2026 06:18 05К 012 ПЕНЗА 1 - САМАРА
    ПН7515715595 БОРИСЕНКОВ-АВ 101195
    Заказ: 70763636259211
    Перевозчик: ФПК ЗАП-СИБИРСКИЙ / ФПК ИНН 7708709686
    2Ш 2Ш
    Оплата наличными Билет Плацкарта НДС 0% НДС 22%
    1 172,50 ₽ 1 016,20 ₽ 0,00 ₽ 74,30 ₽
    Итого Вкл. НДС 2 188,70 ₽
    """

    fields = _rail(text)

    assert fields is not None
    assert fields["passenger_name"] == "БОРИСЕНКОВ АЛЕКСАНДР ВЛАДИМИРОВИЧ"
    assert fields["issuer"] == "ФПК ЗАП-СИБИРСКИЙ / ФПК"
    assert fields["total"] == Decimal("2188.70")
    assert fields["ticketCost"] == Decimal("1172.50")
    assert fields["reservedSeatCost"] == Decimal("1016.20")
    assert fields["booking_class"] == "2Ш"
    assert fields["segments"][0]["from"] == "ПЕНЗА 1"
    assert fields["segments"][0]["to"] == "САМАРА"
    assert fields["segments"][0]["date"] == "30.01.2026"
    assert fields["segments"][0]["flightNo"] == "124ВА"


def test_rzd_control_coupon_wins_over_timezone_labels_and_splits_costs():
    text = """
    ЭЛЕКТРОННЫЙ БИЛЕТ. КОНТРОЛЬНЫЙ КУПОН
    ПОЕЗД ВАГОН МЕСТО 721 721 07 07 096 096
    № 77 506 905 747 822
    Часовой пояс Поездом 722722
    18:29 18:29 27.10.2025
    22:33 22:33 27.10.2025
    ПАСПОРТ РФ 2211755330 29.08.1986 RUS М
    СУЛЕЙМАНОВ РЕНАТ РАШИДОВИЧ
    Посадка в поезд осуществляется
    721ЩА 27.10.2025 18:29 07С 096 МОСКВА ВК ВОСТОЧНЫЙ - НИЖНИЙ НОВГОРОД МОСКОВ
    ПН2211755330 СУЛЕЙМАНОВ-РР 290886
    Заказ: 77506905747822
    Перевозчик: ФПК МОСКОВСКИЙ / ФПК ИНН 7708709686
    2Ю 2Ю
    Оплата наличными Билет Плацкарта НДС 0% НДС 20%
    1 861,60 ₽ 1 016,10 ₽ 0,00 ₽ 10,00 ₽
    Итого Вкл. НДС 2 877,70 ₽
    """

    fields = _rail(text)

    assert fields is not None
    assert fields["passenger_name"] == "СУЛЕЙМАНОВ РЕНАТ РАШИДОВИЧ"
    assert fields["reference"] == "77506905747822"
    assert fields["ticket_number"] == "77506905747822"
    assert fields["segments"][0]["from"] == "МОСКВА ВК ВОСТОЧНЫЙ"
    assert fields["segments"][0]["to"] == "НИЖНИЙ НОВГОРОД МОСКОВ"
    assert fields["segments"][0]["dep"] == "18:29"
    assert fields["segments"][0]["arr"] == "22:33"
    assert fields["segments"][0]["coach"] == "07"
    assert fields["segments"][0]["seat"] == "096"
    assert fields["ticketCost"] == Decimal("1861.60")
    assert fields["reservedSeatCost"] == Decimal("1016.10")
    assert fields["total"] == Decimal("2877.70")


def test_compact_s7_layout_recognizes_passenger_document_route_and_finances():
    text = (
        "ЭЛЕКТРОННЫЙ БИЛЕТ(маршрут-квитанция для пассажира)Заказ No6482422"
        "код бронирования: O48TFQ ПассажирДата рожденияНомер документаНомер билета"
        "Бонусная картаПродажаKOROTKOV ALEKSEI MIKHAILOVICH11.10.1961"
        "ПС 2206878390421 213535626118.06.2026Рейс под брендом авиакомпании S7 Airlines"
        "DMEМосква, ДомодедовоOMSОмскПеревозчикРейсТарифБагажРучнаякладьСтатус"
        "23:30Вс, 28 Июня 202605:50Пн, 29 Июня 2026S7 Airlines"
        "S7-2565ECONOMYXSTOW1PC10KGOKРАСЧЕТ ТАРИФА:"
        "ТАРИФ: RUB17790СБОР/TAX: RUB2765RI490RUB YQ75RUB YR2200RUB"
        "ВСЕГО К ОПЛАТЕ: RUB20555КВИТАНЦИЯ РАЗНЫХ СБОРОВ"
        "СБОР АСБ160,00 РУБ.СБОР СА0,00 РУБ.ИТОГО К ОПЛАТЕ160,00 РУБ."
    )

    fields = _s7_compact_fields(text)

    assert fields["passenger_name"] == "KOROTKOV ALEKSEI MIKHAILOVICH"
    assert fields["document_number"] == "ПС 2206878390"
    assert fields["ticket_number"] == "421 2135356261"
    assert fields["reference"] == "O48TFQ"
    assert fields["supplier_order_number"] == "6482422"
    assert fields["segments"] == [
        {
            "from": "Москва, Домодедово",
            "fromCode": "DME",
            "to": "Омск",
            "toCode": "OMS",
            "date": "28.06.2026",
            "dep": "23:30",
            "arr": "05:50",
            "flightNo": "S7-2565",
            "carrier": "S7 Airlines",
            "cls": "ECONOMY",
            "status": "OK",
            "fareBasis": "XSTOW",
            "cabin": "ECONOMY",
            "baggage": "1PC",
            "dir": "out",
        }
    ]
    assert fields["fare"] == Decimal("17790")
    assert fields["taxes"] == Decimal("2765")
    assert fields["fees"] == Decimal("160.00")
    assert fields["total"] == Decimal("20715.00")


def test_avia_standalone_ps_passport_line_is_recognized():
    text = """
    Электронный билет (маршрут/квитанция для пассажира)
    ДАТА : 12СЕН24
    ФАМИЛИЯ : ZAVGORODNII/ALEKSANDR VALEREVICH MR
    ПС4713420143
    ОТПРВ/НАЗН : GOJMMK
    ВЫДАН ОТ : АЭРОФЛОТ
    КОД БРОНИРОВАНИЯ : 01W5F6
    НОМЕР БИЛЕТА : 555 2337332744
    Маршрут: GOJ -> MMK
    Дата отправления: 26.09.2024
    Рейс SU-6106
    ТАРИФ : RUB10800
    СБОР/TAX : RUB608
    ВСЕГО К ОПЛАТЕ : RUB11508
    """

    fields = extract_receipt_fields(
        text.encode(),
        mime="text/plain",
        name="zavgorodnii-receipt.txt",
    )["fields"]

    assert fields["service_kind"] == "avia"
    assert fields["passenger_name"] == "ZAVGORODNII ALEKSANDR VALEREVICH"
    assert fields["document_number"] == "ПС4713420143"
    assert fields["ticket_number"] == "555 2337332744"


def test_multiline_s7_layout_reassembles_split_amounts_and_route():
    text = """
    ЭЛЕКТРОННЫЙ БИЛЕТ (маршрут-квитанция для пассажира)
    Заказ No 5994230
    код бронирования: NLZF1I
    Пассажир
    Дата рождения
    Номер документа
    Номер билета
    Бонусная карта
    Продажа
    GRABCHUK MIKHAIL PETROVICH
    ПС 4621263964
    421 2129571864
    01.11.2025
    Рейс под брендом авиакомпании S7 Airlines
    Новосибирск, OVB
    Москва, DME
    Перевозчик
    Рейс
    Тариф
    Багаж
    Ручная кладь
    Статус
    11:55
    Сб, 1 Ноября 2025
    12:35
    Сб, 1 Ноября 2025
    S7 Airlines
    S7-2508
    ECONOMY
    VSTOW
    1PC
    10 кг
    OK
    РАСЧЕТ ТАРИФА:
    ТАРИФ
    : RUB26
    85
    5
    ЭКВИВ.
    СБОР/TAX
    : RUB2298RI490RUB
    ВСЕГО К ОПЛАТЕ
    : RUB2
    915
    3
    В Т.Ч.
    """

    fields = _s7_compact_fields(text)

    assert fields["passenger_name"] == "GRABCHUK MIKHAIL PETROVICH"
    assert fields["document_number"] == "ПС 4621263964"
    assert fields["ticket_number"] == "421 2129571864"
    assert fields["reference"] == "NLZF1I"
    assert fields["supplier_order_number"] == "5994230"
    assert fields["fare"] == Decimal("26855")
    assert fields["taxes"] == Decimal("2298")
    assert fields["total"] == Decimal("29153")
    assert fields["segments"][0] == {
        "from": "Новосибирск",
        "fromCode": "OVB",
        "to": "Москва",
        "toCode": "DME",
        "date": "01.11.2025",
        "dep": "11:55",
        "arr": "12:35",
        "flightNo": "S7-2508",
        "carrier": "S7 Airlines",
        "cls": "ECONOMY",
        "status": "OK",
        "fareBasis": "VSTOW",
        "cabin": "ECONOMY",
        "baggage": "1PC",
        "dir": "out",
    }


def test_s7_two_routes_on_single_lines_do_not_turn_headers_into_receipt_data():
    text = """
    ИП Хмель Марина Валерьевна 8(831) 246-02-04 mkhmel@list.ru
    ЭЛЕКТРОННЫЙ БИЛЕТ
    (маршрут-квитанция для пассажира)
    Заказ No6046978
    код бронирования: NO65R7
    Пассажир Номер документа Номер билета Бонусная карта Продажа
    POROSHIN DENIS PAVLOVICH 6513748739 421 2130342785 24.11.2025
    Рейс под брендом авиакомпании S7 Airlines
    Екатеринбург, Кольцово, SVX Новосибирск, Толмачево, OVB Авиакомпания-
    перевозчик Рейс Тариф Багаж Статус
    07:15
    Вт, 02 Декабря 2025
    11:30
    Вт, 02 Декабря 2025 S7 Airlines S7-5018 ECONOMY
    LSTRT 1PC OK
    Новосибирск, Толмачево, OVB Екатеринбург, Кольцово, SVX Авиакомпания-
    перевозчик Рейс Тариф Багаж Статус
    20:15
    Чт, 04 Декабря 2025
    20:40
    Чт, 04 Декабря 2025 S7 Airlines S7-5019 ECONOMY
    VSTRT 1PC OK
    РАСЧЕТ ТАРИФА:
    ТАРИФ : RUB31335 ЭКВИВ. В ВАЛ. ПЛ: RUB31335
    СБОР/TAX : RUB3924 RI824RUB YQ100RUB YR3000RUB
    ВСЕГО К ОПЛАТЕ : RUB35259 В Т.Ч. НДС 20% : RUB0
    Передат. надписи/огранич.:
    S7-5018 SVX - OVB (LSTRT)
    S7-5019 OVB - SVX (VSTRT)
    Тариф Эконом Стандарт
    Ручная кладь 10 кг. Габариты 55x40x23 см.
    Багаж 1 место по 23 кг.
    """

    fields = _s7_compact_fields(text)

    assert fields["passenger_name"] == "POROSHIN DENIS PAVLOVICH"
    assert fields["document_number"] == "6513748739"
    assert fields["ticket_number"] == "421 2130342785"
    assert fields["issue_date"] == "24.11.2025"
    assert fields["supplier_order_number"] == "6046978"
    assert fields["reference"] == "NO65R7"
    assert fields["fare"] == Decimal("31335")
    assert fields["taxes"] == Decimal("3924")
    assert fields["total"] == Decimal("35259")
    assert fields["booking_class"] == "ECONOMY"
    assert fields["fare_basis"] == "LSTRT / VSTRT"
    assert fields["baggage"] == "1PC"
    assert fields["segments"] == [
        {
            "from": "Екатеринбург, Кольцово",
            "fromCode": "SVX",
            "to": "Новосибирск, Толмачево",
            "toCode": "OVB",
            "date": "02.12.2025",
            "dep": "07:15",
            "arr": "11:30",
            "flightNo": "S7-5018",
            "carrier": "S7 Airlines",
            "cls": "ECONOMY",
            "status": "OK",
            "fareBasis": "LSTRT",
            "cabin": "ECONOMY",
            "baggage": "1PC",
            "dir": "out",
        },
        {
            "from": "Новосибирск, Толмачево",
            "fromCode": "OVB",
            "to": "Екатеринбург, Кольцово",
            "toCode": "SVX",
            "date": "04.12.2025",
            "dep": "20:15",
            "arr": "20:40",
            "flightNo": "S7-5019",
            "carrier": "S7 Airlines",
            "cls": "ECONOMY",
            "status": "OK",
            "fareBasis": "VSTRT",
            "cabin": "ECONOMY",
            "baggage": "1PC",
            "dir": "back",
        },
    ]


def test_rossiya_multi_ticket_receipt_keeps_all_passengers_and_sums_total():
    text = """
    27 октября 2025
    Маршрутная квитанция электронного билета
    KISELEVA MARGARITA SEMENOVNA
    Документ:
    2202559501
    No эл.билета:
    5556171217873
    Код бронирования*
    6RZ483
    Москва
    Благовещенск
    Рейс: SU 6255
    08 дек. 2025
    18:40 SVO B
    Шереметьево, B
    09 дек. 2025
    Перевозчик: Россия*
    BQS 07:50
    Аэропорт Игнатьево
    Класс: Эконом / P
    Вид тарифа: PCDSOC
    Статус: Оформлен
    Провоз багажа: 1 место до 23 кг
    Тариф
    RUB 6400.00
    P2202559501/DOB02JUN57/NDSA/C0.00
    Итого по тарифу/сборам
    6400.00 RUB
    27 октября 2025
    Маршрутная квитанция электронного билета
    DUBROVSKAIA IRINA ILINICHNA
    Документ:
    2201903735
    No эл.билета:
    5556171217874
    Код бронирования*
    6RZ483
    Москва
    Благовещенск
    Рейс: SU 6255
    08 дек. 2025
    18:40 SVO B
    Шереметьево, B
    09 дек. 2025
    Перевозчик: Россия*
    BQS 07:50
    Аэропорт Игнатьево
    Класс: Эконом / P
    Вид тарифа: PCDSOC
    Статус: Оформлен
    Провоз багажа: 1 место до 23 кг
    Тариф
    RUB 6400.00
    P2201903735/DOB16JUN52/NDSA/C0.00
    Итого по тарифу/сборам
    6400.00 RUB
    """

    fields = _rossiya_itinerary_fields(text)

    assert fields["passenger_name"] == (
        "KISELEVA MARGARITA SEMENOVNA, DUBROVSKAIA IRINA ILINICHNA"
    )
    assert fields["passengers"] == [
        {
            "name": "KISELEVA MARGARITA SEMENOVNA",
            "dob": "02.06.1957",
            "document": "2202559501",
            "ticketNo": "5556171217873",
        },
        {
            "name": "DUBROVSKAIA IRINA ILINICHNA",
            "dob": "16.06.1952",
            "document": "2201903735",
            "ticketNo": "5556171217874",
        },
    ]
    assert fields["ticket_number"] == "5556171217873, 5556171217874"
    assert fields["reference"] == "6RZ483"
    assert fields["fare"] == Decimal("12800.00")
    assert fields["total"] == Decimal("12800.00")
    assert fields["segments"] == [
        {
            "from": "Москва",
            "fromCode": "SVO",
            "to": "Благовещенск",
            "toCode": "BQS",
            "date": "08.12.2025",
            "dep": "18:40",
            "arr": "07:50",
            "flightNo": "SU6255",
            "dir": "out",
        }
    ]


def test_bilingual_hotel_voucher_populates_editor_specific_fields():
    text = """
    Ваучер отеля Hotel voucher
    Номер заказа в системе бронирования Order number in the booking system 1989071
    Дата выдачи Date of issue 27.01.2026
    Лесная Сафмар (бывший Холидей Инн Москва Лесная) 4*
    Lesnaya Safmar (ex.Holiday Inn Moscow Lesnaya) 4*
    28.01.2026 14:00 29.01.2026 12:00 Ночей: 1
    АдресAddress 125047, Россия, Москва, ул Лесная, д 15
    ТелефонPhone +74957836500
    Электронный адресEmail reservations@hi-mole.ru
    ФИ гостяGuest name MR АЛЕКСАНДР ЧИЧЕВ
    Тип номераRoom type Представительский номер с большойдвуспальной кроватью
    Тип питанияMeal type Завтрак (Шведский стол)
    Номер бронированияBooking reference number 13527804
    При отмене или изменении заказа, а так же в случае незаезда гостя в отель
    применяются штрафные санкции в соответствии с условиями тарифа и договора.
    """
    fields = {
        "issuer": (
            "Лесная Сафмар (бывший Холидей Инн Москва Лесная) 4*"
            "Lesnaya Safmar (ex.Holiday Inn Moscow Lesnaya) 4*"
        ),
        "passenger_name": "АЛЕКСАНДР ЧИЧЕВ",
        "reference": "13527804",
        "issue_date": "27.01.2026",
        "segments": [
            {
                "date": "28.01.2026",
                "endDate": "29.01.2026",
                "flightNo": "Представительский номер с большойдвуспальной кроватью",
            }
        ],
    }

    details = _hotel_details(text, fields)

    assert details["supplierOrderNo"] == "1989071"
    assert details["hotelBookingNo"] == "13527804"
    assert details["hotel"] == {
        "name": "Лесная Сафмар (бывший Холидей Инн Москва Лесная) 4*",
        "category": "4*",
        "country": "Россия",
        "city": "Москва",
        "address": "125047, Россия, Москва, ул Лесная, д 15",
        "phone": "+74957836500",
        "email": "reservations@hi-mole.ru",
        "map": "",
    }
    assert details["rooms"][0]["name"] == (
        "Представительский номер с большой двуспальной кроватью"
    )
    assert details["rooms"][0]["bedType"] == "Двуспальная кровать"
    assert details["rooms"][0]["meal"] == "Завтрак"
    assert details["nights"] == 1
    assert details["hotelTerms"]["cancellation"]


def test_sparse_transfer_does_not_receive_terms_missing_from_source():
    fields = {
        "passenger_name": "KIM ALEX",
        "reference": "TRF456",
        "segments": [
            {
                "from": "IST AIRPORT",
                "to": "HOTEL",
                "date": "",
                "dep": "",
                "flightNo": "",
                "dir": "out",
            }
        ],
    }

    details = _transfer_details(
        "TRANSFER VOUCHER TRF456 KIM ALEX IST AIRPORT HOTEL 45 USD",
        fields,
    )

    assert details["segments"] == fields["segments"]
    assert details["vehicle"] == {
        "className": "",
        "category": "",
        "passengers": "",
        "luggage": "",
        "requirements": "",
    }
    assert all(not value for value in details["transferTerms"].values())


def test_iway_transfer_uses_route_heading_instead_of_passenger_sign_text():
    text = """
    Маршрутная квитанция на трансфер
    Заказ
    C-0279135
    от 22.07.2026
    Пассажиры
    Имя, фамилия
    Мобильный телефон
    Надпись на табличке
    Примечания
    Lavit Dmitrii
    +7 986 169-31-03
    Dmitrii Lavit
    Аэропорт им. Ф. Шопена (Варшава) –
    Варшава
    Стоимость
    3016 RUB
    Авиарейс
    Время прилета
    Адрес назначения
    Класс автомобиля, кол-во пассажиров и багажа
    W6- 1442
    терминал 2
    11:35
    12.09.2026
    Luxury apartment piekna,
    Piękna 24/lok. 17, 00-549
    Warszawa, Польша
    Стандарт
    1
    пассажир
    до 3 мест багажа размером 64x42x24 см
    Условия изменения и отмены: бесплатно за 25 часов
    Пассажиры
    Lavit Dmitrii
    +7 986 169-31-03
    Lavit Dmitrii
    Варшава
    –
    Аэропорт им. Ф. Шопена (Варшава)
    Стоимость
    3016 RUB
    Адрес отправления
    Время подачи автомобиля
    Время вылета
    Класс автомобиля, кол-во пассажиров и багажа
    Luxury apartment piekna,
    Piękna 24/lok. 17, 00-549
    Warszawa, Польша
    06:05
    19.09.2026
    10:05
    19.09.2026
    Стандарт
    1
    пассажир
    до 3 мест багажа размером 64x42x24 см
    Условия изменения и отмены: бесплатно за 25 часов
    Итого 6032 RUB
    """

    segments = _transfer_segments(text)
    details = _transfer_details(
        text,
        {
            "passenger_name": "Lavit Dmitrii",
            "reference": "C-0279135",
            "segments": segments,
        },
    )

    assert [(row["from"], row["to"]) for row in segments] == [
        ("Аэропорт им. Ф. Шопена (Варшава)", "Варшава"),
        ("Варшава", "Аэропорт им. Ф. Шопена (Варшава)"),
    ]
    assert segments[0]["flightNo"] == "W6-1442"
    assert segments[0]["toAddress"].startswith("Luxury apartment piekna")
    assert segments[1]["fromAddress"].startswith("Luxury apartment piekna")
    assert details["passengers"][0]["phone"] == "+7 986 169-31-03"
    assert details["vehicle"]["className"] == "Стандарт"
    assert details["vehicle"]["passengers"] == "1"
    assert details["vehicle"]["luggage"].startswith("до 3 мест багажа")


def test_compact_avia_table_preserves_connection_and_ignores_baggage_header():
    text = """
    ДАТА ОФОРМЛЕНИЯ: 12СЕН24
    ОТПРВ/НАЗН : GOJMMK
    МАРШРУТ/ПЕРЕВОЗЧИК
    РЕЙС
    КЛАСС
    ДАТА
    ВРЕМЯ ОТПР
    ВРЕМЯ ПРИБ
    СТАТУС
    БАЗОВЫЙ ТАРИФ
    БАГ
    НИЖНИЙ НОВГОРОД, СТРИГИНО
    GOJ / РОССИЯ
    SU-6106
    T
    26СЕН
    1400
    1550
    OK
    TNOR
    ECONOMY
    0PC
    САНКТ-ПЕТЕРБУРГ, ПУЛКОВО
    LED 1 / РОССИЯ
    SU-6345
    T
    26СЕН
    1735
    1930
    OK
    TNOR
    ECONOMY
    0PC
    МУРМАНСК
    MMK
    ПЕРЕДАТ. НАДПИСИ/ОГРАНИЧ.:
    СБОР СА
    : RUB0
    СБОР АСБ
    :
    RUB100
    """

    assert _avia_segments(text) == [
        {
            "from": "НИЖНИЙ НОВГОРОД, СТРИГИНО",
            "fromCode": "GOJ",
            "to": "САНКТ-ПЕТЕРБУРГ, ПУЛКОВО",
            "toCode": "LED",
            "date": "26.09.2024",
            "dep": "14:00",
            "arr": "15:50",
            "flightNo": "SU6106",
            "carrier": "РОССИЯ",
            "cls": "T",
            "status": "OK",
            "fareBasis": "TNOR",
            "cabin": "ECONOMY",
            "baggage": "0PC",
            "dir": "out",
        },
        {
            "from": "САНКТ-ПЕТЕРБУРГ, ПУЛКОВО",
            "fromCode": "LED",
            "to": "МУРМАНСК",
            "toCode": "MMK",
            "date": "26.09.2024",
            "dep": "17:35",
            "arr": "19:30",
            "flightNo": "SU6345",
            "carrier": "РОССИЯ",
            "cls": "T",
            "status": "OK",
            "fareBasis": "TNOR",
            "cabin": "ECONOMY",
            "baggage": "0PC",
            "dir": "seg",
        },
    ]
    assert _fee_breakdown(text) == [
        {"code": "SA", "label": "СБОР СА", "amount": "0", "currency": "RUB"},
        {"code": "ASB", "label": "СБОР АСБ", "amount": "100", "currency": "RUB"},
    ]
