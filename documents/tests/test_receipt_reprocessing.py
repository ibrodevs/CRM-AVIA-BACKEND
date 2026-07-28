from decimal import Decimal

from documents.receipt_parser_patch_safe import _hotel_details, _rail, _transfer_details


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
    assert fields["booking_class"] == "2Ш"
    assert fields["segments"][0]["from"] == "ПЕНЗА 1"
    assert fields["segments"][0]["to"] == "САМАРА"
    assert fields["segments"][0]["date"] == "30.01.2026"
    assert fields["segments"][0]["flightNo"] == "124ВА"


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
