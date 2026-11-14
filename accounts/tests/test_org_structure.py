import pytest

from accounts.models import JobPosition, OrgUnit, Role, UserRole
from conftest import auth_client, make_user
from notifications.models import Notification

pytestmark = pytest.mark.django_db


def catalog(client, kind, **data):
    response = client.post(f'/api/v1/org-structure/catalog/{kind}/', data, format='json')
    assert response.status_code == 201, response.content
    return response.json()


def test_existing_directory_assignment_requires_approval_and_applies_policy(admin_client, admin_user, operator_client, operator_user, tenant):
    role = Role.objects.get(tenant=tenant, code='operator')
    unit = catalog(admin_client, 'units', name='Продажи', manager=str(admin_user.pk), default_role=str(role.pk))
    job = catalog(admin_client, 'positions', name='Оператор', unit=unit['id'])
    result = operator_client.post('/api/v1/org-structure/requests/', {'unit': unit['id'], 'position': job['id']}, format='json')
    assert result.status_code == 201, result.content
    assert result.json()['status'] == 'pending'
    operator_user.refresh_from_db()
    assert operator_user.department == ''
    assert Notification.objects.filter(user=admin_user, resource_type='org_structure').exists()
    url = f"/api/v1/org-structure/requests/{result.json()['id']}/decision/"
    assert operator_client.post(url, {'action': 'approve'}).status_code == 403
    assert admin_client.post(url, {'action': 'approve'}).status_code == 200
    operator_user.refresh_from_db()
    assert operator_user.org_unit_id == unit['id']
    assert operator_user.department == 'Продажи'
    assert operator_user.manager_id == admin_user.pk
    assert list(operator_user.user_roles.values_list('role__code', flat=True)) == ['operator']
    assert operator_client.patch('/api/v1/me/', {'department': 'fake'}).status_code == 400
    assert operator_client.patch('/api/v1/me/', {'position': 'Администратор'}).status_code == 400


def test_operator_can_suggest_new_values_without_assigning_rights(admin_client, operator_client, operator_user, tenant):
    unit = catalog(operator_client, 'units', name='Новое подразделение')
    job = catalog(operator_client, 'positions', name='Новая должность', unit=unit['id'])
    assert unit['status'] == job['status'] == 'pending'
    role = Role.objects.get(tenant=tenant, code='admin')
    assert operator_client.post('/api/v1/org-structure/catalog/units/', {'name': 'Unsafe', 'default_role': str(role.pk)}).status_code == 403
    response = operator_client.post('/api/v1/org-structure/requests/', {'unit': unit['id'], 'position': job['id']})
    assert response.status_code == 201
    url = f"/api/v1/org-structure/requests/{response.json()['id']}/decision/"
    assert admin_client.post(url, {'action': 'reject'}).status_code == 400
    assert admin_client.post(url, {'action': 'reject', 'comment': 'Нужна другая схема'}).status_code == 200
    operator_user.refresh_from_db()
    assert not operator_user.org_unit_id
    assert list(operator_user.user_roles.values_list('role__code', flat=True)) == ['operator']


def test_hierarchy_inherits_manager_and_role_but_position_can_override(admin_client, admin_user, operator_user, tenant):
    role = Role.objects.get(tenant=tenant, code='operator')
    manager_role = Role.objects.get(tenant=tenant, code='manager')
    root = catalog(admin_client, 'units', name='Головной офис', manager=str(admin_user.pk), default_role=str(role.pk))
    child = catalog(admin_client, 'units', name='Филиал', parent=root['id'])
    job = catalog(admin_client, 'positions', name='Менеджер', unit=child['id'], role=str(manager_role.pk))
    result = admin_client.post('/api/v1/org-structure/requests/', {'user': str(operator_user.pk), 'unit': child['id'], 'position': job['id']})
    assert result.status_code == 201, result.content
    operator_user.refresh_from_db()
    assert operator_user.manager_id == admin_user.pk
    assert UserRole.objects.filter(user=operator_user, role=manager_role).exists()


