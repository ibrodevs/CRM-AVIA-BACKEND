from decimal import Decimal

from documents.receipt_client_pdf_requirements import (
    _parse_modern_aeroflot,
    _parse_old_aeroflot,
    _parse_russian_aeroflot_group,
)
from documents.receipt_client_pdf_text_source import _partner_hotel_from_pdfminer

OLD_AEROFLOT = """
Электронный билет (маршрут/квитанция для пассажира)
ДАТА : 18АПР25
ФАМИЛИЯ : GRIDNEVSKII/ALEKSANDR GENNADEVICH MR
ОТПРВ/НАЗН : GOJGOJ
ПС2206896684
ВЫДАН ОТ
НОМЕР БИЛЕТА
В ОБМЕН НА
ПЕРВОН. ВЫДАН
: АЭРОФЛОТ
: 555 2353939951
: 555 2352884350
: 18АПР25
КОД БРОНИРОВАНИЯ
КОДЫ РЕГИСТРАЦИИ НА РЕЙС
КОД ТУРА
БОНУСНАЯ КАРТА
: 3DKGVW
МАРШРУТ/ПЕРЕВОЗЧИК
НИЖНИЙ НОВГОРОД, СТРИГИНО
GOJ / РОССИЯ
МОСКВА, ШЕРЕМЕТЬЕВО
SVO B / АЭРОФЛОТ
ОРСК
OSW / АЭРОФЛОТ
МОСКВА, ШЕРЕМЕТЬЕВО
SVO B / РОССИЯ
НИЖНИЙ НОВГОРОД, СТРИГИНО
GOJ
РЕЙС
SU-6230 L
КЛАСС ДАТА ВРЕМЯ ОТПР ВРЕМЯ ПРИБ СТАТУС БАЗОВЫЙ ТАРИФ БАГ
0PC
10МАЯ 0820
0940
OK
SU-1060 L
10МАЯ 1040
SU-1061 T
24МАЯ 1620
SU-6347 T
25МАЯ 0010
1510
1700
0125
OK
OK
OK
LNBR
ECONOMY
LNBR
ECONOMY
TNBR
ECONOMY
TNBR
ECONOMY
0PC
0PC
0PC
ПЕРЕДАТ. НАДПИСИ/ОГРАНИЧ.:
РАСЧЕТ ТАРИФА:
ТАРИФ
СБОР/TAX
ИТОГО ПО БИЛЕТУ
: RUB21900
: RUB0
: RUB21900
СБОР СА
СБОР АСБ
ВСЕГО К ОПЛАТЕ
: RUB0
: RUB0
: RUB0
YR480RUB XT-480RUB
УВЕДОМЛЕНИЕ:
"""


ALVIR_AEROFLOT = """
Электронный билет (маршрут/квитанция для пассажира)
ДАТА : 22АПР25
ФАМИЛИЯ : AGLULLIN/ALVIR FURKATOVICH MR
ОТПРВ/НАЗН : NBCNBC
ПС9208557268
ВЫДАН ОТ
НОМЕР БИЛЕТА
В ОБМЕН НА
ПЕРВОН. ВЫДАН
: АЭРОФЛОТ
: 555 2354215971
:
:
КОД БРОНИРОВАНИЯ
КОДЫ РЕГИСТРАЦИИ НА РЕЙС
КОД ТУРА
БОНУСНАЯ КАРТА
: 3TP5KF
МАРШРУТ/ПЕРЕВОЗЧИК
НИЖНЕКАМСК/НАБЕРЕЖНЫЕ ЧЕЛНЫ,
БЕГИШЕВО
NBC / АЭРОФЛОТ
МОСКВА, ШЕРЕМЕТЬЕВО
SVO B / АЭРОФЛОТ
НИЖНЕКАМСК/НАБЕРЕЖНЫЕ ЧЕЛНЫ,
БЕГИШЕВО
NBC
ПЕРЕДАТ. НАДПИСИ/ОГРАНИЧ.:
РАСЧЕТ ТАРИФА:
ТАРИФ
СБОР/TAX
ИТОГО ПО БИЛЕТУ
: RUB23720
: RUB1250
: RUB24970
СБОР СА
СБОР АСБ
: RUB0
: RUB500
ВСЕГО К ОПЛАТЕ
: RUB25470
РЕЙС КЛАСС ДАТА ВРЕМЯ
ОТПР
27АПР 1720
SU-
1681
R
ВРЕМЯ
ПРИБ
1920
СТАТУС БАЗОВЫЙ
OK
ТАРИФ
RCLR
ECONOMY
SU-
1252
K
30АПР 1815
2000
OK
KCLR
ECONOMY
БАГ
23KG
23KG
YR240RUB XT1010RUB
УВЕДОМЛЕНИЕ:
"""


