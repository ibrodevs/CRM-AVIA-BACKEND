from decimal import Decimal

from documents.receipt_multiform_patch import (
    _aggregate_rail_receipts,
    _parse_psc_air,
    _parse_psc_hotel,
)


HOTEL_TWO_GUESTS = """
Компания: Пассажирский Сервисный Центр. Забронировано:28.01.2026 г.
Подтверждение бронирования. Контакты отеля.
Город: Екатеринбург, Россия
Название отеля: «Фридом»
Адрес: Россия, 620066, Свердловская область, Екатеринбург, ул. Комвузовская, д. 21В
Детали размещения
Имя гостя: Бакаев Максим Юрьевич
Категория номера: стандарт с двуспальной кроватью
Питание: завтрак (включен)
Дата заезда / Дата выезда: 02.02.2026 14:00 / 15.02.2026 12:00 (по местному времени отеля)
Номер бронирования: заселение по ФИО
Имя гостя: Нагаев Александр Сергеевич
Категория номера: стандарт с двуспальной кроватью
Питание: завтрак (включен)
Дата заезда / Дата выезда: 02.02.2026 14:00 / 15.02.2026 12:00 (по местному времени отеля)
Номер бронирования: заселение по ФИО
При заселении обязательно иметь при себе данный ваучер.
"""

AIR_EGOR = """
ИП Хмель Марина Валерьевна
ЭЛЕКТРОННЫЙ БИЛЕТ
(маршрут-квитанция для пассажира)
Заказ №6155791
код бронирования: 8SVRKM
Пассажир
BARANOV EGOR ALEKSANDROVICH
Рейс под брендом авиакомпании Аэрофлот
Дата рождения
27.12.1998
Номер документа
ПС 4519143485
Номер билета
555 2378965185
Бонусная карта
Продажа
28.01.2026
SVO (Терминал B)
Москва, Шереметьево
10:10
Чт, 29 Января 2026
KUF
Самара
12:55
Чт, 29 Января 2026
Перевозчик
Рейс
Тариф
Багаж
Ручная
кладь
Статус
Аэрофлот
SU-1604
ECONOMY
UCOR
23KG
10KG
OK
РАСЧЕТ ТАРИФА:
ТАРИФ
СБОР/TAX
ИТОГО ПО БИЛЕТУ
СБОР СА
СБОР АСБ
ВСЕГО К ОПЛАТЕ
: RUB25880
: RUB693
: RUB26573
: RUB0
: RUB400
: RUB26973
"""

AIR_SERBIA_WITHOUT_FARE_BASIS = """
ИП Хмель Марина Валерьевна
ЭЛЕКТРОННЫЙ БИЛЕТ
(маршрут-квитанция для пассажира)
Заказ №6151117
код бронирования: FQW7JB
Пассажир
ILIN VIACHESLAV
Дата рождения
30.05.1960
Номер документа
ПСП 673796676
Номер билета
115 9531174060
Бонусная карта
Продажа
27.01.2026
SVO (Терминал C)
Москва, Шереметьево
19:45
Вт, 11 Августа 2026
BEG (Терминал 2)
Белград, Никола Тесла
22:00
Вт, 11 Августа 2026
Перевозчик
Рейс
Тариф
Багаж
Ручная
кладь
Статус
Air Serbia
JU-133
ECONOMY
1PC
8KG
OK
Стоимость:
в том числе сбор АСБ:
в том числе сбор СА:
43 462,76 руб.
710,00 руб.
0,00 руб.
"""


def rail_receipt(*, passenger, ticket, train, seat, total, ticket_cost, reserved, route_from, route_to, date, dep, arr):
    return {
        "issuer": "ФПК",
        "passenger_name": passenger,
        "ticket_number": ticket,
        "document_number": "0000000000",
        "date_of_birth": "01.01.1990",
        "ticketCost": Decimal(ticket_cost),
        "reservedSeatCost": Decimal(reserved),
        "total": Decimal(total),
        "currency": "RUB",
        "service_kind": "rail",
        "service_type": "ЖД",
        "segments": [
            {
                "from": route_from,
                "to": route_to,
                "date": date,
                "endDate": date,
                "dep": dep,
                "arr": arr,
                "flightNo": train,
                "coach": "04",
                "seat": seat,
                "dir": "out",
            }
        ],
    }


