"""Client-facing hotel details without provider authentication or guest data."""


def hotel_service_details(snapshot):
    if not snapshot or snapshot.get("provider_adapter") != "aanda":
        return None
    booking = snapshot.get("aanda_booking") or {}
    return {
        "provider": "aanda",
        "itinerary": snapshot.get("itinerary") or {},
        "fare": snapshot.get("fare") or {},
        "special_requests": snapshot.get("special_requests", ""),
        "booking": {
            k: booking.get(k)
            for k in (
                "order_id",
                "reference_number",
                "raw_status",
                "raw_status_name",
                "normalized_status",
                "actual_price",
                "deadline",
                "created_at",
                "news",
                "checkin_notice",
            )
        },
        "rooms": [
            {
                k: room.get(k)
                for k in (
                    "room_name",
                    "rate_name",
                    "rate_description",
                    "arrival_time",
                    "departure_time",
                    "check_in",
                    "check_out",
                    "penalty",
                    "deadline",
                    "penalty_info",
                    "currency",
                    "room_amenities",
                    "room_size",
                    "payment_name",
                    "where_to_pay",
                    "meal",
                )
            }
            for room in (snapshot.get("aanda") or {}).get("rooms", [])
        ],
        "accommodations": [
            {
                k: item.get(k)
                for k in (
                    "confirmation_number",
                    "raw_status",
                    "raw_status_name",
                    "allow_cancel",
                    "penalty",
                    "possible_penalty",
                    "deadline_date",
                    "deadline_time",
                    "supplier_info",
                    "penalty_note",
                )
            }
            for item in booking.get("items", [])
        ],
    }


def hotel_map_coordinates(itinerary, snapshot=None, provider=""):
    """Recover an exact point from saved supplier content for older search results."""
    from integrations.hotelbook_content import hotel_coordinates

    point = hotel_coordinates(itinerary)
    if point:
        return point
    snapshot = snapshot or {}
    if (provider or snapshot.get("provider_adapter")) != "hotelbook":
        return {}
    saved = snapshot.get("hotelbook") or {}
    hotel = saved.get("hotel") or {}
    expected = (itinerary or {}).get("hotel_id") or saved.get("hotel_id")
    if not expected or str(hotel.get("id")) != str(expected):
        return {}
    return hotel_coordinates(hotel)
