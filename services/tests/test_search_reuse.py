from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from common.models import BackgroundJob
from services.models import SearchSession
from services.search_reuse import flight_routing_stamp
from suppliers.models import Supplier

pytestmark = pytest.mark.django_db


@pytest.fixture
def gds(tenant):
    return Supplier.objects.create(tenant=tenant, name='Search fixture', status='active', service_kinds=['avia'])


def search(client, **extra):
    response = client.post('/api/v1/service-searches/', {'kind': 'avia', 'criteria': {'origin': 'MOW', 'destination': 'LED', 'date': '2027-03-10', 'currency': 'RUB'}, **extra}, format='json')
    assert response.status_code == 202, response.content
    return response.json()


def test_identical_pending_search_reuses_one_job(admin_client, gds):
    first = search(admin_client)
    second = search(admin_client)
    assert first['search_id'] == second['search_id']
    assert second['reused'] is True
    assert BackgroundJob.objects.filter(kind='services.search').count() == 1
    assert search(admin_client, refresh=True)['search_id'] != first['search_id']


def test_completed_search_reuse_is_short_and_revalidates_routing_configuration(admin_client, gds):
    first = search(admin_client)
    call_command('run_jobs', '--once')
    assert search(admin_client)['search_id'] == first['search_id']
    session = SearchSession.objects.get(pk=first['search_id'])
    BackgroundJob.objects.filter(pk=session.job_id).update(completed_at=timezone.now() - timedelta(seconds=61))
    assert search(admin_client)['search_id'] != first['search_id']
    stamp = flight_routing_stamp(gds.tenant_id)
    gds.name = 'Changed configuration'
    gds.save()
    assert flight_routing_stamp(gds.tenant_id) != stamp


def test_reuse_is_isolated_by_user_and_exact_criteria(admin_client, operator_client, gds):
    first = search(admin_client)
    assert search(operator_client)['search_id'] != first['search_id']
    assert search(admin_client, criteria={'origin': 'MOW', 'destination': 'LED', 'date': '2027-03-11', 'currency': 'RUB'})['search_id'] != first['search_id']


def test_failed_cancelled_empty_and_expired_searches_are_never_reused(admin_client, gds):
    for status in ['failed', 'cancelled', 'completed']:
        first = search(admin_client, refresh=True)
        SearchSession.objects.filter(pk=first['search_id']).update(status=status, expires_at=timezone.now() + timedelta(minutes=10))
        assert search(admin_client)['search_id'] != first['search_id']
    first = search(admin_client, refresh=True)
    call_command('run_jobs', '--once')
    session = SearchSession.objects.get(pk=first['search_id'])
    session.offers.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert search(admin_client)['search_id'] != first['search_id']


def test_offer_persistence_reuses_markup_lookup_only_for_equal_price_context(tenant, admin_user, gds, monkeypatch):
    from services.job_handlers import _persist_offer

    session = SearchSession.objects.create(tenant=tenant, user=admin_user, kind='avia', criteria={'origin': 'MOW', 'destination': 'LED', 'cabin': 'economy'})
    calls = []
    monkeypatch.setattr('services.job_handlers.resolve_markup_rules', lambda *args, **kwargs: calls.append(kwargs) or [])
    cache, seen = {}, set()
    for key, airline in [('one', 'SU'), ('two', 'SU'), ('three', 'S7')]:
        raw = {'external_key': key, 'price': {'amount': '100', 'currency': 'RUB'}, 'itinerary': {'segments': [{'airline': airline}]}, 'fare': {}, 'availability': {}}
        assert _persist_offer(session, gds, 'mock', raw, seen, cache) is True
    assert len(calls) == 2
    assert session.offers.count() == 3
    assert sum(offer.price_snapshots.count() for offer in session.offers.all()) == 3
