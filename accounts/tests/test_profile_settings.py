import pytest
from rest_framework.test import APIClient

from accounts.models import User
from accounts.permissions import has_service_action
from conftest import make_user
from notifications.models import NotificationRule
from workforce.models import MotivationRule

pytestmark = pytest.mark.django_db


def test_profile_contacts_and_preferences_round_trip(admin_client):
    result = admin_client.patch('/api/v1/me/', {'max': 'max-user', 'whatsapp': '+996555123456', 'telegram': '@tester', 'work_phone': '+996555111222'}, format='json')
    assert result.status_code == 200
    saved = admin_client.get('/api/v1/me/').json()
    assert saved['max'] == 'max-user'
    assert saved['whatsapp'] == '+996555123456'
    assert 'preferences' in saved and 'service_access' in saved
    response = admin_client.patch('/api/v1/me/preferences/', {'time_format': '12 часов (AM/PM)', 'date_format': 'ГГГГ-ММ-ДД', 'start_page': 'Заказы', 'theme': 'dark', 'page_size': 50}, format='json')
    assert response.status_code == 200, response.content
    assert response.json()['time_format'] == '12h'
    assert admin_client.get('/api/v1/me/').json()['preferences']['start_page'] == 'orders'


@pytest.mark.parametrize('data', [{'theme': 'broken'}, {'page_size': 0}, {'time_format': 'wrong'}, {'notification_channels': {'email': 'false'}}])
def test_preferences_reject_invalid_values(admin_client, data):
    assert admin_client.patch('/api/v1/me/preferences/', data, format='json').status_code == 400


def test_own_sla_is_readable_but_not_editable_by_operator(operator_client, operator_user):
    assert operator_client.get(f'/api/v1/users/{operator_user.pk}/sla/').status_code == 200
    assert operator_client.put(f'/api/v1/users/{operator_user.pk}/sla/', {'sla_response_minutes': 1}).status_code == 403
    assert operator_client.get('/api/v1/roles/').status_code == 200


def test_explicit_empty_service_rights_deny_all(admin_client, operator_user):
    path = f'/api/v1/users/{operator_user.pk}/service-access/'
    response = admin_client.put(path, [{'service_kind': 'hotel', 'allowed_actions': []}], format='json')
    assert response.status_code == 200
    assert not has_service_action(User.objects.get(pk=operator_user.pk), 'hotel', 'book')
    assert not has_service_action(User.objects.get(pk=operator_user.pk), 'avia', 'book')
    assert admin_client.put(path, [{'service_kind': 'hotel', 'allowed_actions': ['book', 'send_document']}], format='json').status_code == 200
    assert has_service_action(User.objects.get(pk=operator_user.pk), 'hotel', 'send_document')
    assert admin_client.put(path, [{'service_kind': 'hotel', 'allowed_actions': ['made_up']}], format='json').status_code == 400
    assert admin_client.put(path, [{'service_kind': 'hotel', 'allowed_actions': []}] * 2, format='json').status_code == 400


def test_create_user_is_atomic_with_role_and_initial_password(admin_client):
    response = admin_client.post('/api/v1/users/', {'email': 'new@example.test', 'first_name': 'Test', 'status': 'active', 'roles': ['operator'], 'password': 'User-Test-2026!'}, format='json')
    assert response.status_code == 201, response.content
    user = User.objects.get(email='new@example.test')
    assert user.check_password('User-Test-2026!')
    assert response.json()['roles'] == ['operator']
    assert APIClient().post('/api/v1/auth/login/', {'login': user.email, 'password': 'User-Test-2026!'}).status_code == 200
    response = admin_client.post('/api/v1/users/', {'email': 'bad@example.test', 'roles': ['unknown']}, format='json')
    assert response.status_code == 400
    assert not User.objects.filter(email='bad@example.test').exists()


