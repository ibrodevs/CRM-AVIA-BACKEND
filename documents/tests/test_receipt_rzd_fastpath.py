from decimal import Decimal

from documents.receipt_rzd_fastpath import (
    _compact_passenger_summary,
    recognize_rzd_coupon_pages,
)


PASSENGERS = [
    "ИВАНОВ ИВАН ИВАНОВИЧ",
    "ПЕТРОВ ПЕТР ПЕТРОВИЧ",
    "СИДОРОВ СЕРГЕЙ СЕРГЕЕВИЧ",
    "СМИРНОВ АЛЕКСЕЙ АЛЕКСЕЕВИЧ",
    "КУЗНЕЦОВ АРТЕМ АРТЕМОВИЧ",
    "ПОПОВ ДАНИЛ ДАНИЛОВИЧ",
    "СОКОЛОВ КИРИЛЛ КИРИЛЛОВИЧ",
    "ЛЕБЕДЕВ МАКСИМ МАКСИМОВИЧ",
]
SEATS = ["025", "026", "027", "028", "033", "034", "035", "036"]


def _coupon_page(index: int) -> str:
    passenger = PASSENGERS[index]
    seat = SEATS[index]
    passport = f"56240000{index + 10:02d}"
    ticket = f"78 706 152 27{index + 6:01d} {981 + index:03d}"
    expensive = index % 2 == 0
    ticket_cost = "2 627,60" if expensive else "1 839,30"
    reserved = "1 806,60" if expensive else "1 404,10"
    total = "4 434,20" if expensive else "3 243,40"
    return f"""
ЭЛЕКТРОННЫЙ БИЛЕТ. КОНТРОЛЬНЫЙ КУПОН
ПОЕЗД ВАГОН МЕСТО
098 098 04 04 {seat} {seat}
Купе
№ {ticket}
№ {ticket}
06:44 06:44 14.12.2025 вс
Отправление по местному времени
UTC+5, МСК+2 Часовой пояс
Курган
16:42 16:42 14.12.2025 вс
Прибытие по местному времени
UTC+6, МСК+3 Часовой пояс
Омск-Пассажирский
ПАСПОРТ РФ {passport} 26.03.2004 RUS М
{passenger} {passenger}
Посадка в поезд осуществляется при предъявлении документа.
098*СА 14.12.2025 06:44 04К {seat} КУРГАН - ОМСК-ПАССАЖИРСКИЙ ПН{passport}
Оформлен: 31.10.2025 13:07
Заказ: 78706152276981
Перевозчик: ФПК ДАЛЬНЕВОСТОЧНЫЙ / ФПК ИНН 7708709686
Оплата банковской картой ****9574
Билет Плацкарта НДС 0% НДС 20%
{ticket_cost} ₽ {reserved} ₽ 0,00 ₽ 77,50 ₽ Итого
Вкл. НДС {total} ₽
"""


def test_eight_page_rzd_group_is_parsed_without_generic_engine():
    result = recognize_rzd_coupon_pages([_coupon_page(index) for index in range(8)])

    assert result is not None
    assert result["status"] == "parsed"
    assert result["confidence"] == Decimal("0.995")
    assert result["raw"]["rzd_fastpath"] is True
    assert result["raw"]["source_coupon_pages"] == 8
    assert result["raw"]["parsed_coupon_pages"] == 8

    fields = result["fields"]
    assert fields["service_kind"] == "rail"
    assert fields["receipt_count"] == 8
    assert len(fields["receipts"]) == 8
    assert len(fields["passengers"]) == 8
    assert [receipt["segments"][0]["seat"] for receipt in fields["receipts"]] == SEATS
    assert fields["total"] == Decimal("30710.40")
    assert len(fields["passenger_name"]) <= 255
    assert "8 из 8" in result["warnings"][0]


def test_partial_group_never_becomes_fatal_error():
    pages = [_coupon_page(0), "ЭЛЕКТРОННЫЙ БИЛЕТ. КОНТРОЛЬНЫЙ КУПОН\nповрежденный текст"]

    result = recognize_rzd_coupon_pages(pages)

    assert result is not None
    assert result["status"] == "manual_review"
    assert result["raw"]["source_coupon_pages"] == 2
    assert result["raw"]["parsed_coupon_pages"] == 1
    assert result["raw"]["failed_coupon_pages"] == [2]
    assert "1 из 2" in result["warnings"][0]


def test_large_group_summary_stays_inside_receipt_draft_limit():
    fields = {
        "passenger_name": ", ".join(
            f"ОЧЕНЬ ДЛИННОЕ ИМЯ ПАССАЖИРА НОМЕР {index}" for index in range(30)
        ),
        "passengers": [
            {"name": f"ОЧЕНЬ ДЛИННОЕ ИМЯ ПАССАЖИРА НОМЕР {index}"}
            for index in range(30)
        ],
    }

    _compact_passenger_summary(fields)

    assert len(fields["passenger_name"]) <= 255
    assert "+" in fields["passenger_name"]
    assert len(fields["passengers"]) == 30
