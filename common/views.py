import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require
from common.audit import audit
from common.errors import ApiError, EventCursorExpiredError
from common.jobs import get_handler
from common.models import BackgroundJob, OutboxEvent, WorkspaceAction, WorkspaceSetting
from common.outbox import emit_event


@require_GET
def health_live(request):  # noqa: ARG001
    return JsonResponse({"status": "ok"})


@require_GET
def health_ready(request):  # noqa: ARG001
    checks: dict[str, str] = {}
    ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "fail"
        ok = False

    stale_after = settings.JOB_RUNNER["STALE_AFTER_SECONDS"]
    if checks["database"] == "ok":
        running = BackgroundJob.objects.filter(status=BackgroundJob.Status.RUNNING)
        stale = running.filter(heartbeat_at__lt=timezone.now() - timedelta(seconds=stale_after)).exists()
        checks["job_runner"] = "stale" if stale else "ok"

    return JsonResponse({"status": "ok" if ok else "fail", "checks": checks}, status=200 if ok else 503)


class EventSerializer(serializers.ModelSerializer):
    event_id = serializers.IntegerField(source="id")
    type = serializers.CharField(source="event_type")
    version = serializers.IntegerField(source="resource_version")

    class Meta:
        model = OutboxEvent
        fields = ["event_id", "type", "occurred_at", "resource_type", "resource_id", "version", "payload"]


class EventFeedView(APIView):
    """GET /events/?cursor=<last_event_id>&limit=100

    Cursor монотонный в рамках tenant/user feed. Событие не является
    источником истины — после события frontend запрашивает актуальный ресурс.
    """

    def get(self, request):
        try:
            cursor = int(request.query_params.get("cursor", 0))
            limit = min(int(request.query_params.get("limit", 100)), 500)
        except ValueError:
            raise ApiError(
                code="INVALID_CURSOR", message="cursor и limit должны быть числами", status_code=400
            ) from None

        feed = OutboxEvent.objects.filter(tenant_id=request.user.tenant_id).filter(
            Q(audience_user__isnull=True) | Q(audience_user=request.user)
        )

        if cursor:
            oldest = feed.order_by("id").values_list("id", flat=True).first()
            if oldest is not None and cursor < oldest - 1:
                raise EventCursorExpiredError()

        latest_id = feed.order_by("-id").values_list("id", flat=True).first() or 0
        etag = f'W/"events-{latest_id}"'
        if request.headers.get("If-None-Match") == etag and latest_id <= cursor:
            return Response(status=304, headers={"ETag": etag})

        events = list(feed.filter(id__gt=cursor).order_by("id")[:limit])
        next_cursor = events[-1].id if events else cursor
        return Response(
            {"events": EventSerializer(events, many=True).data, "cursor": next_cursor},
            headers={"ETag": etag},
        )


class JobSerializer(serializers.ModelSerializer):
    class Meta:
        model = BackgroundJob
        fields = [
            "id",
            "kind",
            "status",
            "priority",
            "progress",
            "attempts",
            "max_attempts",
            "run_after",
            "started_at",
            "completed_at",
            "error_code",
            "error_message",
            "result",
            "created_at",
        ]


class JobDetailView(APIView):
    def get_object(self, request, job_id) -> BackgroundJob:
        try:
            return BackgroundJob.objects.get(pk=job_id, tenant_id=request.user.tenant_id)
        except BackgroundJob.DoesNotExist:
            raise ApiError(code="NOT_FOUND", message="Задание не найдено", status_code=404) from None

    def get(self, request, job_id):
        return Response(JobSerializer(self.get_object(request, job_id)).data)


class JobCancelView(JobDetailView):
    def post(self, request, job_id):
        from django.db import transaction

        with transaction.atomic():
            job = BackgroundJob.objects.select_for_update().get(pk=self.get_object(request, job_id).pk)
            handler = get_handler(job.kind)
            if handler is not None and not handler.user_cancellable:
                raise ApiError(
                    code="JOB_NOT_CANCELLABLE",
                    message="Это задание нельзя отменить: внешняя операция может уже выполняться",
                    status_code=409,
                )
            if job.status not in (BackgroundJob.Status.QUEUED, BackgroundJob.Status.RUNNING):
                raise ApiError(code="JOB_ALREADY_FINISHED", message="Задание уже завершено", status_code=409)
            job.status = BackgroundJob.Status.CANCELLED
            job.completed_at = timezone.now()
            job.save(update_fields=["status", "completed_at"])
        return Response(JobSerializer(job).data)