def test_invitation_activates_once_and_suspended_user_stays_blocked(admin_client, tenant):
    user = make_user(tenant, 'invited@example.test', 'operator')
    user.status = 'invited'
    user.set_unusable_password()
    user.save()
    invite = admin_client.post(f'/api/v1/users/{user.pk}/invite/').json()['invite_token']
    client = APIClient()
    payload = {'token': invite, 'new_password': 'Invite-Password-2026!'}
    assert client.post('/api/v1/auth/password/reset/confirm/', payload).status_code == 204
    user.refresh_from_db()
    assert user.status == 'active'
    assert client.post('/api/v1/auth/password/reset/confirm/', payload).status_code == 400
    assert admin_client.post(f'/api/v1/users/{user.pk}/suspend/').status_code == 200
    assert client.post('/api/v1/auth/login/', {'login': user.email, 'password': payload['new_password']}).status_code == 401
    assert admin_client.post(f'/api/v1/users/{user.pk}/activate/').status_code == 200
    assert client.post('/api/v1/auth/login/', {'login': user.email, 'password': payload['new_password']}).status_code == 200


def test_motivation_is_individual_and_validates_percent(admin_client, operator_user, admin_user):
    rule = {'service_kind': '*', 'fee_percent': '10', 'markup_percent': '5', 'commission_percent': '2'}
    assert admin_client.put('/api/v1/motivation/rules/', {'rules': [rule]}, format='json').status_code == 200
    assert admin_client.put('/api/v1/motivation/rules/', {'user': str(operator_user.pk), 'rules': [{**rule, 'fee_percent': '20'}]}, format='json').status_code == 200
    assert MotivationRule.objects.filter(user=None, archived_at__isnull=True).count() == 1
    assert admin_client.get(f'/api/v1/motivation/rules/?user={operator_user.pk}').json()[0]['fee_percent'] == '20.000'
    assert admin_client.get(f'/api/v1/motivation/rules/?user={admin_user.pk}').json()[0]['fee_percent'] == '10.000'
    assert admin_client.put('/api/v1/motivation/rules/', {'rules': [{**rule, 'fee_percent': '-1'}]}, format='json').status_code == 400


def test_notification_settings_preserve_custom_rules(admin_client, tenant):
    custom = NotificationRule.objects.create(tenant=tenant, event_type='chat.*', name='Custom')
    flags = [True, True, False, False, False, True]
    response = admin_client.put('/api/v1/notification-rules/', {'rules': flags}, format='json')
    assert response.status_code == 200, response.content
    assert NotificationRule.objects.filter(pk=custom.pk).exists()
    assert NotificationRule.objects.filter(name='settings:0', event_type='order.updated', is_active=True).exists()
    rows = admin_client.get('/api/v1/notification-rules/').json()
    assert [row['is_active'] for row in rows if row['event_type'].startswith('ui.preference.')] == flags


def test_api_key_edit_and_revoke_are_persistent_and_scoped(admin_client, operator_client):
    action = {'action': 'integration.api_key.generate', 'payload': {'organization': 'Partner', 'access': ['Доступ к данным']}}
    assert operator_client.post('/api/v1/workspace-actions/', action, format='json').status_code == 403
    response = admin_client.post('/api/v1/workspace-actions/', action, format='json')
    assert response.status_code == 201
    record = response.json()
    client = APIClient()
    client.credentials(HTTP_X_API_TOKEN=record['result']['api'], HTTP_X_API_KEY=record['result']['api_key'])
    assert client.get('/api/v1/orders/').status_code == 200
    assert client.get('/api/v1/me/').status_code == 403
    assert client.post('/api/v1/orders/', {}).status_code == 403
    update = {'action': 'settings.api_access.update', 'resource_id': record['id'], 'payload': {'org': 'Renamed'}}
    assert admin_client.post('/api/v1/workspace-actions/', update, format='json').status_code == 201
    assert admin_client.get('/api/v1/workspace-actions/?action=integration.api_key.generate').json()[0]['payload']['organization'] == 'Renamed'
    update['action'] = 'settings.api_access.revoke'
    assert admin_client.post('/api/v1/workspace-actions/', update, format='json').status_code == 201
    assert client.get('/api/v1/orders/').status_code == 401
    assert admin_client.get('/api/v1/workspace-actions/?action=integration.api_key.generate').json() == []


def test_statistics_are_not_limited_by_pagination(admin_client, admin_user, operator_user, tenant):
    from crm.models import Person
    from orders.models import Order
    from services.models import OrderService
    person = Person.objects.create(tenant=tenant, surname='QA', given_name='Test')
    for index in range(28):
        order = Order.objects.create(tenant=tenant, operator=admin_user, client_person=person, number=f'QA-{index}')
    Order.objects.create(tenant=tenant, operator=operator_user, client_person=person, number='OTHER')
    OrderService.objects.create(tenant=tenant, order=order, kind='avia', status='issued', currency='USD', client_total=120, supplier_cost=100)
    OrderService.objects.create(tenant=tenant, order=order, kind='hotel', status='issued', currency='RUB', client_total=2000, supplier_cost=1000)
    response = admin_client.get('/api/v1/me/statistics/')
    assert response.status_code == 200, response.content
    assert response.json()['orders'] == 28
    assert response.json()['profit'] == {'USD': '20.00', 'RUB': '1000.00'}


