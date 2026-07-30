from documents.receipt_avia_brand_patch import parse_brand_avia_ticket


def test_brand_ticket_keeps_seller_out_of_route_and_airline():
    text = """
    ИП Хмель Марина Валерьевна 8(831) 246-02-04 mkhmel@list.ru
    ЭЛЕКТРОННЫЙ БИЛЕТ
    (маршрут-квитанция для пассажира)
    Заказ №5997319
    код бронирования: 79KB4S
    Пассажир Номер документа Номер билета Бонусная карта Продажа
    KOLOSKOV EVGENII IUREVICH 4619509273 555 2371700770 233791154 28.10.2025
    Рейс под брендом авиакомпании Аэрофлот
    Новосибирск, Толмачево, OVB Москва, Шереметьево, SVO (Терминал B) Авиакомпания-перевозчик Рейс Тариф Багаж Статус
    18:55
    Чт, 30 Октября 2025
    18:55
    Чт, 30 Октября 2025 Аэрофлот SU-1461 BUSINESS
    ZCOR 32KG x2 OK
    РАСЧЕТ ТАРИФА:
    """

    fields = parse_brand_avia_ticket(text)

    assert fields["carrier"] == "Аэрофлот"
    assert fields["issuer"] == "Аэрофлот"
    assert fields["passenger_name"] == "KOLOSKOV EVGENII IUREVICH"
    assert fields["document_number"] == "4619509273"
    assert fields["ticket_number"] == "555 2371700770"
    assert fields["issue_date"] == "28.10.2025"

    segment = fields["segments"][0]
    assert segment["from"] == "Новосибирск, Толмачево"
    assert segment["fromCode"] == "OVB"
    assert segment["to"] == "Москва, Шереметьево"
    assert segment["toCode"] == "SVO"
    assert segment["carrier"] == "Аэрофлот"
    assert segment["flightNo"] == "SU-1461"
    assert segment["cls"] == "BUSINESS"
    assert segment["fareBasis"] == "ZCOR"
    assert segment["baggage"] == "32KG x2"
    assert segment["status"] == "OK"
    assert "ИП" not in f'{segment["from"]} {segment["to"]} {segment["carrier"]}'


def test_non_matching_pdf_text_is_left_untouched():
    assert parse_brand_avia_ticket("Обычный документ без авиамаршрута") == {}