class WorkspaceSettingView(APIView):
    permission_classes = [require("orders.view", "settings.manage")]

    def get(self, request, namespace):
        setting = WorkspaceSetting.objects.filter(
            tenant_id=request.user.tenant_id, owner=request.user, namespace=namespace
        ).first()
        return Response({"namespace": namespace, "value": setting.value if setting else {}, "version": setting.version if setting else 0})

    def patch(self, request, namespace):
        value = request.data.get("value")
        if not isinstance(value, dict):
            raise ApiError(code="VALIDATION_ERROR", message="value должен быть объектом", status_code=400)
        setting, created = WorkspaceSetting.objects.get_or_create(
            tenant_id=request.user.tenant_id,
            owner=request.user,
            namespace=namespace,
            defaults={"value": value, "created_by": request.user},
        )
        if not created:
            setting.value = value
            setting.version += 1
            setting.updated_by = request.user
            setting.save(update_fields=["value", "version", "updated_by", "updated_at"])
        audit("workspace.setting_saved", actor=request.user, resource=setting, request=request, after={"namespace": namespace})
        emit_event("workspace.setting_updated", setting, payload={"namespace": namespace}, audience_user=request.user)
        return Response({"namespace": namespace, "value": setting.value, "version": setting.version})


class WorkspaceActionListCreateView(APIView):
    permission_classes = [require("orders.view", "settings.manage")]

    ALLOWED_PREFIXES = (
        "chat.", "client.", "company.", "document.", "finance.", "group.",
        "integration.", "order.", "profile.", "service.", "settings.", "supplier.", "travel.",
    )

    def get(self, request):
        rows = WorkspaceAction.objects.filter(tenant_id=request.user.tenant_id)
        if action := request.query_params.get("action"):
            rows = rows.filter(action=action)
        if resource_type := request.query_params.get("resource_type"):
            rows = rows.filter(resource_type=resource_type)
        if resource_id := request.query_params.get("resource_id"):
            rows = rows.filter(resource_id=resource_id)
        rows = rows.order_by("-created_at")[:100]
        return Response([self.serialize(row) for row in rows])

    def post(self, request):
        action = str(request.data.get("action", "")).strip()
        if not action.startswith(self.ALLOWED_PREFIXES):
            raise ApiError(code="VALIDATION_ERROR", message="Недопустимое действие", status_code=400)
        payload = request.data.get("payload") or {}
        if not isinstance(payload, dict):
            raise ApiError(code="VALIDATION_ERROR", message="payload должен быть объектом", status_code=400)
        persisted_result = {"accepted": True}
        response_result = persisted_result
        if action == "integration.api_key.generate":
            api_token = secrets.token_urlsafe(24)
            activation_key = secrets.token_urlsafe(12)
            persisted_result = {
                "accepted": True,
                "token_sha256": hashlib.sha256(api_token.encode()).hexdigest(),
                "key_sha256": hashlib.sha256(activation_key.encode()).hexdigest(),
                "endpoint": request.build_absolute_uri("/api/v1/"),
            }
            response_result = {
                "accepted": True, "api": api_token, "api_key": activation_key,
                "endpoint": persisted_result["endpoint"],
            }
        row = WorkspaceAction.objects.create(
            tenant_id=request.user.tenant_id,
            action=action,
            resource_type=str(request.data.get("resource_type", "")),
            resource_id=str(request.data.get("resource_id", "")),
            payload=payload,
            result=persisted_result,
            created_by=request.user,
        )
        audit(f"workspace.{action}", actor=request.user, resource=row, request=request, after=payload)
        emit_event("workspace.action_completed", row, payload={"action": action, "result": row.result})
        data = self.serialize(row)
        data["result"] = response_result
        return Response(data, status=201)

    @staticmethod
    def serialize(row):
        return {
            "id": str(row.id), "action": row.action, "resource_type": row.resource_type,
            "resource_id": row.resource_id, "payload": row.payload, "status": row.status,
            "result": row.result, "created_at": row.created_at,
        }