def test_document_template_editor_returns_body(admin_client):
    created = admin_client.post('/api/v1/document-templates/', {'code': 'qa', 'name': 'QA', 'body': 'Hello {{ name }}', 'publish': True}, format='json')
    assert created.status_code == 201
    rows = admin_client.get('/api/v1/document-templates/').json()
    assert rows[0]['body'] == 'Hello {{ name }}'


def test_profile_and_preferences_share_language(admin_client):
    assert admin_client.patch('/api/v1/me/', {'language': 'en'}, format='json').status_code == 200
    assert admin_client.get('/api/v1/me/preferences/').json()['language'] == 'en'
    assert admin_client.patch('/api/v1/me/preferences/', {'language': 'ky'}, format='json').status_code == 200
    assert admin_client.get('/api/v1/me/').json()['language'] == 'ky'


def test_avatar_validates_bytes_and_is_private(admin_client, settings, tmp_path):
    from io import BytesIO

    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image

    settings.MEDIA_ROOT = tmp_path
    path = '/api/v1/me/avatar/'
    invalid = SimpleUploadedFile('fake.png', b'not an image', content_type='image/png')
    assert admin_client.put(path, {'avatar': invalid}, format='multipart').status_code == 400
    data = BytesIO()
    Image.new('RGB', (8, 8)).save(data, format='PNG')
    valid = SimpleUploadedFile('avatar.png', data.getvalue(), content_type='image/png')
    assert admin_client.put(path, {'avatar': valid}, format='multipart').status_code == 200
    avatar = admin_client.get(path)
    assert avatar.status_code == 200
    assert avatar['Cache-Control'] == 'private, no-store'
    avatar.close()
    assert APIClient().get(path).status_code == 401
    assert admin_client.delete(path).status_code == 204
    assert admin_client.get(path).status_code == 404


@pytest.mark.parametrize('value', [
    {'base': 'USD', 'rates': {'EUR': 0}},
    {'base': 'USD', 'rates': {'EUR': -1}},
    {'base': 'USD', 'currencies': [{'code': 'EUR', 'name': 'Euro'}]},
])
def test_currency_settings_reject_invalid_rates(admin_client, value):
    from common.models import WorkspaceSetting
    response = admin_client.patch('/api/v1/workspace-settings/finance-currencies/', {'value': value}, format='json')
    assert response.status_code == 400, response.content
    assert not WorkspaceSetting.objects.filter(namespace='finance-currencies').exists()


def test_financial_notification_rule_matches_real_payment_event(admin_client, admin_user, tenant):
    from common.models import OutboxEvent
    from notifications.models import Notification
    from notifications.outbox_handlers import create_notifications

    admin_client.put('/api/v1/notification-rules/', {'rules': [False, True, False, False, False, False]}, format='json')
    event = OutboxEvent.objects.create(tenant=tenant, event_type='order.updated', payload={'action': 'payment_confirmed'})
    create_notifications(event)
    assert Notification.objects.filter(user=admin_user).count() == 1
    event.payload = {'action': 'created'}
    create_notifications(event)
    assert Notification.objects.filter(user=admin_user).count() == 1


def test_motivation_history_retains_replaced_rules(admin_client, operator_user):
    rule = {'service_kind': '*', 'fee_percent': '10', 'markup_percent': '5', 'commission_percent': '2'}
    payload = {'user': str(operator_user.pk), 'rules': [rule]}
    path = '/api/v1/motivation/rules/'
    assert admin_client.put(path, payload, format='json').status_code == 200
    payload['rules'][0]['fee_percent'] = '20'
    assert admin_client.put(path, payload, format='json').status_code == 200
    assert len(admin_client.get(f'{path}?user={operator_user.pk}').json()) == 1
    history = admin_client.get(f'{path}?user={operator_user.pk}&history=1').json()
    assert len(history) == 2
    assert sum(bool(row['archived_at']) for row in history) == 1
