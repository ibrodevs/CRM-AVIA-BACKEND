from datetime import datetime, timedelta, timezone

import pytest

from aftersales.models import HotelOperation
from integrations.adapters import AdapterContext, AdapterError
from integrations.hotelbook import HotelbookAdapter
from integrations.hotelbook_observability import sanitize
from integrations.hotelbook_options import cancellation_quote, check_quote, validated_remarks
from integrations.models import IntegrationLog
from integrations.tests.test_hotelbook import gateway as gateway
from integrations.tests.test_hotelbook import hb_supplier as hb_supplier
from integrations.tests.test_hotelbook import jobs, ready_booking, start
from services.models import OrderService, ServiceOffer
from services.tests.test_hotel_actions import booked as booked
from services.tests.test_hotel_actions import cancels, confirm, create

pytestmark = pytest.mark.django_db


def test_static_remarks_survive_offer_order_preflight_and_booking(admin_client, tenant, hb_supplier, gateway):
    selected = [{"remarkId": 7, "data": None}, {"remarkId": 10, "data": "PRIVATE-REMARK-123"}]
    workflow, service = ready_booking(admin_client, tenant, hb_supplier, booking_options={"hotel_remarks": selected})
    assert start(admin_client, workflow)["items"][0]["status"] == "booked"
    sent = next(row[3] for row in gateway.calls if row[1].endswith('/hotel/gateway/book'))
    assert sent["remarks"] == selected
    assert "freeRemark" not in sent
    assert "PRIVATE-REMARK-123" not in str(list(IntegrationLog.objects.values()))
    service = OrderService.objects.get(pk=service["id"])
    assert service.provider_snapshot["hotel_remarks"] == selected
    offer = ServiceOffer.objects.latest('created_at')
    rows = admin_client.get(f'/api/v1/service-offers/{offer.pk}/hotel-remarks/').json()['results']
    assert {row['id'] for row in rows} == {1, 7, 10}


@pytest.mark.parametrize('value', [
    [{"remarkId": 2}], [{"remarkId": 999}], [{"remarkId": True}],
    [{"remarkId": 7, "data": "arbitrary"}], [{"remarkId": 10, "data": ""}],
    [{"remarkId": 1, "data": "2026-99-99T13:00"}],
    [{"remarkId": 7}, {"remarkId": 7}],
])
def test_invalid_or_deleted_remarks_never_create_orders(value, tenant, hb_supplier, gateway):
    with pytest.raises(AdapterError, match=''):
        validated_remarks(HotelbookAdapter(), AdapterContext(tenant_id=tenant.pk, supplier_id=hb_supplier.pk), value)
    assert not any(row[0] == 'POST' and row[1].endswith('/orders') for row in gateway.calls)


def test_dated_penalties_sum_items_and_expire_at_boundary():
    now = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
    rows = [{"from": None, "amount": {"amount": "10", "currency": "RUB"}}, {"from": (now + timedelta(minutes=1)).isoformat(), "price": {"amount": "50", "currency": "RUB"}}]
    items = [{"itemId": 'a', "finePolicies": {"cancel": rows}}, {"itemId": 'b', "finePolicies": {"cancel": rows}}]
    quote = cancellation_quote(items, 'RUB', now)
    assert quote['penalty'] == '20'
    assert quote['expires_at'] == (now + timedelta(minutes=1)).isoformat()
    assert cancellation_quote(items, 'RUB', now + timedelta(minutes=1))['penalty'] == '100'
    for malformed in [[], ['unstructured'], [{"from": None, "price": {"amount": 'NaN', "currency": 'RUB'}}], [{"from": '2026-11-07T12:00', "price": {"amount": '0', "currency": 'RUB'}}]]:
        items[0]['finePolicies']['cancel'] = malformed
        assert cancellation_quote(items, 'RUB', now)['penalty_exact'] is False


def test_changed_penalty_blocks_cancel_before_supplier_mutation(admin_client, booked, gateway):
    operation = create(admin_client, booked, 'cancel_booking').json()
    assert operation['financial_snapshot']['penalty'] == '0'
    assert confirm(admin_client, booked, operation).status_code == 202
    gateway.current['finePolicies']['cancel'] = [{"from": None, "price": {"amount": 25, "currency": 'EUR'}}]
    jobs()
    assert not cancels(gateway)
    result = HotelOperation.objects.get(pk=operation['id'])
    assert result.status == 'failed'
    assert result.supplier_response['error_code'] == 'CANCELLATION_QUOTE_CHANGED'


def test_unknown_penalty_cannot_be_accepted_as_zero(admin_client, booked, gateway):
    gateway.current['finePolicies'] = None
    operation = create(admin_client, booked, 'cancel_booking').json()
    assert operation['financial_snapshot']['penalty'] is None
    assert confirm(admin_client, booked, operation).status_code == 409
    assert not cancels(gateway)


def test_quote_expiry_and_remark_data_redaction():
    quote = cancellation_quote([], 'RUB', datetime(2000, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(AdapterError):
        check_quote(quote, quote)
    assert sanitize({'remarks': [{'remarkId': 10, 'data': 'PRIVATE'}]}) == {'remarks': [{'remarkId': 10, 'data': '[REDACTED]'}]}
