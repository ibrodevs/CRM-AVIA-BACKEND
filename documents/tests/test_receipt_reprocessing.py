from decimal import Decimal

from documents.receipt_parser_patch_safe import _hotel_details, _rail, _transfer_details
from documents.services import (
    _avia_segments,
    _fee_breakdown,
    _rossiya_itinerary_fields,
    _s7_compact_fields,
)


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
            "dir": "out",
        }
    ]
    assert fields["fare"] == Decimal("17790")
    assert fields["taxes"] == Decimal("2765")
    assert fields["fees"] == Decimal("160.00")
    assert fields["total"] == Decimal("20715.00")


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
        "dir": "out",
    }


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
