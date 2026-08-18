from decimal import Decimal

from documents.receipt_multiform_patch import _aggregate_rail_receipts
from documents.receipt_parser_patch_safe import _rail
from documents.receipt_problem_formats_patch import _parse_azimuth_ticket, _parse_s7_ticket


S7_DENIS = """
ИП Хмель Марина Валерьевна 8(831) 246-02-04 mkhmel@list.ru
ЭЛЕКТРОННЫЙ БИЛЕТ
(маршрут-квитанция для пассажира)
Заказ №6046978
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


S7_MIKHAIL = """
ИП Хмель Марина Валерьевна 8(831) 246-02-04 mkhmel@list.ru
ЭЛЕКТРОННЫЙ БИЛЕТ
(маршрут-квитанция для пассажира)
Заказ №5994230
код бронирования: NLZF1I
Пассажир Номер документа Номер билета Бонусная карта Продажа
GRABCHUK MIKHAIL PETROVICH 4621263964 421 2129571864 27.10.2025
Рейс под брендом авиакомпании S7 Airlines
Новосибирск, Толмачево, OVB Москва, Домодедово, DME Авиакомпания-
перевозчик Рейс Тариф Багаж Статус
11:55
Сб, 01 Ноября 2025
12:35
Сб, 01 Ноября 2025 S7 Airlines S7-2508 ECONOMY
VSTOW 1PC OK
РАСЧЕТ ТАРИФА:
ТАРИФ : RUB26855 ЭКВИВ. В ВАЛ. ПЛ: RUB26855
СБОР/TAX : RUB2298 RI748RUB YQ50RUB YR1500RUB
ВСЕГО К ОПЛАТЕ : RUB29153 В Т.Ч. НДС 10% : RUB208.91
Передат. надписи/огранич.:
S7-2508 OVB - DME (VSTOW)
Тариф Эконом Стандарт
Ручная кладь 10 кг. Габариты 55x40x23 см.
Багаж 1 место по 23 кг.
"""


AZIMUTH = """
ЭЛЕКТРОННЫЙ БИЛЕТ (маршрут-квитанция для пассажира)
Заказ №5073872 код бронирования: MNN7JV
Пассажир Номер документа Номер билета Бонусная карта Продажа
ЛОСЕВ АЛЕКСАНДР ВАДИМОВИЧ Г-Н(ГОА) 23OCT2003 6024738770 222 2409777877 01.10.2024
Рейс под брендом авиакомпании Азимут
Киров, KVX Минеральные Воды, MRV Авиакомпания-
перевозчик Рейс Тариф Багаж Статус
05:55 Чт, 03 Октября 2024 09:30 Чт, 03 Октября 2024 Азимут A4-6014 ECONOMY
BGRFLOW 1М OK
Стоимость: 10 808,00 руб.
в том числе сбор АСБ: 450,00 руб.
в том числе сбор СА: 0,00 руб.
"""


def rzd_page(*, passenger, passport, dob, ticket, seat, ticket_cost, reserved):
    total = Decimal(ticket_cost) + Decimal(reserved)
    return f"""