MODERN_AEROFLOT = """
Электронный билет
(маршрут/квитанция)
Electronic ticket
(itinerary/receipt)
Фамилия пассажира
Name of passenger
AKHTIAMOV/ILDAR F MR
Документ/Document
ПС9218390547
Рейс/Flight
Вылет/Departure
Прибытие/Arrival
SU 1193
18:40
16.06.2025
Казань
Казань 1
20:25
16.06.2025
Москва
Шереметьево B
Перевозчик/Carrier: ПАО АЭРОФЛОТ
Статус/Status: OK
Вид тарифа/Fare basis: TCOENR
Рейс/Flight
Вылет/Departure
Прибытие/Arrival
SU 1586
22:05
19.06.2025
1 ч 25 мин
19 июня 2025
четверг
Москва
Шереметьево B
23:30
19.06.2025
Чебоксары
Перевозчик/Carrier: ПАО АЭРОФЛОТ
Статус/Status: OK
Вид тарифа/Fare basis: RCOENR
Дополнительные детали/Additional details
Расчет тарифа/Fare calculation
KZN SU MOW9180SU CSY6800RUB15980END
Тариф/Fare
15980.00РУБ
Сбор/Tax/fee/charge
YR240РУБ RI850РУБ
Итого/Total
17070РУБ
Номер билета
Ticket number
555 6121862513
Выдан от/Issued by
АЭРОФЛОТ
Дата выдачи
Date of issue
23 апреля 2025
Данные бронирования
Booking ref
3F31S6/1H S7T6RR/SU
Место выдачи
Place of issue
Багаж/Baggage allow: 1КМ
Класс/Class: Эконом/T
Багаж/Baggage allow: 1КМ
Класс/Class: Эконом/R
"""


PARTNER_HOTEL = """
1/1
Reservation 134555371 made on 18.04.25
This accommodation is booked by our partner
Пассажирский Сервисный Центр
+7 9103844101
Hard Rock Hotel Shenzhen
518110, No. 9 Mission Hills Road, Shenzhen
8675533952888
Check-in
28.04.2025, from
15:00:00
Check-out:
01.05.2025, until
12:00:00
Double Suite (full double bed) (bed type is subject to availability), for 1 adult
Bedding:
Guests:
Double bed
Koloskov Evgenii
Important. Please Note
Hotels may charge additional mandatory fees payable by the guest directly at the property, including city tax and resort fee.
Amendment & Cancellation Policy
An alteration of Reservation by the Customer is considered as a cancellation of Reservation and making new Reservation.
Cancellation of reservation or no-show may result in penalties, according to rate and contract terms.
Please notify in advance if you expect to check-in after 6 pm. Hotel may cancel the reservation and charge the no-show fee in case you don’t show up by that time.
Meal type
Breakfast included
Deposit
Deposit
1000
CNY
per room for the night
GPS 22.726416 114.06274
"""


def test_compact_aeroflot_keeps_every_segment_and_reissue_ticket_total():
    parsed = _parse_old_aeroflot(OLD_AEROFLOT)

    assert parsed is not None
    assert parsed["ticket_number"] == "555 2353939951"
    assert parsed["passenger_name"] == "GRIDNEVSKII ALEKSANDR GENNADEVICH"
    assert [segment["flightNo"] for segment in parsed["segments"]] == ["SU6230", "SU1060", "SU1061", "SU6347"]
    assert [(segment["dep"], segment["arr"]) for segment in parsed["segments"]] == [
        ("08:20", "09:40"), ("10:40", "15:10"), ("16:20", "17:00"), ("00:10", "01:25")
    ]
    # This exchanged ticket prints ВСЕГО К ОПЛАТЕ = 0, but the actual ticket
    # subtotal remains 21 900 and must not disappear from the editor.
    assert parsed["total"] == Decimal("21900")
    assert {row["code"]: Decimal(row["amount"]) for row in parsed["tax_breakdown"]} == {
        "YR": Decimal("480"), "XT": Decimal("-480")
    }


def test_compact_aeroflot_supports_split_flight_number_columns():
    parsed = _parse_old_aeroflot(ALVIR_AEROFLOT)

    assert parsed is not None
    assert [segment["flightNo"] for segment in parsed["segments"]] == ["SU1681", "SU1252"]
    assert parsed["segments"][0]["fromCode"] == "NBC"
    assert parsed["segments"][0]["toCode"] == "SVO B"
    assert parsed["fare"] == Decimal("23720")
    assert parsed["taxes"] == Decimal("1250")
    assert parsed["fees"] == Decimal("500")
    assert parsed["total"] == Decimal("25470")


