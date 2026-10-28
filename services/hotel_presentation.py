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
