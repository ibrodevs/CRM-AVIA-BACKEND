from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import AbsenceRequest
from conftest import auth_client, make_user
from notifications.models import Notification

pytestmark = pytest.mark.django_db


def payload(user=None, offset=0):
    today = timezone.localdate() + timedelta(days=offset)
    return {'kind': 'vacation', 'starts_on': str(today), 'ends_on': str(today + timedelta(days=3)), **({'user': str(user.pk)} if user else {})}


def test_operator_requests_approval_then_manager_confirms(admin_client, admin_user, operator_client, operator_user):
    operator_user.manager = admin_user
    operator_user.save()
    result = operator_client.post('/api/v1/absences/', payload(), format='json')
    assert result.status_code == 201, result.content
    row = result.json()
    assert row['status'] == 'pending'
    assert operator_client.get('/api/v1/me/').json()['work_status'] == 'working'
    assert Notification.objects.filter(user=admin_user, resource_id=str(row['id'])).exists()
    assert operator_client.post(f"/api/v1/absences/{row['id']}/decision/", {'action': 'approve'}).status_code == 403
    assert admin_client.post(f"/api/v1/absences/{row['id']}/decision/", {'action': 'approve'}).status_code == 200
    profile = operator_client.get('/api/v1/me/').json()
    assert profile['work_status'] == 'vacation'
    assert profile['absence_period']['ends_on'] == row['ends_on']
    assert Notification.objects.filter(user=operator_user, title='Отсутствие согласовано').exists()
    assert admin_client.post(f"/api/v1/absences/{row['id']}/decision/", {'action': 'reject', 'comment': 'late'}).status_code != 200


def test_admin_period_future_expiry_and_cancellation(admin_client, operator_client, operator_user):
    result = admin_client.post('/api/v1/absences/', payload(operator_user, 10), format='json')
    assert result.status_code == 201, result.content
    row = AbsenceRequest.objects.get(pk=result.json()['id'])
    assert row.status == 'approved'
    assert operator_client.get('/api/v1/me/').json()['work_status'] == 'working'
    row.starts_on = timezone.localdate() - timedelta(days=5)
    row.ends_on = timezone.localdate() - timedelta(days=1)
    row.save()
    assert operator_client.get('/api/v1/me/').json()['work_status'] == 'working'
    row.ends_on = timezone.localdate() + timedelta(days=2)
    row.save()
    assert operator_client.get('/api/v1/me/').json()['work_status'] == 'vacation'
    assert operator_client.post(f'/api/v1/absences/{row.pk}/decision/', {'action': 'cancel'}).status_code == 403
    assert admin_client.post(f'/api/v1/absences/{row.pk}/decision/', {'action': 'cancel'}).status_code == 200
    assert operator_client.get('/api/v1/me/').json()['work_status'] == 'working'


def test_absences_validate_overlap_permissions_and_tenant(admin_client, operator_client, operator_user, other_tenant):
    assert operator_client.patch('/api/v1/me/', {'work_status': 'vacation'}).status_code == 400
    assert admin_client.post('/api/v1/absences/', {**payload(operator_user), 'ends_on': '2000-01-01'}).status_code == 400
    assert admin_client.post('/api/v1/absences/', payload(operator_user)).status_code == 201
    assert admin_client.post('/api/v1/absences/', payload(operator_user)).status_code == 409
    outsider = make_user(other_tenant, 'outside@example.test', 'admin')
    assert admin_client.post('/api/v1/absences/', payload(outsider)).status_code == 404
    assert auth_client(outsider).get('/api/v1/absences/').json()['results'] == []
    assert operator_client.post('/api/v1/absences/', payload(outsider)).status_code == 404


def test_fallback_admin_rejection_and_own_withdrawal(admin_client, admin_user, operator_client, operator_user):
    row = operator_client.post('/api/v1/absences/', payload()).json()
    assert Notification.objects.filter(user=admin_user, resource_id=str(row['id'])).exists()
    url = f"/api/v1/absences/{row['id']}/decision/"
    assert admin_client.post(url, {'action': 'reject'}).status_code == 400
    assert admin_client.post(url, {'action': 'reject', 'comment': 'Нужна замена'}).status_code == 200
    assert operator_client.get('/api/v1/me/').json()['work_status'] == 'working'
    next_row = operator_client.post('/api/v1/absences/', payload()).json()
    assert operator_client.post(f"/api/v1/absences/{next_row['id']}/decision/", {'action': 'cancel'}).status_code == 200


def test_direct_manager_can_review_without_global_user_management(operator_user, tenant):
    manager = make_user(tenant, 'lead@example.test', 'operator')
    operator_user.manager = manager
    operator_user.save()
    client = auth_client(operator_user)
    row = client.post('/api/v1/absences/', payload()).json()
    lead = auth_client(manager)
    assert lead.get('/api/v1/absences/').json()['results'][0]['can_review']
    assert lead.post(f"/api/v1/absences/{row['id']}/decision/", {'action': 'approve'}).status_code == 200