def test_compact_aeroflot_reads_human_readable_cost_without_mixing_included_fees():
    text = OLD_AEROFLOT.replace(
        ": RUB21900\n: RUB0\n: RUB21900\nСБОР СА\nСБОР АСБ\nВСЕГО К ОПЛАТЕ\n: RUB0\n: RUB0\n: RUB0",
        "СТОИМОСТЬ:\nВ ТОМ ЧИСЛЕ СБОР АСБ:\nВ ТОМ ЧИСЛЕ СБОР СА:\n20 982,63 РУБ.\n120,00 РУБ.\n0,00 РУБ.",
    )
    parsed = _parse_old_aeroflot(text)

    assert parsed is not None
    assert parsed["fare"] == Decimal("20982.63")
    assert parsed["fees"] == Decimal("0")
    assert parsed["total"] == Decimal("20982.63")


def test_modern_aeroflot_itinerary_preserves_both_flights_and_tax_rows():
    parsed = _parse_modern_aeroflot(MODERN_AEROFLOT)

    assert parsed is not None
    assert parsed["passenger_name"] == "AKHTIAMOV ILDAR F"
    assert [segment["flightNo"] for segment in parsed["segments"]] == ["SU1193", "SU1586"]
    assert [(segment["from"], segment["to"]) for segment in parsed["segments"]] == [
        ("Казань", "Москва"), ("Москва", "Чебоксары")
    ]
    assert parsed["fare"] == Decimal("15980.00")
    assert parsed["taxes"] == Decimal("1090")
    assert parsed["total"] == Decimal("17070")
    assert [row["code"] for row in parsed["tax_breakdown"]] == ["YR", "RI"]


def test_partner_hotel_is_structured_instead_of_mixed_text():
    parsed = _partner_hotel_from_pdfminer(PARTNER_HOTEL)

    assert parsed is not None
    assert parsed["hotel"]["name"] == "Hard Rock Hotel Shenzhen"
    assert parsed["hotel"]["address"] == "518110, No. 9 Mission Hills Road, Shenzhen"
    assert parsed["hotel"]["phone"] == "8675533952888"
    assert parsed["passenger_name"] == "Koloskov Evgenii"
    assert parsed["rooms"][0]["bedType"] == "Double bed"
    assert parsed["rooms"][0]["meal"] == "Завтрак"
    assert parsed["hotelTerms"]["deposit"] == "1000 CNY per room for the night"
    assert parsed["segments"][0]["date"] == "28.04.2025"
    assert parsed["segments"][0]["endDate"] == "01.05.2025"
    assert parsed["nights"] == 3


def test_russian_aeroflot_group_keeps_each_passenger_and_all_segment_fields():
    page = """27 октября 2025
Маршрутная квитанция электронного билета
{passenger}
Документ: {document}
№ эл.билета: {ticket}
МАРШРУТ СЛЕДОВАНИЯ
Код бронирования* 6RZ483 Москва Благовещенск Рейс: SU 6255
08 дек. 2025 18:40 SVO B Шереметьево, B
09 дек. 2025 Перевозчик: Россия* BQS 07:50 Аэропорт Игнатьево
Класс: Эконом / P
Вид тарифа: PCDSOC
Статус: Оформлен
Провоз багажа: 1 место до 23 кг
Посадка заканчивается за 20 мин.
Тариф RUB 6400.00
Итого по тарифу/сборам 6400.00 RUB
"""
    text = page.format(passenger="KISELEVA MARGARITA SEMENOVNA", document="2202559501", ticket="5556171217873")
    text += "\f" + page.format(passenger="DUBROVSKAIA IRINA ILINICHNA", document="2201903735", ticket="5556171217874")

    parsed = _parse_russian_aeroflot_group(text)

    assert parsed is not None
    assert parsed["receipt_count"] == 2
    assert [row["ticket_number"] for row in parsed["receipts"]] == ["5556171217873", "5556171217874"]
    assert [row["passenger_name"] for row in parsed["receipts"]] == [
        "KISELEVA MARGARITA SEMENOVNA", "DUBROVSKAIA IRINA ILINICHNA",
    ]
    segment = parsed["segments"][0]
    assert segment == {
        "from": "Москва", "fromCode": "SVO B", "to": "Благовещенск", "toCode": "BQS",
        "date": "08.12.2025", "endDate": "09.12.2025", "dep": "18:40", "arr": "07:50",
        "flightNo": "SU6255", "carrier": "Россия", "cls": "P", "status": "Оформлен",
        "fareBasis": "PCDSOC", "cabin": "Эконом", "baggage": "1 место до 23 кг", "dir": "out",
    }
    assert parsed["total"] == Decimal("12800.00")
