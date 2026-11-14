import re
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from accounts.models import MessengerBinding

pytestmark = pytest.mark.django_db


def test_unconfigured_channels_never_claim_binding(operator_client, settings):
    settings.TELEGRAM_BOT_TOKEN = ''
    result = operator_client.post('/api/v1/me/messenger-bindings/telegram/start/', {'address': '12345'})
    assert result.status_code == 409
    assert not MessengerBinding.objects.exists()
    assert operator_client.patch('/api/v1/me/', {'telegram': '12345'}).status_code == 400


def test_binding_requires_delivered_code_and_consumes_it_once(operator_client, operator_user, settings):
    settings.TELEGRAM_BOT_TOKEN = 'test-token'
    with patch('accounts.messenger_bindings.send_message') as send:
        result = operator_client.post('/api/v1/me/messenger-bindings/telegram/start/', {'address': '1234567'})
        assert result.status_code == 200, result.content
        code = re.search(r': (\d{6})', send.call_args.kwargs['body'])[1]
    row = MessengerBinding.objects.get(user=operator_user)
    assert code not in row.code_hash
    assert not row.verified_at
    operator_user.refresh_from_db()
    assert operator_user.telegram == ''
    assert operator_client.post('/api/v1/me/messenger-bindings/telegram/confirm/', {'code': 'invalid'}).status_code == 400
    row.refresh_from_db()
    assert row.attempts == 1
    result = operator_client.post('/api/v1/me/messenger-bindings/telegram/confirm/', {'code': code})
    assert result.status_code == 200
    operator_user.refresh_from_db()
    assert operator_user.telegram == '1234567'
    assert result.json()['results'][0]['verified']
    assert operator_client.post('/api/v1/me/messenger-bindings/telegram/confirm/', {'code': code}).status_code == 400
    assert operator_client.post('/api/v1/me/messenger-bindings/telegram/unlink/', {}).status_code == 200
    operator_user.refresh_from_db()
    assert operator_user.telegram == ''


def test_failed_attempt_limit_expiry_and_delivery_failure_preserve_old_binding(operator_client, operator_user, settings):
    settings.TELEGRAM_BOT_TOKEN = 'test-token'
    row = MessengerBinding.objects.create(user=operator_user, channel='telegram', address='11111', verified_at=timezone.now())
    operator_user.telegram = '11111'
    operator_user.save()
    with patch('accounts.messenger_bindings.send_message'):
        assert operator_client.post('/api/v1/me/messenger-bindings/telegram/start/', {'address': '22222'}).status_code == 200
    for _ in range(5):
        assert operator_client.post('/api/v1/me/messenger-bindings/telegram/confirm/', {'code': 'bad'}).status_code == 400
    row.refresh_from_db()
    assert row.attempts == 5 and row.address == '11111'
    assert row.verified_at
    assert operator_client.post('/api/v1/me/messenger-bindings/telegram/start/', {'address': '22222'}).status_code == 429
    row.sent_at = timezone.now() - timedelta(minutes=2)
    row.save()
    from common.transports import TransportError
    with patch('accounts.messenger_bindings.send_message', side_effect=TransportError('offline')):
        assert operator_client.post('/api/v1/me/messenger-bindings/telegram/start/', {'address': '33333'}).status_code == 502
    row.refresh_from_db()
    assert row.address == '11111'
    operator_user.refresh_from_db()
    assert operator_user.telegram == '11111'


def test_telegram_personal_link_binds_only_from_a_signed_bot_callback(operator_client, operator_user, settings):
    import hashlib
    import hmac
    import json
    from urllib.parse import parse_qs, urlparse

    from communications.models import WebhookEvent

    settings.TELEGRAM_BOT_TOKEN = 'test-token'
    settings.WEBHOOK_SECRETS = {'telegram': 'test-signing-secret'}
    with patch('accounts.messenger_bindings.telegram_bot_name', return_value='test_psc_bot'):
        result = operator_client.post('/api/v1/me/messenger-bindings/telegram/start/', {})
    assert result.status_code == 200
    token = parse_qs(urlparse(result.json()['url']).query)['start'][0]
    assert len(token) <= 64
    row = MessengerBinding.objects.get(user=operator_user)
    assert token not in row.code_hash
    payload = {'event_id': 'personal-binding-1', 'sender': '123456789', 'message': {'text': '/start ' + token}}
    body = json.dumps(payload).encode()
    url = '/api/v1/communications/webhooks/telegram/'
    assert operator_client.post(url, body, content_type='application/json').status_code == 401
    row.refresh_from_db()
    assert not row.verified_at
    signature = hmac.new(b'test-signing-secret', body, hashlib.sha256).hexdigest()
    confirmed = operator_client.post(url, body, content_type='application/json', HTTP_X_WEBHOOK_SIGNATURE=signature)
    assert confirmed.status_code == 200, confirmed.content
    assert confirmed.json()['status'] == 'binding_confirmed'
    operator_user.refresh_from_db()
    assert operator_user.telegram == '123456789'
    assert 'psc_' not in str(WebhookEvent.objects.get(external_event_id='personal-binding-1').payload)
    assert operator_client.post(url, body, content_type='application/json', HTTP_X_WEBHOOK_SIGNATURE=signature).json()['status'] == 'duplicate'
