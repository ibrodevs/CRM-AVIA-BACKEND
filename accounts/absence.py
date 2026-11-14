from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import AbsenceRequest, User
from accounts.permissions import has_permission
from common.audit import audit
from common.errors import ApiError
from common.pagination import DefaultPagination
from notifications.models import Notification


class AbsenceSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source='user.get_full_name', read_only=True)
    reviewer_name = serializers.CharField(source='reviewer.get_full_name', read_only=True, default='')
    can_review = serializers.SerializerMethodField()

    class Meta:
        model = AbsenceRequest
        fields = ['id', 'user', 'user_name', 'kind', 'starts_on', 'ends_on', 'comment', 'status', 'reviewer_name', 'decision_comment', 'decided_at', 'created_at', 'can_review']
        read_only_fields = ['id', 'user', 'status', 'decision_comment', 'decided_at', 'created_at']

    def get_can_review(self, obj):
        actor = self.context.get('actor')
        return bool(actor and obj.status == 'pending' and actor.pk != obj.user_id and (has_permission(actor, 'users.manage') or obj.reviewer_id == actor.pk))

    def validate(self, data):
        if data['ends_on'] < data['starts_on']:
            raise serializers.ValidationError('Дата окончания должна быть не раньше начала')
        return data


def notify(user, row, title):
    Notification.objects.create(tenant_id=row.user.tenant_id, user=user, source='accounts', event_type='accounts.absence', title=title, body=f'{row.user.get_full_name()}: {row.get_kind_display()}, {row.starts_on:%d.%m.%Y} — {row.ends_on:%d.%m.%Y}' + (f' · {row.decision_comment}' if row.decision_comment else ''), resource_type='absence', resource_id=str(row.pk), deep_link='profile', priority='high' if row.status == 'pending' else 'medium')


def effective_absence(user):
    today = timezone.localdate(timezone=ZoneInfo(user.timezone or 'UTC'))
    history = list(user.absence_requests.all())
    upcoming = sorted((row for row in history if row.status == 'approved' and row.ends_on >= today), key=lambda row: row.starts_on)
    row = upcoming[0] if upcoming else None
    return row, bool(row and row.starts_on <= today), any(row.status in ['approved', 'cancelled'] for row in history)


class AbsenceListView(APIView):
    def get(self, request):
        actor = request.user
        qs = AbsenceRequest.objects.filter(user__tenant_id=actor.tenant_id).select_related('user', 'reviewer').order_by('-created_at')
        if not has_permission(actor, 'users.manage'):
            qs = qs.filter(Q(user=actor) | Q(reviewer=actor))
        if request.query_params.get('user'):
            qs = qs.filter(user_id=serializers.UUIDField().run_validation(request.query_params['user']))
        paginator = DefaultPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(AbsenceSerializer(page, many=True, context={'actor': actor}).data)

    @transaction.atomic
    def post(self, request):
        actor = request.user
        target = serializers.UUIDField().run_validation(request.data.get('user') or str(actor.pk))
        user = User.objects.select_for_update().filter(pk=target, tenant_id=actor.tenant_id).first()
        if not user:
            raise ApiError(code='NOT_FOUND', message='Сотрудник не найден', status_code=404)
        manage = has_permission(actor, 'users.manage')
        if user.pk != actor.pk and not manage:
            raise ApiError(code='FORBIDDEN', message='Можно подать заявку только за себя', status_code=403)
        serializer = AbsenceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if AbsenceRequest.objects.filter(user=user, status__in=['pending', 'approved'], starts_on__lte=data['ends_on'], ends_on__gte=data['starts_on']).exists():
            raise ApiError(code='ABSENCE_OVERLAP', message='На эти даты уже есть отсутствие или заявка. Сначала отмените прежнюю запись.', status_code=409)
        reviewer = User.objects.filter(pk=user.manager_id, tenant_id=user.tenant_id, status='active').exclude(pk=user.pk).first()
        recipients = [reviewer] if reviewer else [u for u in User.objects.filter(tenant_id=user.tenant_id, status='active').exclude(pk=user.pk) if has_permission(u, 'users.manage')]
        if not manage and not recipients:
            raise ApiError(code='NO_REVIEWER', message='Не назначен руководитель и нет администратора для согласования. Обратитесь к администратору.', status_code=409)
        row = serializer.save(user=user, created_by=actor, reviewer=reviewer, status='approved' if manage else 'pending', decided_by=actor if manage else None, decided_at=timezone.now() if manage else None)
        audit('accounts.absence_created', request=request, resource=row, after={'status': row.status, 'starts_on': str(row.starts_on), 'ends_on': str(row.ends_on), 'user': str(user.pk)})
        if manage:
            notify(user, row, 'Отсутствие согласовано администратором')
        else:
            for recipient in recipients:
                notify(recipient, row, 'Заявка на отсутствие: требуется согласование')
        return Response(AbsenceSerializer(row, context={'actor': actor}).data, status=201)


class AbsenceDecisionView(APIView):
    @transaction.atomic
    def post(self, request, absence_id):
        actor = request.user
        row = AbsenceRequest.objects.select_for_update(of=('self',)).select_related('user', 'reviewer').filter(pk=absence_id, user__tenant_id=actor.tenant_id).first()
        if not row:
            raise ApiError(code='NOT_FOUND', message='Заявка не найдена', status_code=404)
        action = request.data.get('action')
        manage = has_permission(actor, 'users.manage')
        can_review = actor.pk != row.user_id and (manage or row.reviewer_id == actor.pk)
        if action == 'cancel':
            allowed = manage or (row.user_id == actor.pk and row.status == 'pending')
        else:
            allowed = can_review and row.status == 'pending' and action in ['approve', 'reject']
        if not allowed:
            raise ApiError(code='FORBIDDEN', message='Это действие недоступно для заявки', status_code=403)
        if row.status not in ['pending', 'approved']:
            raise ApiError(code='ALREADY_DECIDED', message='Заявка уже обработана', status_code=409)
        comment = str(request.data.get('comment') or '').strip()
        if len(comment) > 2000 or (action == 'reject' and not comment):
            raise ApiError(code='COMMENT_REQUIRED', message='Укажите причину (до 2000 символов)', status_code=400)
        row.status = {'approve': 'approved', 'reject': 'rejected', 'cancel': 'cancelled'}[action]
        row.decided_by = actor
        row.decided_at = timezone.now()
        row.decision_comment = comment
        row.save()
        audit('accounts.absence_decided', request=request, resource=row, after={'status': row.status}, reason=comment)
        notify(row.user, row, {'approve': 'Отсутствие согласовано', 'reject': 'Заявка на отсутствие отклонена', 'cancel': 'Отсутствие отменено'}[action])
        return Response(AbsenceSerializer(row, context={'actor': actor}).data)
