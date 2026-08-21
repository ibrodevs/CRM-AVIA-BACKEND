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
