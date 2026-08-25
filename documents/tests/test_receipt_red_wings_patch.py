from documents.receipt_metadata import receipt_verified_data
from documents.receipt_red_wings_patch import _parse_red_wings


RED_WINGS_ITINERARY = """
ИП ХМЕЛЬ МАРИНА ВАЛЕРЬЕВНА
ИНН 525803171496
Электронный билет
(маршрут/квитанция)
Electronic ticket
(itinerary/receipt)
Фамилия пассажира
Name of passenger
КОРБУТ/АНАСТАСИЯ И Г-ЖА
Документ/Document
ПС7622223740
Нижний Новгород
Екатеринбург
Рейс/Flight
Вылет/Departure
Прибытие/Arrival
WZ 2030
11:50
25.05.2025
1 ч 55 мин
15:45
25.05.2025
25 мая 2025
воскресенье
Нижний Новгород
Стригино
Екатеринбург
Кольцово
Перевозчик/Carrier: АО РЕД ВИНГС
Статус/Status: OK
Недействителен до/Not valid before:
Вид тарифа/Fare basis: GGROPS
Недействителен после/Not valid after:
Бренд/Brand: Standard
Номер билета
Ticket number
Выдан от/Issued by
Дата выдачи
Date of issue
309 6123763438
РЕД ВИНГС
23 мая 2025
Данные бронирования
Booking ref
3ZXWLR/1H CPV3DK/WZ
Место выдачи
Place of issue
11НЖС ТКП
ИП ХМЕЛЬ М.В.
НИЖНИЙ НОВГОРОД РФ
92199866 0001 1
В пути/Travel time:
1 ч 55 мин
Багаж/Baggage allow:
1КМ
Класс/Class:
Эконом/G
Рейс выполняет/Flight operated by:
АО РЕД ВИНГС
Дополнительные детали/Additional details
Код тура/Tour code
Передаточные надписи/Ограничения
Endorsements/Restrictions
PSPT ПС/7622223740/РФ/НДСА/К0.00/GRP-3ZXWLR СПОРТ
Сведения об оплате/Payment information
Форма оплаты/Form of payment
НАЛ
Расчет тарифа/Fare calculation
Тариф/Fare
Эквив. тарифа/Equivalent fare paid
Сбор/Tax/fee/charge
Итого/Total
IT
IT
Уведомление
"""

RED_WINGS_NUMERIC_ITINERARY = RED_WINGS_ITINERARY.replace(
    "IT\nIT\nУведомление",
    "INV\nGOJ WZ TBS157.68NUC157.68END ROE0.875142\n"
    "EUR138.00\nRUB13110\nRUB1425YQ RUB430SA RUB1835TU\nRUB16800\nУведомление",
)


def test_red_wings_itinerary_recognizes_every_visible_field_without_inventing_price():
    parsed = _parse_red_wings(RED_WINGS_ITINERARY)

    assert parsed is not None
    assert parsed["issuer"] == "РЕД ВИНГС"
    assert parsed["passenger_name"] == "КОРБУТ АНАСТАСИЯ И"
    assert parsed["document_number"] == "ПС7622223740"
    assert parsed["ticket_number"] == "309 6123763438"
    assert parsed["reference"] == "3ZXWLR/1H CPV3DK/WZ"
    assert parsed["issue_date"] == "23.05.2025"
    assert parsed["booking_status"] == "OK"
    assert parsed["booking_class"] == "G"
    assert parsed["fare_basis"] == "GGROPS"
    assert parsed["baggage"] == "1КМ"
    assert parsed["brand"] == "Standard"
    assert parsed["payment_method"] == "НАЛ"
    assert parsed["output"]["priceMode"] == "it"
    assert parsed["fare"] is None
    assert parsed["taxes"] is None
    assert parsed["fees"] is None
    assert parsed["total"] is None

    assert len(parsed["segments"]) == 1
    segment = parsed["segments"][0]
    assert segment["flightNo"] == "WZ2030"
    assert segment["from"] == "Нижний Новгород"
    assert segment["fromAddress"] == "Стригино"
    assert segment["to"] == "Екатеринбург"
    assert segment["toAddress"] == "Кольцово"
    assert segment["date"] == "25.05.2025"
    assert segment["endDate"] == "25.05.2025"
    assert segment["dep"] == "11:50"
    assert segment["arr"] == "15:45"
    assert segment["duration"] == "1 ч 55 мин"
    assert segment["carrier"] == "АО РЕД ВИНГС"
    assert segment["operatedBy"] == "АО РЕД ВИНГС"
    assert segment["cabin"] == "Эконом"
    assert segment["cls"] == "G"
    assert segment["status"] == "OK"
    assert segment["fareBasis"] == "GGROPS"
    assert segment["baggage"] == "1КМ"
    assert segment["brand"] == "Standard"


def test_red_wings_fields_survive_canonical_verified_data_mapping():
    parsed = _parse_red_wings(RED_WINGS_ITINERARY)
    verified = receipt_verified_data(parsed, parser_status="parsed")

    assert verified["carrier"] == "АО РЕД ВИНГС"
    assert verified["passenger"] == "КОРБУТ АНАСТАСИЯ И"
    assert verified["docNo"] == "ПС7622223740"
    assert verified["ticketNo"] == "309 6123763438"
    assert verified["ref"] == "3ZXWLR/1H CPV3DK/WZ"
    assert verified["issueDate"] == "23.05.2025"
    assert verified["bookingStatus"] == "OK"
    assert verified["cls"] == "G"
    assert verified["fareBasis"] == "GGROPS"
    assert verified["legs"][0]["fromAddress"] == "Стригино"
    assert verified["legs"][0]["toAddress"] == "Кольцово"
    assert verified["legs"][0]["duration"] == "1 ч 55 мин"
    assert verified["output"]["priceMode"] == "it"
    assert verified["recognitionPending"] is False


def test_red_wings_numeric_payment_table_keeps_both_fares_for_it_closure():
    parsed = _parse_red_wings(RED_WINGS_NUMERIC_ITINERARY)

    assert parsed is not None
    assert parsed["output"]["priceMode"] == "total"
    assert parsed["publishedFare"] == 138
    assert parsed["publishedFareCurrency"] == "EUR"
    assert parsed["equivalentFare"] == 13110
    assert parsed["equivalentFareCurrency"] == "RUB"
    assert parsed["fare"] == 13110
    assert parsed["taxes"] == 3690
    assert parsed["fees"] == 0
    assert parsed["total"] == 16800
    assert [row["code"] for row in parsed["tax_breakdown"]] == ["YQ", "SA", "TU"]
