from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework import status as http
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require
from common.audit import audit
from common.errors import ApiError
from common.pagination import DefaultPagination
from notifications.models import Notification, NotificationRule


class NotificationSerializer(serializers.ModelSerializer):
    responsible_name = serializers.SerializerMethodField()

    def get_responsible_name(self, obj):
        return obj.user.get_full_name() or obj.user.email

    class Meta:
        model = Notification
        fields = [
            "id",
            "priority",
            "source",
            "event_type",
            "title",
            "body",
            "resource_type",
            "resource_id",
            "deep_link",
            "read_at",
            "pinned_at",
            "dismissed_at",
            "created_at",
            "responsible_name",
        ]


def _my_notifications(request):
    return Notification.objects.filter(user=request.user)


class NotificationListView(GenericAPIView):
    pagination_class = DefaultPagination

    def get(self, request):
        qs = _my_notifications(request).filter(dismissed_at__isnull=True).select_related("user")
        params = request.query_params
        if params.get("unread") in ("true", "1"):
            qs = qs.filter(read_at__isnull=True)
        if priority := params.get("priority"):
            qs = qs.filter(priority=priority)
        if source := params.get("source"):
            qs = qs.filter(source=source)
        qs = qs.order_by("-pinned_at", "-created_at")
        page = self.paginate_queryset(qs)
        response = self.get_paginated_response(NotificationSerializer(page, many=True).data)
        response.data["unread_count"] = (
            _my_notifications(request).filter(read_at__isnull=True, dismissed_at__isnull=True).count()
        )
        return response


def _get_notification(request, notification_id) -> Notification:
    notification = _my_notifications(request).filter(pk=notification_id).first()
    if notification is None:
        raise ApiError(code="NOT_FOUND", message="Уведомление не найдено", status_code=404)
    return notification


class NotificationReadView(APIView):
    def post(self, request, notification_id):
        notification = _get_notification(request, notification_id)
        should_read = request.data.get("read", True)
        notification.read_at = timezone.now() if should_read else None
        notification.save(update_fields=["read_at"])
        return Response(NotificationSerializer(notification).data)


class NotificationPinView(APIView):
    def post(self, request, notification_id):
        notification = _get_notification(request, notification_id)
        notification.pinned_at = None if notification.pinned_at else timezone.now()
        notification.save(update_fields=["pinned_at"])
        return Response(NotificationSerializer(notification).data)


class NotificationDismissView(APIView):
    def post(self, request, notification_id):
        notification = _get_notification(request, notification_id)
        notification.dismissed_at = timezone.now()
        notification.save(update_fields=["dismissed_at"])
        return Response(status=http.HTTP_204_NO_CONTENT)


class NotificationReadAllView(APIView):
    def post(self, request):
        count = _my_notifications(request).filter(read_at__isnull=True).update(read_at=timezone.now())
        return Response({"read": count})


class NotificationDismissReadView(APIView):
    def post(self, request):
        count = (
            _my_notifications(request)
            .filter(read_at__isnull=False, dismissed_at__isnull=True)
            .update(dismissed_at=timezone.now())
        )
        return Response({"dismissed": count})


class NotificationRulesView(APIView):
    permission_classes = [require("settings.manage")]

    def get(self, request):
        rules = NotificationRule.objects.filter(tenant_id=request.user.tenant_id, archived_at__isnull=True)
        data = [
                {
                    "id": str(r.id),
                    "event_type": r.event_type,
                    "name": r.name,
                    "priority": r.priority,
                    "recipients": r.recipients,
                    "channels": r.channels,
                    "is_active": r.is_active,
                }
                for r in rules
            ]
        from common.models import WorkspaceSetting

        setting = WorkspaceSetting.objects.filter(tenant_id=request.user.tenant_id, owner__isnull=True, namespace="notification-delivery").first()
        if setting:
            data = [row for row in data if not row["event_type"].startswith("ui.preference.")]
            data.extend({"event_type": f"ui.preference.{index}", "is_active": enabled} for index, enabled in enumerate(setting.value.get("rules", [])))
        return Response(data)

    def post(self, request):
        rule = NotificationRule.objects.create(
            tenant_id=request.user.tenant_id,
            event_type=str(request.data.get("event_type", "*")),
            name=str(request.data.get("name", "Правило")),
            priority=str(request.data.get("priority", "medium")),
            recipients=request.data.get("recipients", {}),
            channels=request.data.get("channels", ["desktop"]),
            created_by=request.user,
        )
        return Response({"id": str(rule.id)}, status=http.HTTP_201_CREATED)

    def put(self, request):
        from common.models import WorkspaceSetting

        rules = request.data.get("rules")
        if not isinstance(rules, list) or len(rules) != 6 or any(not isinstance(value, bool) for value in rules):
            raise ApiError(code="VALIDATION_ERROR", message="Ожидаются шесть переключателей true/false", status_code=400)
        channels = ["desktop"] + (["sms"] if rules[2] else []) + (["email"] if rules[3] else []) + (["telegram"] if rules[4] else [])
        with transaction.atomic():
            setting, _ = WorkspaceSetting.objects.get_or_create(tenant_id=request.user.tenant_id, owner=None, namespace="notification-delivery", defaults={"created_by": request.user})
            setting.value = {"rules": rules}
            setting.save(update_fields=["value", "updated_at"])
            # Replace only the six legacy UI preference rows. Preserve custom event rules.
            NotificationRule.objects.filter(tenant_id=request.user.tenant_id, event_type__startswith="ui.preference.").delete()
            for index, pattern in [(0, "order.updated"), (1, "order.updated"), (5, "sla.*")]:
                NotificationRule.objects.update_or_create(
                    tenant_id=request.user.tenant_id, name=f"settings:{index}", event_type=pattern,
                    defaults={"priority": "medium", "recipients": {"roles": ["admin", "operator", "manager", "accountant"]}, "channels": channels, "is_active": rules[index], "created_by": request.user},
                )
        audit("notifications.preferences_saved", request=request, resource=request.user, after={"rules": rules})
        return Response({"rules": rules})
