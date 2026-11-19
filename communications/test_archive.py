import pytest
from rest_framework.test import APIClient

from communications.models import ChatThread, Message, ThreadParticipant

pytestmark = pytest.mark.django_db


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_archive_is_personal_reversible_and_preserves_messages(tenant, admin_user, operator_user):
    thread = ChatThread.objects.create(tenant=tenant, type='client', title='Проверка')
    Message.objects.create(tenant=tenant, thread=thread, body='История', author_user=admin_user)
    first, second = client_for(admin_user), client_for(operator_user)
    url = f'/api/v1/chat/threads/{thread.id}/archive/'
    assert first.post(url, {'archived': True}, format='json').status_code == 200
    def rows(client, state):
        response = client.get('/api/v1/chat/threads/', {'archived': state})
        assert response.status_code == 200
        return response.json()['results']
    assert rows(first, 'false') == []
    assert rows(first, 'true')[0]['is_archived'] is True
    assert rows(second, 'false')[0]['is_archived'] is False
    assert second.get(f'/api/v1/chat/threads/{thread.id}/messages/').status_code == 200
    assert first.post(url, {'archived': False}, format='json').json() == {'is_archived': False}
    assert rows(first, 'false')[0]['id'] == str(thread.id)
    assert Message.objects.filter(thread=thread).count() == 1


def test_archive_validates_input_and_tenant(tenant, other_tenant, admin_user):
    local = ChatThread.objects.create(tenant=tenant, type='client')
    foreign = ChatThread.objects.create(tenant=other_tenant, type='client')
    client = client_for(admin_user)
    assert client.post(f'/api/v1/chat/threads/{local.id}/archive/', {'archived': 'invalid'}, format='json').status_code == 400
    assert not ThreadParticipant.objects.filter(thread=local).exists()
    assert client.post(f'/api/v1/chat/threads/{foreign.id}/archive/', {'archived': True}, format='json').status_code == 404