ЭЛЕКТРОННЫЙ БИЛЕТ. КОНТРОЛЬНЫЙ КУПОН
ПОЕЗД ВАГОН МЕСТО
098 098 04 04 {seat} {seat}
Купе Нижнее
№ {' '.join([ticket[:2], ticket[2:5], ticket[5:8], ticket[8:11], ticket[11:]])}
06:44 14.12.2025 вс
Отправление по местному времени UTC+5, МСК+2 Часовой пояс
Курган
16:42 14.12.2025 вс
Прибытие по местному времени UTC+6, МСК+3 Часовой пояс
Омск-Пассажирский
ПАСПОРТ РФ {passport} {dob} RUS М
{passenger}
Посадка в поезд осуществляется при предъявлении документа.
098*СА 14.12.2025 06:44 04К {seat} КУРГАН - ОМСК-ПАССАЖИРСКИЙ ПН{passport} ТЕСТ-ТТ 010101
2А 2А Тариф: Полный
Оплата банковской картой ****9574 Билет Плацкарта НДС 0% НДС 20%
{ticket_cost.replace('.', ',')} ₽ {reserved.replace('.', ',')} ₽ 0,00 ₽ 77,50 ₽ Итого
Вкл. НДС {str(total).replace('.', ',')} ₽
Заказ: {ticket}
Перевозчик: ФПК ДАЛЬНЕВОСТОЧНЫЙ / ФПК ИНН 7708709686
"""


def test_s7_roundtrip_keeps_both_segments_and_full_financials():
    result = _parse_s7_ticket(S7_DENIS)

    assert result is not None
    assert result["passenger_name"] == "POROSHIN DENIS PAVLOVICH"
    assert result["document_number"] == "6513748739"
    assert result["ticket_number"] == "421 2130342785"
    assert result["reference"] == "NO65R7"
    assert result["supplier_order_number"] == "6046978"
    assert result["trip_type"] == "roundtrip"
    assert len(result["segments"]) == 2
    assert [(leg["fromCode"], leg["toCode"], leg["flightNo"]) for leg in result["segments"]] == [
        ("SVX", "OVB", "S7-5018"),
        ("OVB", "SVX", "S7-5019"),
    ]
    assert [leg["date"] for leg in result["segments"]] == ["02.12.2025", "04.12.2025"]
    assert result["fare_basis"] == "LSTRT / VSTRT"
    assert result["baggage"] == "1PC"
    assert result["hand_baggage"] == "10 кг"
    assert result["fare"] == Decimal("31335")
    assert result["taxes"] == Decimal("3924")
    assert result["fees"] == Decimal("0")
    assert result["total"] == Decimal("35259")
    assert [(row["code"], row["amount"]) for row in result["tax_breakdown"]] == [
        ("RI", "824"),
        ("YQ", "100"),
        ("YR", "3000"),
    ]


def test_s7_oneway_keeps_route_ticket_and_costs():
    result = _parse_s7_ticket(S7_MIKHAIL)

    assert result is not None
    assert result["passenger_name"] == "GRABCHUK MIKHAIL PETROVICH"
    assert result["ticket_number"] == "421 2129571864"
    assert result["reference"] == "NLZF1I"
    assert result["trip_type"] == "oneway"
    assert len(result["segments"]) == 1
    segment = result["segments"][0]
    assert segment["from"] == "Новосибирск, Толмачево"
    assert segment["fromCode"] == "OVB"
    assert segment["to"] == "Москва, Домодедово"
    assert segment["toCode"] == "DME"
    assert segment["date"] == "01.11.2025"
    assert segment["dep"] == "11:55"
    assert segment["arr"] == "12:35"
    assert segment["flightNo"] == "S7-2508"
    assert segment["fareBasis"] == "VSTOW"
    assert result["fare"] == Decimal("26855")
    assert result["taxes"] == Decimal("2298")
    assert result["total"] == Decimal("29153")


def test_azimuth_columns_do_not_shift_status_into_baggage():
    result = _parse_azimuth_ticket(AZIMUTH)

    assert result is not None
    assert result["passenger_name"] == "ЛОСЕВ АЛЕКСАНДР ВАДИМОВИЧ"
    assert result["booking_class"] == "ECONOMY"
    assert result["fare_basis"] == "BGRFLOW"
    assert result["baggage"] == "1PC"
    assert result["booking_status"] == "OK"
    assert result["fare"] == Decimal("10358.00")
    segment = result["segments"][0]
    assert segment["from"] == "Киров"
    assert segment["fromCode"] == "KVX"
    assert segment["to"] == "Минеральные Воды"
    assert segment["toCode"] == "MRV"
    assert "Рейс под брендом" not in segment["from"]
    assert segment["flightNo"] == "A4-6014"
    assert segment["cabin"] == "ECONOMY"
    assert segment["fareBasis"] == "BGRFLOW"
    assert segment["baggage"] == "1PC"
    assert segment["status"] == "OK"


def test_eight_rzd_coupons_stay_eight_independent_receipts():
    source = [
        ("ШВАНГИРАДЗЕ ДАВИД ЗАЗОВИЧ", "5626790603", "26.03.2004", "78706152276981", "025", "2627.60", "1806.60"),
        ("РУДАКОВ ГРИГОРИЙ КОНСТАНТИНОВИЧ", "3222450293", "25.08.2002", "78706152276992", "026", "1839.30", "1404.10"),
        ("МОСКВИН КИРИЛЛ ЕВГЕНЬЕВИЧ", "6724345545", "04.03.2005", "78706152277003", "027", "2627.60", "1806.60"),
        ("ЛАРИЧЕВ ВЛАДИСЛАВ ФЕДОРОВИЧ", "7124943913", "12.02.2005", "78706152277014", "028", "1839.30", "1404.10"),
        ("АРЗАМАСЦЕВ АРТЕМ ДМИТРИЕВИЧ", "5625865801", "19.07.2005", "78706152277025", "033", "2627.60", "1806.60"),
        ("ФИЛЛИПОВ ТИМОФЕЙ КОНСТАНТИНОВИЧ", "5624822038", "17.09.2004", "78706152277036", "034", "1839.30", "1404.10"),
        ("ПОЛШКОВ АРСЕНТИЙ АЛЕКСАНДРОВИЧ", "5623783366", "22.01.2004", "78706152277040", "035", "2627.60", "1806.60"),
        ("ЦИУЛИН ДАНИЛ АЛЕКСАНДРОВИЧ", "9219701077", "27.12.2005", "78706152277051", "036", "1839.30", "1404.10"),
    ]
    receipts = []
    for passenger, passport, dob, ticket, seat, ticket_cost, reserved in source:
        parsed = _rail(
            rzd_page(
                passenger=passenger,
                passport=passport,
                dob=dob,
                ticket=ticket,
                seat=seat,
                ticket_cost=ticket_cost,
                reserved=reserved,
            )
        )
        assert parsed is not None
        receipts.append(parsed)

    result = _aggregate_rail_receipts(receipts, {})

    assert result["receipt_count"] == 8
    assert len(result["receipts"]) == 8
    assert [item["passenger"] for item in result["receipts"]] == [row[0] for row in source]
    assert [item["ticketNo"] for item in result["receipts"]] == [row[3] for row in source]
    assert [item["legs"][0]["seat"] for item in result["receipts"]] == [row[4] for row in source]
    assert all(item["legs"][0]["flightNo"] == "098" for item in result["receipts"])
    assert all(item["legs"][0]["from"] == "КУРГАН" for item in result["receipts"])
    assert all(item["legs"][0]["to"] == "ОМСК-ПАССАЖИРСКИЙ" for item in result["receipts"])
    assert result["total"] == Decimal("30710.40")
