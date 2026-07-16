"""Интеграции: операции, инциденты, каталог ошибок (ТЗ §13, §21.3)."""
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status as http
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import has_permission, require
from common.audit import audit
from common.errors import ApiError
from common.jobs import enqueue
from common.pagination import DefaultPagination
from integrations.models import (
    IncidentTimelineEntry, IntegrationErrorCode, IntegrationIncident, IntegrationLog,
)


class IncidentSerializer(serializers.ModelSerializer):
    class Meta:
        model = IntegrationIncident
        fields = ["id", "error_code", "severity", "provider_adapter", "supplier",
                  "operation", "order", "service", "job", "sanitized_error",
                  "correlation_id", "status", "assignee", "snoozed_until",
                  "retry_count", "fallback_supplier", "resolution_code",
                  "resolution_comment", "developer_ticket", "occurrences",
                  "created_at", "updated_at"]


class IntegrationLogListView(GenericAPIView):
    permission_classes = [require("integrations.manage")]
    pagination_class = DefaultPagination

    def get(self, request):
        qs = IntegrationLog.objects.filter(tenant_id=request.user.tenant_id)
        params = request.query_params
        if adapter := params.get("provider_adapter"):
            qs = qs.filter(provider_adapter=adapter)
        if correlation := params.get("correlation_id"):
            qs = qs.filter(correlation_id=correlation)
        if result := params.get("result"):
            qs = qs.filter(result=result)
        page = self.paginate_queryset(qs.order_by("-created_at"))
        return self.get_paginated_response([
            {"id": entry.id, "correlation_id": entry.correlation_id,
             "provider_adapter": entry.provider_adapter, "operation": entry.operation,
             "result": entry.result, "error_code": entry.error_code,
             "http_status": entry.http_status, "duration_ms": entry.duration_ms,
             "created_at": entry.created_at,
             # raw error — только с admin/debug permission (ТЗ §13)
             **({"raw_error": entry.raw_error}
                if has_permission(request.user, "integrations.manage") else {})}
            for entry in page
        ])


class IncidentListView(GenericAPIView):
    permission_classes = [require("orders.view")]
    pagination_class = DefaultPagination
    serializer_class = IncidentSerializer

    def get(self, request):
        qs = IntegrationIncident.objects.filter(tenant_id=request.user.tenant_id)
        params = request.query_params
        if incident_status := params.get("status"):
            qs = qs.filter(status=incident_status)
        if severity := params.get("severity"):
            qs = qs.filter(severity=severity)
        page = self.paginate_queryset(qs.order_by("-created_at"))
        return self.get_paginated_response(IncidentSerializer(page, many=True).data)


def _get_incident(request, incident_id) -> IntegrationIncident:
    incident = IntegrationIncident.objects.filter(
        pk=incident_id, tenant_id=request.user.tenant_id).first()
    if incident is None:
        raise ApiError(code="NOT_FOUND", message="Инцидент не найден", status_code=404)
    return incident


def _timeline(incident, action, user, **details):
    IncidentTimelineEntry.objects.create(incident=incident, action=action, actor=user,
                                         details=details)


class IncidentAssignView(APIView):
    permission_classes = [require("integrations.manage", "orders.change")]

    def post(self, request, incident_id):
        from accounts.models import User

        incident = _get_incident(request, incident_id)
        assignee = User.objects.filter(pk=request.data.get("assignee"),
                                       tenant_id=request.user.tenant_id).first()
        if assignee is None:
            raise ApiError(code="VALIDATION_ERROR", message="assignee не найден",
                           status_code=400)
        incident.assignee = assignee
        incident.status = IntegrationIncident.Status.ASSIGNED
        incident.save(update_fields=["assignee", "status", "updated_at"])
        _timeline(incident, "assigned", request.user, assignee=str(assignee.pk))
        return Response(IncidentSerializer(incident).data)


