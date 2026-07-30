from documents.receipt_metadata import receipt_verified_data


def test_aviation_segments_keep_booking_class_status_and_route_aliases():
    verified = receipt_verified_data(
        {
            "service_kind": "avia",
            "issuer": "Air Test",
            "passenger_name": "IVAN IVANOV",
            "booking_reference": "PNR123",
            "booking_status": "CONFIRMED",
            "ticket_number": "1234567890",
            "segments": [
                {
                    "origin": "Bishkek",
                    "origin_code": "FRU",
                    "destination": "Istanbul",
                    "destination_code": "IST",
                    "departure_date": "30.07.2026",
                    "departure_time": "08:15",
                    "arrival_time": "11:10",
                    "airline": "Air Test",
                    "flight_number": "AT101",
                    "booking_class": "Y",
                    "booking_status": "HK",
                    "fare_basis": "YOWKG",
                    "cabin_class": "Economy",
                    "baggage_allowance": "1PC",
                }
            ],
        },
        parser_status="parsed",
    )

    assert verified["ref"] == "PNR123"
    assert verified["bookingStatus"] == "CONFIRMED"
    assert verified["ticketNo"] == "1234567890"
    assert verified["carrier"] == "Air Test"
    assert verified["cls"] == "Y"
    assert verified["fareBasis"] == "YOWKG"

    segment = verified["legs"][0]
    assert segment["from"] == "Bishkek"
    assert segment["fromCode"] == "FRU"
    assert segment["to"] == "Istanbul"
    assert segment["toCode"] == "IST"
    assert segment["date"] == "30.07.2026"
    assert segment["dep"] == "08:15"
    assert segment["arr"] == "11:10"
    assert segment["flightNo"] == "AT101"
    assert segment["cls"] == "Y"
    assert segment["status"] == "HK"
    assert segment["fareBasis"] == "YOWKG"
    assert segment["cabin"] == "Economy"
    assert segment["baggage"] == "1PC"
