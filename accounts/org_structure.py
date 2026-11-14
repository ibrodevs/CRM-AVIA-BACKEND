from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import JobPosition, OrgChangeRequest, OrgUnit, Role, User, UserRole
from accounts.permissions import has_permission
from common.audit import audit
from common.errors import ApiError
from notifications.models import Notification


def fail(message, status=400):
    raise ApiError(code='ORG_STRUCTURE', message=message, status_code=status)


def lock_org(actor):
    from tenancy.models import Organization
    Organization.objects.select_for_update().get(pk=actor.tenant_id)


def admin(actor):
    return has_permission(actor, 'users.manage')


def record(model, value, actor, *, required=False):
    if value in (None, ''):
        if required:
            fail('Выберите обязательное значение')
        return None
    pk = serializers.UUIDField().run_validation(value) if model in (User, Role) else serializers.IntegerField(min_value=1).run_validation(value)
    row = model.objects.filter(pk=pk, tenant_id=actor.tenant_id).first()
    if not row:
        fail('Значение не найдено в вашей организации', 404)
    return row


def notify(actor, title, body, recipient=None):
    people = [recipient] if recipient else [u for u in User.objects.filter(tenant_id=actor.tenant_id, status='active').exclude(pk=actor.pk) if admin(u)]
    if not people:
        fail('Нет администратора для согласования. Обратитесь к руководителю.', 409)
    for person in people:
        Notification.objects.create(tenant_id=actor.tenant_id, user=person, source='accounts', event_type='accounts.org_structure', resource_type='org_structure', title=title, body=body, deep_link='profile', priority='high' if not recipient else 'medium')


def unit_data(row):
    return {'id': row.pk, 'name': row.name, 'parent': row.parent_id, 'manager': str(row.manager_id) if row.manager_id else None, 'manager_name': row.manager.get_full_name() if row.manager else '', 'default_role': str(row.default_role_id) if row.default_role_id else None, 'role_name': row.default_role.name if row.default_role else '', 'status': row.status, 'decision_comment': row.decision_comment}


def position_data(row):
    return {'id': row.pk, 'name': row.name, 'unit': row.unit_id, 'role': str(row.role_id) if row.role_id else None, 'role_name': row.role.name if row.role else '', 'status': row.status, 'decision_comment': row.decision_comment}


def policy(unit, position, user=None):
    manager = None
    role = position.role
    current = unit
    seen = set()
    while current and current.pk not in seen:
        seen.add(current.pk)
        manager = manager or (current.manager if current.manager_id != getattr(user, "pk", None) else None)
        role = role or current.default_role
        current = current.parent
    return manager, role


def request_data(row, actor):
    manager, role = policy(row.unit, row.position, row.user)
    return {'id': row.pk, 'user': str(row.user_id), 'user_name': row.user.get_full_name(), 'unit': row.unit_id, 'unit_name': row.unit.name, 'position': row.position_id, 'position_name': row.position.name, 'manager_name': manager.get_full_name() if manager else '', 'role_name': role.name if role else 'Текущие роли сохранятся', 'status': row.status, 'comment': row.comment, 'decision_comment': row.decision_comment, 'created_at': row.created_at, 'can_review': admin(actor) and row.status == 'pending'}


def apply(row, actor):
    if row.unit.status == 'rejected' or row.position.status == 'rejected':
        fail('Подразделение или должность отклонены. Выберите другую схему.')
    user = User.objects.select_for_update().get(pk=row.user_id, tenant_id=actor.tenant_id)
    manager, role = policy(row.unit, row.position, row.user)
    current = manager
    seen = {user.pk}
    while current:
        if current.pk in seen:
            fail('Руководитель образует замкнутую цепочку подчинения')
        if current.tenant_id != actor.tenant_id or current.status != 'active':
            fail('Руководитель должен быть активным сотрудником вашей организации')
        seen.add(current.pk)
        current = current.manager
    if role:
        if not has_permission(actor, 'roles.manage'):
            fail('Для назначения роли из схемы необходимо право управления ролями', 403)
        for code in ['roles.manage', 'users.manage']:
            if has_permission(user, code) and not role.permissions.filter(permission_code=code).exists():
                others = User.objects.filter(tenant_id=actor.tenant_id, status='active').exclude(pk=user.pk)
                if not any(has_permission(other, code) for other in others):
                    fail('Нельзя снять права последнего администратора', 409)
        user.user_roles.all().delete()
        UserRole.objects.create(user=user, role=role, assigned_by=actor)
    row.unit.status = 'approved'
    row.unit.save(update_fields=['status'])
    row.position.status = 'approved'
    row.position.save(update_fields=['status'])
    user.org_unit = row.unit
    user.job_position = row.position
    user.department = row.unit.name
    user.position = row.position.name
    user.manager = manager
    user.save(update_fields=['org_unit', 'job_position', 'department', 'position', 'manager'])
    row.status = 'approved'
    row.decided_by = actor
    row.decided_at = timezone.now()
    row.save()
    audit('accounts.org_assignment', request=None, actor=actor, tenant_id=actor.tenant_id, resource=row, after={'user': str(user.pk), 'unit': row.unit_id, 'position': row.position_id, 'role': role.code if role else None})
    notify(actor, 'Структура профиля обновлена', f'{row.unit.name} · {row.position.name}', user)