class IncidentRetryView(APIView):
    permission_classes = [require("integrations.manage", "orders.change")]

    def post(self, request, incident_id):
        incident = _get_incident(request, incident_id)
        # issue/payment с неизвестным результатом — только status inquiry (ТЗ §21.3)
        if incident.error_code in ("ISSUE_UNKNOWN", "PAYMENT_UNKNOWN"):
            raise ApiError(
                code="RETRY_UNSAFE",
                message="Операция с неизвестным результатом: выполните status inquiry",
                status_code=409,
            )
        if incident.job is None:
            raise ApiError(code="NO_JOB", message="Нет связанного задания для повтора",
                           status_code=409)
        job = enqueue(incident.job.kind, incident.job.payload,
                      correlation_id=incident.correlation_id, request=request)
        incident.status = IntegrationIncident.Status.RETRYING
        incident.retry_count += 1
        incident.save(update_fields=["status", "retry_count", "updated_at"])
        _timeline(incident, "retried", request.user, new_job=str(job.id))
        return Response({"job_id": str(job.id)}, status=http.HTTP_202_ACCEPTED)


class IncidentSnoozeView(APIView):
    permission_classes = [require("integrations.manage", "orders.change")]

    def post(self, request, incident_id):
        incident = _get_incident(request, incident_id)
        until = request.data.get("until")
        if not until:
            raise ApiError(code="VALIDATION_ERROR", message="until обязателен",
                           status_code=400)
        incident.status = IntegrationIncident.Status.SNOOZED
        incident.snoozed_until = until
        incident.save(update_fields=["status", "snoozed_until", "updated_at"])
        _timeline(incident, "snoozed", request.user, until=str(until))
        return Response(IncidentSerializer(incident).data)


class IncidentSwitchSupplierView(APIView):
    permission_classes = [require("integrations.manage", "orders.change")]

    def post(self, request, incident_id):
        from suppliers.models import Supplier

        incident = _get_incident(request, incident_id)
        supplier = Supplier.objects.filter(pk=request.data.get("supplier"),
                                           tenant_id=request.user.tenant_id).first()
        if supplier is None:
            raise ApiError(code="VALIDATION_ERROR", message="supplier не найден",
                           status_code=400)
        incident.fallback_supplier = supplier
        incident.save(update_fields=["fallback_supplier", "updated_at"])
        _timeline(incident, "supplier_switched", request.user,
                  supplier=str(supplier.pk))
        return Response(IncidentSerializer(incident).data)


class IncidentResolveView(APIView):
    permission_classes = [require("integrations.manage", "orders.change")]

    def post(self, request, incident_id):
        incident = _get_incident(request, incident_id)
        resolution_code = str(request.data.get("resolution_code", ""))
        if not resolution_code:
            raise ApiError(code="RESOLUTION_CODE_REQUIRED",
                           message="Закрытие требует resolution code", status_code=400)
        incident.status = IntegrationIncident.Status.RESOLVED
        incident.resolution_code = resolution_code
        incident.resolution_comment = str(request.data.get("comment", ""))
        incident.save(update_fields=["status", "resolution_code", "resolution_comment",
                                     "updated_at"])
        _timeline(incident, "resolved", request.user, resolution_code=resolution_code)
        audit("integrations.incident_resolved", actor=request.user, resource=incident,
              request=request)
        return Response(IncidentSerializer(incident).data)


class IncidentReopenView(APIView):
    permission_classes = [require("integrations.manage", "orders.change")]

    def post(self, request, incident_id):
        incident = _get_incident(request, incident_id)
        incident.status = IntegrationIncident.Status.REOPENED
        incident.occurrences += 1
        incident.save(update_fields=["status", "occurrences", "updated_at"])
        _timeline(incident, "reopened", request.user)
        return Response(IncidentSerializer(incident).data)


class IncidentEscalateView(APIView):
    permission_classes = [require("integrations.manage", "orders.change")]

    def post(self, request, incident_id):
        incident = _get_incident(request, incident_id)
        incident.status = IntegrationIncident.Status.ESCALATED
        incident.developer_ticket = str(request.data.get("developer_ticket", ""))
        incident.save(update_fields=["status", "developer_ticket", "updated_at"])
        _timeline(incident, "escalated", request.user,
                  ticket=incident.developer_ticket)
        return Response(IncidentSerializer(incident).data)


class ErrorCodeListView(APIView):
    def get(self, request):  # noqa: ARG002
        return Response([
            {"code": e.code, "title": e.title, "category": e.category,
             "default_severity": e.default_severity,
             "recommended_action": e.recommended_action,
             "is_retry_safe": e.is_retry_safe}
            for e in IntegrationErrorCode.objects.all()
        ])
