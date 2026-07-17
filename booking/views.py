from django.db import transaction
from rest_framework import serializers
from rest_framework import status as http
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require
from booking.models import BookingWorkflow, BookingWorkflowItem
from booking.preflight import run_preflight
from common.audit import audit
from common.errors import ApiError
from common.idempotency import idempotent_command
from common.jobs import enqueue
from orders.selectors import get_order_or_404
from services.models import OrderService


class WorkflowItemSerializer(serializers.ModelSerializer):
    service_kind = serializers.CharField(source="service.kind", read_only=True)
    service_title = serializers.CharField(source="service.title", read_only=True)

    class Meta:
        model = BookingWorkflowItem
        fields = [
            "id",
            "service",
            "service_kind",
            "service_title",
            "sequence",
            "status",
            "locator",
            "error_code",
            "error_message",
        ]


class WorkflowSerializer(serializers.ModelSerializer):
    items = WorkflowItemSerializer(many=True, read_only=True)

    class Meta:
        model = BookingWorkflow
        fields = [
            "id",
            "order",
            "status",
            "preflight_result",
            "preflight_at",
            "price_confirmation_required",
            "prices_confirmed_at",
            "items",
            "created_at",
            "version",
        ]


def _get_workflow(request, workflow_id) -> BookingWorkflow:
    workflow = BookingWorkflow.objects.filter(pk=workflow_id, tenant_id=request.user.tenant_id).first()
    if workflow is None:
        raise ApiError(code="NOT_FOUND", message="Workflow не найден", status_code=404)
    return workflow


class WorkflowCreateView(APIView):
    permission_classes = [require("services.book")]

    def post(self, request):
        order = get_order_or_404(request.user, request.data.get("order"))
        service_ids = request.data.get("services", [])
        if not isinstance(service_ids, list) or not service_ids:
            raise ApiError(
                code="VALIDATION_ERROR",
                message="Нужен список services",
                fields={"services": ["Непустой список id услуг"]},
                status_code=400,
            )
        services = list(OrderService.objects.filter(pk__in=service_ids, order=order))
        if len(services) != len(service_ids):
            raise ApiError(
                code="VALIDATION_ERROR", message="Часть услуг не найдена в заказе", status_code=400
            )
        for service in services:
            if service.status not in (OrderService.Status.PROPOSED, OrderService.Status.APPROVAL):
                raise ApiError(
                    code="SERVICE_NOT_BOOKABLE",
                    message=f"Услуга {service.title} в статусе {service.status}",
                    status_code=409,
                )
        with transaction.atomic():
            workflow = BookingWorkflow.objects.create(
                tenant_id=request.user.tenant_id,
                order=order,
                created_by=request.user,
                plan_snapshot={
                    "services": [
                        {
                            "id": str(s.id),
                            "kind": s.kind,
                            "title": s.title,
                            "client_total": str(s.client_total) if s.client_total else None,
                            "currency": s.currency,
                        }
                        for s in services
                    ]
                },
            )
            for index, service in enumerate(services, start=1):
                BookingWorkflowItem.objects.create(
                    tenant_id=request.user.tenant_id,
                    workflow=workflow,
                    service=service,
                    sequence=index,
                    created_by=request.user,
                )
        audit("booking.workflow_created", actor=request.user, resource=workflow, request=request)
        return Response(WorkflowSerializer(workflow).data, status=http.HTTP_201_CREATED)


class WorkflowPreflightView(APIView):
    permission_classes = [require("services.book")]

    def post(self, request, workflow_id):
        workflow = _get_workflow(request, workflow_id)
        if workflow.status not in (BookingWorkflow.Status.DRAFT, BookingWorkflow.Status.PREFLIGHT_OK):
            raise ApiError(
                code="WORKFLOW_ALREADY_STARTED",
                message="Preflight доступен только до старта",
                status_code=409,
            )
        result = run_preflight(workflow, request.user)
        return Response(result)


