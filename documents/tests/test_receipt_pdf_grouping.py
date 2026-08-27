from decimal import Decimal

from documents import receipt_pdf_grouping as grouping


def _avia_fields(ticket: str, passenger: str, fare: str) -> dict:
    return {
        "service_kind": "avia",
        "service_type": "Авиа",
        "issuer": "Тестовая авиакомпания",
        "passenger_name": passenger,
        "ticket_number": ticket,
        "fare": fare,
        "taxes": "100",
        "fees": "50",
        "total": str(Decimal(fare) + Decimal("150")),
        "currency": "RUB",
        "segments": [{
            "from": "Москва",
            "fromCode": "MOW",
            "to": "Сочи",
            "toCode": "AER",
            "date": "21.08.2026",
            "flightNo": "SU100",
        }],
    }


def _parser(content: bytes, *, mime: str = "", name: str = "") -> dict:
    text = content.decode("utf-8")
    if "TICKET-1" in text:
        fields = _avia_fields("5550000000001", "ПЕРВЫЙ ПАССАЖИР", "1000")
    elif "TICKET-2" in text:
        fields = _avia_fields("5550000000002", "ВТОРОЙ ПАССАЖИР", "2000")
    else:
        fields = {"service_kind": "other"}
    return {"fields": fields, "raw": dict(fields), "warnings": [], "status": "parsed", "confidence": Decimal("0.99")}


def test_multi_page_avia_pdf_becomes_independent_child_tickets(monkeypatch):
    marker = "МАРШРУТ/ПЕРЕВОЗЧИК ОТПРВ/НАЗН РЕЙС"
    monkeypatch.setattr(grouping, "_best_page_texts", lambda _content: [
        (0, f"{marker} TICKET-1"),
        (1, f"{marker} TICKET-2"),
    ])
    initial = _parser(b"TICKET-1", mime="text/plain", name="group.pdf")

    result = grouping.apply_pdf_grouping(
        _parser,
        b"%PDF grouped",
        mime="application/pdf",
        name="group.pdf",
        result=initial,
    )

    items = result["fields"]["receipt_items"]
    assert [item["ticketNo"] for item in items] == ["5550000000001", "5550000000002"]
    assert [item["sourcePage"] for item in items] == [1, 2]
    assert [Decimal(item["total"]) for item in items] == [Decimal("1150"), Decimal("2150")]
    assert result["fields"]["total"] == Decimal("3300")
    assert result["fields"]["document_is_container"] is True
    assert result["status"] == "parsed"


def test_duplicate_continuation_page_is_not_a_second_ticket(monkeypatch):
    marker = "МАРШРУТ/ПЕРЕВОЗЧИК ОТПРВ/НАЗН РЕЙС"
    monkeypatch.setattr(grouping, "_best_page_texts", lambda _content: [
        (0, f"{marker} TICKET-1"),
        (1, f"{marker} TICKET-1 continuation"),
        (2, f"{marker} TICKET-2"),
    ])
    initial = _parser(b"TICKET-1", mime="text/plain", name="group.pdf")

    result = grouping.apply_pdf_grouping(
        _parser,
        b"%PDF grouped",
        mime="application/pdf",
        name="group.pdf",
        result=initial,
    )

    items = result["fields"]["receipt_items"]
    assert len(items) == 2
    assert items[0]["sourcePages"] == [1, 2]
    assert result["raw"]["deduplicated_blank_count"] == 1


def test_existing_group_keeps_non_ordinal_supplier_pages(monkeypatch):
    first = {**_avia_fields("5550000000001", "ПЕРВЫЙ", "1000"), "receiptPage": 1}
    second = {**_avia_fields("5550000000002", "ВТОРОЙ", "2000"), "receiptPage": 3}
    initial = {
        "fields": {**first, "receipt_items": [first, second]},
        "raw": {},
        "warnings": [],
        "status": "parsed",
        "confidence": Decimal("0.99"),
    }
    monkeypatch.setattr(grouping, "_best_page_texts", lambda _content: [
        (0, "page 1"), (1, "terms"), (2, "page 3"), (3, "terms"),
    ])

    result = grouping.apply_pdf_grouping(
        _parser,
        b"%PDF grouped",
        mime="application/pdf",
        name="group.pdf",
        result=initial,
    )

    assert [item["sourcePage"] for item in result["fields"]["receipt_items"]] == [1, 3]


