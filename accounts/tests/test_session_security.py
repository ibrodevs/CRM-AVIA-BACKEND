from datetime import timedelta

import pytest
from django.contrib.auth.hashers import check_password
from django.utils import timezone

from accounts.models import UserPreference, UserSession

pytestmark = pytest.mark.django_db


def configure(client, **extra):
    return client.patch('/api/v1/auth/security/', {'password':'Str0ng-Pass-123!', 'pin':'123456', 'auto_lock_minutes':5, 'auto_logout_minutes':30, **extra}, format='json')


def test_pin_hash_lock_unlock_and_api_boundary(admin_client, admin_user):
    assert configure(admin_client).status_code == 200
    pref = UserPreference.objects.get(user=admin_user)
    assert pref.pin_hash != '123456' and check_password('123456', pref.pin_hash)
    assert admin_client.post('/api/v1/auth/lock/').status_code == 200
    assert admin_client.get('/api/v1/orders/').status_code == 423
    me = admin_client.get('/api/v1/me/')
    assert me.data['session_security']['locked'] is True
    assert admin_client.post('/api/v1/auth/unlock/', {'pin':'000000'}).status_code == 400
    assert admin_client.post('/api/v1/auth/unlock/', {'pin':'123456'}).status_code == 200
    assert admin_client.get('/api/v1/orders/').status_code == 200


def test_idle_logout_cannot_be_bypassed_by_refresh(admin_client, admin_user):
    assert configure(admin_client).status_code == 200
    UserSession.objects.filter(user=admin_user).update(last_activity_at=timezone.now()-timedelta(minutes=31))
    assert admin_client.get('/api/v1/me/').status_code == 401
    assert admin_client.post('/api/v1/auth/token/refresh/', {'refresh':admin_client._tokens['refresh']}).status_code == 401


def test_idle_lock_ignores_background_polling(admin_client, admin_user):
    assert configure(admin_client).status_code == 200
    UserSession.objects.filter(user=admin_user).update(last_activity_at=timezone.now()-timedelta(minutes=6))
    assert admin_client.get('/api/v1/me/').data['session_security']['locked']
    assert admin_client.post('/api/v1/auth/activity/').status_code == 423


def test_pin_attempts_are_limited_and_password_required(admin_client):
    assert configure(admin_client, password='wrong').status_code == 400
    assert configure(admin_client).status_code == 200
    for _ in range(5):
        assert admin_client.post('/api/v1/auth/unlock/', {'pin':'000000'}).status_code == 400
    assert admin_client.post('/api/v1/auth/unlock/', {'pin':'123456'}).status_code == 429
    assert configure(admin_client, auto_logout_minutes=1).status_code == 400
