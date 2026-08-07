from decimal import Decimal

from documents.receipt_preflight_patch import _parse_known_pages, _parser_failure


def rzd_page(*, passenger, passport, dob, ticket, seat, ticket_cost, reserved):
    return f"""
ЭЛЕКТРОННЫЙ БИЛЕТ. КОНТРОЛЬНЫЙ КУПОН
ПОЕЗД ВАГОН МЕСТО
098 04 {seat}
№ {ticket}
06:44 14.12.2025 вс Курган
16:42 14.12.2025 вс Омск-Пассажирский
ПАСПОРТ РФ {passport} {dob} RUS М {passenger}
Посадка в поезд осуществляется при предъявлении документа.
098*СА 14.12.2025 06:44 04К {seat} КУРГАН - ОМСК-ПАССАЖИРСКИЙ ПН{passport} TEST 010101
Оформлен: 31.10.2025 13:07
Заказ: {ticket.replace(' ', '')}
Перевозчик: ФПК ДАЛЬНЕВОСТОЧНЫЙ / ФПК ИНН 7708709686
Оплата банковской картой ****9574
Билет Плацкарта НДС 0% НДС 20%
{ticket_cost} ₽ {reserved} ₽ 0,00 ₽ 77,50 ₽ Итого
Вкл. НДС {str(Decimal(ticket_cost.replace(' ', '').replace(',', '.')) + Decimal(reserved.replace(' ', '').replace(',', '.'))).replace('.', ',')} ₽
"""


def test_eight_page_rzd_pdf_is_recognised_before_generic_parser():
    pages = [
        rzd_page(passenger="ШВАНГИРАДЗЕ ДАВИД ЗАЗОВИЧ", passport="5626790603", dob="26.03.2004", ticket="78 706 152 276 981", seat="025", ticket_cost="2 627,60", reserved="1 806,60"),
        rzd_page(passenger="РУДАКОВ ГРИГОРИЙ КОНСТАНТИНОВИЧ", passport="3222450293", dob="25.08.2002", ticket="78 706 152 276 992", seat="026", ticket_cost="1 839,30", reserved="1 404,10"),
        rzd_page(passenger="МОСКВИН КИРИЛЛ ЕВГЕНЬЕВИЧ", passport="6724345545", dob="04.03.2005", ticket="78 706 152 277 003", seat="027", ticket_cost="2 627,60", reserved="1 806,60"),
        rzd_page(passenger="ЛАРИЧЕВ ВЛАДИСЛАВ ФЕДОРОВИЧ", passport="7124943913", dob="12.02.2005", ticket="78 706 152 277 014", seat="028", ticket_cost="1 839,30", reserved="1 404,10"),
        rzd_page(passenger="АРЗАМАСЦЕВ АРТЕМ ДМИТРИЕВИЧ", passport="5625865801", dob="19.07.2005", ticket="78 706 152 277 025", seat="033", ticket_cost="2 627,60", reserved="1 806,60"),
        rzd_page(passenger="ФИЛЛИПОВ ТИМОФЕЙ КОНСТАНТИНОВИЧ", passport="5624822038", dob="17.09.2004", ticket="78 706 152 277 036", seat="034", ticket_cost="1 839,30", reserved="1 404,10"),
        rzd_page(passenger="ПОЛШКОВ АРСЕНТИЙ АЛЕКСАНДРОВИЧ", passport="5623783366", dob="22.01.2004", ticket="78 706 152 277 040", seat="035", ticket_cost="2 627,60", reserved="1 806,60"),
        rzd_page(passenger="ЦИУЛИН ДАНИЛ АЛЕКСАНДРОВИЧ", passport="9219701077", dob="27.12.2005", ticket="78 706 152 277 051", seat="036", ticket_cost="1 839,30", reserved="1 404,10"),
    ]

    result = _parse_known_pages(pages)

    assert result is not None
    assert result["status"] == "parsed"
    assert result["raw"]["source_coupon_pages"] == 8
    assert result["raw"]["parsed_coupon_pages"] == 8
    assert result["fields"]["receipt_count"] == 8
    assert len(result["fields"]["receipts"]) == 8
    assert [row["legs"][0]["seat"] for row in result["fields"]["receipts"]] == [
        "025", "026", "027", "028", "033", "034", "035", "036"
    ]
    assert [row["passenger"] for row in result["fields"]["receipts"]] == [
        "ШВАНГИРАДЗЕ ДАВИД ЗАЗОВИЧ",
        "РУДАКОВ ГРИГОРИЙ КОНСТАНТИНОВИЧ",
        "МОСКВИН КИРИЛЛ ЕВГЕНЬЕВИЧ",
        "ЛАРИЧЕВ ВЛАДИСЛАВ ФЕДОРОВИЧ",
        "АРЗАМАСЦЕВ АРТЕМ ДМИТРИЕВИЧ",
        "ФИЛЛИПОВ ТИМОФЕЙ КОНСТАНТИНОВИЧ",
        "ПОЛШКОВ АРСЕНТИЙ АЛЕКСАНДРОВИЧ",
        "ЦИУЛИН ДАНИЛ АЛЕКСАНДРОВИЧ",
    ]
    assert result["fields"]["total"] == Decimal("30710.40")


def test_incomplete_coupon_set_goes_to_review_not_error():
    good = rzd_page(passenger="ШВАНГИРАДЗЕ ДАВИД ЗАЗОВИЧ", passport="5626790603", dob="26.03.2004", ticket="78 706 152 276 981", seat="025", ticket_cost="2 627,60", reserved="1 806,60")
    broken = "ЭЛЕКТРОННЫЙ БИЛЕТ. КОНТРОЛЬНЫЙ КУПОН\nПОЕЗД ВАГОН МЕСТО\n098 04 026"

    result = _parse_known_pages([good, broken])

    assert result is not None
    assert result["status"] == "manual_review"
    assert result["raw"]["source_coupon_pages"] == 2
    assert result["raw"]["parsed_coupon_pages"] == 1
    assert "1 из 2" in result["warnings"][0]


def test_generic_parser_exception_is_not_a_fatal_upload_error():
    result = _parser_failure(RuntimeError("parser crashed"))

    assert result["status"] == "manual_review"
    assert result["raw"]["parser_exception"] == "RuntimeError"
    assert result["confidence"] == Decimal("0")
