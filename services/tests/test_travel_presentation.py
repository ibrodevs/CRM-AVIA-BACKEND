from services.travel_presentation import service_itinerary


def test_itinerary_keeps_public_fields_without_supplier_tokens():
    result = service_itinerary({
        'itinerary': {'segments': [{
            'origin': 'FRU', 'destination': 'SVO', 'departure': '2026-10-30T12:40',
            'arrival': '2026-10-30T15:35', 'flight_number': '1883', 'provider_token': 'secret',
        }], 'token': 'secret', 'guests': [{'passport': 'secret'}]},
        'credentials': 'secret',
    })
    assert result == {'segments': [{'origin': 'FRU', 'destination': 'SVO', 'departure': '2026-10-30T12:40', 'arrival': '2026-10-30T15:35', 'flight_number': '1883'}]}


def test_hotel_itinerary_keeps_city_and_stay_dates():
    assert service_itinerary({'itinerary': {'city': 'Москва', 'property_name': 'Отель', 'check_in': '2026-10-30', 'check_out': '2026-11-02'}}) == {'city': 'Москва', 'property_name': 'Отель', 'check_in': '2026-10-30', 'check_out': '2026-11-02'}
    assert service_itinerary(None) == {}


def test_booking_references_are_service_specific_and_do_not_expose_tokens():
    from types import SimpleNamespace
    from services.travel_presentation import service_booking_reference

    flight = SimpleNamespace(external_id='EXTERNAL-1', provider_snapshot={'provider_adapter': 'ttbooking', 'ttbooking_booking': {'locator': 'WZAG56/01', 'token': 'secret'}})
    hotel = SimpleNamespace(external_id='HOTEL-2', provider_snapshot={})
    missing = SimpleNamespace(external_id='', provider_snapshot=None)
    assert service_booking_reference(flight) == 'WZAG56/01'
    assert service_booking_reference(hotel) == 'HOTEL-2'
    assert service_booking_reference(missing) == ''