class OrgStructureView(APIView):
    def get(self, request):
        actor = request.user
        units = OrgUnit.objects.filter(tenant_id=actor.tenant_id).select_related('manager', 'default_role')
        positions = JobPosition.objects.filter(tenant_id=actor.tenant_id).select_related('role')
        requests = OrgChangeRequest.objects.filter(user__tenant_id=actor.tenant_id).select_related('user', 'unit__manager', 'unit__default_role', 'position__role')
        if not admin(actor):
            visibility = Q(status='approved') | Q(created_by=actor)
            units = units.filter(visibility)
            positions = positions.filter(visibility)
            requests = requests.filter(user=actor)
        return Response({'units': [unit_data(u) for u in units.order_by('name')], 'positions': [position_data(p) for p in positions.order_by('name')], 'requests': [request_data(r, actor) for r in requests.order_by('-created_at')], 'roles': list(Role.objects.filter(tenant_id=actor.tenant_id).values('id', 'code', 'name'))})


class OrgCatalogView(APIView):
    @transaction.atomic
    def post(self, request, kind):
        actor = request.user
        lock_org(actor)
        if kind not in ['units', 'positions']:
            fail('Неизвестный справочник', 404)
        model = OrgUnit if kind == 'units' else JobPosition
        existing = record(model, request.data.get('id'), actor)
        if existing and not admin(actor):
            fail('Изменять справочник может только администратор', 403)
        name = serializers.CharField(max_length=150, allow_blank=False).run_validation(request.data.get('name'))
        key = ' '.join(name.split()).casefold()
        fields = {'name': ' '.join(name.split()), 'name_key': key}
        if kind == 'positions':
            fields['unit'] = record(OrgUnit, request.data.get('unit'), actor, required=True)
            if fields['unit'].status != 'approved' and fields['unit'].created_by_id != actor.pk and not admin(actor):
                fail('Подразделение ещё не согласовано')
            fields['role'] = record(Role, request.data.get('role'), actor)
        else:
            fields['parent'] = record(OrgUnit, request.data.get('parent'), actor)
            fields['manager'] = record(User, request.data.get('manager'), actor)
            fields['default_role'] = record(Role, request.data.get('default_role'), actor)
            if fields['manager'] and fields['manager'].status != 'active':
                fail('Выберите активного руководителя')
            parent = fields['parent']
            seen = {existing.pk} if existing else set()
            while parent:
                if parent.pk in seen:
                    fail('В иерархии подразделений обнаружен цикл')
                if parent.status != 'approved':
                    fail('Родительское подразделение должно быть согласовано')
                seen.add(parent.pk)
                parent = parent.parent
        if not admin(actor) and any(request.data.get(k) for k in ['parent', 'manager', 'role', 'default_role']):
            fail('Схему подчинения и роли настраивает администратор', 403)
        if (fields.get('role') or fields.get('default_role')) and not has_permission(actor, 'roles.manage'):
            fail('Необходимо право управления ролями', 403)
        duplicates = model.objects.filter(tenant_id=actor.tenant_id, name_key=key)
        if kind == 'positions':
            duplicates = duplicates.filter(unit=fields['unit'])
        if existing:
            duplicates = duplicates.exclude(pk=existing.pk)
        if duplicates.exists():
            fail('Такое значение уже есть в справочнике', 409)
        was_pending = bool(existing and existing.status == 'pending')
        if existing:
            assigned = existing.members.exists()
            if assigned:
                fail('Используемую схему нельзя менять без пересогласования сотрудников. Создайте новую схему.', 409)
            for key, value in fields.items():
                setattr(existing, key, value)
            existing.status = 'approved'
            existing.save()
            row = existing
        else:
            row = model.objects.create(tenant_id=actor.tenant_id, created_by=actor, status='approved' if admin(actor) else 'pending', **fields)
        audit('accounts.org_catalog', request=request, resource=row, after={'name': row.name, 'status': row.status, 'decision_comment': row.decision_comment})
        if was_pending and row.created_by_id and row.created_by_id != actor.pk:
            notify(actor, 'Предложение для справочника согласовано', row.name, row.created_by)
        if not admin(actor):
            notify(actor, 'Новое значение оргструктуры на согласовании', row.name)
        return Response(unit_data(row) if kind == 'units' else position_data(row), status=201)


