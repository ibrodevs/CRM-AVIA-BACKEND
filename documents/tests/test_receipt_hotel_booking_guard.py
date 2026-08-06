from documents.receipt_hotel_booking_guard import _clean_hotel_payload, _clean_supplier_booking


def test_supplier_booking_rejects_label_fragment():
    assert _clean_supplier_booking("рования") == ""
    assert _clean_supplier_booking("бронирования") == ""
    assert _clean_supplier_booking("Заселение по ФИО") == ""


def test_supplier_booking_keeps_real_identifier():
    assert _clean_supplier_booking("328343646") == "328343646"
    assert _clean_supplier_booking("C-0151866") == "C-0151866"


def test_hotel_payload_only_cleans_supplier_reference_fields():
    payload = {
        "service_kind": "hotel",
        "supplier_order_number": "рования",
        "reference": "Заселение по ФИО",
        "hotel_booking_number": "Заселение по ФИО",
    }

    _clean_hotel_payload(payload)

    assert payload["supplier_order_number"] == ""
    assert payload["reference"] == ""
    assert payload["hotel_booking_number"] == "Заселение по ФИО"
