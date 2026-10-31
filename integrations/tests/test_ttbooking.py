import hashlib
from urllib.parse import urlencode

import pytest

from integrations.ttbooking import TTBookingAdapter

pytestmark = pytest.mark.django_db


def test_ttbooking_hash_uses_documented_parameter_order():
    timestamp = "2026-10-13T10:00:00+00:00"
    expected = hashlib.md5(
        urlencode(
            [
                ("agency", "42"),
                ("password", "secret"),
                ("time", timestamp),
                ("user", "7"),
            ]
        ).encode("utf-8")
    ).hexdigest()

    assert TTBookingAdapter.build_hash("42", "secret", timestamp, "7") == expected


def test_ttbooking_search_payload_maps_round_trip():
    adapter = TTBookingAdapter()

    payload = adapter._search_parameters(
        {
            "origin": "FRU",
            "destination": "IST",
            "date": "2026-11-01",
            "return_date": "2026-11-10",
            "adults": 2,
            "children": 1,
            "cabin": "economy",
        }
    )

    assert [row["locationBegin"]["code"] for row in payload["route"]] == ["FRU", "IST"]
    assert [row["locationEnd"]["code"] for row in payload["route"]] == ["IST", "FRU"]
    assert payload["seats"] == [
        {"passengerType": "ADULT", "count": 2},
        {"passengerType": "CHILD", "count": 1},
    ]
    assert payload["serviceClass"] == "ECONOMY"


def test_ttbooking_normalizes_search_to_crm_offer():
    adapter = TTBookingAdapter()
    response = {
        "respond": {
            "token": "search-token",
            "flightsGroup": {
                "flightGroup": [
                    {
                        "token": "group-token",
                        "carrier": {"code": "TK"},
                        "eticket": True,
                        "timeLimit": "2026-11-01T10:00:00+03:00",
                        "gds": "TEST",
                        "itineraries": {
                            "itinerary": [
                                {
                                    "token": "itin-token",
                                    "flights": {
                                        "flight": [
                                            {
                                                "token": "flight-token",
                                                "segments": {
                                                    "segment": [
                                                        {
                                                            "token": "segment-token",
                                                            "serviceClass": "ECONOMY",
                                                            "bookingClass": "Y",
                                                            "fareBasis": "YRT",
                                                            "carrier": {"code": "TK"},
                                                            "dateBegin": "2026-11-01T08:00:00",
                                                            "dateEnd": "2026-11-01T13:00:00",
                                                            "flightNumber": "347",
                                                            "locationBegin": {"code": "FRU"},
                                                            "locationEnd": {"code": "IST"},
                                                            "baggage": {"type": "1PC"},
                                                        }
                                                    ]
                                                },
                                            }
                                        ]
                                    },
                                }
                            ]
                        },
                        "fares": {
                            "fareTotal": {"total": 25000},
                            "fareSeats": {
                                "fareSeat": [
                                    {"passengerType": "ADULT", "count": 1, "total": 25000}
                                ]
                            },
                        },
                    }
                ]
            },
        }
    }

    offers = adapter._normalize_search(response, "RUB")

    assert len(offers) == 1
    offer = offers[0]
    assert offer["price"] == {"amount": "25000", "currency": "RUB"}
    assert offer["itinerary"]["segments"][0]["origin"] == "FRU"
    assert offer["itinerary"]["segments"][0]["destination"] == "IST"
    assert offer["provider_selection"]["token"] == "search-token"


def test_supplier_connection_accepts_ttbooking_config(admin_client):
    supplier_response = admin_client.post(
        "/api/v1/suppliers/",
        {
            "name": "TT Booking",
            "status": "active",
            "service_kinds": ["avia"],
            "currencies": ["RUB"],
        },
        format="json",
    )
    assert supplier_response.status_code == 201, supplier_response.content
    supplier = supplier_response.json()

    credential = admin_client.post(
        f"/api/v1/suppliers/{supplier['id']}/credentials/",
        {
            "provider_adapter": "ttbooking",
            "environment": "sandbox",
            "secrets": {
                "agency": "42",
                "user": "7",
                "password": "secret",
                "url": "https://www.ttbooking.ru/api/scbs/json",
                "locale": "ru",
            },
        },
        format="json",
    )
    assert credential.status_code == 201, credential.content

    response = admin_client.post(
        f"/api/v1/suppliers/{supplier['id']}/check-connection/",
        {},
        format="json",
    )

    assert response.status_code == 200, response.content
    assert response.json()["status"] == "configured"
    assert response.json()["checked"][0]["provider_adapter"] == "ttbooking"