class OrgRequestView(APIView):
    @transaction.atomic
    def post(self, request):
        actor = request.user
        lock_org(actor)
        user = record(User, request.data.get('user') or str(actor.pk), actor, required=True)
        if user.pk != actor.pk and not admin(actor):
            fail('Можно отправить заявку только за себя', 403)
        User.objects.select_for_update().get(pk=user.pk)
        if user.org_requests.filter(status='pending').exists():
            fail('Сначала дождитесь решения или отзовите текущую заявку', 409)
        unit = record(OrgUnit, request.data.get('unit'), actor, required=True)
        position = record(JobPosition, request.data.get('position'), actor, required=True)
        if position.unit_id not in [None, unit.pk]:
            fail('Должность не относится к выбранному подразделению')
        if any(r.status == 'rejected' for r in [unit, position]):
            fail('Значение справочника отклонено. Выберите другое или предложите новое.')
        if not admin(actor) and any(r.status != 'approved' and r.created_by_id != actor.pk for r in [unit, position]):
            fail('Значение справочника недоступно')
        comment = serializers.CharField(max_length=2000, allow_blank=True).run_validation(request.data.get('comment', ''))
        row = OrgChangeRequest.objects.create(user=user, unit=unit, position=position, comment=comment, created_by=actor)
        if admin(actor):
            apply(row, actor)
        else:
            notify(actor, 'Изменение структуры профиля: требуется согласование', f'{user.get_full_name()} · {unit.name} · {position.name}')
        return Response(request_data(row, actor), status=201)


class OrgDecisionView(APIView):
    @transaction.atomic
    def post(self, request, request_id):
        actor = request.user
        lock_org(actor)
        row = OrgChangeRequest.objects.select_for_update(of=('self',)).select_related('user', 'unit', 'position').filter(pk=request_id, user__tenant_id=actor.tenant_id).first()
        if not row:
            fail('Заявка не найдена', 404)
        if row.status != 'pending':
            fail('Заявка уже обработана', 409)
        action = request.data.get('action')
        if action == 'cancel' and row.user_id == actor.pk:
            row.status = 'cancelled'
        elif admin(actor) and action in ['approve', 'reject']:
            if action == 'approve':
                apply(row, actor)
                return Response(request_data(row, actor))
            row.decision_comment = serializers.CharField(max_length=2000, allow_blank=False).run_validation(request.data.get('comment', ''))
            row.status = 'rejected'
            notify(actor, 'Изменение структуры профиля отклонено', row.decision_comment, row.user)
        else:
            fail('Недостаточно прав', 403)
        row.decided_by = actor
        row.decided_at = timezone.now()
        row.save()
        audit('accounts.org_decision', request=request, resource=row, after={'status': row.status, 'decision_comment': row.decision_comment})
        return Response(request_data(row, actor))


class OrgCatalogDecisionView(APIView):
    @transaction.atomic
    def post(self, request, kind, entry_id):
        actor = request.user
        lock_org(actor)
        if not admin(actor):
            fail('Требуется право управления пользователями', 403)
        if kind not in ['units', 'positions']:
            fail('Неизвестный справочник', 404)
        model = OrgUnit if kind == 'units' else JobPosition
        row = model.objects.select_for_update().filter(pk=entry_id, tenant_id=actor.tenant_id).first()
        if not row:
            fail('Предложение не найдено', 404)
        if row.status != 'pending':
            fail('Предложение уже обработано', 409)
        reason = serializers.CharField(max_length=2000, allow_blank=False).run_validation(request.data.get('comment', ''))
        row.status = 'rejected'
        row.decision_comment = reason
        row.save(update_fields=['status', 'decision_comment'])
        if kind == 'units':
            JobPosition.objects.filter(unit=row, status='pending').update(status='rejected', decision_comment=reason)
        pending = OrgChangeRequest.objects.filter(user__tenant_id=actor.tenant_id, status='pending')
        pending = pending.filter(unit=row) if kind == 'units' else pending.filter(position=row)
        for change in pending.select_for_update(of=('self',)).select_related('user'):
            change.status = 'rejected'
            change.decision_comment = reason
            change.decided_by = actor
            change.decided_at = timezone.now()
            change.save()
            notify(actor, 'Изменение структуры профиля отклонено', reason, change.user)
        if row.created_by_id:
            notify(actor, 'Предложение для справочника отклонено', f'{row.name}: {reason}', row.created_by)
        audit('accounts.org_catalog_rejected', request=request, resource=row, reason=reason)
        return Response(unit_data(row) if kind == 'units' else position_data(row))