class WorkflowStartView(APIView):
    permission_classes = [require("services.book")]

    @idempotent_command("booking.start")
    def post(self, request, workflow_id):
        from django.utils import timezone

        with transaction.atomic():
            workflow = BookingWorkflow.objects.select_for_update().get(
                pk=_get_workflow(request, workflow_id).pk
            )
            if workflow.status != BookingWorkflow.Status.PREFLIGHT_OK:
                raise ApiError(
                    code="PREFLIGHT_REQUIRED", message="Сначала выполните успешный preflight", status_code=409
                )

            if workflow.price_confirmation_required and not request.data.get("confirm"):
                raise ApiError(
                    code="CONFIRMATION_REQUIRED",
                    message="Подтвердите изменения цен/предупреждения: confirm=true",
                    details=workflow.preflight_result,
                    status_code=409,
                )
            if request.data.get("confirm"):
                workflow.prices_confirmed_at = timezone.now()
            workflow.status = BookingWorkflow.Status.RUNNING
            workflow.save(update_fields=["status", "prices_confirmed_at"])
            job = enqueue("booking.run", {"workflow_id": str(workflow.id)}, request=request)
            workflow.job = job
            workflow.save(update_fields=["job"])
        audit("booking.workflow_started", actor=request.user, resource=workflow, request=request)
        return Response(
            {"workflow": WorkflowSerializer(workflow).data, "job_id": str(job.id)},
            status=http.HTTP_202_ACCEPTED,
        )


class WorkflowStatusView(APIView):
    permission_classes = [require("orders.view")]

    def get(self, request, workflow_id):
        return Response(WorkflowSerializer(_get_workflow(request, workflow_id)).data)


class WorkflowIssueView(APIView):
    permission_classes = [require("services.issue")]

    @idempotent_command("booking.issue")
    def post(self, request, workflow_id):
        workflow = _get_workflow(request, workflow_id)

        unknown = workflow.items.filter(status=BookingWorkflowItem.Status.UNKNOWN)
        if unknown.exists():
            raise ApiError(
                code="ISSUE_BLOCKED_UNKNOWN",
                message="Есть операции с неизвестным результатом; выполните status inquiry",
                details={"items": [str(i.id) for i in unknown]},
                status_code=409,
            )
        if not workflow.items.filter(status=BookingWorkflowItem.Status.BOOKED).exists():
            raise ApiError(
                code="NOTHING_TO_ISSUE", message="Нет забронированных услуг для выписки", status_code=409
            )
        job = enqueue(
            "booking.issue",
            {
                "workflow_id": str(workflow.id),
                "item_ids": request.data.get("items"),
                "passengers": request.data.get("passengers", []),
                "_mock": request.data.get("_mock", {}),
            },
            request=request,
        )
        audit("booking.issue_requested", actor=request.user, resource=workflow, request=request)
        return Response({"job_id": str(job.id)}, status=http.HTTP_202_ACCEPTED)


class WorkflowInquiryView(APIView):
    permission_classes = [require("services.issue")]

    def post(self, request, workflow_id):
        workflow = _get_workflow(request, workflow_id)
        item = workflow.items.filter(
            pk=request.data.get("item"), status=BookingWorkflowItem.Status.UNKNOWN
        ).first()
        if item is None:
            raise ApiError(code="VALIDATION_ERROR", message="Нужен item в статусе unknown", status_code=400)
        job = enqueue("booking.status_inquiry", {"item_id": str(item.id)}, request=request)
        return Response({"job_id": str(job.id)}, status=http.HTTP_202_ACCEPTED)


class WorkflowCancelView(APIView):
    permission_classes = [require("services.cancel")]

    @idempotent_command("booking.cancel")
    def post(self, request, workflow_id):
        workflow = _get_workflow(request, workflow_id)
        if workflow.status in (BookingWorkflow.Status.CANCELLED,):
            return Response(WorkflowSerializer(workflow).data)
        job = enqueue("booking.compensate", {"workflow_id": str(workflow.id)}, request=request)
        audit(
            "booking.workflow_cancelled",
            actor=request.user,
            resource=workflow,
            request=request,
            reason=str(request.data.get("reason", "")),
        )
        return Response({"job_id": str(job.id)}, status=http.HTTP_202_ACCEPTED)