def test_invalid_cycles_cross_tenant_and_last_admin_demotion_are_blocked(admin_client, admin_user, tenant, other_tenant):
    root = catalog(admin_client, 'units', name='Root')
    child = catalog(admin_client, 'units', name='Child', parent=root['id'])
    assert admin_client.post('/api/v1/org-structure/catalog/units/', {'id': root['id'], 'name': 'Root', 'parent': child['id']}).status_code == 400
    foreign = make_user(other_tenant, 'foreign@test.local', 'admin')
    assert admin_client.post('/api/v1/org-structure/catalog/units/', {'name': 'Bad', 'manager': str(foreign.pk)}).status_code == 404
    role = Role.objects.get(tenant=tenant, code='operator')
    job = catalog(admin_client, 'positions', name='Без админских прав', unit=root['id'], role=str(role.pk))
    assert admin_client.post('/api/v1/org-structure/requests/', {'unit': root['id'], 'position': job['id']}).status_code == 409
    assert UserRole.objects.filter(user=admin_user, role__code='admin').exists()
    outsider = auth_client(foreign)
    assert outsider.get('/api/v1/org-structure/').json()['units'] == []


def test_pending_values_are_private_and_used_schemes_cannot_silently_change(admin_client, operator_client, operator_user, tenant):
    unit = catalog(operator_client, 'units', name='Private proposal')
    other = auth_client(make_user(tenant, 'peer@test.local', 'operator'))
    assert other.get('/api/v1/org-structure/').json()['units'] == []
    job = catalog(operator_client, 'positions', name='Job', unit=unit['id'])
    result = admin_client.post('/api/v1/org-structure/requests/', {'user': str(operator_user.pk), 'unit': unit['id'], 'position': job['id']})
    assert result.status_code == 201
    assert admin_client.post('/api/v1/org-structure/catalog/units/', {'id': unit['id'], 'name': 'Changed'}).status_code == 201
    assert OrgUnit.objects.get(pk=unit['id']).name == 'Changed'
    operator_user.refresh_from_db()
    assert operator_user.department == 'Private proposal'
    assert operator_user.org_requests.filter(status='pending').exists()
    assert JobPosition.objects.get(pk=job['id']).status == 'approved'


def test_catalog_rejection_notifies_proposer_and_prevents_application(admin_client, operator_client, operator_user):
    unit = catalog(operator_client, 'units', name='Предложение на отказ')
    job = catalog(operator_client, 'positions', name='Должность на отказ', unit=unit['id'])
    change = operator_client.post('/api/v1/org-structure/requests/', {'unit': unit['id'], 'position': job['id']}).json()
    url = f"/api/v1/org-structure/catalog/units/{unit['id']}/reject/"
    assert operator_client.post(url, {'comment': 'No'}).status_code == 403
    assert admin_client.post(url, {'comment': 'Не соответствует структуре'}).status_code == 200
    saved = operator_client.get('/api/v1/org-structure/').json()
    assert saved['units'][0]['status'] == 'rejected'
    assert saved['units'][0]['decision_comment'] == 'Не соответствует структуре'
    assert saved['requests'][0]['status'] == 'rejected'
    assert admin_client.post(f"/api/v1/org-structure/requests/{change['id']}/decision/", {'action': 'approve'}).status_code == 409
    assert operator_client.post('/api/v1/org-structure/requests/', {'unit': unit['id'], 'position': job['id']}).status_code == 400
    assert Notification.objects.filter(user=operator_user, title='Предложение для справочника отклонено').exists()


def test_approved_catalog_proposal_notifies_its_author(admin_client, operator_client, operator_user):
    unit = catalog(operator_client, 'units', name='Согласуемое подразделение')
    catalog(admin_client, 'units', id=unit['id'], name=unit['name'])
    assert Notification.objects.filter(user=operator_user, title='Предложение для справочника согласовано').exists()