def test_four_rail_coupons_remain_four_independent_blanks():
    receipts = [
        rail_receipt(
            passenger="ИЛЬИН ВЯЧЕСЛАВ АБРАМОВИЧ",
            ticket="70813636536606",
            train="719",
            seat="091",
            total="2431.00",
            ticket_cost="1563.20",
            reserved="867.80",
            route_from="НИЖНИЙ НОВГОРОД МОСКОВ",
            route_to="МОСКВА ВК ВОСТОЧНЫЙ",
            date="02.02.2026",
            dep="09:30",
            arr="13:42",
        ),
        rail_receipt(
            passenger="ЛЕБЕДЕВ ТИМОФЕЙ НИКОЛАЕВИЧ",
            ticket="70813636536610",
            train="719",
            seat="092",
            total="2431.00",
            ticket_cost="1563.20",
            reserved="867.80",
            route_from="НИЖНИЙ НОВГОРОД МОСКОВ",
            route_to="МОСКВА ВК ВОСТОЧНЫЙ",
            date="02.02.2026",
            dep="09:30",
            arr="13:42",
        ),
        rail_receipt(
            passenger="ИЛЬИН ВЯЧЕСЛАВ АБРАМОВИЧ",
            ticket="70863636536621",
            train="721",
            seat="005",
            total="1728.90",
            ticket_cost="1099.30",
            reserved="629.60",
            route_from="МОСКВА ВК ВОСТОЧНЫЙ",
            route_to="НИЖНИЙ НОВГОРОД МОСКОВ",
            date="04.02.2026",
            dep="18:29",
            arr="22:33",
        ),
        rail_receipt(
            passenger="ЛЕБЕДЕВ ТИМОФЕЙ НИКОЛАЕВИЧ",
            ticket="70863636536632",
            train="721",
            seat="006",
            total="1728.90",
            ticket_cost="1099.30",
            reserved="629.60",
            route_from="МОСКВА ВК ВОСТОЧНЫЙ",
            route_to="НИЖНИЙ НОВГОРОД МОСКОВ",
            date="04.02.2026",
            dep="18:29",
            arr="22:33",
        ),
    ]

    result = _aggregate_rail_receipts(receipts, {})

    assert result["receipt_count"] == 4
    assert len(result["receipts"]) == 4
    assert [item["passenger"] for item in result["receipts"]] == [
        "ИЛЬИН ВЯЧЕСЛАВ АБРАМОВИЧ",
        "ЛЕБЕДЕВ ТИМОФЕЙ НИКОЛАЕВИЧ",
        "ИЛЬИН ВЯЧЕСЛАВ АБРАМОВИЧ",
        "ЛЕБЕДЕВ ТИМОФЕЙ НИКОЛАЕВИЧ",
    ]
    assert [item["legs"][0]["seat"] for item in result["receipts"]] == ["091", "092", "005", "006"]
    assert [item["ticketNo"] for item in result["receipts"]] == [
        "70813636536606",
        "70813636536610",
        "70863636536621",
        "70863636536632",
    ]
    assert [item["total"] for item in result["receipts"]] == [
        Decimal("2431.00"),
        Decimal("2431.00"),
        Decimal("1728.90"),
        Decimal("1728.90"),
    ]
    assert result["total"] == Decimal("8319.80")
    assert result["trip_type"] == "roundtrip"


def test_hotel_repeated_guest_blocks_create_separate_rooms():
    result = _parse_psc_hotel(HOTEL_TWO_GUESTS)

    assert result is not None
    assert result["hotel"]["name"] == "Фридом"
    assert result["hotel"]["city"] == "Екатеринбург"
    assert result["guest_count"] == 2
    assert result["room_count"] == 2
    assert [guest["name"] for guest in result["passengers"]] == [
        "Бакаев Максим Юрьевич",
        "Нагаев Александр Сергеевич",
    ]
    assert result["rooms"][0]["guestIds"] == ["Бакаев Максим Юрьевич"]
    assert result["rooms"][1]["guestIds"] == ["Нагаев Александр Сергеевич"]
    assert result["rooms"][0]["checkInDate"] == "02.02.2026"
    assert result["rooms"][1]["checkOutDate"] == "15.02.2026"
    assert result["nights"] == 13


def test_malformed_supplier_air_pdf_text_has_complete_fallback():
    result = _parse_psc_air(AIR_EGOR)

    assert result is not None
    assert result["passenger_name"] == "BARANOV EGOR ALEKSANDROVICH"
    assert result["ticket_number"] == "555 2378965185"
    assert result["reference"] == "8SVRKM"
    assert result["segments"][0]["fromCode"] == "SVO"
    assert result["segments"][0]["toCode"] == "KUF"
    assert result["segments"][0]["flightNo"] == "SU-1604"
    assert result["fare"] == Decimal("25880")
    assert result["taxes"] == Decimal("693")
    assert result["fees"] == Decimal("400")
    assert result["total"] == Decimal("26973")


def test_air_serbia_short_columns_do_not_shift_baggage_into_fare_basis():
    result = _parse_psc_air(AIR_SERBIA_WITHOUT_FARE_BASIS)

    assert result is not None
    assert result["fare_basis"] == ""
    assert result["baggage"] == "1PC"
    assert result["hand_baggage"] == "8KG"
    assert result["booking_status"] == "OK"
    assert result["segments"][0]["fareBasis"] == ""
    assert result["segments"][0]["baggage"] == "1PC"
    assert result["segments"][0]["handBaggage"] == "8KG"