def test_rail_group_with_common_order_number_keeps_all_child_blanks(monkeypatch):
    first = {
        "service_kind": "rail",
        "service_type": "ЖД",
        "passenger_name": "ИВАНОВ ИВАН ИВАНОВИЧ",
        "ticket_number": "78706152276981",
        "document_number": "5624000001",
        "total": "4434.20",
        "currency": "RUB",
        "segments": [{
            "from": "Курган",
            "to": "Омск",
            "date": "14.12.2025",
            "dep": "06:44",
            "flightNo": "098",
            "coach": "04",
            "seat": "025",
        }],
        "receiptPage": 1,
    }
    second = {
        "service_kind": "rail",
        "service_type": "ЖД",
        "passenger_name": "ПЕТРОВ ПЕТР ПЕТРОВИЧ",
        # Common group order number printed as ticket number on all coupons
        "ticket_number": "78706152276981",
        "document_number": "5624000002",
        "total": "3243.40",
        "currency": "RUB",
        "segments": [{
            "from": "Курган",
            "to": "Омск",
            "date": "14.12.2025",
            "dep": "06:44",
            "flightNo": "098",
            "coach": "04",
            "seat": "026",
        }],
        "receiptPage": 2,
    }
    initial = {
        "fields": {**first, "receipt_items": [first, second]},
        "raw": {},
        "warnings": [],
        "status": "parsed",
        "confidence": Decimal("0.99"),
    }
    monkeypatch.setattr(grouping, "_best_page_texts", lambda _content: [
        (0, "page 1"), (1, "page 2"),
    ])

    result = grouping.apply_pdf_grouping(
        _parser,
        b"%PDF rail group",
        mime="application/pdf",
        name="rail_group.pdf",
        result=initial,
    )

    items = result["fields"]["receipt_items"]
    assert len(items) == 2
    assert [item["passenger"] for item in items] == ["ИВАНОВ ИВАН ИВАНОВИЧ", "ПЕТРОВ ПЕТР ПЕТРОВИЧ"]
    assert [item["segments"][0]["seat"] for item in items] == ["025", "026"]
    assert result["raw"].get("deduplicated_blank_count", 0) == 0


def test_rail_passenger_with_multiple_seats_keeps_all_blanks(monkeypatch):
    first = {
        "service_kind": "rail",
        "service_type": "ЖД",
        "passenger_name": "МЫШЛЯЕВ ДЕНИС АЛЕКСАНДРОВИЧ",
        "ticket_number": "74205065230155",
        "document_number": "2221359314",
        "total": "4819.20",
        "currency": "RUB",
        "segments": [{
            "from": "Нижний Новгород",
            "to": "Москва",
            "date": "18.06.2025",
            "dep": "09:30",
            "flightNo": "719ГА",
            "coach": "01",
            "seat": "025",
        }],
        "receiptPage": 1,
    }
    second = {
        "service_kind": "rail",
        "service_type": "ЖД",
        "passenger_name": "МЫШЛЯЕВ ДЕНИС АЛЕКСАНДРОВИЧ",
        "ticket_number": "74205065230166",
        "document_number": "2221359314",
        "total": "4819.20",
        "currency": "RUB",
        "segments": [{
            "from": "Нижний Новгород",
            "to": "Москва",
            "date": "18.06.2025",
            "dep": "09:30",
            "flightNo": "719ГА",
            "coach": "01",
            "seat": "026",
        }],
        "receiptPage": 2,
    }
    initial = {
        "fields": {**first, "receipt_items": [first, second]},
        "raw": {},
        "warnings": [],
        "status": "parsed",
        "confidence": Decimal("0.99"),
    }
    monkeypatch.setattr(grouping, "_best_page_texts", lambda _content: [
        (0, "page 1"), (1, "page 2"),
    ])

    result = grouping.apply_pdf_grouping(
        _parser,
        b"%PDF rail group",
        mime="application/pdf",
        name="rail_group.pdf",
        result=initial,
    )

    items = result["fields"]["receipt_items"]
    assert len(items) == 2
    assert [item["segments"][0]["seat"] for item in items] == ["025", "026"]
    assert result["raw"].get("deduplicated_blank_count", 0) == 0


def test_rail_group_same_route_and_amount_keeps_neighbour_seats(monkeypatch):
    passengers = [
        ("МАСЛЮКОВ АЛЕКСЕЙ", "005"),
        ("ФАХРУТДИНОВ РУСТАМ", "033"),
        ("ШАРДАНОВ ИЛЬЯ", "034"),
        ("ШУТОВ ДМИТРИЙ", "035"),
    ]
    tickets = [
        {
            "service_kind": "rail",
            "service_type": "ЖД",
            "passenger_name": passenger,
            "ticket_number": "021AA-GROUP",
            "document_number": "",
            "total": "21489.20",
            "currency": "RUB",
            "segments": [{
                "from": "САНКТ-ПЕТЕРБУРГ-ГЛАВНЫЙ",
                "to": "МОСКВА ОКТЯБРЬСКАЯ",
                "date": "15.03.2025",
                "flightNo": "021АА",
                "coach": "13",
                "seat": seat,
            }],
            "receiptPage": index + 1,
        }
        for index, (passenger, seat) in enumerate(passengers)
    ]
    initial = {
        "fields": {**tickets[0], "receipt_items": tickets},
        "raw": {},
        "warnings": [],
        "status": "parsed",
        "confidence": Decimal("0.99"),
    }
    monkeypatch.setattr(
        grouping,
        "_best_page_texts",
        lambda _content: [(index, f"page {index + 1}") for index in range(len(tickets))],
    )

    result = grouping.apply_pdf_grouping(
        _parser,
        b"%PDF rail group",
        mime="application/pdf",
        name="rail_group.pdf",
        result=initial,
    )

    items = result["fields"]["receipt_items"]
    assert len(items) == 4
    assert [item["passenger"] for item in items] == [passenger for passenger, _seat in passengers]
    assert [item["segments"][0]["seat"] for item in items] == ["005", "033", "034", "035"]
    assert result["raw"].get("deduplicated_blank_count", 0) == 0
