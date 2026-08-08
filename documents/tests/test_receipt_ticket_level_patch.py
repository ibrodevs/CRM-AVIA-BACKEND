from decimal import Decimal

from documents.models import ReceiptDraft
from documents.receipt_ticket_level_patch import (
    _rail_aggregate,
    normalize_receipt_items,
    receipt_item_total,
    receipt_items_from,
)


def _rail_item(name, ticket_no, seat, ticket_cost, reserved_cost):
    total = Decimal(ticket_cost) + Decimal(reserved_cost)
    return {
        "service_kind": "rail",
        "passenger_name": name,
        "ticket_number": ticket_no,
        "document_number": "4510123456",
        "date_of_birth": "30.03.1993",
        "ticketCost": ticket_cost,
        "reservedSeatCost": reserved_cost,
        "agencyServiceFee": "0",
        "additionalFees": "0",
        "total": str(total),
        "currency": "RUB",
        "segments": [
            {
                "from": "САНКТ-ПЕТЕРБУРГ-ГЛАВНЫЙ",
                "to": "МОСКВА ОКТЯБРЬСКАЯ",
                "date": "15.03.2025",
                "flightNo": "021АА",
                "coach": "13",
                "seat": seat,
            }
        ],
    }


def test_receipt_draft_has_persistent_child_ticket_storage():
    field = ReceiptDraft._meta.get_field("receipt_items")
    assert field.get_internal_type() == "JSONField"


def test_group_pdf_is_normalized_as_independent_ticket_items():
    first = _rail_item("ПРОХОРОВ АЛЕКСЕЙ НИКОЛАЕВИЧ", "71853988581936", "013", "5000.00", "1221.50")
    second = _rail_item("БЯКИН МИХАИЛ ИЛЬИЧ", "71853988581937", "014", "3000.00", "535.80")
    payload = {"supplier_original": {"verified_data": {"groupTickets": [first, second]}}}

    assert receipt_items_from(payload) == [first, second]
    items = normalize_receipt_items(payload, parser_status="parsed", service_kind="rail")

    assert len(items) == 2
    assert items[0]["passenger"] == "ПРОХОРОВ АЛЕКСЕЙ НИКОЛАЕВИЧ"
    assert items[0]["ticketNo"] == "71853988581936"
    assert items[0]["passengers"][0]["document"] == "4510123456"
    assert items[0]["legs"][0]["seat"] == "013"
    assert items[0]["receiptCount"] == 1
    assert items[1]["ticketNo"] == "71853988581937"
    assert items[1]["legs"][0]["seat"] == "014"
    assert items[0]["groupTickets"] == []
    assert items[1]["groupTickets"] == []


def test_each_ticket_keeps_own_cost_and_parent_total_is_sum_of_children():
    first = _rail_item("ПРОХОРОВ АЛЕКСЕЙ НИКОЛАЕВИЧ", "71853988581936", "013", "5000.00", "1221.50")
    second = _rail_item("БЯКИН МИХАИЛ ИЛЬИЧ", "71853988581937", "014", "3000.00", "535.80")

    assert receipt_item_total(first) == Decimal("6221.50")
    assert receipt_item_total(second) == Decimal("3535.80")

    fare, taxes, fees, total = _rail_aggregate([first, second])
    assert fare == Decimal("9757.30")
    assert taxes == Decimal("0")
    assert fees == Decimal("0")
    assert total == Decimal("9757.30")
