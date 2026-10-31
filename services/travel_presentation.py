"""Public travel fields for order cards, excluding supplier tokens and guest data."""


def service_itinerary(snapshot):
    itinerary = (snapshot or {}).get("itinerary") or {}
    if not isinstance(itinerary, dict):
        return {}
    fields = (
        "property_name", "city", "city_name", "city_name_ru", "check_in", "check_out",
        "check_in_time", "check_out_time", "address", "room", "meal_plan", "nights",
        "origin", "destination", "departure", "arrival", "train_number",
    )
    result = {key: itinerary[key] for key in fields if key in itinerary}
    segment_fields = (
        "origin", "destination", "origin_city", "destination_city", "origin_city_ru",
        "destination_city_ru", "departure", "arrival", "airline", "flight_number",
        "cabin", "booking_class", "baggage",
    )
    if isinstance(itinerary.get("segments"), list):
        result["segments"] = [
            {key: segment[key] for key in segment_fields if key in segment}
            for segment in itinerary["segments"] if isinstance(segment, dict)
        ]
    return result


def service_booking_reference(service):
    snapshot = service.provider_snapshot or {}
    adapter = str(snapshot.get('provider_adapter') or '')
    booking = snapshot.get(adapter + '_booking') or {}
    reference = (
        booking.get('locator') or booking.get('reference_number') or booking.get('order_id')
        or service.external_id
    )
    return str(reference) if reference is not None else ''
